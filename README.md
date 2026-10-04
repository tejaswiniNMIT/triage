# AML Watchlist Triage Agent

Watches a mailbox for the weekly AML watchlist export, scores every player against the
trigger/KPI framework, triages HIGH / MEDIUM / LOW with a case narrative, publishes the workbook
for analysts, and raises tickets for the cases that need work.

Full design and phased delivery plan: **[BUILD_PLAN.md](BUILD_PLAN.md)**
Data quirks found in the source feed: **[docs/DATA_NOTES.md](docs/DATA_NOTES.md)**
Zoho Mail in / Zoho WorkDrive out, credentials and triggers: **[docs/ZOHO_SETUP.md](docs/ZOHO_SETUP.md)**

## Quickstart (fully offline, no credentials)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python scripts/generate_dummy_data.py        # writes data/sample/
python -m pytest tests -q                    # 10 golden tests
uvicorn src.api:app --reload --port 8000
```

Open <http://localhost:8000>, set **Where the watchlist comes from → Manual upload**, attach
`data/sample/Mailexcel1_dummy.csv` and `Mailexcel2_dummy.csv`, leave destination on *Local
folder* and tickets on *Dry-run log*, then **Run triage now**.

Headless equivalent:

```python
from pathlib import Path
from src.config import load_settings
from src.orchestrator import AMLTriageAgent

files = [(p.name, p.read_bytes()) for p in Path("data/sample").glob("Mailexcel*_dummy.csv")]
state = AMLTriageAgent(load_settings("dev")).run(manual_files=files)
print(state.status, state.counts, state.workbook_path)
```

## What comes out

`AML_Triage_<run_id>.xlsx`, four sheets:

| Sheet | Contents |
|---|---|
| Run Summary | provenance, counts, framework version, tickets created |
| Triage | one row per player: tier, score, top signals, decision path, narrative, ticket link, all raw metrics |
| Signal Detail | one row per fired signal per player — the audit trail |
| Signal Stats | firing rate per signal, for threshold calibration |

## Switching systems

Everything is a dropdown in the console, or a profile in `config/settings.yaml`:

- **In:** Zoho Mail · Outlook/M365 · generic IMAP · manual upload
- **Out:** Zoho WorkDrive · SharePoint · Teams channel · email to analysts · local folder
- **Tickets:** Jira · GitLab · Odoo · dry-run log

## Changing the scoring model

Edit `config/scoring_config.yaml` only — scores, KPI conditions, families, thresholds, tier
rules and SLAs all live there. Run with **Dry run** enabled, compare the Signal Stats sheet
before and after, then commit. Never edit `src/scoring/engine.py` to change a number.
