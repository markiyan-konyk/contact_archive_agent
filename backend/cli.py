"""
Command-line views into the email-agent database — the "human at a terminal"
way to see what the verifier saved.

All data comes from backend.database.queries. The future UI will call those
same functions and render its own sections (a contacts list, a conversation
pane); this module is just one more presentation layer over that data layer,
so nothing here needs to be reused by the UI — only queries.py does.

Usage:
    python -m backend.cli contacts                 # everyone you have history with
    python -m backend.cli convo someone@example.com   # the full thread with one person
"""

from __future__ import annotations

import sys

from backend.database import queries, schema
from backend.textclean import strip_quotes_and_signature


def _oneline(text: str | None, limit: int = 200) -> str:
    """Collapse a (possibly multi-line) string to a single short preview line."""
    if not text:
        return ""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def show_contacts() -> None:
    """Print every saved contact: email, name, #emails, first/last contact date."""
    schema.init_db()
    conn = schema.get_connection()
    try:
        rows = queries.list_contacts(conn)
    finally:
        conn.close()

    if not rows:
        print("No contacts saved yet. Run the agent (python -m backend.main) first.")
        return

    print(f"Contacts ({len(rows)}):\n")
    print(f"  {'NAME':<28} {'ADDRS':>5} {'#':>4}  {'FIRST':<10}  {'LAST':<10}")
    print(f"  {'-' * 28} {'-' * 5} {'-' * 4}  {'-' * 10}  {'-' * 10}")
    for r in rows:
        print(
            f"  {(r['name'] or '(unnamed)'):<28.28} {r['address_count']:>5} "
            f"{r['email_count']:>4}  {(r['first_email_at'] or '')[:10]:<10}  "
            f"{(r['last_email_at'] or '')[:10]:<10}"
        )


def show_conversation(email: str) -> None:
    """Print the full back-and-forth with one email address, oldest first."""
    schema.init_db()
    conn = schema.get_connection()
    try:
        contact_id = queries.get_contact_id_by_email(conn, email)
        contact = queries.get_contact(conn, contact_id) if contact_id else None
        messages = queries.get_conversation_by_email(conn, email)
    finally:
        conn.close()

    if contact is None:
        print(f"No contact with email {email!r}. "
              "Run `python -m backend.cli contacts` to see what's saved.")
        return

    name = contact["name"] or ""
    header = f"{name} <{email}>" if name else email
    print(f"Conversation with {header}  ({len(messages)} emails)")
    if contact["notes"]:
        print(f"  notes: {contact['notes']}")
    print()

    # Width of the "timestamp + direction" prefix, so body lines line up under
    # the subject. "2026-01-03 09:14" (16) + 2 + "RECEIVED" padded to 8 + 2.
    indent = " " * (16 + 2 + 8 + 2)
    for m in messages:
        when = (m["timestamp"] or "")[:16].replace("T", " ")
        tag = "SENT" if m["direction"] == "sent" else "RECEIVED"
        print(f"{when:<16}  {tag:<8}  {m['subject'] or '(no subject)'}")
        # Display-only: strip quoted history / signatures for the preview. The
        # DB still holds the raw body. Fall back to raw if stripping leaves
        # nothing (a bare bottom-posted reply that's all quote).
        raw = m["body"]
        body = _oneline(strip_quotes_and_signature(raw) or raw)
        if body:
            print(f"{indent}{body}")


def reset_everything() -> None:
    """Wipe all saved state — the SQLite DB, the mailparse cursor, and the
    parsed-emails handoff file — so the next run starts from a clean slate.
    DESTRUCTIVE; asks for confirmation."""
    from backend.node import mailparse  # heavy import (googleapiclient); only here

    targets = [
        ("database (all contacts + emails)", schema.DB_PATH),
        ("mailparse cursor", mailparse.STATE_PATH),
        ("parsed-emails handoff", mailparse.OUTPUT_PATH),
    ]
    present = [(label, p) for label, p in targets if p.exists()]
    if not present:
        print("Nothing to reset — already a clean slate.")
        return

    print("This will DELETE:")
    for label, p in present:
        print(f"  - {label}: {p}")
    if input("Type 'yes' to confirm: ").strip().lower() != "yes":
        print("Aborted — nothing changed.")
        return

    schema.reset_db()                       # deletes + recreates empty tables
    for label, p in present:
        if p != schema.DB_PATH and p.exists():
            p.unlink()
    print("Done — clean slate. `python -m backend.main` will re-backfill from scratch.")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["contacts"]:
        show_contacts()
        return 0
    if len(argv) == 2 and argv[0] in ("convo", "conversation"):
        show_conversation(argv[1])
        return 0
    if argv == ["reset"]:
        reset_everything()
        return 0
    print(
        "usage:\n"
        "  python -m backend.cli contacts            list saved contacts\n"
        "  python -m backend.cli convo <email>       show the thread with one contact\n"
        "  python -m backend.cli reset               wipe the DB + mailparse state (asks first)",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
