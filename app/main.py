from dataclasses import asdict

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import pipeline
from app.session import create_session, get_session

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

    reply = pipeline.run_turn(session, body.text)

    return MessageOut(
        reply=reply,
        phase=session.phase.value,
        verified=session.identity.verified,
        matched_fields=session.identity.matched_fields,
    )


@app.get("/api/session/{session_id}/state")
def get_state(session_id: str):
    """Debug-only endpoint: dumps the entire SessionState (every sub-dataclass,
    plus last_extracted — the most recent understand() output) for demo
    transparency. Deliberately not curated to specific fields so new
    SessionState fields show up here automatically."""
    session = get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session_id")
    state = asdict(session)
    state["phase"] = session.phase.value
    return state


app.mount("/", StaticFiles(directory="web", html=True), name="web")
