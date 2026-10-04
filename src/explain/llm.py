"""
Narration layer.

Hard rule: the LLM NEVER changes the score or the tier. It receives the already
computed deterministic result and writes the analyst-facing narrative. That keeps
the model auditable under AMLA / MaRisk-style model-governance expectations.

Two modes:
  * "anthropic" - real calls, batched, with a strict JSON contract
  * "template"  - offline deterministic wording (default in tests / demos)
"""
from __future__ import annotations

import json
import os
import textwrap
from typing import Any

SYSTEM_PROMPT = """You are an AML analyst assistant for a licensed sports-betting operator (Germany, GwG/AMLA scope).
You receive a DETERMINISTIC risk assessment for one player. You must NOT re-score, contradict, or second-guess it.

Write for a human AML analyst who has 60 seconds per case. Output strict JSON with keys:
  "summary"        : 2-3 sentences. What the behaviour looks like and why it fired.
  "typology"       : the closest money-laundering typology (e.g. "Placement via rapid in-out",
                     "Layering via low-loss high-turnover play", "Integration via consumption laundering",
                     "Structuring / smurfing", "Match-fixing proceeds", "Insufficient evidence").
  "key_evidence"   : array of max 4 short factual strings quoting the actual figures.
  "recommended_actions": array of 2-4 concrete next steps (SoF request, EDD, account restriction,
                     SAR/Verdachtsmeldung consideration, monitor only).
  "confidence"     : "HIGH" | "MEDIUM" | "LOW" - your confidence that this is genuinely suspicious
                     given the data provided (data gaps lower it).

Rules:
- Never invent figures. Use only what is in the payload.
- If the payload carries a data_warning, mention it in summary and lower confidence.
- Never state that the player IS laundering money. Use "consistent with", "indicative of".
- No customer-identifying speculation (nationality, occupation, ethnicity).
- German operator context: amounts are EUR.
Return ONLY the JSON object."""


def _payload(result: dict) -> dict:
    return {
        "player_id": result["player_id"],
        "shop": result.get("shop"),
        "source_list": result["source"],
        "final_score": result["final_score"],
        "risk_tier": result["risk_tier"],
        "review_decision": result["review_decision"],
        "critical_signals": result["critical_codes"],
        "decision_path": result["decision_path"],
        "signals": [
            {"code": s["code"], "kind": s["kind"], "score": s["score"],
             "family": s["family"], "reason": s["reason"], "evidence": s["evidence"]}
            for s in result["all_signals"][:8]
        ],
        "family_breakdown": {k: v["score"] for k, v in result["family_breakdown"].items()},
        "metrics": {k: (None if v is None else round(v, 3))
                    for k, v in result["metrics"].items()
                    if k.startswith(("deposit", "withdrawal", "bet", "loss_ratio_", "wd_dep_", "bet_dep_"))},
        "data_warning": result.get("data_warning"),
    }


# --------------------------------------------------------------------------- #
def explain_template(result: dict) -> dict:
    """Offline narrative - deterministic, no API cost, safe default."""
    sigs = result["all_signals"]
    top = ", ".join(s["code"] for s in sigs[:3]) or "no scored signals"
    fam = max(result["family_breakdown"].items(), key=lambda kv: kv[1]["score"], default=("n/a", {"score": 0}))[0]
    typ = {
        "rapid_in_out": "Placement / layering via rapid in-out of funds",
        "low_gambling_low_loss": "Layering via low-loss, low-engagement play",
        "safe_sports": "Layering via near-certain (low-odds) betting",
        "volume_escalation": "Unexplained volume escalation",
        "consumption_laundering": "Integration via consumption laundering",
        "payment_complexity": "Structuring across payment instruments",
        "recurrence": "Persistent high-risk recurrence",
    }.get(fam, "Insufficient evidence")
    warn = f" DATA CAVEAT: {result['data_warning']}" if result.get("data_warning") else ""
    crit = (f"Critical signal {', '.join(result['critical_codes'])} forces immediate manual review. "
            if result["critical_signal"] else "")
    summary = (f"Player {result['player_id']} (source: {result['source']}) scored "
               f"{result['final_score']:.0f}/100 and is triaged {result['risk_tier']}. "
               f"{crit}Dominant pattern: {typ.lower()}, driven by {top}.{warn}")
    evidence = [f"{s['code']} (+{s['score']}): {s['evidence'] or s['reason']}" for s in sigs[:4]]
    actions = {
        "HIGH": ["Open EDD case within 24h", "Request Source of Funds / Source of Wealth documentation",
                 "Consider payout hold pending review", "Assess SAR (Verdachtsmeldung) obligation"],
        "MEDIUM": ["Analyst review within 72h", "Request Source of Funds if pattern persists",
                   "Place on 30-day enhanced monitoring"],
        "LOW": ["No action; retain in periodic monitoring", "Re-evaluate at next weekly batch"],
    }[result["risk_tier"]]
    conf = "LOW" if result.get("data_warning") else ("HIGH" if result["critical_signal"] else "MEDIUM")
    return {"summary": summary, "typology": typ, "key_evidence": evidence,
            "recommended_actions": actions, "confidence": conf, "engine": "template"}


def explain_anthropic(result: dict, model: str = "claude-sonnet-4-6", timeout: int = 60) -> dict:
    """Real LLM narration. Falls back to the template on any failure."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], timeout=timeout)
        msg = client.messages.create(
            model=model, max_tokens=900, system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(_payload(result), ensure_ascii=False)}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(text)
        data["engine"] = model
        return data
    except Exception as exc:                                   # noqa: BLE001
        fb = explain_template(result)
        fb["engine"] = f"template-fallback ({type(exc).__name__})"
        return fb


def explain(result: dict, mode: str = "template", model: str = "claude-sonnet-4-6") -> dict:
    return explain_anthropic(result, model) if mode == "anthropic" else explain_template(result)


def flatten(expl: dict) -> dict:
    """Explanation -> flat columns for the Excel sheet."""
    return {
        "LLM_Summary": expl.get("summary", ""),
        "LLM_Typology": expl.get("typology", ""),
        "LLM_KeyEvidence": " | ".join(expl.get("key_evidence", [])),
        "LLM_RecommendedActions": " | ".join(expl.get("recommended_actions", [])),
        "LLM_Confidence": expl.get("confidence", ""),
        "LLM_Engine": expl.get("engine", ""),
    }
