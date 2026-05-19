"""
All SQLite reads/writes for the email agent. No SQL lives anywhere else
(CLAUDE.md). Every function takes an open connection so callers can batch a
whole run inside one transaction; use `schema.get_connection()` to make one.

Data model (see schema.py): a *contact* is a person; `contact_emails` holds
that person's addresses; an `emails` row links to the contact, so a
conversation aggregates across every address the person uses automatically.
`contacts.notes` doubles as the per-contact writing prompt; the single
`app_config` row holds the general writing-style prompt.
"""

from __future__ import annotations

import sqlite3


# ============================================================================
# Contact / address resolution
# ============================================================================

def get_contact_id_by_email(conn: sqlite3.Connection, email: str) -> int | None:
    """The contact (person) id that owns `email`, or None if unknown.
    `email` should already be lowercased by the caller."""
    row = conn.execute(
        "SELECT contact_id FROM contact_emails WHERE email = ?", (email,)
    ).fetchone()
    return int(row[0]) if row else None


def resolve_or_create_contact(
    conn: sqlite3.Connection,
    email: str,
    display_name: str | None = None,
) -> int:
    """Return the contact id that owns `email`, creating a new single-address
    contact if the address is unknown.

    On a brand-new address a fresh `contacts` row is made with name =
    `display_name` (may be None — the user can name/merge it later via
    `assign_email_to_contact`). An already-known address never changes owner
    here. `email` must be normalized (lowercased) by the caller. Caller commits.
    """
    existing = get_contact_id_by_email(conn, email)
    if existing is not None:
        return existing

    cur = conn.execute(
        "INSERT INTO contacts (name) VALUES (?)", (display_name or None,)
    )
    contact_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO contact_emails (contact_id, email, display_name) "
        "VALUES (?, ?, ?)",
        (contact_id, email, display_name or None),
    )
    return contact_id


# ============================================================================
# Email storage
# ============================================================================

def email_exists(conn: sqlite3.Connection, gmail_message_id: str) -> bool:
    """True if an email with this Gmail message id is already stored."""
    row = conn.execute(
        "SELECT 1 FROM emails WHERE gmail_message_id = ? LIMIT 1",
        (gmail_message_id,),
    ).fetchone()
    return row is not None


def count_emails_for_contact(conn: sqlite3.Connection, contact_id: int) -> int:
    """How many emails are stored for this contact — across ALL of the
    person's addresses (the aggregated conversation length the verifier's
    >6 threshold uses)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM emails WHERE contact_id = ?", (contact_id,)
    ).fetchone()
    return int(row[0])


def insert_email(
    conn: sqlite3.Connection,
    *,
    contact_id: int,
    direction: str,           # 'sent' | 'received'
    subject: str | None,
    body: str | None,
    timestamp: str,           # ISO-8601 UTC
    gmail_message_id: str,
) -> None:
    """Insert one email row. A duplicate gmail_message_id is silently ignored,
    so re-running the verifier over the same batch is safe. Caller commits."""
    conn.execute(
        "INSERT OR IGNORE INTO emails "
        "(contact_id, direction, subject, body, timestamp, gmail_message_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (contact_id, direction, subject, body, timestamp, gmail_message_id),
    )


# ============================================================================
# Conversation reads
# ============================================================================

def get_conversation_by_contact_id(
    conn: sqlite3.Connection, contact_id: int
) -> list[sqlite3.Row]:
    """Every email with this contact, oldest first — aggregated across all of
    the person's addresses. Row columns: id, contact_id, direction, subject,
    body, timestamp, gmail_message_id."""
    return conn.execute(
        "SELECT * FROM emails WHERE contact_id = ? ORDER BY timestamp ASC",
        (contact_id,),
    ).fetchall()


def get_conversation_by_email(
    conn: sqlite3.Connection, email: str
) -> list[sqlite3.Row]:
    """Same as get_conversation_by_contact_id, but keyed by one of the
    person's addresses. Empty if the address is unknown."""
    contact_id = get_contact_id_by_email(conn, email)
    if contact_id is None:
        return []
    return get_conversation_by_contact_id(conn, contact_id)


# ============================================================================
# Contact / address listing + lookup
# ============================================================================

def list_contacts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every contact (person) with size + recency, most recently active first.

    Row columns: id, name, notes, relationship, address_count, email_count,
    first_email_at, last_email_at. Contacts with no emails sort last.
    """
    return conn.execute(
        "SELECT c.id, c.name, c.notes, c.relationship, "
        "       (SELECT COUNT(*) FROM contact_emails ce "
        "        WHERE ce.contact_id = c.id)              AS address_count, "
        "       COUNT(e.id)                               AS email_count, "
        "       MIN(e.timestamp)                          AS first_email_at, "
        "       MAX(e.timestamp)                          AS last_email_at "
        "FROM contacts c "
        "LEFT JOIN emails e ON e.contact_id = c.id "
        "GROUP BY c.id "
        "ORDER BY last_email_at DESC"
    ).fetchall()


def list_addresses(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every stored email address with its owning contact, most recently
    active first. Row columns: email, display_name, contact_id, contact_name,
    email_count, last_email_at. (Powers the UI's `:emails` view.)"""
    return conn.execute(
        "SELECT ce.email, ce.display_name, ce.contact_id, "
        "       c.name AS contact_name, "
        "       COUNT(e.id)      AS email_count, "
        "       MAX(e.timestamp) AS last_email_at "
        "FROM contact_emails ce "
        "JOIN contacts c ON c.id = ce.contact_id "
        "LEFT JOIN emails e ON e.contact_id = ce.contact_id "
        "GROUP BY ce.id "
        "ORDER BY last_email_at DESC"
    ).fetchall()


def get_addresses_for_contact(
    conn: sqlite3.Connection, contact_id: int
) -> list[sqlite3.Row]:
    """All addresses for one contact. Columns: id, email, display_name."""
    return conn.execute(
        "SELECT id, email, display_name FROM contact_emails "
        "WHERE contact_id = ? ORDER BY created_at ASC",
        (contact_id,),
    ).fetchall()


def get_contact(conn: sqlite3.Connection, contact_id: int) -> sqlite3.Row | None:
    """The contact row (id, name, relationship, notes, created_at) or None."""
    return conn.execute(
        "SELECT * FROM contacts WHERE id = ?", (contact_id,)
    ).fetchone()


def get_contact_by_name(
    conn: sqlite3.Connection, name: str
) -> sqlite3.Row | None:
    """First contact with this exact name (case-insensitive), or None.
    Names aren't unique in the schema; this returns the oldest match."""
    return conn.execute(
        "SELECT * FROM contacts WHERE name = ? COLLATE NOCASE "
        "ORDER BY id ASC LIMIT 1",
        (name,),
    ).fetchone()


# ============================================================================
# Contact mutation — naming, grouping, prompts
# ============================================================================

def set_contact_name(
    conn: sqlite3.Connection, contact_id: int, name: str | None
) -> None:
    """Set (or clear) a contact's display name. Caller commits."""
    conn.execute(
        "UPDATE contacts SET name = ? WHERE id = ?", (name or None, contact_id)
    )


def set_contact_notes(
    conn: sqlite3.Connection, contact_id: int, notes: str | None
) -> None:
    """Set the contact's notes — which is the per-contact writing prompt the
    UI's `:prompt` edits. Caller commits."""
    conn.execute(
        "UPDATE contacts SET notes = ? WHERE id = ?",
        (notes or None, contact_id),
    )


def get_contact_notes(
    conn: sqlite3.Connection, contact_id: int
) -> str | None:
    """The per-contact writing prompt (contacts.notes), or None if unset."""
    row = conn.execute(
        "SELECT notes FROM contacts WHERE id = ?", (contact_id,)
    ).fetchone()
    return row[0] if row else None


def assign_email_to_contact(
    conn: sqlite3.Connection, email: str, contact_name: str
) -> int:
    """`:addcon` — group `email` under the contact named `contact_name`,
    creating that contact if needed. Returns the target contact id.

    If the address's current contact is left with no addresses (the common
    case: an auto-created single-address contact), that contact's emails are
    repointed to the target and the now-empty contact is deleted, so history
    follows the address. If the source contact has OTHER addresses too, only
    the address moves (its messages can't be split out — emails link to the
    contact, not the address); this is acceptable for the auto-created
    single-address contacts the verifier produces. `email` must be lowercased.
    Caller commits.
    """
    target = get_contact_by_name(conn, contact_name)
    if target is None:
        cur = conn.execute(
            "INSERT INTO contacts (name) VALUES (?)", (contact_name,)
        )
        target_id = int(cur.lastrowid)
    else:
        target_id = int(target["id"])

    src = conn.execute(
        "SELECT contact_id FROM contact_emails WHERE email = ?", (email,)
    ).fetchone()
    if src is None:
        # Address not stored yet — just attach it to the target contact.
        conn.execute(
            "INSERT INTO contact_emails (contact_id, email) VALUES (?, ?)",
            (target_id, email),
        )
        return target_id

    src_id = int(src[0])
    if src_id == target_id:
        return target_id

    conn.execute(
        "UPDATE contact_emails SET contact_id = ? WHERE email = ?",
        (target_id, email),
    )

    remaining = conn.execute(
        "SELECT COUNT(*) FROM contact_emails WHERE contact_id = ?", (src_id,)
    ).fetchone()[0]
    if remaining == 0:
        # Source had only this address — its history belongs to the address.
        conn.execute(
            "UPDATE emails SET contact_id = ? WHERE contact_id = ?",
            (target_id, src_id),
        )
        conn.execute("DELETE FROM contacts WHERE id = ?", (src_id,))
    return target_id


# ============================================================================
# General config prompt (single-row app_config)
# ============================================================================

def get_general_prompt(conn: sqlite3.Connection) -> str | None:
    """The general writing-style prompt (the UI's `:config`), or None."""
    row = conn.execute(
        "SELECT general_prompt FROM app_config WHERE id = 1"
    ).fetchone()
    return row[0] if row else None


def set_general_prompt(conn: sqlite3.Connection, text: str | None) -> None:
    """Upsert the single app_config row with the general writing-style prompt.
    Caller commits."""
    conn.execute(
        "INSERT INTO app_config (id, general_prompt) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET general_prompt = excluded.general_prompt",
        (text or None,),
    )
