"""
Writer node — a chatbot that drafts emails.

Powered by the LLM from backend.llm. The conversation lives only in memory
(state["messages"]); nothing is persisted — when a session ends it starts from
zero next time. This is the backend behind the CLI's writing mode (a message
list + the draft, like a coding agent).

How a turn works — `respond(state, user_message)`:

  1. Build a fresh system prompt:
       BASE_SYSTEM_PROMPT                 "whatever you're told, output an email"
       + general style                    app_config.general_prompt (the UI's
                                           `:config`), if set
       + per-contact style                that contact's contacts.notes (the
                                           UI's `:prompt`), if a contact is
                                           selected and it's set
       + the stored conversation with     only if a contact is selected
         that contact, aggregated across   (state["selected_contact_id"]) — the
         ALL their addresses               relationship-context / "RAG" path
  2. messages = [system] + state["messages"] + [user's new message]
  3. llm.invoke(...) -> the email text
  4. append (user message, reply) to state["messages"]; set state["draft"]

No contact selected -> the history block is omitted: just the local LLM writing
an email from the chat. Contact selected -> that contact's stored conversation
(aggregated across every address they use, via queries.get_conversation_by_
contact_id) is fed in, so the draft matches the tone/history of that
relationship.

All writing-style configuration now lives in the database (general prompt in
the single app_config row; per-contact prompt in contacts.notes), edited
through the CLI — there is no writer_config.json anymore.
"""

from __future__ import annotations

import sys

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from backend.database import queries, schema
from backend.llm import get_llm
from backend.state import AgentState
from backend.textclean import strip_quotes_and_signature

# The writer wants some variety, unlike the verifier — so it overrides
# state["llm_temperature"] (the deterministic default) while keeping the
# state-configured provider/model.
WRITER_TEMPERATURE = 0.7

# How much of a contact's history to feed in: at most this many of the most
# recent emails, each body trimmed (after stripping quotes/signatures) to this
# many chars. Generous — conversations are usually short.
_MAX_HISTORY_EMAILS = 40
_MAX_BODY_CHARS = 1200

BASE_SYSTEM_PROMPT = """\
You are an email-writing assistant. Whatever the user says to you, your job is \
to produce an email — never a chat reply, never commentary or explanation, just \
the email itself. If the user's instruction is vague ("respond positively", \
"decline politely", "ask for an extension", "follow up"), treat it as guidance \
for the email you should write, and write a complete, natural email.

Do not invent facts. Don't refer to replies, meetings, calls, attachments, \
names, dates or events that aren't in your conversation with the user or in the \
email history you've been given. If you don't have enough specifics, write a \
short, natural email that fits the instruction without fabricating details — \
generic is fine, made-up is not. In particular, never thank someone for getting \
back to you unless the history actually shows them replying.

Don't use placeholder brackets like "[Your Name]", "[Recipient]" or "[Original \
Subject]". If you don't know something, leave it out — end with a simple \
sign-off and no name rather than a bracketed placeholder, and only write a \
"Subject:" line if you actually know or can reasonably infer the subject.

Output format: an optional "Subject: ..." line, then a blank line, then the \
email body. When replying within an existing thread use "Subject: Re: <original \
subject>". Output ONLY the email — no preamble like "Here's the email:".

Write in the first person, as the user. Be natural and human; match whatever \
tone the context (recipient, history, the user's instruction) calls for. The \
user may follow up to revise the email — when they do, output the full updated \
email, not a diff."""


# ----------------------------------------------------------------------------
# Prompt assembly
# ----------------------------------------------------------------------------

def _format_history(rows: list, contact_name: str) -> str:
    """Render a contact's stored conversation (queries.get_conversation_by_
    contact_id rows) as a context block: a who-sent-what summary, then each
    email headed with its sender + date, with a cleaned, truncated body."""
    who = contact_name or "this contact"
    n = len(rows)
    n_you = sum(1 for r in rows if r["direction"] == "sent")
    n_them = n - n_you
    last_from = "you" if rows and rows[-1]["direction"] == "sent" else who

    out: list[str] = [
        f"Your stored email history with {who} — {n} email(s): {n_you} from you, "
        f"{n_them} from {who}. The most recent was from {last_from}.",
    ]
    if n_them == 0:
        out.append(
            f"NOTE: {who} has not replied to you. If asked to reply or follow up, "
            f"write a polite nudge — do NOT thank {who} for getting back to you, "
            "because they haven't."
        )
    out.append(
        "Use this history to match tone, formality and style, and to refer to "
        "relevant details — do not copy it verbatim. Note who sent each email:"
    )
    out.append("")
    for r in rows[-_MAX_HISTORY_EMAILS:]:
        when = (r["timestamp"] or "")[:16].replace("T", " ")
        sender = "you" if r["direction"] == "sent" else who
        out.append(f"--- from {sender} · {when} ---")
        out.append(f"Subject: {r['subject'] or '(no subject)'}")
        body = (strip_quotes_and_signature(r["body"]) or (r["body"] or "")).strip()
        if len(body) > _MAX_BODY_CHARS:
            body = body[:_MAX_BODY_CHARS].rstrip() + " […]"
        if body:
            out.append(body)
        out.append("")
    return "\n".join(out).rstrip()


def _build_system_prompt(contact_id: int | None) -> str:
    """Assemble the system prompt: base rules + general style (app_config) +,
    if a contact is selected, that contact's per-contact prompt (contacts.notes)
    and aggregated conversation history."""
    parts: list[str] = [BASE_SYSTEM_PROMPT]

    conn = schema.get_connection()
    try:
        general = (queries.get_general_prompt(conn) or "").strip()
        if general:
            parts.append(f"General style preferences set by the user:\n{general}")

        if contact_id is not None:
            contact = queries.get_contact(conn, contact_id)
            name = (contact["name"] if contact else "") or ""
            addresses = [
                r["email"] for r in queries.get_addresses_for_contact(conn, contact_id)
            ]
            per_contact = (
                (contact["notes"] if contact and contact["notes"] else "")
            ).strip()
            history = queries.get_conversation_by_contact_id(conn, contact_id)
        else:
            contact = None
            name = ""
            addresses = []
            per_contact = ""
            history = []
    finally:
        conn.close()

    if contact_id is not None:
        if name and addresses:
            target = f"{name} <{', '.join(addresses)}>"
        elif addresses:
            target = ", ".join(addresses)
        else:
            target = name or "this contact"
        parts.append(f"You are writing this email to: {target}.")
        if per_contact:
            parts.append(
                f"Style notes for writing to this person specifically:\n{per_contact}"
            )
        if history:
            parts.append(_format_history(history, name))
        else:
            parts.append(
                f"(No stored email history with {target} yet — write a "
                "fresh, appropriately neutral email.)"
            )

    return "\n\n".join(parts)


# ----------------------------------------------------------------------------
# A chat turn
# ----------------------------------------------------------------------------

def respond(state: AgentState, user_message: str) -> str:
    """One chat turn. Builds the system prompt (with the selected contact's
    aggregated history if state["selected_contact_id"] is set), calls the LLM,
    returns the drafted email, and appends the (user, assistant) exchange to
    state["messages"]. The conversation lives only in `state` — not persisted.
    """
    contact_id = state.get("selected_contact_id")
    system_prompt = _build_system_prompt(contact_id)

    history = list(state.get("messages") or [])
    prompt_messages = [SystemMessage(content=system_prompt), *history,
                       HumanMessage(content=user_message)]

    llm = get_llm(
        provider=state.get("llm_provider"),
        model=state.get("llm_model"),
        temperature=WRITER_TEMPERATURE,
    )
    reply = llm.invoke(prompt_messages)
    email_text = str(reply.content or "").strip()

    msgs = state.setdefault("messages", [])
    msgs.append(HumanMessage(content=user_message))
    msgs.append(AIMessage(content=email_text))
    state["draft"] = email_text
    return email_text


# ----------------------------------------------------------------------------
# Terminal chat (debug). The CLI frontend does the same thing with a TUI.
# ----------------------------------------------------------------------------

def _select_contact(state: AgentState, *, email: str | None = None,
                     name: str | None = None) -> str:
    """Resolve a contact by address or name, set state['selected_contact_id']
    (+ selected_contact_email for display), and return a status line."""
    conn = schema.get_connection()
    try:
        if email is not None:
            cid = queries.get_contact_id_by_email(conn, email)
            label = email
        else:
            row = queries.get_contact_by_name(conn, name or "")
            cid = int(row["id"]) if row else None
            label = name or ""
        n = (len(queries.get_conversation_by_contact_id(conn, cid))
             if cid is not None else 0)
    finally:
        conn.close()

    state["selected_contact_id"] = cid
    state["selected_contact_email"] = email
    if cid is None:
        return (f"{label!r} isn't in the DB — selected anyway "
                "(will write a fresh email, no history).")
    return f"using {label} ({n} email(s) in aggregated history)."


def _repl() -> None:
    from backend.state import initial_state

    state = initial_state()
    print("Writer — chat to draft emails. The conversation is in-memory only.")
    print("Commands:  /to mail <email>   /to contact <name>   /no-contact   "
          "/reset   /quit")
    print("no contact selected.\n")

    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("/quit", "/exit"):
            break
        if line == "/reset":
            state["messages"] = []
            state["draft"] = None
            print("conversation cleared.\n")
            continue
        if line == "/no-contact":
            state["selected_contact_id"] = None
            state["selected_contact_email"] = None
            print("no contact selected.\n")
            continue
        if line.startswith("/to mail"):
            email = line[len("/to mail"):].strip().lower()
            if not email:
                print("usage: /to mail <email>\n")
                continue
            print(_select_contact(state, email=email) + "\n")
            continue
        if line.startswith("/to contact"):
            name = line[len("/to contact"):].strip()
            if not name:
                print("usage: /to contact <name>\n")
                continue
            print(_select_contact(state, name=name) + "\n")
            continue
        if line.startswith("/"):
            print("unknown command: /to mail <email> | /to contact <name> | "
                  "/no-contact | /reset | /quit\n")
            continue

        print("[ writing... ]")
        try:
            email_text = respond(state, line)
        except Exception as e:  # noqa: BLE001
            print(f"[error] {type(e).__name__}: {e}", file=sys.stderr)
            print("(is Ollama running with the model pulled?)\n")
            continue
        print("─" * 64)
        print(email_text)
        print("─" * 64 + "\n")


if __name__ == "__main__":
    _repl()
