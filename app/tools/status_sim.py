from app.session import SessionState
from app.fixtures_data import CONSENT_SCENARIOS

def check_status(session: SessionState, case_id: str, scenario: str = "default") -> str:
    seq = CONSENT_SCENARIOS[scenario]["status_sequence"]
    index = session.case.status_poll_index.get(case_id, 0)
    index = min(index, len(seq) - 1)  # clamp: stay on the last status once exhausted

    status = seq[index]
    session.case.status_poll_index[case_id] = index + 1  # advance for the next poll
    return status
