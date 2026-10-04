"""Headless run. Example:
   python scripts/run_local.py --files data/sample/Mailexcel1_dummy.csv data/sample/Mailexcel2_dummy.csv
"""
import argparse, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import load_settings
from src.orchestrator import AMLTriageAgent

ap = argparse.ArgumentParser()
ap.add_argument("--profile", default="dev")
ap.add_argument("--files", nargs="*", default=[])
ap.add_argument("--dry-run", action="store_true")
a = ap.parse_args()

files = [(Path(f).name, Path(f).read_bytes()) for f in a.files] or None
st = AMLTriageAgent(load_settings(a.profile)).run(manual_files=files, dry_run=a.dry_run)
print(st.status, st.counts)
for s in st.steps:
    print(f"  {s['name']:9s} {s['status']:8s} {s['detail'][:110]}")
print("workbook:", st.workbook_path)
