"""
FastAPI control plane for the AML triage agent.

  GET  /                      the operator console (single page)
  GET  /api/options           connectors + profiles available, for the dropdowns
  POST /api/runs              start a run (manual trigger, optional file upload)
  GET  /api/runs              run history
  GET  /api/runs/{id}         live status of one run
  GET  /api/runs/{id}/download  the triage workbook
  POST /api/webhook/mail      push endpoint for a real mailbox notification
  GET  /api/health            connector health

Run: uvicorn src.api:app --reload --port 8000
"""
from __future__ import annotations

import asyncio
import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from src.config import load_settings, profiles
from src.connectors import base as cbase
from src.connectors import sinks, sources, tickets  # noqa: F401
from src.orchestrator import RUNS_DIR, AMLTriageAgent
from src.scoring.engine import load_config

ROOT = Path(__file__).resolve().parents[1]
app = FastAPI(title="AML Triage Agent", version="1.0")

_RUNS: dict[str, dict] = {}
_LOCK = threading.Lock()
_KEEP_LIVE = 20                       # in-memory progress cache; the JSON files are the record


def _new_run_id() -> str:
    return f"AMLRUN-{datetime.utcnow():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"


def _seed(run_id: str, settings: dict, trigger: str) -> dict:
    """
    Register the run as PENDING before the background task starts, so the console can
    poll GET /api/runs/{id} immediately instead of guessing which run is 'the new one'.
    """
    state = {"run_id": run_id, "status": "PENDING", "trigger_type": trigger,
             "started_at": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
             "finished_at": "", "steps": [], "counts": {}, "tickets": [], "notes": [],
             "workbook_path": None, "workbook_url": None, "error": None,
             "config_snapshot": {k: settings.get(k) for k in
                                 ("source", "sink", "ticket_system", "explain_mode", "sender_filter")}}
    with _LOCK:
        _RUNS[run_id] = state
        for stale in list(_RUNS)[:-_KEEP_LIVE]:
            _RUNS.pop(stale, None)
    return state


# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def console() -> str:
    return (ROOT / "src" / "ui" / "index.html").read_text(encoding="utf-8")


@app.get("/api/options")
def options() -> dict:
    cfg = load_config()
    return {
        "sources": [
            {"key": "zoho_mail", "label": "Zoho Mail (poll inbox)"},
            {"key": "outlook_graph", "label": "Outlook / Microsoft 365"},
            {"key": "imap", "label": "Generic IMAP"},
            {"key": "manual_upload", "label": "Manual upload (attach files below)"},
        ],
        "sinks": [
            {"key": "zoho_workdrive_mail", "label": "Zoho WorkDrive + high-risk mail alert"},
            {"key": "zoho_workdrive", "label": "Zoho WorkDrive"},
            {"key": "sharepoint", "label": "SharePoint / OneDrive"},
            {"key": "teams", "label": "Microsoft Teams channel"},
            {"key": "outlook_mail", "label": "Email to analysts (Outlook)"},
            {"key": "local", "label": "Local folder"},
        ],
        "tickets": [
            {"key": "jira", "label": "Jira"},
            {"key": "gitlab", "label": "GitLab issues"},
            {"key": "odoo", "label": "Odoo Helpdesk"},
            {"key": "mock", "label": "Dry-run log (no external calls)"},
        ],
        "explain_modes": [
            {"key": "template", "label": "Deterministic template (no LLM cost)"},
            {"key": "anthropic", "label": "Claude narrative"},
        ],
        "profiles": profiles(),
        "registered": {k: cbase.available(k) for k in ("source", "sink", "ticket")},
        "thresholds": cfg["thresholds"],
        "triage": cfg["triage"],
        "framework_version": cfg["meta"]["framework_version"],
    }


def _load_history() -> list[dict]:
    out = []
    for p in sorted(RUNS_DIR.glob("*.json"), reverse=True)[:50]:
        try:
            out.append(json.loads(p.read_text()))
        except json.JSONDecodeError:
            continue
    return out


@app.get("/api/runs")
def list_runs() -> list[dict]:
    with _LOCK:
        live = list(_RUNS.values())
    seen = {r["run_id"] for r in live}
    merged = live + [r for r in _load_history() if r["run_id"] not in seen]
    # newest first. run ids are timestamp-prefixed, so lexicographic == chronological.
    # _RUNS is insertion-ordered (oldest first), so without this the console picks up
    # the FIRST run of the process instead of the newest one.
    merged.sort(key=lambda r: r.get("run_id", ""), reverse=True)
    return merged


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    with _LOCK:
        if run_id in _RUNS:
            return _RUNS[run_id]
    p = RUNS_DIR / f"{run_id}.json"
    if p.exists():
        return json.loads(p.read_text())
    raise HTTPException(404, "run not found")


@app.get("/api/runs/{run_id}/download")
def download(run_id: str):
    state = get_run(run_id)
    path = state.get("workbook_path")
    if not path or not Path(path).exists():
        raise HTTPException(404, "workbook not available")
    return FileResponse(path, filename=Path(path).name,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# --------------------------------------------------------------------------- #
def _build_settings(form: dict) -> dict:
    s = load_settings(form.get("profile") or "dev")
    s["source"] = form.get("source", s["source"])
    s["sink"] = form.get("sink", s["sink"])
    s["ticket_system"] = form.get("ticket_system", s["ticket_system"])
    s["explain_mode"] = form.get("explain_mode", s.get("explain_mode", "template"))
    s.setdefault("source_config", {})
    if form.get("sender_filter"):
        s["source_config"]["sender_filter"] = form["sender_filter"]
    if form.get("remote_folder"):
        s["remote_folder"] = form["remote_folder"]
    for key, blob in (("source_config", form.get("source_config_json")),
                      ("sink_config", form.get("sink_config_json")),
                      ("ticket_config", form.get("ticket_config_json"))):
        if blob:
            try:
                s[key] = {**s.get(key, {}), **json.loads(blob)}
            except json.JSONDecodeError:
                raise HTTPException(400, f"{key} is not valid JSON")
    return s


def _execute(settings: dict, files: list[tuple[str, bytes]] | None, dry_run: bool, trigger: str,
             run_id: str | None = None) -> None:
    def progress(st):
        with _LOCK:
            _RUNS[st.run_id] = json.loads(json.dumps(st.__dict__, default=str))
    AMLTriageAgent(settings, on_progress=progress).run(
        manual_files=files, dry_run=dry_run, trigger_type=trigger, run_id=run_id)


@app.post("/api/runs")
async def start_run(background: BackgroundTasks,
                    source: str = Form("manual_upload"),
                    sink: str = Form("local"),
                    ticket_system: str = Form("mock"),
                    explain_mode: str = Form("template"),
                    profile: str = Form("dev"),
                    sender_filter: str = Form(""),
                    remote_folder: str = Form("AML/Triage"),
                    dry_run: bool = Form(False),
                    source_config_json: str = Form(""),
                    sink_config_json: str = Form(""),
                    ticket_config_json: str = Form(""),
                    uploads: list[UploadFile] = File(default=[])) -> JSONResponse:
    settings = _build_settings(locals())
    payload = [(f.filename, await f.read()) for f in uploads if f.filename]
    if source == "manual_upload" and not payload:
        raise HTTPException(400, "Manual upload selected but no files attached")
    run_id = _new_run_id()
    _seed(run_id, settings, "manual")
    background.add_task(_execute, settings, payload or None, dry_run, "manual", run_id)
    return JSONResponse({"accepted": True, "run_id": run_id,
                         "message": f"Run started - poll /api/runs/{run_id} for status"})


@app.post("/api/webhook/mail")
async def mail_webhook(background: BackgroundTasks, payload: dict[str, Any]) -> dict:
    """
    Push trigger. Zoho Mail: outgoing webhook / Zoho Flow.
    Microsoft: Graph change notification (validationToken handshake handled by caller).
    Body is treated as a NOTIFICATION ONLY - the agent re-fetches from the mailbox
    itself, so a spoofed webhook cannot inject data into the pipeline.
    """
    settings = load_settings()
    run_id = _new_run_id()
    _seed(run_id, settings, "email_event")
    background.add_task(_execute, settings, None, False, "email_event", run_id)
    return {"accepted": True, "run_id": run_id, "notification_id": payload.get("id", "")}


@app.get("/api/health")
def health() -> dict:
    s = load_settings()
    out = {"profile": s["_profile"], "connectors": {}}
    for kind, key in (("source", s["source"]), ("sink", s["sink"]), ("ticket", s["ticket_system"])):
        try:
            out["connectors"][kind] = cbase.build(kind, key, s.get(f"{kind}_config", {})).health()
        except Exception as exc:                              # noqa: BLE001
            out["connectors"][kind] = {"connector": key, "ok": False, "error": str(exc)}
    return out
