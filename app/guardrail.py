"""Deterministic, not an LLM call (ARCHITECTURE.md §12 / IMPLEMENTATION.md §10).

Runs after every `pipeline.respond()` draft, before it reaches the caller. Belt-and-suspenders:
`respond()` only ever being handed authorized facts is the primary gate, this exists to catch
model drift/hallucination, not prompt injection from claim data.
"""
import re

# Identity fields that must never surface before the caller is verified.
_IDENTITY_KEYS = ("full_name", "dob", "phone", "email", "id_last4", "policy_number")

# Claim fields that can leak case-specific detail if not explicitly authorized
# this turn. Deliberately narrow (IMPLEMENTATION.md §10): "status"/"case_type"
# are low-sensitivity classification info that naturally gets paraphrased
# ("your healthcare claim", "it's denied") even when authorized via a
# differently-worded fact, so including them here just produced false-positive
# guardrail rejections without protecting anything meaningfully sensitive.
_CLAIM_LEAK_KEYS = (
    "case_id",
    "denial_reason",
    "documents_needed",
    "appeal_deadline",
    # Monetary fields are all sensitive by nature, and some are estimates
    # rather than finalized amounts (net_pay is final; the rest aren't) — kept
    # in this list so the model can't casually mention any of them unless a
    # phase controller explicitly authorized that specific figure this turn.
    "net_pay",
    "expected_reimbursement_amount",
    "allowed_max_amount",
    "net_fee",
)


def _tokens_from_claim(claim: dict) -> set[str]:
    tokens: set[str] = set()
    for key in _CLAIM_LEAK_KEYS:
        value = claim.get(key)
        if not value:
            continue
        if isinstance(value, list):
            tokens.update(str(v) for v in value)
        else:
            tokens.add(str(value))
    return tokens


def check(reply: str, facts_to_convey: list[str], claim: dict | None = None) -> bool:
    """Best-effort check that the reply didn't introduce claim specifics
    beyond what was explicitly authorized. Not the primary gate — respond()
    only ever seeing authorized facts is the primary gate. This exists to
    catch model drift/hallucination, not prompt injection from claim data."""
    authorized_text = " ".join(facts_to_convey)

    if claim:
        for token in _tokens_from_claim(claim):
            if token in reply and token not in authorized_text:
                return False

    # No claim in scope yet (VERIFY_ID/RESOLVE_INTENT): the reply must not contain
    # identity fields the caller supplied, since facts_to_convey is always [] there.
    if not claim:
        for pattern in (
            r"\b\d{4}-\d{2}-\d{2}\b",  # DOB
            r"\b\d{4}\b(?=.{0,15}(ssn|id|last))",  # id last-4 in context
        ):
            if re.search(pattern, reply, re.IGNORECASE) and not re.search(pattern, authorized_text, re.IGNORECASE):
                return False

    return True
