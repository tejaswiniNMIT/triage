"""
Ingest layer: accept whatever the mailbox sends (csv / xls / xlsx), work out which
file is the High-Risk list and which is the Trigger list, and normalise columns.

Nothing downstream should ever see a raw vendor column name.
"""
from __future__ import annotations

import io
import math
import re
from pathlib import Path

import pandas as pd

# canonical name -> accepted aliases (lower-cased, non-alnum stripped)
ALIASES = {
    "player_id":       ["playerid", "player", "kundennummer", "customerid", "accountid"],
    "shop":            ["shop", "shopid", "shopcode", "outlet", "branch", "filiale"],
    "brand":           ["brand", "brandid"],
    "registration":    ["registration", "registrationdate", "regdate"],
    "week":            ["week", "kw"],
    "days_in_hr":      ["daysinhr", "dayshighrisk"],
    "pep":             ["pep"],
    "age":             ["age", "alter"],
    "last_active":     ["lastactivedate", "lastactive"],
    "alert_date":      ["alertdate"],
    "alert_code":      ["alertcode", "trigger", "triggercode", "rule"],
    "incident_date":   ["incidentdate"],
    "incident_reason": ["incidentreason"],
}
for base in ("deposit", "withdrawal", "bet", "loss"):
    for w in ("7d", "30d", "90d"):
        ALIASES[f"{base}{w}"] = [f"{base}{w}", f"{base}_{w}", f"{base}{w[:-1]}days"]

_NUMERIC = [f"{b}{w}" for b in ("deposit", "withdrawal", "bet", "loss") for w in ("7d", "30d", "90d")]


def _key(col: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(col).lower())


def _rename_map(cols) -> dict:
    lookup = {}
    for canon, alts in ALIASES.items():
        for a in alts:
            lookup[_key(a)] = canon
        lookup[_key(canon)] = canon
    return {c: lookup[_key(c)] for c in cols if _key(c) in lookup}


def read_tabular(path_or_bytes, filename: str = "") -> pd.DataFrame:
    name = (filename or str(path_or_bytes)).lower()
    if isinstance(path_or_bytes, (bytes, bytearray)):
        buf = io.BytesIO(path_or_bytes)
        return pd.read_csv(buf) if name.endswith((".csv", ".txt")) else pd.read_excel(buf)
    if name.endswith((".csv", ".txt")):
        return pd.read_csv(path_or_bytes)
    return pd.read_excel(path_or_bytes)


def classify(df: pd.DataFrame) -> str:
    """Return 'highrisk' | 'trigger' | 'unknown' from the column signature."""
    keys = {_key(c) for c in df.columns}
    if any(k in keys for k in ("alertcode", "triggercode", "rule")):
        return "trigger"
    if {"deposit30d", "bet30d"} <= keys or sum(_key(n) in keys for n in _NUMERIC) >= 6:
        return "highrisk"
    return "unknown"


def normalise(df: pd.DataFrame, kind: str) -> pd.DataFrame:
    out = df.rename(columns=_rename_map(df.columns)).copy()
    out = out.replace({"null": None, "NULL": None, "": None})
    if "player_id" not in out.columns:
        raise ValueError(f"{kind} file has no Player ID column - cannot process")
    out["player_id"] = out["player_id"].astype(str).str.strip()
    out = out[out["player_id"].notna() & (out["player_id"] != "") & (out["player_id"].str.lower() != "nan")]
    if "shop" not in out.columns:
        out["shop"] = "UNKNOWN"
    out["shop"] = out["shop"].fillna("UNKNOWN")
    if kind == "highrisk":
        for c in _NUMERIC:
            out[c] = pd.to_numeric(out.get(c), errors="coerce").fillna(0.0) if c in out.columns else 0.0
        out = out.drop_duplicates(subset=["player_id"], keep="last")
    else:
        if "alert_code" in out.columns:
            out["alert_code"] = out["alert_code"].astype(str).str.strip().str.upper()
    return out


def load_watchlists(files: list[tuple[str, object]]) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """files = [(filename, path-or-bytes), ...] -> (highrisk_df, trigger_df, notes)"""
    hr, tr, notes = [], [], []
    for fname, payload in files:
        df = read_tabular(payload, fname)
        kind = classify(df)
        if kind == "unknown":
            notes.append(f"{fname}: unrecognised schema, skipped")
            continue
        (hr if kind == "highrisk" else tr).append(normalise(df, kind))
        notes.append(f"{fname}: detected as {kind} list ({len(df)} rows)")
    hr_df = pd.concat(hr, ignore_index=True) if hr else pd.DataFrame(columns=["player_id", "shop"] + _NUMERIC)
    tr_df = pd.concat(tr, ignore_index=True) if tr else pd.DataFrame(columns=["player_id", "shop", "alert_code"])
    if not hr_df.empty:
        hr_df = hr_df.drop_duplicates(subset=["player_id"], keep="last")
    return hr_df, tr_df, notes


def build_review_population(hr_df: pd.DataFrame, tr_df: pd.DataFrame) -> pd.DataFrame:
    """PDF section 3: DISTINCT(trigger) UNION DISTINCT(highrisk), with Source tagging."""
    hr_ids = set(hr_df["player_id"]) if not hr_df.empty else set()
    tr_ids = set(tr_df["player_id"]) if not tr_df.empty else set()
    rows = []
    shop_map = {}
    for df in (hr_df, tr_df):
        if not df.empty and "shop" in df.columns:
            shop_map.update(dict(zip(df["player_id"], df["shop"])))
    for pid in sorted(hr_ids | tr_ids):
        source = "Both" if pid in hr_ids and pid in tr_ids else ("HighRisk" if pid in hr_ids else "Trigger")
        rows.append({"player_id": pid, "shop": shop_map.get(pid, "UNKNOWN"), "source": source})
    return pd.DataFrame(rows)
