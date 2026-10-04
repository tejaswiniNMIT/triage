"""
Generate the two watchlist files with realistic dummy Player IDs / Shops and a
balanced HIGH / MEDIUM / LOW risk distribution.

  * Real-shaped rows are taken from the anonymised reference exports.
  * Player IDs and Shops are synthesised and deliberately overlapped so the
    population contains Trigger-only, HighRisk-only and Both players.
  * After generation the file is scored; if any tier is under-represented,
    purpose-built HIGH / MEDIUM archetypes are injected until it balances.

Usage: python scripts/generate_dummy_data.py --ref-dir <dir> --out-dir data/sample
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.scoring.engine import load_config, score_player  # noqa: E402

SEED = 20260826
SHOPS = ["SHOP-BER-001", "SHOP-BER-002", "SHOP-MUC-011", "SHOP-HAM-021", "SHOP-FRA-031",
         "SHOP-KOE-041", "SHOP-STU-051", "SHOP-LEI-061", "ONLINE-DE-001", "ONLINE-DE-002"]
NUM_COLS = [f"{b}{w}" for b in ("deposit", "withdrawal", "bet", "loss") for w in ("7d", "30d", "90d")]


def pid(n: int) -> str:
    return f"PL-{100000 + n}"


# --------------------------------------------------------------------------- #
# Archetypes - each returns a high-risk row and/or a list of trigger codes
# --------------------------------------------------------------------------- #
def archetype_high(rng: random.Random, kind: str) -> dict:
    if kind == "rapid_in_out":            # KPI_RAPID_IN_OUT_7D  (critical)
        dep7 = rng.uniform(4000, 12000)
        return dict(deposit7d=dep7, withdrawal7d=dep7 * rng.uniform(0.85, 0.97),
                    bet7d=dep7 * rng.uniform(0.05, 0.25), loss7d=-dep7 * 0.05,
                    deposit30d=dep7 * 2.6, withdrawal30d=dep7 * 2.3, bet30d=dep7 * 0.7, loss30d=-dep7 * 0.1,
                    deposit90d=dep7 * 6.5, withdrawal90d=dep7 * 5.9, bet90d=dep7 * 1.8, loss90d=-dep7 * 0.3)
    if kind == "high_volume":             # KPI_HIGH_DEPOSIT_VOLUME (critical)
        d90 = rng.uniform(60000, 140000)
        return dict(deposit7d=d90 * 0.08, withdrawal7d=d90 * 0.05, bet7d=d90 * 0.06, loss7d=-d90 * 0.01,
                    deposit30d=d90 * 0.35, withdrawal30d=d90 * 0.28, bet30d=d90 * 0.30, loss30d=-d90 * 0.04,
                    deposit90d=d90, withdrawal90d=d90 * 0.72, bet90d=d90 * 0.85, loss90d=-d90 * 0.12)
    if kind == "payout_low_loss":         # KPI_HIGH_TURNOVER_LOW_LOSS_HIGH_PAYOUT_30D (critical)
        d30 = rng.uniform(18000, 40000)
        return dict(deposit7d=d30 * 0.2, withdrawal7d=d30 * 0.18, bet7d=d30 * 0.25, loss7d=-d30 * 0.02,
                    deposit30d=d30, withdrawal30d=d30 * rng.uniform(0.75, 0.92),
                    bet30d=d30 * rng.uniform(1.1, 2.0), loss30d=-d30 * rng.uniform(0.03, 0.12),
                    deposit90d=d30 * 2.4, withdrawal90d=d30 * 2.0, bet90d=d30 * 3.6, loss90d=-d30 * 0.25)
    # withdrawal_heavy -> KPI_HIGH_WITHDRAWAL_VOLUME (critical)
    w90 = rng.uniform(60000, 120000)
    return dict(deposit7d=w90 * 0.05, withdrawal7d=w90 * 0.10, bet7d=w90 * 0.04, loss7d=w90 * 0.02,
                deposit30d=w90 * 0.25, withdrawal30d=w90 * 0.30, bet30d=w90 * 0.20, loss30d=w90 * 0.05,
                deposit90d=w90 * 0.60, withdrawal90d=w90, bet90d=w90 * 0.55, loss90d=w90 * 0.15)


def archetype_medium(rng: random.Random, kind: str) -> dict:
    if kind == "low_gambling":            # KPI_LOW_GAMBLING_30D + 90D, no critical
        d30 = rng.uniform(3000, 9000)
        return dict(deposit7d=d30 * 0.2, withdrawal7d=0, bet7d=d30 * 0.03, loss7d=-d30 * 0.02,
                    deposit30d=d30, withdrawal30d=d30 * 0.3, bet30d=d30 * rng.uniform(0.05, 0.18),
                    loss30d=-d30 * 0.05,
                    deposit90d=d30 * 2.5, withdrawal90d=d30 * 0.9, bet90d=d30 * 0.35, loss90d=-d30 * 0.12)
    if kind == "turnover_low_loss":       # KPI_HIGH_TURNOVER_VERY_LOW_LOSS_30D + STAKE_ACCEL
        b30 = rng.uniform(11000, 19000)
        return dict(deposit7d=b30 * 0.25, withdrawal7d=b30 * 0.1, bet7d=b30 * 0.65, loss7d=-b30 * 0.02,
                    deposit30d=b30 * 0.9, withdrawal30d=b30 * 0.4, bet30d=b30, loss30d=-b30 * 0.09,
                    deposit90d=b30 * 2.2, withdrawal90d=b30 * 1.1, bet90d=b30 * 2.6, loss90d=-b30 * 0.25)
    # consumption laundering: very high turnover, near-total loss
    b30 = rng.uniform(21000, 45000)
    return dict(deposit7d=b30 * 0.2, withdrawal7d=0, bet7d=b30 * 0.3, loss7d=-b30 * 0.2,
                deposit30d=b30 * 0.85, withdrawal30d=b30 * 0.02, bet30d=b30, loss30d=-b30 * 0.80,
                deposit90d=b30 * 2.0, withdrawal90d=b30 * 0.05, bet90d=b30 * 2.5, loss90d=-b30 * 1.7)


HIGH_TRIGGER_SETS = [
    ["AML_DEPOSIT_WITHOUT_BET_7D_10K", "AML_AMOUNT_DEPOSIT_30D"],
    ["AML_HIGH_WINS_NO_GAMBLING_30D", "AML_AGAIN_HIGH_RISK"],
    ["AML_MONEY_TRANSFER_LOW_GAMBLING_7D", "AML_LOW_GAMBLING_30D"],
    ["AML_SAFE_SPORTS_BET_10K", "AML_SAFE_SPORTS_BETS_30D_10K", "AML_AMOUNT_DEPOSIT_180D"],
]
MEDIUM_TRIGGER_SETS = [
    ["AML_LOW_LOSSES_30D", "AML_LOW_GAMBLING_30D", "AML_AMOUNT_DEPOSIT_180D", "AML_AMOUNT_DEPOSIT_30D"],
    ["AML_LOW_LOSSES_90D", "AML_LOW_GAMBLING_90D", "AML_MULTIPLE_PAYMENT_ACCOUNTS", "AML_AGAIN_HIGH_RISK"],
    ["AML_SAFE_SPORTS_BETS_30D", "AML_SAFE_SPORTS_BET", "AML_STAKE_INCREASE_7D", "AML_AMOUNT_DEPOSIT_DAY"],
    ["AML_DEPOSIT_WITHOUT_BET_7D", "AML_LOW_GAMBLING_30D", "AML_FIRST_TIME_HIGH_RISK"],
]
LOW_TRIGGER_SETS = [
    ["AML_AMOUNT_DEPOSIT_DAY"],
    ["AML_FIRST_TIME_HIGH_RISK"],
    ["AML_AMOUNT_DEPOSIT_DAY", "AML_MULTIPLE_PAYMENT_ACCOUNTS"],
]


def alert_ts(rng: random.Random) -> str:
    day = rng.randint(10, 17)
    return f"Aug {day}, 2026, {rng.randint(1,11)}:{rng.randint(0,59):02d}:{rng.randint(0,59):02d} {rng.choice(['AM','PM'])}"


# --------------------------------------------------------------------------- #
def build(ref_dir: Path, out_dir: Path, target_min_share: float = 0.28) -> dict:
    rng = random.Random(SEED)
    cfg = load_config()

    hr_ref = pd.read_csv(ref_dir / "Mailexcel1.csv")
    tr_ref = pd.read_csv(ref_dir / "Mailexcel2.csv")

    # ---- identity assignment ------------------------------------------------
    n_hr = len(hr_ref)
    hr_ref = hr_ref.copy()
    hr_ref["Player ID"] = [pid(i) for i in range(1, n_hr + 1)]
    hr_ref["Shop"] = [rng.choice(SHOPS) for _ in range(n_hr)]
    hr_ref["Brand ID"] = "BR-33"

    # 55% of trigger rows map onto high-risk players ("Both"), rest are trigger-only
    tr_ref = tr_ref.copy()
    trigger_only_pool = [pid(i) for i in range(n_hr + 1, n_hr + 16)]
    tr_ids, tr_shops = [], []
    hr_shop_map = dict(zip(hr_ref["Player ID"], hr_ref["Shop"]))
    for _ in range(len(tr_ref)):
        if rng.random() < 0.55:
            p = pid(rng.randint(1, n_hr))
        else:
            p = rng.choice(trigger_only_pool)
        tr_ids.append(p)
        tr_shops.append(hr_shop_map.get(p) or rng.choice(SHOPS))
    tr_ref["Player ID"], tr_ref["Shop"], tr_ref["Brand"] = tr_ids, tr_shops, "BR-33"
    # keep shops stable per player
    shop_lock = {}
    for p, s in zip(tr_ref["Player ID"], tr_ref["Shop"]):
        shop_lock.setdefault(p, s)
    tr_ref["Shop"] = tr_ref["Player ID"].map(shop_lock)

    hr_rows = hr_ref.to_dict("records")
    tr_rows = tr_ref.to_dict("records")
    next_id = n_hr + 30

    def add_case(tier: str) -> None:
        nonlocal next_id
        next_id += 1
        p, shop = pid(next_id), rng.choice(SHOPS)
        if tier == "HIGH":
            style = rng.choice(["kpi", "kpi", "trigger", "both"])
            if style in ("kpi", "both"):
                row = archetype_high(rng, rng.choice(["rapid_in_out", "high_volume", "payout_low_loss", "withdrawal_heavy"]))
                hr_rows.append(_hr_record(p, shop, row, rng))
            if style in ("trigger", "both"):
                for code in rng.choice(HIGH_TRIGGER_SETS):
                    tr_rows.append(_tr_record(p, shop, code, rng))
        elif tier == "MEDIUM":
            style = rng.choice(["kpi", "trigger", "both"])
            if style in ("kpi", "both"):
                row = archetype_medium(rng, rng.choice(["low_gambling", "turnover_low_loss", "consumption"]))
                hr_rows.append(_hr_record(p, shop, row, rng))
            if style in ("trigger", "both"):
                for code in rng.choice(MEDIUM_TRIGGER_SETS):
                    tr_rows.append(_tr_record(p, shop, code, rng))
        else:  # LOW
            for code in rng.choice(LOW_TRIGGER_SETS):
                tr_rows.append(_tr_record(p, shop, code, rng))

    # ---- seed a baseline of designed cases ---------------------------------
    for _ in range(14):
        add_case("HIGH")
    for _ in range(16):
        add_case("MEDIUM")
    for _ in range(8):
        add_case("LOW")

    # ---- rebalance loop -----------------------------------------------------
    for _ in range(40):
        dist = _distribution(hr_rows, tr_rows, cfg)
        total = sum(dist.values())
        deficits = [t for t in ("HIGH", "MEDIUM", "LOW") if dist.get(t, 0) / total < target_min_share]
        if not deficits:
            break
        for t in deficits:
            for _ in range(4):
                add_case(t)

    out_dir.mkdir(parents=True, exist_ok=True)
    hr_out = pd.DataFrame(hr_rows)[
        ["Player ID", "Shop", "Brand ID", "Registration", "Week", "Days in HR", "PEP", "Age",
         "Last Active Date"] + [c for c in hr_ref.columns if c in NUM_COLS]]
    tr_out = pd.DataFrame(tr_rows)[
        ["Player ID", "Shop", "Brand", "PEP", "Alert Date", "Alert Code", "Incident Date", "Incident Reason"]]
    hr_out.to_csv(out_dir / "Mailexcel1_dummy.csv", index=False)
    tr_out.to_csv(out_dir / "Mailexcel2_dummy.csv", index=False)
    with pd.ExcelWriter(out_dir / "AML_Watchlist_Input.xlsx", engine="openpyxl") as xl:
        hr_out.to_excel(xl, sheet_name="HighRiskList", index=False)
        tr_out.to_excel(xl, sheet_name="TriggerList", index=False)

    dist = _distribution(hr_rows, tr_rows, cfg)
    return {"highrisk_rows": len(hr_out), "trigger_rows": len(tr_out), "distribution": dist,
            "out_dir": str(out_dir)}


def _hr_record(p: str, shop: str, metrics: dict, rng: random.Random) -> dict:
    rec = {"Player ID": p, "Shop": shop, "Brand ID": "BR-33",
           "Registration": f"{rng.choice(['Jan','Mar','Jun','Sep','Nov'])} {rng.randint(1,28)}, {rng.randint(2018,2026)}",
           "Week": 33, "Days in HR": rng.choice([2, 3, 4, 7, 7, 7]), "PEP": rng.choice(["null", "null", "null", "true"]),
           "Age": rng.randint(21, 68), "Last Active Date": f"Aug {rng.randint(10,19)}, 2026"}
    for c in NUM_COLS:
        rec[c] = round(float(metrics.get(c, 0.0)), 2)
    return rec


def _tr_record(p: str, shop: str, code: str, rng: random.Random) -> dict:
    ts = alert_ts(rng)
    incident = code in ("AML_LOW_LOSSES_30D", "AML_LOW_LOSSES_90D", "AML_AMOUNT_DEPOSIT_30D")
    return {"Player ID": p, "Shop": shop, "Brand": "BR-33", "PEP": "null", "Alert Date": ts,
            "Alert Code": code, "Incident Date": ts if incident else "null",
            "Incident Reason": code if incident else "null"}


def _distribution(hr_rows, tr_rows, cfg) -> dict:
    hr = {r["Player ID"]: r for r in hr_rows}
    trig: dict[str, list[str]] = {}
    shops = {}
    for r in tr_rows:
        trig.setdefault(r["Player ID"], []).append(r["Alert Code"])
        shops.setdefault(r["Player ID"], r["Shop"])
    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for p in set(hr) | set(trig):
        source = "Both" if p in hr and p in trig else ("HighRisk" if p in hr else "Trigger")
        row = {k: hr[p].get(k) for k in NUM_COLS} if p in hr else None
        res = score_player(p, hr.get(p, {}).get("Shop", shops.get(p, "")), source, row, trig.get(p, []), cfg)
        counts[res.risk_tier] += 1
    return counts


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref-dir", default="/mnt/user-data/uploads")
    ap.add_argument("--out-dir", default=str(ROOT / "data" / "sample"))
    a = ap.parse_args()
    print(build(Path(a.ref_dir), Path(a.out_dir)))
