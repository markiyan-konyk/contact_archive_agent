"""
LangGraph agent state — the shared dict that flows through the graph's nodes,
plus `initial_state()`, the factory every entry point starts from.

The verifier, writer, and janitor nodes each read and write fields here. The
LLM choice lives in the state too (the usual LangGraph place for it), so any
node can rebuild its model with `backend.llm.get_llm(provider=..., model=...,
temperature=...)` from the active settings.

`total=False`: not every key is set on every run — a verify pass never touches
`draft`, a draft pass never touches `parsed_emails`, etc. Nodes return partial
updates and LangGraph merges them in.

The conversation in `messages` is in-memory only — nothing persists it. A new
session calls `initial_state()` and starts from zero.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from backend.llm import DEFAULT_MODELS, DEFAULT_PROVIDER, DEFAULT_TEMPERATURE

LLMProvider = Literal["ollama", "google", "anthropic", "openai"]


class AgentState(TypedDict, total=False):
    # --- chat history -------------------------------------------------------
    # The writer node converses with the user here. `add_messages` is the
    # reducer when this runs as a graph node; the writer currently appends
    # directly (no graph yet).
    messages: Annotated[list[AnyMessage], add_messages]

    # --- LLM choice ---------------------------------------------------------
    # Read by nodes -> backend.llm.get_llm(provider=..., model=..., temperature=...)
    llm_provider: LLMProvider
    llm_model: str
    llm_temperature: float

    # --- verifier -----------------------------------------------------------
    # Emails loaded from backend/parsed_emails.json awaiting a relevance
    # verdict, keyed by Gmail message id. The verifier classifies each by
    # prompt, writes the relevant ones to SQLite, then clears this.
    parsed_emails: dict[str, dict]

    # --- writer -------------------------------------------------------------
    # The contact the user picked in the chat UI. selected_contact_id is the
    # contacts.id the writer pulls aggregated history for (None = write without
    # RAG history). selected_contact_email is kept only for the UI's persistent
    # "to:" header display (the address the user typed via /to mail, if any).
    selected_contact_id: int | None
    selected_contact_email: str | None
    draft: str | None                  # the most recent email the writer produced


def initial_state() -> AgentState:
    """A fresh agent state: empty conversation, default LLM choice, no contact
    selected. Used by `python -m backend.main`, the writer REPL, and (later) the
    per-session state behind the chat UI."""
    return AgentState(
        messages=[],
        llm_provider=DEFAULT_PROVIDER,
        llm_model=DEFAULT_MODELS[DEFAULT_PROVIDER],
        llm_temperature=DEFAULT_TEMPERATURE,
        parsed_emails={},
        selected_contact_id=None,
        selected_contact_email=None,
        draft=None,
    )
