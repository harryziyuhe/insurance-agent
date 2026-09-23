import uuid
from dataclasses import dataclass, field
from enum import StrEnum


class Phase(StrEnum):
    VERIFY_ID = "VERIFY_ID"
    RESOLVE_INTENT = "RESOLVE_INTENT"
    PROCESS_CASE = "PROCESS_CASE"
    POST_PROCESS = "POST_PROCESS"
    HUMAN_HANDOFF = "HUMAN_HANDOFF"


@dataclass
class IdentityState:
    caller_role: str | None = None  # self | representative | None
    claimed_fields: dict = field(default_factory=dict)
    matched_fields: list = field(default_factory=list)
    verified: bool = False
    party_id: str | None = None
    rep_name: str | None = None
    rep_relationship: str | None = None
    verification_attempts: int = 0


@dataclass
class MemoryState:
    intent_hint: str | None = None
    case_hint_type: str | None = None
    case_hint_period: str | None = None


@dataclass
class IntentState:
    resolved_intent: str | None = None
    case_id: str | None = None


@dataclass
class CaseState:
    topics_covered: list[str] = field(default_factory=list)
    status_poll_index: dict[str, int] = field(default_factory=dict)


@dataclass
class PostProcessState:
    summary: str | None = None
    awaiting_consent: bool = False
    email_consent: str | None = None
    email_sent: bool = False


@dataclass
class SessionState:
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    phase: Phase = Phase.VERIFY_ID
    turns: list = field(default_factory=list)
    identity: IdentityState = field(default_factory=IdentityState)
    memory: MemoryState = field(default_factory=MemoryState)
    intent: IntentState = field(default_factory=IntentState)
    case: CaseState = field(default_factory=CaseState)
    post_process: PostProcessState = field(default_factory=PostProcessState)
    off_topic_streak: int = 0
    last_extracted: dict = field(default_factory=dict)


_SESSIONS: dict[str, SessionState] = {}


def create_session() -> SessionState:
    s = SessionState()
    _SESSIONS[s.session_id] = s
    return s


def get_session(session_id: str) -> SessionState | None:
    return _SESSIONS.get(session_id)
