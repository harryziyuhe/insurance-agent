# IMPLEMENTATION.md

Implementation guidance for wiring the LLM into the SOP harness per the architecture in
`CLAUDE.md`. This is a plan for a human (or Claude Code) to write the actual diffs — no code
changes are made by this document.

Two LLM call sites (`understand()`, `respond()`), one deterministic post-check
(`guardrail.py`), both LLM calls backed by Gemini free tier with a regex/template fallback so
the app never hard-fails when the LLM is unavailable or rate-limited.

---

## 1. `app/llm_client.py`

**Purpose:** the only file that talks to Gemini. No SOP, session, or phase knowledge.

```python
class LLMUnavailable(Exception): ...

def call_llm(system: str, user: str, schema: dict, *, temperature: float = 0.0) -> dict:
    ...
```

- Build the client once at module load: `genai.Client(api_key=os.environ["GEMINI_API_KEY"])`.
  If the env var is unset, don't crash at import time — raise `LLMUnavailable` lazily on first
  call, so the app can still boot and run in fully-offline/fallback mode for local dev.
- Use `model="gemini-2.5-flash"` (or `gemini-2.0-flash` if 2.5 isn't free-tier-eligible on your
  key — check quota) with `config={"response_mime_type": "application/json",
  "response_schema": schema}` so Gemini returns validated JSON, not text you regex out.
- Wrap the call in a narrow try/except: catch the SDK's rate-limit/quota/network exceptions
  specifically (don't catch-all) and re-raise as `LLMUnavailable(str(e))`. Let programming
  errors (bad schema, bad prompt) surface normally — silently swallowing those would hide bugs
  behind the fallback path and make them invisible in dev.
- Set a short timeout (e.g. 10s) — this is a synchronous request handler
  (`POST /message`), so a hung LLM call shouldn't hang the whole session.
- `temperature=0.0` default: both `understand()` (extraction) and `respond()` (constrained by
  explicit facts) want low variance; don't add creativity knobs you don't need yet.
- No retry loop here — one attempt, fail fast to `LLMUnavailable`, let the caller's fallback
  handle it. A retry-with-backoff inside a synchronous chat-turn handler just makes the user
  wait longer for the same free-tier rate limit to reject twice.

**What NOT to put here:** any reference to `SessionState`, `Phase`, or claim data. If you find
yourself importing from `app.session` into this file, that logic belongs in `pipeline.py`.

---

## 2. `app/pipeline.py`

**Purpose:** the only file that knows both `understand()` and `respond()` exist. Defines the
two LLM-backed functions and the scope/emotion bookkeeping that rides on `understand()`'s
output. Orchestrates the fallback-on-failure behavior.

### `understand(session: SessionState, user_text: str) -> ExtractedTurn`

- Build the schema to match the (extended) `ExtractedTurn` dataclass — see §3 below for the
  new fields. Include `session.phase.value` in the prompt as context (not as a hard filter —
  the model can still return an identity field volunteered early during `RESOLVE_INTENT`, same
  as today's regex does for the intent hint volunteered during `VERIFY_ID`) so the model knows
  what's *likely* relevant this turn without being restricted to only that phase's fields.
- System prompt should state plainly: "Extract only what the caller actually said. Leave a
  field null if it wasn't stated. Do not infer or guess PII values." — this matters because a
  hallucinated `dob` or `ssn_last4` would corrupt `match_identity()`'s input in `verify_id.py`,
  which has no way to tell a hallucinated field from a real one.
- On `LLMUnavailable`: fall back to calling the existing regex functions (see §3) and assemble
  the same `ExtractedTurn` shape from their outputs. Log a warning (not silent) so fallback
  usage is visible in dev — you want to know if you're burning through free-tier quota faster
  than expected.
- Return type is unchanged (`ExtractedTurn`) so `engine.handle_turn(session, user_text,
  extracted)` and every phase controller's signature stay untouched.

### `respond(facts_to_convey: list[str], next_action: str, tone_hint: str = "neutral") -> str`

- This is called **once, at the end** of a phase controller's `handle()`, replacing that
  controller's hardcoded reply strings (`_verified_reply`, `_denial_reply`, `_resolved_reply`,
  etc.).
- System prompt: agent persona + a hard constraint — "Only state facts from the FACTS list
  below. Do not add, infer, or speculate about claim details not listed. If FACTS is empty,
  just deliver NEXT_ACTION." Feed `tone_hint` in as a style instruction ("the caller sounds
  frustrated — acknowledge that briefly before continuing") not as permission to change what's
  disclosed.
- Schema: `{"reply": str}` — one field, free text.
- On `LLMUnavailable`: fall back to a simple deterministic join — `". ".join(facts_to_convey +
  [next_action])`. It'll read stiffly but it's correct and safe, which is what the fallback
  path is for.
- **Important:** `respond()` takes no `session` argument and no `claim` dict — only the
  strings the caller assembled. This is what makes the "LLM can't leak what it wasn't given"
  guarantee actually true; don't add a `session` passthrough "just in case" later, it defeats
  the point.

### Off-topic / escalation bookkeeping (replaces `scope_guard.py`'s original LLM-call plan)

- After `understand()` returns, `pipeline.py`'s turn-runner does:
  ```python
  if not extracted.in_scope:
      session.off_topic_streak += 1
  else:
      session.off_topic_streak = 0
  ```
- Add `off_topic_streak: int = 0` to `SessionState` (see §3.1). Pick a threshold (e.g. 2
  consecutive off-topic turns) as a module-level constant in `pipeline.py`, not inside the LLM
  prompt — the escalation trigger must be code-enforced and testable without depending on the
  model's judgement of "how many times is too many."
- When the streak crosses the threshold, the turn-runner short-circuits *before* calling the
  phase controller: set `session.phase = Phase.HUMAN_HANDOFF` and return a fixed handoff
  message (not LLM-generated — you want this message to be 100% reliable). Reset the streak on
  transition.
- In-scope questions never touch this counter, including ones the phase controller can't
  currently answer — "out of scope for the SOP" only means "not about this insurance claim,"
  e.g. "what is RL?" — not "a claim question the current phase doesn't handle."

### Tone bookkeeping (replaces `emotion.py`'s original LLM-call plan)

- No separate call. `extracted.emotion` from `understand()`'s output is passed straight into
  `respond(..., tone_hint=extracted.emotion)` by whichever phase controller is replying.
  `pipeline.py` doesn't need to store emotion on `session` unless you want it visible in the
  `/state` debug endpoint (optional, harmless to add to `SessionState` if you want it there for
  demo purposes).

### Turn-runner entry point

Give `pipeline.py` one function that `main.py` calls instead of calling `extract()` +
`handle_turn()` directly:

```python
def run_turn(session: SessionState, user_text: str) -> str:
    extracted = understand(session, user_text)
    if _off_topic_escalation_triggered(session, extracted):
        return _handoff_reply(session)
    reply = handle_turn(session, user_text, extracted)   # app/sop/engine.py, unchanged
    return reply
```

`main.py` changes to a single call: `reply = run_turn(session, body.text)`.

---

## 3. `app/extraction.py`

- Rename the current `extract()` to `regex_fallback_extract()` (or similar) — keep every
  existing regex exactly as-is, this becomes the fallback implementation `pipeline.understand()`
  calls on `LLMUnavailable`. Don't delete or "clean up" the regexes; they're load-bearing for
  offline/free-tier-exhausted operation.
- Extend `ExtractedTurn` with the new fields `understand()`'s schema needs:

  ```python
  @dataclass
  class ExtractedTurn:
      claimed_fields: dict = field(default_factory=dict)
      intent_hint: str | None = None
      caller_role: str | None = None
      rep_relationship: str | None = None
      yes_no: str | None = None                 # "yes" | "no" | None
      mentioned_document: str | None = None
      wants_different_case: bool = False
      is_done: bool = False
      in_scope: bool = True
      emotion: str = "neutral"                   # neutral|frustrated|anxious|angry|confused|refusing
  ```

  Defaults matter: `in_scope=True`/`emotion="neutral"` so a fallback path that doesn't bother
  filling these (e.g. if you only patch the regex fallback for identity fields first) doesn't
  accidentally trip the escalation counter or misroute tone.
- `regex_fallback_extract()` needs three more small heuristics added to produce the new fields
  when it's used as the fallback (mirroring the logic currently scattered across
  `process_case.py`/`post_process.py` — see §4-6): a yes/no parser, an "is done" / "wants
  different case" phrase check, and a naive in-scope check (e.g. keyword-match against an
  insurance-domain wordlist, default to `True` — false positives on off-topic are worse than
  false negatives here, since it's a fallback path, not the primary defense).

---

## 4. `app/sop/verify_id.py`

- No change to the identity-matching logic, the representative path, or the phase-transition
  conditions — this file's whole job is staying strict, and none of that strictness depends on
  where `extracted` came from.
- Replace `_verified_reply()` and `_in_progress_reply()`'s hardcoded strings with `respond()`
  calls:
  - `_verified_reply`: `facts_to_convey=[]` (nothing to disclose yet), `next_action="tell the
    caller they're verified" + (mention the remembered intent_hint if present, e.g.
    next_action="acknowledge they're verified and that you'll pick up their earlier mention of
    {session.memory.intent_hint}")`.
  - `_in_progress_reply`: `facts_to_convey=[]` (never any claim facts here — this is the
    guarantee this phase exists to enforce), `next_action` describing which fields are still
    needed and why (pull in `extracted.emotion` as `tone_hint` so a frustrated caller gets an
    empathetic explanation of *why* verification is required — this is the bonus "explain why
    SOP steps matter" behavior from `ASSIGNMENT.md`).
- Double-check nothing in this file's new `respond()` calls passes `identity.claimed_fields` or
  any claim/case data into `facts_to_convey` — a quick self-review checklist for this file
  specifically, since it's the phase where a leak would be most damaging.

---

## 5. `app/sop/resolve_intent.py`

- `classify_intent()` currently lives in `app/tools/claims.py` — decide whether `understand()`
  should return intent classification pre-computed (i.e. `extracted` carries a resolved intent
  label the LLM produced) or whether this file keeps calling the deterministic
  `classify_intent()` on `hint_text` after `filter_claims()` narrows candidates. **Recommendation:
  keep `classify_intent()` code-based here** — it's not really "understanding messy text," it's
  a deterministic categorization of already-resolved case data (`hint_text` -> one of a fixed
  set of intent labels used only for `session.intent.resolved_intent` bookkeeping), so there's
  no clear win to routing it through the LLM. Leave `app/tools/claims.py::classify_intent()`
  as-is.
- Replace `_no_claim_reply`, `_clarify_reply`, `_clarify_ambiguous_reply`, `_resolved_reply`
  with `respond()` calls. `_clarify_ambiguous_reply` is the interesting one: pass each
  candidate's `case_type`/`status`/`created_at` as separate strings in `facts_to_convey`
  (already public claim metadata, not protected detail) and `next_action="ask which claim they
  mean"`.
- Nothing about the ambiguity-resolution logic (the candidate-narrowing in
  `filter_claims()`, the `len(candidates) == 1` branch) changes.

---

## 6. `app/sop/process_case.py`

- Delete `_is_done()` and `_wants_different_case()` — replaced by `extracted.is_done` /
  `extracted.wants_different_case` from `understand()`. Keep their current regex bodies as the
  fallback implementations inside `extraction.py::regex_fallback_extract()` per §3.
- `_find_mentioned_document` doesn't exist yet as a named function in this file today (the
  routing to `get_document_guideline()` happens via `app/tools/documents.py`'s priority-ordered
  lookup) — if/when you add a "named document mentioned" fast path, source it from
  `extracted.mentioned_document` rather than re-parsing `user_text`, and keep
  `get_document_guideline()`'s existing priority order (named doc → `match_any` →
  `intent_hints`/`requires_documents` → global fallback) unchanged; only the *first* step's
  input source changes.
- Ownership re-check (`claim["party_id"] == session.identity.party_id`) every turn: unchanged,
  still runs before anything else regardless of what `understand()` returned.
- Replace `_denial_reply`, `_next_steps_reply`, `_general_reply`, `_escalate_reply` with
  `respond()` calls. Each of these already assembles a clean list of facts from `claim` — that
  assembly logic (deciding *which* claim fields are relevant to disclose for this intent) stays
  exactly where it is in this file; only the final string-formatting step becomes
  `respond(facts_to_convey=[...], next_action=...)` instead of an f-string. Concretely:
  - `_denial_reply`: `facts_to_convey = [f"denied because {denial_reason}"]`, conditionally
    append `documents_needed`/`appeal_deadline` as additional fact strings, exactly mirroring
    today's conditional `reply +=` structure.
  - `_escalate_reply`: this one probably should **not** go through `respond()` — it's a
    trust/safety-relevant fixed message ("I can't find your claim, let me connect you with a
    representative") and doesn't benefit from LLM phrasing. Keep it a hardcoded string.
- `get_document_guideline()` in `app/tools/documents.py` — check whether it currently returns
  a final string or structured guidance. If it returns a final string today, that's fine to
  keep as-is (pass it through as a single `facts_to_convey` entry to `respond()`) rather than
  restructuring `documents.py`'s return type just for this milestone.

---

## 7. `app/sop/post_process.py`

- **Note while you're in this file:** `_draft_summary()` currently has a bare `return` with no
  value (line 32) — worth fixing regardless of the LLM work, since it's a real bug (returns
  `None`, which then gets concatenated with a string on line 12 and will raise).
- `_draft_summary`: keep the code-side assembly of `topics`, `outcome_line`, `followups` from
  `session.case.topics_covered` and `claim` — this is exactly the kind of "decide what's
  disclosable" step that should stay deterministic. Feed the assembled pieces into `respond()`
  as `facts_to_convey` (e.g. `[f"discussed: {', '.join(topics)}", outcome_line, *followups]`),
  `next_action="ask if they'd like this emailed"`.
- `_parse_yes_no`: delete, replaced by `extracted.yes_no` from `understand()`. Keep the current
  body as `extraction.py`'s fallback (per §3) — the consent gate itself
  (`session.post_process.email_consent`, the explicit-answer-required loop) is unchanged and
  must keep re-asking on `extracted.yes_no is None`, exactly like today's `if answer is None`
  branch. **This is the one gate in the whole system where a wrong/hallucinated LLM read is
  most costly** (misreading "no" as "yes" sends an email without real consent) — consider
  keeping the regex-based `_parse_yes_no` as an authoritative double-check here rather than
  trusting `understand()` alone: if `understand()` says "yes" but the regex fallback parse of
  the same `user_text` says "no" or "unclear," treat it as unclear and re-ask rather than
  resolving the disagreement in the optimistic direction.
- `_handle_consent_answer`/`_closing_reply`: replace their hardcoded strings with `respond()`
  calls the same way as other phases; the branch logic (`if answer == "yes": send_email...`)
  is unchanged and still the only code path allowed to call `send_email_summary()`.

---

## 8. `app/scope_guard.py`

No LLM call. Per `CLAUDE.md`, this becomes a small code module (or gets folded directly into
`pipeline.py` — either is fine, a separate file is slightly cleaner for testing):

```python
OFF_TOPIC_ESCALATION_THRESHOLD = 2

def register_turn(session: SessionState, extracted: ExtractedTurn) -> bool:
    """Update session.off_topic_streak; return True if escalation should trigger now."""
    if extracted.in_scope:
        session.off_topic_streak = 0
        return False
    session.off_topic_streak += 1
    return session.off_topic_streak >= OFF_TOPIC_ESCALATION_THRESHOLD
```

`pipeline.run_turn()` calls this right after `understand()` and before dispatching to
`engine.handle_turn()`.

---

## 9. `app/emotion.py`

No LLM call, and arguably no file needed — `extracted.emotion` already carries the label from
`understand()`. If you want a home for tone-related helper logic (e.g. mapping emotion labels
to a short phrasing instruction fragment reused across phase controllers' `next_action`
strings), put a single small helper here:

```python
def tone_instruction(emotion: str) -> str:
    return {
        "frustrated": "acknowledge their frustration briefly before continuing",
        "anxious": "reassure them calmly before continuing",
        "angry": "stay calm, acknowledge the frustration, do not get defensive",
        "confused": "clarify simply, avoid jargon",
        "refusing": "acknowledge their pushback, explain why the step matters, offer alternatives",
    }.get(emotion, "")
```

Phase controllers call this and fold the result into their `next_action` string before calling
`respond()`. This keeps the empathy behavior consistent across files instead of copy-pasted.

---

## 10. `app/guardrail.py`

**Deterministic, not an LLM call** — this is the belt-and-suspenders check that runs after
`respond()` returns, before the reply goes back to the caller.

```python
def check(reply: str, facts_to_convey: list[str]) -> bool:
    """Best-effort check that the reply didn't introduce claim specifics
    beyond what was explicitly authorized. Not the primary gate — respond()
    only ever seeing authorized facts is the primary gate. This exists to
    catch model drift/hallucination, not prompt injection from claim data."""
```

- Realistic scope for this check given the fixture data: extract "sensitive-looking" tokens
  from the *claim record* that were available to the calling phase controller this turn but
  were **not** included in `facts_to_convey` (denial reasons, document names, dollar amounts,
  appeal deadlines, case IDs of *other* claims) — if any of those literal strings show up in
  `reply` and weren't authorized, treat it as a guardrail failure.
- On failure: don't try to "fix" the reply — log the mismatch and fall back to the same
  deterministic join `respond()` uses on `LLMUnavailable` (§2), built only from
  `facts_to_convey`. Never return the flagged reply to the user.
- This function needs the claim record as an optional argument (`check(reply,
  facts_to_convey, claim=None)`) since `verify_id.py`/`resolve_intent.py` calls have no claim
  in scope yet — in those phases the check degenerates to "the reply doesn't contain identity
  fields" (name, DOB, phone, email, SSN last-4) since `facts_to_convey` is always `[]` there.
- Call it from `pipeline.py`, wrapping every `respond()` call site in one place rather than
  from inside each phase controller — one call site to test, one place to change if the check
  logic evolves.

---

## 11. `app/main.py`

- Replace the two-line `extracted = extract(body.text); reply = handle_turn(session,
  body.text, extracted)` with the single `reply = pipeline.run_turn(session, body.text)` call
  described in §2.
- No other route changes needed — `MessageOut`'s shape, the `/state` debug endpoint, and
  session creation are untouched. Optionally add `session.off_topic_streak` and last-turn
  `emotion` to the `/state` debug dump — useful for demoing the bonus behaviors without needing
  server logs.

---

## 12. `requirements.txt`

Add `google-genai` (the current Gemini SDK package name — verify against
[the package on PyPI](https://pypi.org/project/google-genai/) at install time, package names
for Google's Python SDKs have changed before). Do not add `google-generativeai` (the older,
now-deprecated package) — confirm which one your installed version actually resolves to before
committing the pin.

---

## Suggested build order

1. `llm_client.py` — get one raw `call_llm()` round trip working against a trivial schema,
   verify the API key plumbing, before touching any SOP file.
2. `extraction.py` — rename `extract` → `regex_fallback_extract`, extend `ExtractedTurn`. Run
   the existing (currently-empty) test scaffold's intent — manually re-verify the Margaret Chen
   flow from `ASSIGNMENT.md` still works with the fallback path alone, no LLM involved yet.
3. `pipeline.py::understand()` — wire the real call, fallback tested by unsetting
   `GEMINI_API_KEY` and confirming behavior is identical to step 2.
4. `pipeline.py::respond()` + `guardrail.py` — wire into **one** phase controller first
   (`verify_id.py`, since it's the strictest and highest-value to get right) before touching
   the rest.
5. Roll `respond()` out to `resolve_intent.py` → `process_case.py` → `post_process.py`.
6. `scope_guard.py` + `emotion.py` wiring in `pipeline.run_turn()`.
7. Re-run the Margaret Chen flow end-to-end with the LLM live, then the emotionally-charged
   bonus example from `ASSIGNMENT.md` ("I already told you who I am...").
