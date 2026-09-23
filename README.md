# Insurance Claims SOP Agent

SOP-harness insurance claims support agent: a deterministic phase state machine in plain Python
owns identity verification, disclosure rules, and phase transitions; an LLM is used only to turn
messy input into structured data and to phrase already-decided facts as natural language. See
`ARCHITECTURE.md` for the full design.

## Project layout

```
app/
  main.py             FastAPI app: /api/session, /api/session/{id}/message, /api/session/{id}/state
  session.py          SessionState + Phase enum
  pipeline.py         turn orchestrator; understand()/respond() — the only LLM call sites
  extraction.py       ExtractedTurn shape + regex_fallback_extract() (used when no LLM key is set)
  identity.py         deterministic >=3-of-5 PII verification
  guardrail.py        deterministic post-generation disclosure check
  emotion.py          emotion -> tone instruction mapping
  llm_client.py       Groq -> Gemini fallback chain; the only file that imports a vendor SDK
  fixtures_data.py    loads fixtures/*.json once at import time
  sop/
    engine.py         phase dispatch table
    verify_id.py
    resolve_intent.py
    process_case.py
    post_process.py
  tools/
    claims.py
    documents.py
    status_sim.py
    email.py
fixtures/             mock claims DB (see "Fixtures" below)
web/
  index.html          single-file test UI, no build step, no external dependencies
.env / .env.example   API key configuration
requirements.txt
pyproject.toml        ruff config (line-length 88)
```

## Setup

Requires Python 3.12 and a free LLM API key.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
$EDITOR .env   # paste your key(s) in

.venv/bin/python -m uvicorn app.main:app --port 8000 --reload
```

Open `http://127.0.0.1:8000/` for the test UI, or drive the API directly:

```bash
curl -X POST http://127.0.0.1:8000/api/session
curl -X POST http://127.0.0.1:8000/api/session/{id}/message -H "Content-Type: application/json" -d '{"text": "..."}'
curl http://127.0.0.1:8000/api/session/{id}/state
```

If port 8000 is already bound: `lsof -i :8000` then `kill <pid>`.

`.env` — copied from `.env.example`, gitignored, loaded automatically via `python-dotenv`:

| Variable | Required? | Purpose |
|---|---|---|
| `GROQ_API_KEY` | Yes (or `GEMINI_API_KEY`) | Default LLM provider. Free key: console.groq.com/keys |
| `GEMINI_API_KEY` | No | Secondary provider, tried only if Groq fails. Free key: aistudio.google.com/apikey |

If neither key is set, every LLM call falls back to deterministic regex/template logic — the
state machine, identity verification, and guardrail keep working; only phrasing fluency degrades.

Lint/format (ruff, configured in `pyproject.toml`):

```bash
.venv/bin/python -m ruff format app
.venv/bin/python -m ruff check app
```

## Fixtures

`app/fixtures_data.py` loads each of these once at import time. All are JSON; dates are
`YYYY-MM-DD` strings, monetary amounts are decimal strings.

### `policyholders.json` — list of records

```json
{
  "party_id": "P9",
  "name": "Margaret Chen",
  "policy_number": "POL-9921",
  "dob": "1985-03-15",
  "id_type": "ssn_last4",
  "id_last4": "4472",
  "phone": "+16505212836",
  "email": "margaret@email.com",
  "name_aliases": ["optional", "alternate spellings"],
  "phone_aliases": ["optional alternate phone strings"],
  "email_aliases": ["optional alternate emails"]
}
```

Required: `party_id`, `name`, `policy_number`, `dob`, `id_type` (`"ssn_last4"` or
`"national_id_last4"`), `id_last4`, `phone`, `email`. The `*_aliases` lists are optional — used by
`identity.py`'s matcher to accept variant spellings/formats for the same person.

### `representatives.json` — list of records

```json
{ "rep_name": "David Chen", "relationship": "son", "buyer_name": "Margaret Chen", "buyer_party_id": "P9" }
```

All four fields required. `buyer_party_id` must match a `party_id` in `policyholders.json`.

### `claims.json` — list of records

```json
{
  "case_id": "CL-2048",
  "party_id": "P9",
  "case_type": "healthcare",
  "created_at": "2026-01-12",
  "status": "denied",
  "summary": "one-line human summary",
  "denial_reason": "required only if status is denied",
  "documents_needed": ["pathology report", "office note"],
  "appeal_deadline": "2026-03-18",
  "expected_reimbursement_amount": "0.00",
  "allowed_max_amount": "1450.00",
  "net_pay": "0.00",
  "net_fee": "1450.00"
}
```

Required: `case_id`, `party_id` (must match a `policyholders.json` record), `case_type`
(`"healthcare"`/`"dental"`/`"auto"` — anything else won't be matched by `filter_claims()`'s
keyword narrowing), `created_at`, `status` (`"denied"`/`"closed"`/`"open"`/`"pending"`/`"approved"`
— also matched by keyword narrowing). `summary` is used as a fallback fact when nothing more
specific applies. `denial_reason`/`documents_needed`/`appeal_deadline` only need to be present
when `status` is `"denied"`. The four monetary fields are optional; when present and `status` is
`"closed"` or `"approved"`, `net_pay` is surfaced to the caller as the finalized amount paid — the
other three are never shown directly, only used for internal documentation
(`claim_schema.json`).

### `consent_scenarios.json` — dict of named scenarios

```json
{
  "default": { "status_sequence": ["pending", "approved"] },
  "timeout": { "status_sequence": ["pending", "pending", "pending", "pending", "pending"] }
}
```

Each key is a scenario name (`status_sim.py` uses `"default"` unless told otherwise); each value
is a list of status strings stepped through one-per-poll, clamping on the last entry once
exhausted. At least one entry named `"default"` is required.

### `required_document_guideline.json` — dict, all keys required

```json
{
  "default_guidance": { "en": "..." },
  "case_type_guidance": { "<case_type>": { "en": "..." } },
  "document_guidance": { "<document name>": { "en": "..." } },
  "document_alternative_guidance": { "default": { "en": "..." }, "<document name>": { "en": "..." } },
  "claim_followup_settings": {
    "average_processing_time_after_submission": { "en": "..." },
    "human_review_after_document_alternatives_exhausted": { "en": "..." }
  },
  "claim_followup_guidance": [
    {
      "topic": "short identifier",
      "match_any": ["optional list of literal phrases to match against the caller's text"],
      "intent_hints": ["document_submission", "next_steps", "denial_question", "general_claim_question", "status_inquiry"],
      "requires_documents": true,
      "en": "guidance text, may reference {case_id}, {documents}, {average_processing_time_after_submission}"
    }
  ],
  "claim_followup_fallback": { "en": "..." }
}
```

`claim_followup_guidance` is a priority-ordered list — entries are tried top to bottom, first
matching `match_any` wins, then first matching `intent_hints`+`requires_documents`. Every `"en"`
string is passed through Python `str.format()` with `case_id`/`documents`/
`average_processing_time_after_submission`, so any of those placeholders used must be spelled
exactly that way.

Note: `app/tools/documents.py::_DOC_CORE_TERMS` maps short document names (e.g. `"pathology
report"`) to the longer fixture keys (e.g. `"original pathology report"`) — a document referenced
in `claims.json`'s `documents_needed` or matched from caller text needs an entry in that Python
dict to resolve to a `document_guidance`/`document_alternative_guidance` key, in addition to
existing in the fixture itself.

### `claim_schema.json` — documentation only

Free-form; loaded into memory but not read by any tool at runtime. Exists purely to document the
meaning of the four monetary fields on a claim record for future maintainers.
