"""Golden tests for the deterministic scorer. Run: python -m pytest tests -q"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scoring.engine import build_metrics, family_scores, load_config, score_player  # noqa: E402

CFG = load_config()


def test_family_rule_100_50_0():
    """PDF 7.1: highest 100%, second 50%, rest 0."""
    r = score_player("P1", "S1", "Trigger", None,
                     ["AML_LOW_LOSSES_30D",     # 30
                      "AML_LOW_GAMBLING_30D",   # 20 -> *0.5 = 10
                      "AML_LOW_GAMBLING_90D"],  # 15 -> 0
                     CFG)
    assert r.final_score == 40.0
    assert r.risk_tier == "LOW"        # trigger-only threshold is 45


def test_families_are_summed_independently():
    r = score_player("P2", "S1", "Trigger", None,
                     ["AML_LOW_LOSSES_30D",       # family low_gambling: 30
                      "AML_LOW_GAMBLING_30D",     #                    + 10
                      "AML_AMOUNT_DEPOSIT_180D",  # family volume:      10
                      "AML_AMOUNT_DEPOSIT_30D"],  #                    + 4
                     CFG)
    assert r.final_score == 54.0
    assert r.risk_tier == "MEDIUM" and r.review_decision == "YES"


def test_score_is_capped_at_100():
    r = score_player("P3", "S1", "Trigger", None, list(CFG["triggers"]), CFG)
    assert r.final_score == 100.0


def test_critical_signal_forces_high_regardless_of_score():
    r = score_player("P4", "S1", "Trigger", None, ["AML_SAFE_SPORTS_BET_10K"], CFG)
    assert r.critical_signal and r.risk_tier == "HIGH" and r.review_decision == "YES"
    assert r.final_score == 40.0     # below every review threshold, still HIGH


def test_nullif_semantics_no_divide_by_zero():
    m = build_metrics({"deposit30d": 0, "bet30d": 5000, "loss30d": -100}, CFG)
    assert m["bet_dep_30d"] is None and m["loss_ratio_30d"] is None


def test_kpi_rapid_in_out_is_critical():
    row = {"deposit7d": 5000, "withdrawal7d": 4500, "bet7d": 1000, "loss7d": -100}
    r = score_player("P5", "S1", "HighRisk", row, [], CFG)
    assert "KPI_RAPID_IN_OUT_7D" in r.top_signals and r.risk_tier == "HIGH"


def test_source_thresholds_differ():
    codes = ["AML_LOW_LOSSES_90D", "AML_LOW_GAMBLING_90D"]   # 34 + 7.5 = 41.5
    assert score_player("A", "S", "Trigger", None, codes, CFG).risk_tier == "LOW"      # needs 45
    assert score_player("B", "S", "HighRisk", None, codes, CFG).risk_tier == "LOW"     # needs 50
    assert score_player("C", "S", "Both", None, codes, CFG).risk_tier == "MEDIUM"      # grey zone 40-70


def test_loss_sign_divergence_is_flagged():
    row = {"deposit30d": 10000, "bet30d": 25000, "loss30d": 90000}   # player WON 90k
    r = score_player("P6", "S1", "HighRisk", row, [], CFG)
    assert r.data_warning is not None


def test_unknown_trigger_is_surfaced_not_dropped():
    r = score_player("P7", "S1", "Trigger", None, ["AML_BRAND_NEW_RULE"], CFG)
    assert any(s["code"] == "AML_BRAND_NEW_RULE" and s["evidence"] == "REVIEW CONFIG"
               for s in r.all_signals)


def test_explainability_contract():
    r = score_player("P8", "S1", "Both", {"deposit30d": 20000, "bet30d": 25000, "loss30d": -1000,
                                          "withdrawal30d": 16000}, ["AML_AMOUNT_DEPOSIT_30D"], CFG)
    assert r.decision_path and r.family_breakdown and r.all_signals
    assert abs(sum(f["score"] for f in r.family_breakdown.values()) - r.raw_score) < 0.01
