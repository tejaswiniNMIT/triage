"""
Scheduled poller. Run alongside the web process:
    python scripts/worker.py

Checks the mailbox every `scheduler.poll_interval_seconds`; when mail from the
configured sender is waiting, it runs the full pipeline. A file lock prevents two
workers from processing the same message.

A tick with no new mail is a no-op and writes no run record - otherwise an idle
mailbox would fill the console's history with failed runs.
"""
import argparse, sys, time, fcntl
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import load_settings
from src.connectors import base as cbase
from src.connectors import sinks, sources, tickets  # noqa: F401  (registers connectors)
from src.orchestrator import AMLTriageAgent

LOCK = ROOT / "data" / "runs" / ".worker.lock"


def tick(profile: str | None = None, verbose: bool = True) -> None:
    settings = load_settings(profile)
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another worker holds the lock, skipping tick")
            return
        conn = cbase.build("source", settings["source"], settings.get("source_config", {}))
        if not conn.has_new():
            if verbose:
                print(f"{time.strftime('%H:%M:%S')}  no new mail from "
                      f"{settings.get('source_config', {}).get('sender_filter')}", flush=True)
            return
        print(f"{time.strftime('%H:%M:%S')}  mail found - starting run", flush=True)
        state = AMLTriageAgent(settings).run(trigger_type="scheduled")
        print(f"  {state.run_id} {state.status} {state.counts} {state.error or ''}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=None,
                    help="override AML_PROFILE / active_profile, e.g. --profile zoho")
    a = ap.parse_args()

    s = load_settings(a.profile)
    interval = s.get("_scheduler", {}).get("poll_interval_seconds", 300)
    print(f"AML worker started: profile {s['_profile']}, source {s['source']}, polling "
          f"{s.get('source_config', {}).get('sender_filter')} every {interval}s", flush=True)
    if s["source"] == "manual_upload":
        # the dev profile has no mailbox - polling it forever looks healthy and does nothing
        print(f"!! profile '{s['_profile']}' reads local files, not mail. Nothing will ever "
              f"trigger. Start with --profile zoho, or set active_profile in settings.yaml.",
              flush=True)
    while True:
        try:
            tick(a.profile)
        except Exception as exc:                       # noqa: BLE001
            print("tick failed:", exc, flush=True)     # never let the loop die
        time.sleep(interval)
