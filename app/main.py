"""FastAPI app — see ARCHITECTURE.md §13 for the intended full API surface.
Milestone 1: session create + message turn, VERIFY_ID phase only, stubbed
extraction (no LLM call yet).
"""
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.extraction import extract
from app.session import Phase, create_session, get_session
from app.sop.engine import handle_turn

app = FastAPI(title="Insurance Claims SOP Agent")


class MessageIn(BaseModel):
    text: str


class MessageOut(BaseModel):
    reply: str
    phase: str
    verified: bool
    matched_fields: list[str]


@app.post("/api/session")
def create_session_endpoint():
    session = create_session()
    return {"session_id": session.session_id}


@app.post("/api/session/{session_id}/message", response_model=MessageOut)
def post_message(session_id: str, body: MessageIn):
    session = get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session_id")

    session.turns.append({"role": "user", "text": body.text})

    extracted = extract(body.text)
    reply = handle_turn(session, body.text, extracted)

    session.turns.append({"role": "agent", "text": reply})

    return MessageOut(
        reply=reply,
        phase=session.phase.value,
        verified=session.identity.verified,
        matched_fields=session.identity.matched_fields,
    )


@app.get("/api/session/{session_id}/state")
def get_state(session_id: str):
    """Debug-only endpoint: dumps full session state for demo transparency."""
    session = get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session_id")
    return {
        "session_id": session.session_id,
        "phase": session.phase.value,
        "identity": vars(session.identity),
        "memory": vars(session.memory),
        "turns": session.turns,
    }


app.mount("/", StaticFiles(directory="web", html=True), name="web")
