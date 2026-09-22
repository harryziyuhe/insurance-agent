# Architecture — Insurance Claims SOP Agent

## 1. What this system has to be

Per `ASSIGNMENT.md`, the agent is not a free-form chatbot and not a rigid IVR tree — it's both,
depending on which phase it's in:

- `VERIFY_ID` — **strict**. No claim disclosure, no phase advance, until ≥3 PII fields match.
  Must still tolerate messy natural conversation (partial answers, refusals, alternate fields,
  clarification, an authorized representative calling on someone else's behalf).
- `RESOLVE_INTENT` / `PROCESS_CASE` — **loose**. LLM interprets ambiguous language and picks a
  workflow path, but every fact it states about a claim must come from a tool call, never from
  the model's own invention.
- `POST_PROCESS` — **loose but gated on one explicit consent**: offer an email summary, only send
  it if the caller says yes.
- Cross-cutting, in every phase: remember anything useful the caller volunteers early (identity
  fields, intent hints), reject out-of-scope questions, detect emotional escalation and
  de-escalate without skipping gates, and know when to hand off to a human.

The one-line architectural answer: **the SOP is a deterministic state machine that owns phase
transitions and disclosure rules in code; the LLM is a stateless reasoning/language function that
the state machine calls with a restricted menu of allowed actions and grounding data.** The LLM
never decides "am I allowed to reveal this" or "should I advance phases" — code decides that,
before and after every LLM call.

## 2. Design principles

1. **Gate before generate, validate after generate.** The phase controller computes what's
   allowed *before* the LLM speaks (which tools it may call, what facts it may reference, whether
   it may advance). A guardrail validator checks the drafted reply *after* the LLM speaks, before
   it reaches the caller. Prompting alone is not a safety boundary — it's the first layer, the
   validator is the second, independent layer.
2. **One mutable session state, LLM stays stateless.** Everything the system currently knows
   about the call lives in one `SessionState` object. Every LLM call is given a fresh, fully
   serialized slice of that state — the model itself holds no memory between turns.
3. **Extraction is decoupled from phase.** A slot-extraction pass runs on *every* user turn,
   regardless of what phase the SOP is in. This is what makes the "remembered the January
   denied-healthcare hint while still in VERIFY_ID" requirement possible — extraction and phase
   control are separate concerns.
4. **Grounding via tools only.** `RESOLVE_INTENT`/`PROCESS_CASE` LLM calls get read-only tool
   functions backed by the `fixtures/` data. The system prompt forbids stating any claim-specific
   fact (amount, status, date, denial reason) that didn't come from a tool result in that turn's
   context.
5. **Escalation is a first-class exit, not an error path.** Both the scope guard and the
   persuasion/de-escalation logic can terminate the SOP early into a `HUMAN_HANDOFF` pseudo-phase.

## 3. Component diagram

```
┌───────────────┐     HTTP/WS      ┌──────────────────────────────────────────────────────┐
│  Test Chat UI │ ───────────────▶│                     API Layer                         │
│ (web, simple) │◀─────────────── │   POST /session  POST /session/{id}/message           │
└───────────────┘   reply + phase  └───────────────────────┬───────────────────────────────┘
                                                            │
                                                            ▼
                                        ┌───────────────────────────────────────┐
                                        │           Turn Orchestrator           │
                                        │  (per-message pipeline, see §5)       │
                                        └───────────────────┬───────────────────┘
                     ┌───────────────────────┬──────────────┼───────────────┬───────────────┐
                     ▼                       ▼              ▼               ▼               ▼
             ┌──────────────┐      ┌──────────────┐ ┌──────────────┐ ┌────────────┐ ┌───────────────┐
             │ Slot Extractor│      │ Scope Guard  │ │ Emotion Tagger│ │ SOP Engine │ │ Guardrail      │
             │ (always runs) │      │ (in/out scope│ │ (bonus)       │ │ (phase FSM)│ │ Validator      │
             └──────────────┘      │ + strike ctr)│ └──────────────┘ └─────┬──────┘ │ (post-gen check)│
                                    └──────────────┘                       │        └───────────────┘
                                                                            ▼
                                                                  ┌───────────────────┐
                                                                  │  Phase Controller  │
                                                                  │ (allowed actions,  │
                                                                  │  disclosure rules) │
                                                                  └─────────┬─────────┘
                                                     ┌────────────────────┬─┴──────────────────┐
                                                     ▼                    ▼                     ▼
                                           ┌──────────────────┐ ┌──────────────────┐  ┌──────────────────┐
                                           │ Identity Matcher  │ │   Tool Layer      │  │   LLM Client     │
                                           │ (policyholders +  │ │ (claims, docs,    │  │  (Anthropic API, │
                                           │  representatives) │ │  status sim)      │  │  token from env) │
                                           └──────────────────┘ └──────────────────┘  └──────────────────┘
                                                                        │
                                                                        ▼
                                                              ┌──────────────────┐
                                                              │ fixtures/*.json  │
                                                              │ (mock claims DB) │
                                                              └──────────────────┘
```

## 4. Session state model

One object per conversation, persisted server-side (in-memory dict keyed by `session_id` is
sufficient for the demo; swap for Redis if it needs to survive process restarts).

```
SessionState
├── session_id
├── phase: VERIFY_ID | RESOLVE_INTENT | PROCESS_CASE | POST_PROCESS | HUMAN_HANDOFF
├── turns: [{role, text, ts}]                 # raw transcript, used for POST_PROCESS summary
│
├── identity
│   ├── caller_role: self | representative | unknown
│   ├── claimed_fields: {full_name?, dob?, phone?, email?, ssn_last4?, policy_number?}
│   ├── matched_fields: [field names that matched a candidate record]
│   ├── candidate_party_id: str | null         # best-matching policyholder while still unverified
│   ├── verified: bool
│   ├── party_id: str | null                   # set only once verified
│   └── rep_name: str | null                   # set only if caller_role == representative
│
├── memory                                      # facts captured ahead of the phase that needs them
│   ├── intent_hint: str | null                 # e.g. "denied healthcare claim from January"
│   ├── case_hint_type: str | null              # case_type guess: healthcare/auto/dental/...
│   ├── case_hint_period: str | null            # date/period guess: "January", "2026-01"
│   └── other_notes: [str]
│
├── intent
│   ├── resolved_intent: one of {status_inquiry, denial_question, document_submission,
│   │                            next_steps, general_claim_question} | null
│   └── case_id: str | null                     # resolved after matching memory + party_id claims
│
├── scope_guard
│   ├── consecutive_off_topic: int
│   └── redirected_topics: [str]
│
├── emotion
│   ├── current: neutral | frustrated | anxious | angry | confused
│   ├── escalation_offers_made: int             # times we've offered human transfer
│   └── de_escalation_attempts: int
│
├── post_process
│   ├── summary_drafted: str | null
│   ├── email_consent: yes | no | unasked
│   └── email_sent: bool
│
└── handoff_reason: str | null
```

## 5. Turn processing pipeline

Every inbound user message runs this fixed pipeline. Steps 1–4 run unconditionally and are what
give the system "memory across phase boundaries" and consistent scope/emotion handling no matter
which phase is active.

1. **Slot Extraction (LLM call #1, structured output).** Given the raw utterance + current
   `SessionState`, extract into a fixed JSON schema: any PII fields present, any intent/case hint
   language, an in-scope/out-of-scope classification, an emotion label, and a yes/no consent
   signal if one is present. This call has no persona and no phase awareness — it's a pure NLU
   extraction step, same shape every time. Extracted values are merged into `SessionState`
   (`identity.claimed_fields`, `memory.*`, `scope_guard`, `emotion`) — merge, never overwrite,
   so information volunteered early is not lost when the phase controller gets to it later.
2. **Scope Guard.** If this turn was tagged out-of-scope: increment `consecutive_off_topic`.
   If under threshold (e.g. 2), respond with a polite scoped redirect and **do not** invoke the
   phase controller. If at/over threshold, offer human transfer; if declined, reset counter and
   continue. This runs before the phase controller so an off-topic tangent never leaks into
   `RESOLVE_INTENT`/`PROCESS_CASE` reasoning.
3. **Emotion check.** If tagged as frustrated/angry/anxious/confused, mark that the next generated
   reply must lead with acknowledgment/empathy (a response-composition instruction, not a phase
   change). Tracks `de_escalation_attempts` so persuasion has a bounded number of tries before
   `HUMAN_HANDOFF` (see §10).
4. **Phase Controller dispatch.** The current phase's controller (§6) is invoked with: the
   sanitized user turn, the full `SessionState`, and the list of tools it's allowed to call. The
   controller decides what the *code-level* allowed action set is (e.g. "may not include any
   claims data", "may call `get_claim` but only for `session.identity.party_id`'s own claims").
5. **Response Generation (LLM call #2).** The LLM gets: phase-specific system prompt, the allowed
   tool list, relevant `SessionState` fields (never the whole raw transcript, to keep prompts
   small and to keep the model from re-deriving things it shouldn't reason about), and
   composition instructions from steps 2–3 (redirect / empathy-first / offer-human). It may call
   tools (see §11) before producing its final natural-language reply.
6. **Guardrail Validator (deterministic, code, not LLM).** Before the reply is returned:
   - If `!verified`, scan the draft reply plus every tool result used this turn for claim-specific
     tokens (case IDs, dollar amounts, denial reasons, statuses) pulled from `fixtures/claims.json`
     — reject/regenerate if found. This is the hard backstop for "must not disclose claim details
     before verification," independent of whatever the prompt said.
   - If phase is about to advance, re-check the *code-level* transition condition (§6) rather than
     trusting the LLM's claim that it's satisfied.
   - In `POST_PROCESS`, refuse to call the (mock) email tool unless `email_consent == yes`.
7. **Persist + respond.** Update `SessionState`, append to `turns`, return `{reply, phase}` (phase
   exposed to the UI mainly for debugging/demo visibility).

## 6. Phase specifications

### 6.1 VERIFY_ID — strict

- **Entry:** start of call.
- **Allowed LLM actions:** ask for / clarify identity fields, acknowledge partial info, handle
  refusal gracefully, explain *why* verification is required, offer the field menu, offer human
  transfer. No tool access to claims data at all (not merely "instructed not to" — the tool isn't
  even in the allowed set for this phase, so it can't be called).
- **Disclosure rule:** may not reference any claim content, even hypothetically, even if the
  caller already stated it themselves ("your claim from January" must not be echoed back with
  specifics until verified — restating the caller's own words back is fine, e.g. "I hear that
  you're calling about a claim from January" is fine since it adds no new info, but confirming
  denial/amount/status is not).
- **Exit condition (code-checked, not LLM-checked):** `identity.matched_fields.length >= 3` against
  a single consistent candidate record (§7), OR representative path satisfied (§7.2). On success:
  set `identity.verified = true`, `identity.party_id`, advance to `RESOLVE_INTENT`.
- **Memory passthrough:** any `memory.intent_hint`/`case_hint_*` captured during this phase is
  carried forward untouched — this phase never acts on it, only stores it.

### 6.2 RESOLVE_INTENT — loose

- **Entry:** `identity.verified == true`.
- **First move:** if `memory.intent_hint` is already populated (the January-denied-healthcare
  case), the controller passes it to the LLM as *prior context to confirm*, not to re-elicit —
  system prompt: "confirm this hint against the caller's actual claims instead of asking from
  scratch." If empty, LLM asks a natural open question.
- **Allowed tool:** `list_claims(party_id)` → returns the caller's claims from
  `fixtures/claims.json`. LLM (or a deterministic matcher first, LLM as fallback for ambiguity)
  matches `memory.case_hint_type`/`case_hint_period`/free text against the returned claims to pick
  `case_id`. If more than one claim plausibly matches, LLM disambiguates by asking a targeted
  clarifying question (e.g. "I see two healthcare claims — the one from January 2026 that was
  denied, or the one closed last year?").
- **Exit condition:** `resolved_intent` set AND `case_id` set (or explicitly "general question,
  no specific case" for account-level questions) → advance to `PROCESS_CASE`.

### 6.3 PROCESS_CASE — loose, grounded

- **Entry:** intent + case resolved.
- **Allowed tools:** `get_claim(case_id)`, `get_document_guideline(doc_name | case_type)`,
  `check_status(case_id)` (mock async simulator, §11.3). All read-only, scoped to the verified
  `party_id`'s own claims — a tool-layer check (not a prompt instruction) rejects any `case_id`
  that doesn't belong to `identity.party_id`.
- **Behavior:** LLM answers naturally (explains denial reason, lists missing documents with
  submission/format/alternative guidance from `required_document_guideline.json`, gives appeal
  deadlines, discusses status) but every specific fact must trace to a tool result from this turn.
  System prompt explicitly bans stating a number/date/status not present in the tool output.
- **Exit condition:** caller signals the case discussion is done (explicit or inferred from
  "no more questions" type turns) → advance to `POST_PROCESS`. Caller can also loop back into
  `RESOLVE_INTENT` mid-phase if they raise a second, different claim.

### 6.4 POST_PROCESS — loose, single consent gate

- **Entry:** case discussion concluded.
- **Allowed LLM actions:** draft a summary (what was discussed, resolved intent, claim
  status/outcome, follow-up items/next steps) built from `SessionState.turns` +
  `intent`/`identity`; **offer** to email it; wait for explicit yes/no.
- **Tool:** `send_email_summary(party_id, summary)` — mocked (logs/prints in the demo, since
  there's no real mail fixture) — gated by `post_process.email_consent == yes`, enforced by the
  guardrail validator, not by prompt.
- **Exit:** either branch (sent or skipped) ends the SOP-controlled conversation; a final closing
  message is generated and the session is marked complete.

## 7. Identity verification engine (§6.1 detail)

Backed by `fixtures/policyholders.json`.

- **Field set considered for the ≥3 count:** full name, DOB, phone, email, SSN/national-ID last 4.
  Policy number is accepted as a strong *hint* to narrow the candidate record but does not count
  toward the 3, since it's not in the assignment's required PII list.
- **Matching, not string equality:** name matches against `name` and `name_aliases`
  (case/whitespace-insensitive, simple fuzzy distance to tolerate "Yaven Li" vs "Ya Wen Li");
  phone/email match against the base value and `*_aliases`; DOB matches on normalized date; last-4
  matches regardless of whether the record's `id_type` is `ssn_last4` or `national_id_last4` (the
  agent should accept either as "last 4 digits of your ID" without forcing the caller to know
  which type their policy uses).
- **Candidate narrowing:** as fields arrive across turns, the matcher filters
  `policyholders.json` to whichever records are still consistent with everything claimed so far.
  If a claimed field matches more than one record, that field doesn't count until a later field
  disambiguates — this prevents a caller from hitting 3 "matches" against three different people.
  Once exactly one candidate remains and it has ≥3 independently-matched fields, verification
  passes.
- **Refusal/alternate-field handling:** if the caller refuses a requested field ("I don't want to
  give my SSN"), the controller offers the next acceptable field from the set rather than
  insisting on one specific field — this is what "alternate identity fields" in the assignment
  means in practice.

### 7.1 Representative path

Backed by `fixtures/representatives.json`.

- Triggered when extraction flags `caller_role = representative` (e.g. "I'm calling on behalf of
  my mother," "this is her son David").
- Verification requires **two things**, not a relaxed version of one:
  1. The rep's stated name + relationship matches a row in `representatives.json` for the
     `buyer_party_id` in play.
  2. The same ≥3-of-5 PII match from §7 is still required, but the fields being matched are the
     **policyholder's** (Margaret Chen's), not the rep's — a rep who only knows their own DOB
     doesn't satisfy anything. The rep is expected to be able to supply the account holder's
     identity details, which is a reasonable real-world assumption for an authorized representative.
- If the claimed relationship doesn't match `representatives.json` at all, treat as `self` path
  and fall back to standard verification (don't assume malice, just don't grant the rep shortcut).

## 8. Memory & slot extraction subsystem

This is the mechanism behind "remembers useful info from any phase." It's not phase-specific
code — it's step 1 of the pipeline (§5), always on. Two things make it correct rather than just
convenient:

- **Merge semantics, never overwrite:** `memory.intent_hint` and `identity.claimed_fields` accumulate
  across turns; a later turn can add fields but a later *empty* extraction never clears earlier
  ones.
- **Write-only from VERIFY_ID's perspective:** the VERIFY_ID controller is architecturally
  incapable of reading `memory.intent_hint` to act on it (§6.1) — it can only ever populate it,
  which is what makes "stays in VERIFY_ID despite knowing the intent already" a structural
  guarantee rather than a prompting hope.

## 9. Scope guard & escalation

- Extraction step tags each turn in-scope/out-of-scope against a short definition of the domain
  (insurance claims: identity, policy, claims, documents, status, coverage questions the fixtures
  can ground — general knowledge questions like "what is RL?" are out).
- Ambiguous/borderline cases (e.g. "what's the weather" vs. "does weather damage count as
  auto damage") should lean in-scope-adjacent and let the phase controller/tooling fail
  gracefully rather than the guard over-triggering.
- `consecutive_off_topic` resets to 0 on any in-scope turn. At threshold (configurable, default
  2 consecutive), the agent politely offers a human representative instead of repeating the same
  redirect a third time.

## 10. Emotion detection & de-escalation (bonus)

- Emotion label from extraction (§5 step 1) drives a **response-composition modifier**, not a
  phase change: the phase controller's allowed actions are unaffected by emotion, only the tone
  and ordering of the generated reply are (acknowledge → explain the *why* behind the gate →
  restate allowed options → move the SOP forward).
- For the canonical bonus example ("I already told you who I am... just tell me why my claim was
  denied"): extraction tags frustration + an implicit disclosure request; scope guard is
  irrelevant (it's in-scope); phase is still `VERIFY_ID` since the gate isn't met — the response
  composer is instructed to empathize, explain the protection rationale, offer the remaining
  acceptable ID fields, and explicitly is *not* given the claims tool, so it structurally cannot
  disclose even if persuaded.
- **Bounded persuasion:** `de_escalation_attempts` / `escalation_offers_made` counters cap how many
  times the agent will hold the line before proactively leading with the human-transfer offer
  instead of asking for ID again — avoids an infinite "please verify" loop against a caller who's
  already refused twice.

## 11. Tool / data layer

Each fixture file backs exactly one tool surface, loaded once at startup into in-memory lookup
structures (dict by `party_id` / `case_id`). No external DB for the demo.

| Tool | Backing fixture | Used in phase | Notes |
|---|---|---|---|
| `match_identity(claimed_fields)` | `policyholders.json` | VERIFY_ID | Returns candidate + matched field list (§7); not exposed to LLM as a callable tool — invoked by the phase controller directly, since verification math must be deterministic. |
| `match_representative(name, relationship, buyer_party_id)` | `representatives.json` | VERIFY_ID | Same — deterministic, controller-invoked. |
| `list_claims(party_id)` | `claims.json` | RESOLVE_INTENT | Filtered to the verified `party_id` only. |
| `get_claim(case_id)` | `claims.json` | PROCESS_CASE | Tool layer rejects any `case_id` not owned by `session.identity.party_id`. |
| `get_document_guideline(case_type?, document_name?)` | `required_document_guideline.json` | PROCESS_CASE | Template-fills `{case_id}`, `{documents}`, `{average_processing_time_after_submission}` placeholders server-side before the LLM sees them, so the model relays rather than free-generates policy text. `claim_followup_guidance[].match_any` is used as a first-pass deterministic router to the right guidance topic; LLM only phrases the retrieved text naturally and picks `claim_followup_fallback` if nothing matches. |
| `check_status(case_id)` | `consent_scenarios.json` | PROCESS_CASE | Mock async status simulator: each call advances a per-session pointer through the named `status_sequence` (`default`: pending→approved in 2 polls; `timeout`: stays pending for 5 polls). Used to exercise "still pending, checking again" conversational turns and, in the `timeout` scenario, to exercise the escalate-to-human path when a caller polls repeatedly with no resolution. Scenario selection defaults to `default`; `timeout` can be forced via a query param for demo/testing. |
| `send_email_summary(party_id, summary)` | none (mocked) | POST_PROCESS | Logs the composed summary server-side (no real SMTP for the demo); gated by explicit consent (§6.4). |

`claim_schema.json` isn't a runtime tool — it's documentation the prompt-builder reads once at
startup to phrase monetary fields correctly (e.g., knowing `net_pay` is "finalized amount paid"
vs. `expected_reimbursement_amount`), not something looked up per-turn.

## 12. Guardrail validator (defense in depth)

Runs after every LLM response, before it reaches the caller:

1. **Pre-verification disclosure check:** if `!identity.verified`, reject any draft containing
   tokens sourced from a claims tool result (case IDs, amounts, denial reasons, statuses,
   document lists) — pure string/field matching against this turn's tool outputs, no LLM
   judgment involved.
2. **Phase transition check:** re-verifies the code-level condition for whatever phase the
   controller says it's about to enter, rather than trusting the model's own "I've verified them"
   claim.
3. **Consent check:** blocks the `send_email_summary` tool call unless
   `post_process.email_consent == yes`, decided at that exact turn (not carried over from a
   different session).
4. **Scope leakage check:** if the current phase is `VERIFY_ID`, reject a draft that answers a
   general knowledge question even if scope guard mis-tagged it in-scope — belt-and-suspenders,
   since disclosure is the higher-stakes failure mode.

On rejection: regenerate once with an explicit correction note appended to the prompt ("you
referenced X, which is not allowed in this phase — do not include claim specifics"); if it fails
twice, fall back to a canned safe response for that phase.

## 13. API design

Minimal HTTP surface, session-based (a WebSocket would also work, but the assignment only calls
for a "simple test UI," so plain request/response keeps the demo easy to run and inspect):

```
POST /api/session                       → { session_id }
POST /api/session/{id}/message
  body:  { text: string }
  reply: { reply: string,
           phase: string,               # for demo visibility, not sent as an instruction
           verified: bool,
           awaiting_email_consent: bool,
           handoff: bool }
GET  /api/session/{id}/state            # debug endpoint: dumps full SessionState (demo/dev only)
```

Auth token for the model provider is read from an environment variable
(`ANTHROPIC_API_KEY`) at process start — never accepted from the client, never logged.

## 14. Test UI

A single static page (plain HTML/JS, no build step) served by the same backend:
chat log + input box, plus a small side panel showing `phase`, `verified`, and matched identity
fields — this is purely for demo transparency (grading/demoing the SOP behavior), not something
a real caller would see. Fulfills "simple test UI where a test user can text with the agent in
natural language."

## 15. Tech stack & repo layout

Python/FastAPI is the natural fit here (fast to stand up, good fixture/JSON ergonomics, easy
Docker story); the design in §2–§13 is otherwise framework-agnostic.

```
insurance-agent/
├── ASSIGNMENT.md
├── ARCHITECTURE.md
├── fixtures/                     # given test data (unmodified)
├── app/
│   ├── main.py                   # FastAPI app, API routes (§13)
│   ├── session.py                # SessionState model + in-memory store
│   ├── pipeline.py               # turn orchestrator (§5)
│   ├── extraction.py             # slot extraction LLM call + merge logic (§8)
│   ├── scope_guard.py            # §9
│   ├── emotion.py                # §10
│   ├── sop/
│   │   ├── engine.py             # phase FSM, transition table
│   │   ├── verify_id.py          # §6.1 controller + §7 identity matcher
│   │   ├── resolve_intent.py     # §6.2 controller
│   │   ├── process_case.py       # §6.3 controller
│   │   └── post_process.py       # §6.4 controller
│   ├── tools/
│   │   ├── claims.py             # list_claims/get_claim (§11)
│   │   ├── documents.py          # get_document_guideline (§11)
│   │   ├── status_sim.py         # check_status mock (§11)
│   │   └── email.py              # send_email_summary mock (§11)
│   ├── guardrail.py              # §12 post-generation validator
│   └── llm_client.py             # Anthropic API wrapper, prompt templates per phase
├── web/
│   └── index.html                # test UI (§14)
├── tests/
│   └── test_margaret_chen_flow.py  # the assignment's sample scenario, end-to-end
├── Dockerfile
├── docker-compose.yml
└── README.md                     # setup + how to supply the API token
```

## 16. Deployment

- `Dockerfile` builds the FastAPI app + serves `web/index.html`; single container, single port.
- API token supplied via env var at container run time (`-e ANTHROPIC_API_KEY=...` or a mounted
  `.env` file consumed by `docker-compose.yml`) — never baked into the image.
- No external services required (fixtures are the only "database"), so the whole demo is one
  container.

## 17. Testing strategy

The assignment's sample scenario is the primary acceptance test and should be encoded literally
as an automated test (`tests/test_margaret_chen_flow.py`):

1. Single utterance: *"I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm
   calling about my denied healthcare claim from January. DOB is 1985-03-15, SSN last four is
   4472."*
2. Assert after turn 1: `phase == VERIFY_ID` is bypassed correctly to `RESOLVE_INTENT` (name + DOB
   + SSN last4 = 3 matched fields against `P9`), `identity.verified == true`,
   `identity.party_id == "P9"`, and — critically — `memory.intent_hint` contains the
   denied-healthcare-January hint, all from one turn.
3. Assert the very next agent turn does **not** re-ask "what are you calling about" and instead
   proceeds to confirm/resolve against `CL-2048` (the denied healthcare claim from `2026-01-12`
   for `P9`), not `CL-2011` (closed healthcare claim from `2025-01-28` for the same party — a
   useful disambiguation trap already present in the fixture data).
4. In `PROCESS_CASE`, assert every dollar amount / denial reason / document name in the reply
   traces back to `CL-2048`'s fixture record.
5. Additional targeted tests: partial-ID refusal-then-alternate-field flow; representative path
   (David Chen calling for Margaret Chen, `P9`); out-of-scope question with escalation after
   repeated retries; the bonus frustration scenario (§10) asserting no disclosure occurs pre-
   verification even under sustained pressure; POST_PROCESS with consent = no (no email tool
   call) and consent = yes (email tool called once with a summary referencing the actual
   resolved case).

## 18. Notable ambiguities / assumptions (flagged, not hidden)

- `consent_scenarios.json`'s `status_sequence` naming reads like it could be about email/consent
  flow, but its shape (arrays of `pending`/`approved`) is functionally a claim-status polling
  simulator; I've modeled it as `check_status` in PROCESS_CASE (§11) rather than in POST_PROCESS.
  This should be confirmed/renamed if the original intent was different.
- Representative verification (§7.1) assumes the rep can supply the *policyholder's* PII, per a
  literal reading of "identity verified based on at least 3 PII info" — an alternative reading
  (verify the rep's own identity, then separately confirm relationship) is also defensible and
  would just change which fields the matcher pulls from `claimed_fields`.
