# AML Watchlist Triage Agent — Build Plan

**Goal.** An event-driven agent that watches a mailbox for the weekly AML watchlist export,
scores every player against the trigger/KPI framework, triages them HIGH/MEDIUM/LOW with an
LLM-written case narrative, publishes the workbook to a document store, and raises tickets for
HIGH and MEDIUM cases — all controllable from an operator console.

This repository already contains a **working reference implementation** of the whole pipeline
running offline (mock connectors). What remains is wiring the real SaaS credentials and
hardening. The plan below is written so you can paste each phase into Claude Code as a task.

---

## 1. What "agent" means here (and what it deliberately does not)

This is a **deterministic pipeline with one LLM step**, not an autonomous reasoning loop. That
is a design decision, not a limitation:

| Concern | Why a fixed pipeline wins |
|---|---|
| Regulatory defensibility | A BaFin/GwG auditor asks "why was player X flagged?" You must answer with an arithmetic trace, not "the model decided". |
| Reproducibility | Re-running last Tuesday's file must produce byte-identical scores. |
| Cost | 150–5,000 players/week × free-roaming agent = unbounded token spend. |
| Model risk | The LLM never touches a number, so it never needs model-risk validation as a *scoring* model. |

The LLM's job is **narration and typology labelling only**. The scoring engine is pure Python
driven by `config/scoring_config.yaml`.

If you later want a graph framework, each `_step_*` method in `src/orchestrator.py` maps 1:1 to
a LangGraph node — the state object and contracts are already shaped for it.

```
  ┌──────────┐   ┌───────┐   ┌───────┐   ┌─────────┐   ┌──────────┐   ┌─────────┐   ┌─────────┐
  │ 01 Fetch │──▶│02 Parse│──▶│03 Score│──▶│04 Explain│──▶│05 Workbook│──▶│06 Deliver│──▶│07 Tickets│
  └──────────┘   └───────┘   └───────┘   └─────────┘   └──────────┘   └─────────┘   └─────────┘
   Zoho Mail      schema      trigger +    Claude        formatted      WorkDrive     Jira
   Outlook        detect      KPI +        narrative     xlsx           SharePoint    GitLab
   IMAP           normalise   family       (per case)    4 sheets       Teams         Odoo
   UI upload      merge       scoring                                   Outlook       (mock)
```

Steps 06 and 07 run in parallel in production (both depend only on 05). Keep them sequential
until the connectors are stable — the ticket body links to the delivered workbook URL.

---

## 2. Architecture

```
                        ┌────────────────────────────────────────────┐
   mail arrives ───────▶│  Trigger layer                             │
                        │  a) webhook  (Zoho Flow / Graph subscription)│
                        │  b) poller   (APScheduler, every 5 min)     │
                        │  c) manual   (operator console button)      │
                        └───────────────┬────────────────────────────┘
                                        ▼
   ┌────────────────────────────────────────────────────────────────────────┐
   │  Orchestrator  (src/orchestrator.py) — linear, idempotent, resumable   │
   │  emits RunState after every step → data/runs/<run_id>.json             │
   └───┬──────────────┬───────────────┬───────────────┬────────────────┬────┘
       ▼              ▼               ▼               ▼                ▼
   SourceConn     Ingest/Merge    ScoringEngine    Explainer      Sink + Ticket
   (pluggable)    (pandas)        (pure, YAML)     (Claude)       (pluggable)
       │                                                              │
       └──────────────── all behind 3 ABCs in connectors/base.py ─────┘

   FastAPI control plane (src/api.py) ── serves the console + REST + webhook
```

**The one rule that keeps this maintainable:** the pipeline never imports a vendor SDK. It only
knows `SourceConnector`, `SinkConnector`, `TicketConnector`. Swapping Zoho → Microsoft is a
dropdown change, not a code change.

---

## 3. Repository layout

```
aml-triage-agent/
├── config/
│   ├── scoring_config.yaml     ← ALL thresholds/scores. Compliance owns this file.
│   └── settings.yaml           ← connector profiles: dev / zoho / microsoft
├── src/
│   ├── config.py               settings loader with ${ENV} interpolation
│   ├── orchestrator.py         the 7-step pipeline + RunState
│   ├── api.py                  FastAPI control plane
│   ├── ui/index.html           operator console (single file, no build step)
│   ├── connectors/
│   │   ├── base.py             3 ABCs + registry
│   │   ├── sources.py          zoho_mail · outlook_graph · imap · manual_upload
│   │   ├── sinks.py            zoho_workdrive · sharepoint · teams · outlook_mail · local
│   │   └── tickets.py          jira · gitlab · odoo · mock
│   ├── ingest/parser.py        schema detection, column aliasing, review population
│   ├── scoring/engine.py       PDF sections 4–11, fully explainable
│   ├── explain/llm.py          Claude narration + deterministic fallback
│   └── output/excel_writer.py  4-sheet analyst workbook
├── scripts/generate_dummy_data.py
├── tests/test_scoring.py       10 golden tests incl. hand-computed family maths
└── data/{sample,runs,outbox}/
```

---

## 4. The scoring engine (already implemented — verify, don't rewrite)

### 4.1 Population
`ReviewPopulation = DISTINCT(trigger.PlayerID) ∪ DISTINCT(highrisk.PlayerID)`, each tagged
`Trigger` / `HighRisk` / `Both`. Implemented in `ingest/parser.build_review_population`.

### 4.2 Normalisation
`loss_ratio_Xd`, `bet_dep_Xd`, `wd_dep_Xd` with `NULLIF(deposit,0)` semantics — division by zero
yields `None`, and a `None` never satisfies a KPI condition. **This matters:** a naive
implementation returns `inf` or `0` and silently mis-fires KPIs on dormant accounts.

### 4.3 Family scoring (PDF §7)
Within each family: highest signal ×1.0, second ×0.5, everything else ×0. Then
`FinalScore = MIN(100, Σ family scores)`. Worked example, verified in `tests/test_scoring.py`:

```
AML_LOW_LOSSES_30D (30) + AML_LOW_GAMBLING_30D (20) + AML_LOW_GAMBLING_90D (15)
  → same family → 30 + (20×0.5) + 0 = 40
```

### 4.4 Decision + triage
Critical signal (PDF §9) → HIGH regardless of score. Otherwise thresholds by source:
`Both ≥70`, `Trigger ≥45`, `HighRisk ≥50`. The PDF leaves 40–69 for `Both` undefined; this
implementation calls that band **MEDIUM** (`triage.medium_uses_grey_zone`). Confirm with
compliance and flip the flag if they disagree.

| Tier | Rule | SLA | Ticket |
|---|---|---|---|
| HIGH | critical signal OR score ≥ 70 | 24 h | yes, P1 |
| MEDIUM | ≥ source threshold, or `Both` grey zone | 72 h | yes, P2 |
| LOW | everything else | — | no |

### 4.5 Two spec issues to raise with the framework owner
1. **`ABS(loss)` conflates wins and losses.** In the source feed a negative loss = the player
   lost money, positive = the player finished ahead. `loss_ratio = ABS(loss)/deposit` therefore
   scores a player who *won* €90k as if they had *lost* €90k, firing
   `KPI_CONSUMPTION_LAUNDERING_30D` on a big winner. The engine computes both interpretations,
   scores with the spec-faithful one, and writes a `DataWarning` when they diverge. See
   `docs/DATA_NOTES.md`.
2. **Trigger conditions are not re-derivable.** Triggers arrive pre-computed as alert codes, so
   the agent cannot verify them against the KPI data. Some overlap heavily with KPIs
   (`AML_LOW_GAMBLING_30D` vs `KPI_LOW_GAMBLING_30D`) — the family rule is what prevents
   double-counting, which is exactly why it must not be "simplified" later.

---

## 5. Build phases

Each phase is a self-contained Claude Code task. Copy the block verbatim.

### Phase 0 — Stand it up locally (30 min)
```
Clone the repo. Create a venv, pip install -r requirements.txt.
Run: python scripts/generate_dummy_data.py
Run: python -m pytest tests -q          # 10 tests must pass
Run: uvicorn src.api:app --reload --port 8000
Open http://localhost:8000, attach data/sample/Mailexcel1_dummy.csv and
Mailexcel2_dummy.csv, source=Manual upload, sink=Local folder, tickets=Dry-run log.
Confirm the run reaches 07 Tickets and the workbook downloads.
```
Exit criteria: green tests, a downloadable workbook, ~157 players triaged.

### Phase 1 — Real scoring against real data (1 day)
```
Take one real (pseudonymised) weekly export. Run it through the pipeline with
sink=local, tickets=mock. Then:
 1. Export the Signal Stats sheet and compare firing rates against the AML team's
    current manual counts. Any KPI firing on >40% or <0.5% of the population is a
    threshold bug, not a finding.
 2. Have an analyst hand-score 20 players. Diff against the engine. Every difference
    is either a config fix or a spec ambiguity to log in docs/DECISIONS.md.
 3. Only change config/scoring_config.yaml — never scoring/engine.py.
```
Exit criteria: ≥95 % agreement with analyst hand-scoring on 20 cases.

### Phase 2 — Zoho Mail intake (1 day)
```
Register a Zoho API client at api-console.zoho.eu (Self Client for a service account).
Scopes: ZohoMail.messages.READ, ZohoMail.accounts.READ, ZohoMail.folders.READ
Generate a refresh token; store ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET / ZOHO_REFRESH_TOKEN.
GET /api/accounts to find accountId; list folders to find the intake folderId.
Fill config/settings.yaml profile `zoho`, then run with source=Zoho Mail.
Verify: sender filter rejects a mail from any other address; a mail with no CSV/XLSX
attachment is skipped, not crashed; mark-as-read prevents reprocessing.
```
Watch for: the datacenter suffix (`.eu` vs `.com`) must match where the account lives, or every
call 401s. Attachment download is a **two-call** flow — `attachmentinfo` then per-attachment GET.

### Phase 3 — Delivery + tickets (1 day)
```
Zoho WorkDrive: scope WorkDrive.files.CREATE, get the target folder_id from the folder URL.
Jira: create an API token, confirm the AML project key and that the priority names in
priority_map exist in that project's scheme (Highest/High/Low are defaults but often renamed).
Run end-to-end with dry_run=false on a 10-player subset first.
Verify idempotency: run the SAME file twice. The second run must find existing tickets by the
external key label and create zero duplicates.
```
Exit criteria: rerunning a batch creates 0 duplicate tickets.

### Phase 4 — LLM narratives (0.5 day)
```
Set ANTHROPIC_API_KEY, switch explain_mode to `anthropic`.
Batch-narrate HIGH and MEDIUM only (explain_tiers) — LOW keeps the template, saving ~60% of
calls. Add concurrency: 8 parallel calls with a semaphore, exponential backoff on 429.
Have two analysts blind-rate 30 narratives on: factually correct / typology plausible /
actionable. Anything under 90% factual accuracy means the prompt is over-reaching — tighten
SYSTEM_PROMPT rather than swapping models.
```
Guardrail already in place: the narrative can never alter score or tier, and a failed API call
silently falls back to the deterministic template so a run never fails on the LLM.

### Phase 5 — Automation + hardening (1–2 days)
```
1. Scheduler: APScheduler job calling agent.run(trigger_type="scheduled") every
   scheduler.poll_interval_seconds. Guard with a file/redis lock so two workers never
   process the same message.
2. Webhook: expose /api/webhook/mail via Zoho Flow (or a Graph subscription for Outlook).
   Treat the payload as a notification only — always re-fetch from the mailbox, never trust
   webhook-supplied data.
3. Persistence: move data/runs/*.json to Postgres (runs, players_scored, signals, tickets)
   once you need cross-run analytics like "players flagged 3 weeks running".
4. Alerting: if a run FAILS or the population size deviates >50% from the 4-week median,
   post to the AML Teams channel. A silently-empty watchlist is the dangerous failure mode.
5. Retention: pseudonymised player data, encrypted at rest, delete raw attachments after N days
   per your GwG retention policy.
```

### Phase 6 — Analyst feedback loop (ongoing)
```
Add a Disposition column the analyst fills in Jira (True positive / False positive / Escalated
to SAR). Weekly, join dispositions back to signals and compute precision per signal code.
That table is what justifies changing a score in scoring_config.yaml — never intuition.
```

---

## 6. Connector reference

| Slot | Option | Auth | Key endpoint |
|---|---|---|---|
| Source | **Zoho Mail** | OAuth2 refresh token | `GET mail.zoho.<dc>/api/accounts/{id}/messages/view` → `/attachmentinfo` → attachment GET |
| Source | Outlook / M365 | Client credentials | `GET /users/{mbx}/mailFolders/Inbox/messages?$filter=...` → `/attachments` |
| Source | Generic IMAP | user + app password | `IMAP4_SSL`, `SEARCH UNSEEN FROM` |
| Source | Manual upload | — | operator console |
| Sink | **Zoho WorkDrive** | OAuth2 | `POST zohoapis.<dc>/workdrive/api/v1/upload` (multipart `content`, `parent_id`) |
| Sink | SharePoint / OneDrive | Client credentials | `PUT /drives/{id}/root:/{path}:/content`, `createUploadSession` >4 MB |
| Sink | Teams channel | Client credentials | SharePoint upload + `POST /teams/{id}/channels/{id}/messages` |
| Sink | Outlook mail | Client credentials | `POST /users/{mbx}/sendMail` with base64 attachment |
| Ticket | Jira | Basic (email + token) | `POST /rest/api/3/issue`, dedupe via `GET /rest/api/3/search` on a label |
| Ticket | GitLab | `PRIVATE-TOKEN` | `POST /api/v4/projects/{id}/issues` |
| Ticket | Odoo | JSON-RPC login | `POST /jsonrpc` → `execute_kw(helpdesk.ticket, create)` |

All implemented in `src/connectors/`. Adding a new one = subclass the ABC, decorate with
`@register("sink", "my_system")`, add one entry to `/api/options`. Nothing else changes.

---

## 7. Operator console

`src/ui/index.html` — one file, no build step, served by FastAPI at `/`.

- **Dropdowns** for input source, destination, ticket system, narrative engine, and profile,
  populated from `/api/options` so the UI can never offer a connector the backend lacks.
- **Manual attach** with drag-and-drop, for reruns and for the day the mailbox breaks.
- **Signal chain**: the seven pipeline stages with live state and per-step timestamps.
- **Counters + stacked bar**: population, HIGH/MEDIUM/LOW, critical-signal count, review count.
- **Step log**, ticket table, run history — every run is replayable from `data/runs/*.json`.
- **Dry run** toggle: score everything, deliver nothing. Use it for every threshold change.

REST surface: `GET /api/options`, `POST /api/runs`, `GET /api/runs[/{id}]`,
`GET /api/runs/{id}/download`, `POST /api/webhook/mail`, `GET /api/health`.

---

## 8. Deployment

```
Docker: python:3.12-slim, pip install -r requirements.txt,
        CMD uvicorn src.api:app --host 0.0.0.0 --port 8000
Processes: 1 web (console + webhook) + 1 worker (scheduler). Same image, different CMD.
Secrets: env vars from your vault. config/*.yaml holds ${PLACEHOLDERS} only — never a secret.
Network: outbound to zoho.eu / graph.microsoft.com / atlassian.net / api.anthropic.com.
Data residency: if player data cannot leave the EU, use the Anthropic EU endpoint or run
explain_mode=template. The pipeline works fully without any LLM call.
```

---

## 9. Testing strategy

| Layer | What | How |
|---|---|---|
| Unit | family maths, cap, NULLIF, critical override, thresholds per source | `tests/test_scoring.py` — 10 golden cases |
| Property | score is monotonic in signals; always 0–100 | add `hypothesis` over random metric dicts |
| Golden file | last quarter's real batch → committed expected output | fail the build on any diff; a config change should show an *intended* diff |
| Contract | each connector against a recorded HTTP fixture | `responses` / `vcrpy` |
| E2E | dev profile, sample files, assert 157 players + workbook exists | already runs via the API |
| Adversarial | empty file, 200k rows, duplicate PlayerIDs, unknown alert code, all-zero deposits, non-UTF8 | must degrade, never crash — unknown codes already surface as `REVIEW CONFIG` |

---

## 10. Risk register

| Risk | Mitigation (status) |
|---|---|
| Silent scoring drift after a config edit | Golden-file test + `framework_version` stamped in every workbook ✅ |
| LLM hallucinates a figure into a case file | LLM sees only computed values; prompt forbids new numbers; template fallback ✅ |
| Duplicate tickets on rerun | `external_key` dedupe in every ticket connector ✅ |
| Watchlist arrives empty / malformed | Population-size guard + explicit parse errors ✅ / alerting ⬜ Phase 5 |
| Mailbox spoofing | Sender allow-list; webhook treated as notification only, data always re-fetched ✅ |
| Player PII in ticket bodies | Ship PlayerID + Shop only, never name/IBAN; keep the workbook behind the document store's ACL ✅ |
| Over-flagging swamps analysts | Dry-run before every threshold change + precision tracking from dispositions ⬜ Phase 6 |
| Framework changes with no audit trail | `scoring_config.yaml` in git; require a compliance approver on the CODEOWNERS path ⬜ |

---

## 11. Effort estimate

| Phase | Effort |
|---|---|
| 0 · local stand-up | 0.5 day |
| 1 · calibration on real data | 1 day |
| 2 · Zoho Mail intake | 1 day |
| 3 · WorkDrive + Jira | 1 day |
| 4 · LLM narratives | 0.5 day |
| 5 · scheduler, webhook, DB, alerting | 1–2 days |
| 6 · feedback loop | ongoing |
| **To production pilot** | **≈ 5–6 working days** |
