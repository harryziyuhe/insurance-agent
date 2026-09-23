import json
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _load(name: str):
    with open(FIXTURES_DIR / name, encoding="utf-8") as f:
        return json.load(f)


POLICYHOLDERS = _load("policyholders.json")
REPRESENTATIVES = _load("representatives.json")
CLAIMS = _load("claims.json")
CLAIM_SCHEMA = _load("claim_schema.json")
DOCUMENT_GUIDELINE = _load("required_document_guideline.json")
CONSENT_SCENARIOS = _load("consent_scenarios.json")


def policyholders_by_party_id() -> dict:
    return {p["party_id"]: p for p in POLICYHOLDERS}


def claims_by_party_id() -> dict:
    out: dict[str, list] = {}
    for c in CLAIMS:
        out.setdefault(c["party_id"], []).append(c)
    return out


def claims_by_case_id() -> dict:
    out: dict[str, list] = {}
    for c in CLAIMS:
        out.setdefault(c["case_id"], []).append(c)
    return out
