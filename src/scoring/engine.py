"""
AML scoring engine - deterministic implementation of AML-Scoring-Framework.pdf.

Design rules:
  * No thresholds or scores are hard-coded here; everything comes from
    config/scoring_config.yaml so compliance can change the model without a deploy.
  * Every output is explainable: each signal carries score, family, reason and the
    metric values that fired it.
  * The LLM never touches the score. It only narrates the deterministic result.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "scoring_config.yaml"


def load_config(path: str | Path | None = None) -> dict:
    with open(path or CONFIG_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# --------------------------------------------------------------------------- #
# Section 4 - KPI standardisation
# --------------------------------------------------------------------------- #
def _safe_div(num: float | None, den: float | None) -> float | None:
    """NULLIF(den, 0) semantics from the PDF: division by zero -> undefined (None)."""
    if num is None or den is None:
        return None
    if den == 0 or (isinstance(den, float) and math.isnan(den)):
        return None
    return num / den


def build_metrics(row: dict, cfg: dict) -> dict:
    """Turn one high-risk-list record into the metric namespace used by KPI expressions."""
    def g(name: str) -> float:
        v = row.get(name)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return 0.0
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    m: dict[str, Any] = {}
    for w in ("7d", "30d", "90d"):
        for base in ("deposit", "withdrawal", "bet", "loss"):
            m[f"{base}{w}"] = g(f"{base}{w}")

    mode = cfg["normalisation"]["loss_ratio_mode"]
    for w in ("7d", "30d", "90d"):
        dep, loss = m[f"deposit{w}"], m[f"loss{w}"]
        # Sign convention in the source feed: loss < 0 => player lost money,
        # loss > 0 => player finished ahead (net win).
        net_loss = -loss if loss < 0 else 0.0
        m[f"net_loss{w}"] = net_loss
        m[f"net_win{w}"] = loss if loss > 0 else 0.0
        lr_abs = _safe_div(abs(loss), dep)
        lr_net = _safe_div(net_loss, dep)
        m[f"loss_ratio_abs_{w}"] = lr_abs
        m[f"loss_ratio_net_{w}"] = lr_net
        m[f"loss_ratio_{w}"] = lr_abs if mode == "abs" else lr_net
        m[f"bet_dep_{w}"] = _safe_div(m[f"bet{w}"], dep)
        m[f"wd_dep_{w}"] = _safe_div(m[f"withdrawal{w}"], dep)
    return m


def loss_sign_warning(m: dict, cfg: dict) -> str | None:
    """Flag records where ABS() vs net-loss interpretation would change the outcome."""
    if not cfg["normalisation"].get("flag_loss_sign_divergence"):
        return None
    tol = cfg["normalisation"].get("divergence_tolerance", 0.10)
    diverging = []
    for w in ("7d", "30d", "90d"):
        a, n = m.get(f"loss_ratio_abs_{w}"), m.get(f"loss_ratio_net_{w}")
        if a is not None and n is not None and abs(a - n) > tol:
            diverging.append(w)
    if diverging:
        return ("Player finished NET POSITIVE in " + ", ".join(diverging) +
                "; ABS(loss) inflates the loss ratio - verify sign convention.")
    return None


# --------------------------------------------------------------------------- #
# Signals
# --------------------------------------------------------------------------- #
@dataclass
class Signal:
    code: str
    kind: str            # "TRIGGER" | "KPI"
    score: int
    family: str
    reason: str
    evidence: str = ""


def evaluate_kpis(m: dict, cfg: dict) -> list[Signal]:
    out: list[Signal] = []
    for code, spec in cfg["kpis"].items():
        try:
            hit = bool(eval(spec["expr"], {"__builtins__": {}}, m))  # noqa: S307 - config authored by compliance
        except Exception:
            hit = False
        if hit:
            out.append(Signal(code, "KPI", int(spec["score"]), spec["family"],
                              spec.get("reason_en", ""), _kpi_evidence(code, m)))
    return out


_EVIDENCE_FIELDS = {
    "KPI_HIGH_DEPOSIT_VOLUME": ["deposit30d", "deposit90d"],
    "KPI_HIGH_WITHDRAWAL_VOLUME": ["withdrawal30d", "withdrawal90d"],
    "KPI_LOW_GAMBLING_30D": ["deposit30d", "bet30d", "bet_dep_30d"],
    "KPI_LOW_GAMBLING_90D": ["deposit90d", "bet90d", "bet_dep_90d"],
    "KPI_HIGH_TURNOVER_VERY_LOW_LOSS_30D": ["bet30d", "loss_ratio_30d"],
    "KPI_VERY_HIGH_TURNOVER_LOW_LOSS_30D": ["bet30d", "loss_ratio_30d"],
    "KPI_VERY_HIGH_TURNOVER_LOW_LOSS_7D": ["bet7d", "loss_ratio_7d"],
    "KPI_HIGH_TURNOVER_LOW_LOSS_HIGH_PAYOUT_30D": ["bet30d", "loss_ratio_30d", "wd_dep_30d"],
    "KPI_RAPID_IN_OUT_7D": ["deposit7d", "wd_dep_7d", "bet_dep_7d"],
    "KPI_PERSISTENT_LOW_LOSS_HIGH_PAYOUT_90D": ["bet90d", "loss_ratio_90d", "wd_dep_90d"],
    "KPI_CONSUMPTION_LAUNDERING_30D": ["bet30d", "loss_ratio_30d"],
    "KPI_STAKE_ACCELERATION_7D": ["bet7d", "bet30d"],
}


def _kpi_evidence(code: str, m: dict) -> str:
    parts = []
    for f in _EVIDENCE_FIELDS.get(code, []):
        v = m.get(f)
        parts.append(f"{f}=" + ("n/a" if v is None else f"{v:,.2f}"))
    return "; ".join(parts)


def evaluate_triggers(codes: list[str], cfg: dict) -> list[Signal]:
    out, seen = [], set()
    for code in codes:
        if not code or code in seen:
            continue
        seen.add(code)
        spec = cfg["triggers"].get(code)
        if spec is None:                       # unknown trigger -> keep, score 0, surface it
            out.append(Signal(code, "TRIGGER", 0, "unmapped",
                              "Unmapped trigger code - not in scoring config", "REVIEW CONFIG"))
            continue
        out.append(Signal(code, "TRIGGER", int(spec["score"]), spec["family"],
                          spec.get("reason_en", ""), ""))
    return out


# --------------------------------------------------------------------------- #
# Section 7 - family based scoring
# --------------------------------------------------------------------------- #
def family_scores(signals: list[Signal], cfg: dict) -> dict[str, dict]:
    weights = cfg["family_weights"]
    by_family: dict[str, list[Signal]] = {}
    for s in signals:
        by_family.setdefault(s.family, []).append(s)

    result = {}
    for fam, sigs in by_family.items():
        ranked = sorted(sigs, key=lambda s: s.score, reverse=True)
        total = 0.0
        contributions = []
        for i, s in enumerate(ranked):
            w = weights[i] if i < len(weights) else 0.0
            contributions.append({"code": s.code, "raw": s.score,
                                  "weight": w, "contribution": round(s.score * w, 2)})
            total += s.score * w
        result[fam] = {"score": round(total, 2), "contributions": contributions}
    return result


# --------------------------------------------------------------------------- #
# Sections 8-11 - final score, critical signals, decision, triage
# --------------------------------------------------------------------------- #
@dataclass
class ScoreResult:
    player_id: str
    shop: str = ""
    source: str = "HighRisk"           # Trigger | HighRisk | Both
    final_score: float = 0.0
    raw_score: float = 0.0
    risk_tier: str = "LOW"
    review_decision: str = "NO"
    critical_signal: bool = False
    critical_codes: list[str] = field(default_factory=list)
    top_signals: list[str] = field(default_factory=list)
    all_signals: list[dict] = field(default_factory=list)
    family_breakdown: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    data_warning: str | None = None
    threshold_applied: int = 0
    decision_path: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def score_player(player_id: str, shop: str, source: str,
                 hr_row: dict | None, trigger_codes: list[str], cfg: dict) -> ScoreResult:
    metrics = build_metrics(hr_row or {}, cfg)
    signals = evaluate_triggers(trigger_codes or [], cfg)
    if hr_row:
        signals += evaluate_kpis(metrics, cfg)

    fams = family_scores(signals, cfg)
    raw = sum(f["score"] for f in fams.values())
    final = min(cfg["final_score"]["cap"], raw)

    crit_set = set(cfg["critical_signals"])
    crit_hits = [s.code for s in signals if s.code in crit_set]

    src_key = {"Both": "both", "Trigger": "trigger", "HighRisk": "highrisk"}[source]
    th = cfg["thresholds"][src_key]
    tri = cfg["triage"]

    if crit_hits and tri.get("critical_forces_high", True):
        tier, decision, path = "HIGH", "YES", f"Critical signal: {', '.join(crit_hits)}"
    elif final >= tri["high_score"]:
        tier, decision, path = "HIGH", "YES", f"FinalScore {final:.0f} >= {tri['high_score']}"
    elif final >= th["review"]:
        tier, decision, path = "MEDIUM", "YES", f"FinalScore {final:.0f} >= {src_key} review threshold {th['review']}"
    elif source == "Both" and tri.get("medium_uses_grey_zone", True) and final >= th["no_review"]:
        tier, decision, path = "MEDIUM", "YES", f"Grey zone {th['no_review']}-{th['review']} for source Both (score {final:.0f})"
    else:
        tier, decision, path = "LOW", "NO", f"FinalScore {final:.0f} below {src_key} threshold {th['review']}"

    top = [s.code for s in sorted(signals, key=lambda x: x.score, reverse=True)[:3]]

    return ScoreResult(
        player_id=player_id, shop=shop, source=source,
        final_score=round(final, 2), raw_score=round(raw, 2),
        risk_tier=tier, review_decision=decision,
        critical_signal=bool(crit_hits), critical_codes=crit_hits,
        top_signals=top,
        all_signals=[asdict(s) for s in sorted(signals, key=lambda x: x.score, reverse=True)],
        family_breakdown=fams, metrics=metrics,
        data_warning=loss_sign_warning(metrics, cfg),
        threshold_applied=th["review"], decision_path=path,
    )
