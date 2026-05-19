"""SQLite schema bootstrap for the email agent.

Data model — ONE contact = ONE person who may write from several addresses:

    contacts         the person: name, relationship, notes (notes doubles as
                     the per-contact writing prompt the UI's `:prompt` edits).
    contact_emails   that person's email addresses (one row per address; an
                     address belongs to exactly one contact).
    emails           every stored message, linked to the *contact* (the
                     person) — never to a single address — so a conversation
                     aggregates across all of that person's addresses for free.
    app_config       a single row holding the general writing-style prompt
                     (the UI's `:config`).

(Earlier versions had a separate, unused `people` table and a unique-email
`contacts` table; that split was a bug and has been removed.)

Running this module directly creates the database file (if missing) and
ensures every table and index is in place. Safe to re-run.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH: Path = Path(__file__).parent / "email_agent.db"

# The person. `name` is nullable: an auto-created contact (first seen via an
# inbound address with no known name) may have no name until the user sets one
# with `:addcon`. `notes` is the per-contact writing prompt.
_CONTACTS_TABLE = """
CREATE TABLE IF NOT EXISTS contacts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT,
    relationship TEXT,
    notes        TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

# A contact's addresses. `email` is globally UNIQUE — one address maps to one
# person. Reassigning an address to another contact (`:addcon`) repoints
# contact_id here and on the affected `emails` rows.
_CONTACT_EMAILS_TABLE = """
CREATE TABLE IF NOT EXISTS contact_emails (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id   INTEGER NOT NULL,
    email        TEXT    NOT NULL UNIQUE,
    display_name TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE
);
"""

# Messages link to the contact (person), so get_conversation by contact_id
# already spans every address that person uses.
_EMAILS_TABLE = """
CREATE TABLE IF NOT EXISTS emails (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id        INTEGER NOT NULL,
    direction         TEXT    NOT NULL CHECK (direction IN ('sent', 'received')),
    subject           TEXT,
    body              TEXT,
    timestamp         TEXT    NOT NULL,
    gmail_message_id  TEXT    NOT NULL UNIQUE,
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE
);
"""

# Exactly one row (id is pinned to 1). Holds the general writing-style prompt.
_APP_CONFIG_TABLE = """
CREATE TABLE IF NOT EXISTS app_config (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    general_prompt TEXT
);
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_emails_contact_id ON emails(contact_id);",
    "CREATE INDEX IF NOT EXISTS idx_contact_emails_contact_id "
    "ON contact_emails(contact_id);",
    "CREATE INDEX IF NOT EXISTS idx_contact_emails_email "
    "ON contact_emails(email);",
)


def get_connection() -> sqlite3.Connection:
    """Open a connection to the email-agent SQLite DB.

    Foreign keys are enforced, and rows come back as sqlite3.Row so callers can
    use both positional (row[0]) and named (row["email"]) access — the latter
    is what the CLI views and the future UI endpoints want.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create the DB file and all tables/indexes if they don't already exist."""
    conn = get_connection()
    try:
        with conn:
            conn.execute(_CONTACTS_TABLE)
            conn.execute(_CONTACT_EMAILS_TABLE)
            conn.execute(_EMAILS_TABLE)
            conn.execute(_APP_CONFIG_TABLE)
            for stmt in _INDEXES:
                conn.execute(stmt)
    finally:
        conn.close()


def reset_db() -> None:
    """Delete the database file and recreate empty tables. DESTRUCTIVE — drops
    every contact and email. (Used by `python -m backend.cli reset`.)"""
    if DB_PATH.exists():
        DB_PATH.unlink()
    init_db()


if __name__ == "__main__":
    init_db()
    print(f"Initialized database at {DB_PATH}")
