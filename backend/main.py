"""
Entry point for the email-agent backend's ingest pass.

A one-shot runner: authenticate (backend.client), build the initial agent
state, fetch new mail (mailparse), then run the verifier — quick relevance
filter + save the kept emails into the SQLite conversation store.

Still direct function calls; the LangGraph graph (verifier + writer + janitor
wired around AgentState) comes later and this file will then invoke that graph
instead. After running this, view results with `python -m backend.cli
contacts` and draft with `python -m backend.node.writer`.
"""

from __future__ import annotations

from backend.client import get_service
from backend.node import mailparse, verifier
from backend.state import initial_state


def main() -> None:
    service = get_service()    # OAuth (browser on first run); read-only
    state = initial_state()

    mailparse.run(service)     # fetch new mail -> backend/parsed_emails.json
    verifier.run(state)        # filter, then save relevant emails to SQLite


if __name__ == "__main__":
    main()
