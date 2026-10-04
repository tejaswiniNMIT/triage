"""
Find AML tasks that were created without a project and file them into one.

    python scripts/odoo_fix_orphans.py --project-id 3            # list them, change nothing
    python scripts/odoo_fix_orphans.py --project-id 3 --apply    # write project_id

A `project.task` created with no `project_id` exists in the database but appears in
no view in Odoo. Worse, `find_existing` still matches it by name on the next run, so
those runs report "existing" and never create a visible replacement. Moving the
orphans into a project fixes both at once - nothing is deleted.

Odoo connection details come from `ticket_config` in config/settings.yaml.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import load_settings                      # noqa: E402
from src.connectors.tickets import OdooConnector          # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default="zoho")
    ap.add_argument("--project-id", type=int, required=True,
                    help="the project the tasks should live in (from the Odoo URL)")
    ap.add_argument("--prefix", default="aml-",
                    help="external-key prefix the agent writes into the task name")
    ap.add_argument("--apply", action="store_true", help="actually write; default lists only")
    a = ap.parse_args()

    cfg = load_settings(a.profile).get("ticket_config", {})
    conn = OdooConnector({**cfg, "model": "project.task", "project_id": a.project_id})
    print(f"{conn.url}  db={conn.db}  as {conn.user}")

    ids = conn._exec("search", [[["name", "like", a.prefix], ["project_id", "=", False]]],
                     {"limit": 1000})
    if not ids:
        print(f"no project-less tasks matching {a.prefix!r} - nothing to do")
        return 0
    rows = conn._exec("read", [ids], {"fields": ["name"]})
    print(f"\n{len(rows)} orphan task(s):")
    for r in rows[:10]:
        print(f"  {r['id']:>6}  {r['name'][:90]}")
    if len(rows) > 10:
        print(f"  ... and {len(rows) - 10} more")

    if not a.apply:
        print(f"\ndry run. Re-run with --apply to move them into project {a.project_id}.")
        return 0
    conn._exec("write", [ids, {"project_id": a.project_id}])
    print(f"\nmoved {len(ids)} task(s) into project {a.project_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
