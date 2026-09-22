# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An SOP-harness insurance claims support agent (see `ASSIGNMENT.md` for the full spec, `ARCHITECTURE.md`
for the full design). The core idea: a deterministic phase state machine owns phase transitions and
disclosure rules in code; an LLM (not yet wired in — see "Current state" below) is meant to be a
stateless reasoning/language function the state machine calls with a restricted per-phase action set.
It never gets to decide "am I allowed to reveal this" or "should I advance phases" — code decides that.

Read `ARCHITECTURE.md` before making structural changes — it documents the full intended design (§1-18)
in more detail than is repeated here, including the rationale for each phase's disclosure rules and the
identity-verification matching algorithm.

## Commands

Python 3.12, Windows/PowerShell dev environment. venv + pip, no other tooling configured.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

# run the dev server (serves both the API and web/index.html)
.venv\Scripts\python -m uvicorn app.main:app --port 8000 --reload
```

Then open `http://127.0.0.1:8000/` for the chat test UI, or hit the API directly:

```powershell
curl -X POST http://127.0.0.1:8000/api/session
curl -X POST http://127.0.0.1:8000/api/session/{id}/message -H "Content-Type: application/json" -d '{"text": "..."}'
curl http://127.0.0.1:8000/api/session/{id}/state   # debug: dumps full SessionState
```

If port 8000 is already bound by a stale process (common after a background run that
lost its parent shell): `netstat -ano | grep ":8000" | grep LISTENING` then
`taskkill //PID <pid> //F`.

No lint/format/build tooling is configured yet (no ruff/mypy/pyproject.toml). There is a
`test/` directory but `test_margaret_chen_flow.py` is currently an empty scaffold — no test
runner is wired up yet; `pytest` is not in `requirements.txt`.

## Architecture

### Turn pipeline

Every inbound message goes through `app/main.py`'s `POST /api/session/{id}/message` →
`app/extraction.py`'s `extract()` → `app/sop/engine.py`'s `handle_turn()`, which dispatches
to whichever phase controller matches `session.phase`. Each phase controller lives in
`app/sop/<phase>.py` and has signature `handle(session, user_text, extracted) -> str`
(`verify_id.handle` only takes `(session, extracted)` — no raw text needed for that phase).
Adding a new phase means: add the enum value to `Phase` in `app/session.py`, write
`app/sop/<phase>.py`, and add one dispatch line in `app/sop/engine.py` — nothing else in the
pipeline needs to change.

### Phases (`app/session.py::Phase`)

`VERIFY_ID -> RESOLVE_INTENT -> PROCESS_CASE -> POST_PROCESS` (plus `HUMAN_HANDOFF`, not yet
wired to anything). Phase transitions are set by the controller itself
(`session.phase = Phase.X`) partway through `handle()` — the *next* incoming message is what
gets dispatched to the new phase, not the current one (a controller's return value is always
this turn's reply under the phase it started in).

- **`verify_id.py`** — strict. Runs identity matching from `app/identity.py::match_identity()`
  against `fixtures/policyholders.json` (≥3-of-5 PII fields: full name, DOB, phone, email,
  SSN/ID last-4 — matching handles each record's `*_aliases` lists). Has a separate
  representative-caller path (`match_representative()` against `fixtures/representatives.json`)
  for someone calling on another policyholder's behalf. This phase never reads
  `session.memory.intent_hint` to act on it — only writes to it — which is what makes "remembers
  the caller's hint but doesn't act on it until verified" a structural guarantee.
- **`resolve_intent.py`** — matches `session.memory.intent_hint` (or the current turn's text)
  against the verified party's claims via `app/tools/claims.py::filter_claims()`. Filtering only
  narrows the candidate list when a keyword match leaves it non-empty — a hint that doesn't match
  anything is ignored rather than zeroing out the candidates. Ambiguous matches trigger a
  clarifying question and stay in this phase.
- **`process_case.py`** — re-fetches the claim via `get_claim()` and re-checks
  `claim["party_id"] == session.identity.party_id` *every turn* (not just once), since this is the
  ownership boundary. Re-classifies intent every turn (`classify_intent()`, not the one frozen by
  `resolve_intent`), since a caller asks several different things across one case discussion.
  Routes to `app/tools/documents.py::get_document_guideline()` for document questions (a
  priority-ordered lookup: named document mentioned → `claim_followup_guidance[].match_any` →
  `intent_hints`/`requires_documents` fallback → global fallback — see that file's docstring) and
  `app/tools/status_sim.py::check_status()` for status questions.
- **`post_process.py`** — drafts a summary once (`session.post_process.summary`), then gates
  `send_email_summary()` behind an explicit parsed yes/no answer
  (`session.post_process.email_consent`). Nothing else may call the email tool.

### Session state (`app/session.py::SessionState`)

One mutable object per conversation (in-memory dict keyed by `session_id`, not persisted across
process restarts). Grouped into `identity` / `memory` / `intent` / `case` / `post_process`
sub-dataclasses. Extraction (`app/extraction.py`) always **merges** into this state, never
overwrites — this is what carries information volunteered early (e.g. an intent hint given
during `VERIFY_ID`) forward across phase boundaries.

Gotcha already hit twice during development: dataclass fields must use
`field(default_factory=list/dict)`, never a bare `= []`/`= {}` — the latter is either rejected
outright by `@dataclass` (mutable literal default) or, if it slips through as `None`, causes
`AttributeError` the first time a phase controller calls `.append()`/`.get()` on it.

### The "deterministic now, LLM-shaped later" boundary

`app/extraction.py` (`extract()`), and the various keyword classifiers (`classify_intent`,
`_is_done`, `_wants_different_case`, `_parse_yes_no`, `_find_mentioned_document`,
`_mentions_not_having_it`) are all regex/keyword **stubs** standing in for a future real LLM
call — this is intentional, not a shortcut to clean up. `app/llm_client.py`, `app/pipeline.py`,
`app/scope_guard.py`, `app/emotion.py`, and `app/guardrail.py` are placeholder files for that
next milestone (real Anthropic API extraction, out-of-scope/emotion detection, and the
post-generation disclosure guardrail — see `ARCHITECTURE.md` §8-12) and are currently empty.

When that swap happens, the rule is: replace only the "understand messy free text" functions
with structured LLM output — the deterministic policy code they feed into (identity matching,
ownership checks, phase-transition conditions, the email-consent gate, tool-layer lookups)
does not change and must stay code-enforced regardless of whether an LLM is involved.

### Fixtures (`fixtures/*.json`) — the mock claims DB

Loaded once into memory by `app/fixtures_data.py`. `policyholders.json` → identity verification;
`claims.json` → `RESOLVE_INTENT`/`PROCESS_CASE` (one caller can have multiple claims, including
same-case-type ambiguous pairs — e.g. party `P9` has two healthcare claims, one denied one
closed, used as the disambiguation test case from `ASSIGNMENT.md`); `representatives.json` →
authorized-caller verification; `required_document_guideline.json` → the document/follow-up
guidance router in `process_case.py`; `consent_scenarios.json` → `status_sim.py`'s mock async
status-polling simulator (`"default"` resolves pending→approved in 2 polls, `"timeout"` stays
pending for 5 — the latter is meant to exercise the "stop persuading, escalate to a human" bonus
behavior); `claim_schema.json` is documentation only, not loaded as runtime data by any tool.
