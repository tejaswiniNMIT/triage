"""
The agent pipeline.

    poll/receive -> parse -> merge -> score -> triage -> explain -> workbook
                 -> upload to sink -> create tickets -> emit run record

Deliberately a linear, idempotent state machine rather than a free-roaming agent
loop: in an AML control the sequence must be reproducible and auditable. The LLM
is used inside step 6 only. If you later want LangGraph / CrewAI, wrap each
`_step_*` as a node - the contracts already match.
"""
from __future__ import annotations

import json
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from src.connectors import base as cbase
from src.connectors import sinks, sources, tickets  # noqa: F401  (registers connectors)
from src.explain import llm as explainer
from src.ingest.parser import build_review_population, load_watchlists
from src.output.excel_writer import write_triage_workbook
from src.scoring.engine import load_config, score_player

ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "data" / "runs"
NUM_COLS = [f"{b}{w}" for b in ("deposit", "withdrawal", "bet", "loss") for w in ("7d", "30d", "90d")]


@dataclass
class RunState:
    run_id: str
    status: str = "PENDING"
    started_at: str = ""
    finished_at: str = ""
    trigger_type: str = "manual"          # manual | scheduled | email_event
    steps: list[dict] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    workbook_path: str | None = None
    workbook_url: str | None = None
    tickets: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str | None = None
    config_snapshot: dict = field(default_factory=dict)

    def step(self, name: str, status: str, detail: str = "", **extra):
        # millisecond resolution: a template-mode run finishes several stages inside one
        # second, and the console shows per-stage durations off these timestamps.
        self.steps.append({"name": name, "status": status, "detail": detail,
                           "at": datetime.utcnow().isoformat(timespec="milliseconds") + "Z", **extra})

    def save(self) -> Path:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        p = RUNS_DIR / f"{self.run_id}.json"
        p.write_text(json.dumps(self.__dict__, indent=2, default=str), encoding="utf-8")
        return p


class AMLTriageAgent:
    def __init__(self, settings: dict, on_progress: Callable[[RunState], None] | None = None):
        self.s = settings
        self.cfg = load_config(settings.get("scoring_config"))
        self.on_progress = on_progress or (lambda st: None)

    # -- helpers ------------------------------------------------------------
    def _emit(self, st: RunState, name: str, status: str, detail: str = "", **extra):
        st.step(name, status, detail, **extra)
        st.save()
        self.on_progress(st)

    # -- pipeline -----------------------------------------------------------
    def run(self, *, manual_files: list[tuple[str, bytes]] | None = None,
            dry_run: bool = False, trigger_type: str = "manual",
            run_id: str | None = None) -> RunState:
        # run_id may be supplied by the caller (the API pre-registers it so the console can
        # follow this exact run instead of guessing which one it just started).
        st = RunState(run_id=run_id or f"AMLRUN-{datetime.utcnow():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}",
                      started_at=datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
                      trigger_type=trigger_type, status="RUNNING",
                      config_snapshot={k: self.s.get(k) for k in
                                       ("source", "sink", "ticket_system", "explain_mode", "sender_filter")})
        try:
            files, msg_meta = self._step_fetch(st, manual_files)
            hr_df, tr_df, pop = self._step_parse(st, files)
            results = self._step_score(st, hr_df, tr_df, pop)
            results = self._step_explain(st, results)
            wb = self._step_workbook(st, results, msg_meta)
            self._step_deliver(st, wb, dry_run)
            self._step_tickets(st, results, dry_run)
            st.status = "SUCCESS"
        except Exception as exc:                              # noqa: BLE001
            st.status = "FAILED"
            st.error = f"{type(exc).__name__}: {exc}"
            self._emit(st, "pipeline", "FAILED", traceback.format_exc()[-1500:])
        st.finished_at = datetime.utcnow().isoformat(timespec="milliseconds") + "Z"
        st.save()
        self.on_progress(st)
        return st

    # 1 -------------------------------------------------------------------- #
    def _step_fetch(self, st, manual_files):
        self._emit(st, "fetch", "RUNNING", f"source={self.s['source']}")
        if manual_files:
            files = [(n, b) for n, b in manual_files]
            meta = {"source_email": "manual upload (UI)", "message_id": None}
            self._emit(st, "fetch", "SUCCESS", f"{len(files)} file(s) uploaded manually",
                       files=[n for n, _ in files])
            return files, meta
        conn = cbase.build("source", self.s["source"], self.s.get("source_config", {}))
        msgs = conn.poll(since=self.s.get("since"))
        if not msgs:
            sc = self.s.get("source_config", {})
            raise RuntimeError(
                f"No matching mail. Looked for UNREAD mail from '{sc.get('sender_filter')}'"
                + (f" with '{sc.get('subject_filter')}' in the subject" if sc.get("subject_filter") else "")
                + f" in folder {sc.get('folder_id')}, carrying a CSV/XLSX attachment. "
                f"Run `python scripts/zoho_token.py --peek` to see what is actually there.")
        msg = msgs[0]
        files = [(a.filename, a.content) for a in msg.attachments if a.is_tabular]
        if not files:
            raise RuntimeError("Matching mail has no CSV/XLSX attachment")
        if self.s.get("mark_processed", True):
            conn.mark_processed(msg.message_id)
        self._emit(st, "fetch", "SUCCESS", f"{len(files)} attachment(s) from {msg.sender}",
                   files=[n for n, _ in files], message_id=msg.message_id)
        return files, {"source_email": msg.sender, "message_id": msg.message_id, "subject": msg.subject}

    # 2 -------------------------------------------------------------------- #
    def _step_parse(self, st, files):
        self._emit(st, "parse", "RUNNING")
        hr_df, tr_df, notes = load_watchlists(files)
        st.notes.extend(notes)
        pop = build_review_population(hr_df, tr_df)
        if pop.empty:
            raise RuntimeError("Review population is empty after parsing - check Player ID column")
        src_counts = pop["source"].value_counts().to_dict()
        self._emit(st, "parse", "SUCCESS",
                   f"{len(pop)} players (Both={src_counts.get('Both',0)}, "
                   f"Trigger={src_counts.get('Trigger',0)}, HighRisk={src_counts.get('HighRisk',0)})",
                   highrisk_rows=len(hr_df), trigger_rows=len(tr_df), notes=notes)
        return hr_df, tr_df, pop

    # 3 -------------------------------------------------------------------- #
    def _step_score(self, st, hr_df, tr_df, pop):
        self._emit(st, "score", "RUNNING")
        hr_map = hr_df.set_index("player_id").to_dict("index") if not hr_df.empty else {}
        trig_map: dict[str, list[str]] = {}
        if not tr_df.empty:
            for pid, code in zip(tr_df["player_id"], tr_df.get("alert_code", [])):
                if isinstance(code, str) and code and code.lower() != "nan":
                    trig_map.setdefault(pid, []).append(code)
        results = []
        for r in pop.to_dict("records"):
            pid = r["player_id"]
            hr_row = {k: hr_map[pid].get(k) for k in NUM_COLS} if pid in hr_map else None
            res = score_player(pid, r["shop"], r["source"], hr_row, trig_map.get(pid, []), self.cfg)
            results.append(res)
        counts = pd.Series([r.risk_tier for r in results]).value_counts().to_dict()
        st.counts = {"total": len(results), "HIGH": counts.get("HIGH", 0),
                     "MEDIUM": counts.get("MEDIUM", 0), "LOW": counts.get("LOW", 0),
                     "critical": sum(r.critical_signal for r in results),
                     "review_yes": sum(r.review_decision == "YES" for r in results)}
        self._emit(st, "score", "SUCCESS",
                   f"HIGH={st.counts['HIGH']} MEDIUM={st.counts['MEDIUM']} LOW={st.counts['LOW']} "
                   f"(critical={st.counts['critical']})", **st.counts)
        return results

    # 4 -------------------------------------------------------------------- #
    def _step_explain(self, st, results):
        mode = self.s.get("explain_mode", "template")
        tiers = set(self.s.get("explain_tiers", ["HIGH", "MEDIUM", "LOW"]))
        self._emit(st, "explain", "RUNNING", f"mode={mode}")
        t0, n_llm = time.time(), 0
        for r in results:
            use = mode if r.risk_tier in tiers else "template"
            r.explanation = explainer.explain(r.to_dict(), use)          # type: ignore[attr-defined]
            n_llm += use != "template"
        self._emit(st, "explain", "SUCCESS",
                   f"{len(results)} narratives ({n_llm} via LLM) in {time.time()-t0:.1f}s")
        return results

    # 5 -------------------------------------------------------------------- #
    def _step_workbook(self, st, results, msg_meta):
        self._emit(st, "workbook", "RUNNING")
        triage_rows, signal_rows = [], []
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        sla = self.cfg["triage"]["sla_hours"]
        for r in results:
            m, ex = r.metrics, getattr(r, "explanation", {})
            top = (r.top_signals + ["", "", ""])[:3]
            triage_rows.append({
                "PlayerID": r.player_id, "Shop": r.shop, "Source": r.source, "RiskTier": r.risk_tier,
                "FinalScore": r.final_score, "ReviewDecision": r.review_decision,
                "Priority": {"HIGH": "P1", "MEDIUM": "P2", "LOW": "P3"}[r.risk_tier],
                "CriticalSignal": r.critical_signal, "CriticalCodes": ", ".join(r.critical_codes),
                "TopSignal1": top[0], "TopSignal2": top[1], "TopSignal3": top[2],
                "SignalCount": len(r.all_signals), "DecisionPath": r.decision_path,
                **explainer.flatten(ex), "DataWarning": r.data_warning,
                **{c: m.get(c) for c in NUM_COLS},
                **{k: m.get(k) for k in ("loss_ratio_7d", "loss_ratio_30d", "loss_ratio_90d",
                                         "bet_dep_30d", "wd_dep_30d", "wd_dep_7d")},
                "TicketSystem": None, "TicketID": None, "TicketURL": None,
                "SLA_Hours": sla.get(r.risk_tier, 0), "RunID": st.run_id, "ScoredAt": now})
            for s in r.all_signals:
                signal_rows.append({"PlayerID": r.player_id, "RiskTier": r.risk_tier,
                                    "SignalCode": s["code"], "Kind": s["kind"], "Family": s["family"],
                                    "RawScore": s["score"], "Reason": s["reason"], "Evidence": s["evidence"]})
        out = RUNS_DIR / st.run_id / f"AML_Triage_{st.run_id}.xlsx"
        write_triage_workbook(triage_rows, signal_rows,
                              {"run_id": st.run_id, "framework_version": self.cfg["meta"]["framework_version"],
                               "explain_mode": self.s.get("explain_mode"), **st.counts,
                               "input_files": [n for n in st.notes], **msg_meta}, out)
        st.workbook_path = str(out)
        self._rows, self._signal_rows = triage_rows, signal_rows
        self._emit(st, "workbook", "SUCCESS", out.name, path=str(out))
        return out

    # 6 -------------------------------------------------------------------- #
    def _step_deliver(self, st, wb, dry_run):
        sink_key = self.s.get("sink", "local")
        self._emit(st, "deliver", "RUNNING", f"sink={sink_key}")
        if dry_run:
            self._emit(st, "deliver", "SKIPPED", "dry run")
            return
        conn = cbase.build("sink", sink_key, self.s.get("sink_config", {}))
        # `players` carries the triage rows so an alerting sink can name the high-risk
        # cases in its notification. Sinks that only move the file ignore the key.
        url = conn.upload(str(wb), self.s.get("remote_folder", "AML/Triage"),
                          {"run_id": st.run_id, "high": st.counts.get("HIGH", 0),
                           "medium": st.counts.get("MEDIUM", 0), "low": st.counts.get("LOW", 0),
                           "critical": st.counts.get("critical", 0), "total": st.counts.get("total", 0),
                           "players": getattr(self, "_rows", [])})
        st.workbook_url = url
        alert = getattr(conn, "alert_status", None)
        if alert:
            st.notes.append(f"alert: {alert['detail']}")
        self._emit(st, "deliver", "SUCCESS", f"{url}{' | ' + alert['detail'] if alert else ''}",
                   url=url, alert=alert)

    # 7 -------------------------------------------------------------------- #
    def _step_tickets(self, st, results, dry_run):
        sys_key = self.s.get("ticket_system", "mock")
        tiers = set(self.cfg["triage"]["ticket_for_tiers"])
        targets = [r for r in results if r.risk_tier in tiers]
        self._emit(st, "tickets", "RUNNING", f"system={sys_key}, candidates={len(targets)}")
        if dry_run:
            st.tickets = [{"player_id": r.player_id, "tier": r.risk_tier, "status": "dry-run"} for r in targets]
            self._emit(st, "tickets", "SKIPPED", f"dry run ({len(targets)} would be created)")
            return
        conn = cbase.build("ticket", sys_key, self.s.get("ticket_config", {}))
        by_player = {row["PlayerID"]: row for row in getattr(self, "_rows", [])}
        created = existing = failed = 0
        for r in targets:
            ex = getattr(r, "explanation", {})
            desc = (
                f"Run: {st.run_id}\nPlayer: {r.player_id}   Shop: {r.shop}   Source list: {r.source}\n"
                f"Risk tier: {r.risk_tier}   FinalScore: {r.final_score:.0f}/100   "
                f"SLA: {self.cfg['triage']['sla_hours'].get(r.risk_tier)}h\n"
                f"Decision path: {r.decision_path}\n"
                f"Critical signals: {', '.join(r.critical_codes) or 'none'}\n\n"
                f"--- Assessment ---\n{ex.get('summary','')}\n"
                f"Typology: {ex.get('typology','')}   Confidence: {ex.get('confidence','')}\n\n"
                f"--- Evidence ---\n" + "\n".join(f"* {e}" for e in ex.get("key_evidence", [])) +
                "\n\n--- Recommended actions ---\n" + "\n".join(f"* {a}" for a in ex.get("recommended_actions", [])) +
                (f"\n\nData warning: {r.data_warning}" if r.data_warning else "") +
                "\n\nScores are produced by the deterministic AML framework; the narrative is LLM-generated "
                "and must be verified by the analyst."
            )
            key = f"aml-{st.run_id.split('-')[1]}-{r.player_id}"
            ref = conn.create(summary=f"[AML-{r.risk_tier}] {r.player_id} score {r.final_score:.0f} - "
                                      f"{ex.get('typology','review required')}",
                              description=desc, priority=r.risk_tier,
                              labels=["aml", f"tier-{r.risk_tier.lower()}", f"shop-{r.shop}"],
                              external_key=key, attachment_url=st.workbook_url)
            st.tickets.append({"player_id": r.player_id, "tier": r.risk_tier, "system": ref.system,
                               "ticket_id": ref.ticket_id, "url": ref.url, "status": ref.status,
                               "error": ref.error})
            if row := by_player.get(r.player_id):
                row.update({"TicketSystem": ref.system, "TicketID": ref.ticket_id, "TicketURL": ref.url})
            created += ref.status == "created"
            existing += ref.status == "existing"
            failed += ref.status not in ("created", "existing")
        # rewrite the workbook so ticket ids land in the analyst file
        if st.workbook_path and (created or existing):
            write_triage_workbook(list(by_player.values()), getattr(self, '_signal_rows', []),
                                  {"run_id": st.run_id, "tickets_created": created,
                                   "framework_version": self.cfg["meta"]["framework_version"],
                                   "explain_mode": self.s.get("explain_mode"), **st.counts},
                                  st.workbook_path)
        # "created" and "already existed" must not be reported as the same number: a run
        # that creates nothing because every key matched an old ticket looks identical to
        # a fully successful one otherwise.
        self._emit(st, "tickets", "SUCCESS",
                   f"{created} created, {existing} already existed, {failed} failed "
                   f"(of {len(targets)}) in {sys_key}",
                   created=created, existing=existing, failed=failed)
