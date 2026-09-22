"""Phase dispatch table (ARCHITECTURE.md §6). Milestone 1 only implements
VERIFY_ID; later phases are added here without touching the pipeline.
"""
from app.extraction import ExtractedTurn
from app.session import Phase, SessionState
from app.sop import post_process, process_case, verify_id, resolve_intent


def handle_turn(session: SessionState, user_text: str, extracted: ExtractedTurn) -> str:
    if session.phase == Phase.VERIFY_ID:
        return verify_id.handle(session, extracted)
    if session.phase == Phase.RESOLVE_INTENT:
        return resolve_intent.handle(session, user_text, extracted)
    if session.phase == Phase.PROCESS_CASE:
        return process_case.handle(session, user_text, extracted)
    if session.phase == Phase.POST_PROCESS:
        return post_process.handle(session, user_text, extracted)
    # Placeholder until RESOLVE_INTENT/PROCESS_CASE/POST_PROCESS are built.
    return (
        f"[{session.phase.value} isn't implemented yet in this milestone. "
        "You're verified — this is where intent resolution would continue.]"
    )
