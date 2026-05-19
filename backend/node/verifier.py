"""
Verifier node — a TWO-STEP process that runs right after `mailparse`.

    Step 1 — quick filter  (quick_filter):
        Loads backend/parsed_emails.json, drops the obvious junk, writes the
        trimmed file back. No database, no cursor changes.
          * SENT mail                 -> KEEP (the user's own writing is a
                                          relationship-tone sample, per CLAUDE.md)
          * CATEGORY_PROMOTIONS /
            _SOCIAL / _UPDATES label   -> DISCARD, no LLM call (CLAUDE.md says
                                          skip these; big speedup on backfills)
          * anything else              -> tiny LLM call on the From header only;
                                          kills no-reply / notifications /
                                          newsletters / job-board / SaaS machine
                                          mail. Errs toward KEEP.

    Step 2 — save to DB  (save_relevant_to_db):
        Walks the kept emails OLDEST -> NEWEST and writes them into SQLite,
        building per-contact conversation histories. The "contact" is the
        *other* person's address: the From on a received email, the (first) To
        on a sent one — that's the commonality across a back-and-forth thread.
          * no prior conversation with that contact -> save (start a thread)
          * prior conversation, length <= 6          -> save (append)
          * prior conversation, length  > 6          -> LLM checks the email's
                                                        snippet for actual
                                                        content; filler like
                                                        "thanks!" / "see you
                                                        soon" is dropped.
        Idempotent: an email whose Gmail id is already stored is skipped (but
        still counts toward the conversation length).

`run(state)` does step 1 then step 2, then clears `parsed_emails.json`'s
`emails` (it's been consumed) while keeping the run's audit trail.

LLM model: both steps use the state-configured model (default gemma4:e4b).
Sender triage and snippet triage are easy tasks — the small Gemma is fine.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from email.utils import getaddresses

from backend.database import queries, schema
from backend.llm import get_llm
from backend.node.mailparse import OUTPUT_PATH
from backend.state import AgentState


# ============================================================================
# STEP 1 — quick filter
# ============================================================================

# Gmail category labels we treat as "definitely not a conversation".
_JUNK_CATEGORY_LABELS = {
    "CATEGORY_PROMOTIONS",
    "CATEGORY_SOCIAL",
    "CATEGORY_UPDATES",
}

# LLM prompt: classify a sender given ONLY the From header. Slim, few-shot
# (the user's own junk examples + a couple of real-person ones).
SENDER_TRIAGE_PROMPT = """\
You triage email senders. You are given only the sender of an email. Decide:
KEEP    - looks like a real individual person the user might reply to.
DISCARD - looks automated/bulk: no-reply, notifications, mailer-daemon,
          newsletters, marketing, job boards, social-network or SaaS-app
          machine mail, order/shipping/billing/security system messages.
When unsure, answer KEEP. Reply with exactly one word: KEEP or DISCARD. No explanation.

Examples:
"LinkedIn <editors-noreply@linkedin.com>"   -> DISCARD
"Google <no-reply@accounts.google.com>"     -> DISCARD
"Workday <spinmaster@myworkday.com>"        -> DISCARD
"Amazon.com <shipment-tracking@amazon.com>" -> DISCARD
"Medium Daily Digest <noreply@medium.com>"  -> DISCARD
"Sarah Chen <sarah.chen@gmail.com>"         -> KEEP
"Tom Baker <tom@acme.io>"                   -> KEEP
"jane@university.edu"                        -> KEEP"""


def _classify_sender(llm, sender: str) -> bool:
    """Ask the LLM whether `sender` looks like a real person. True = KEEP,
    False = DISCARD. Lenient parsing; ambiguous -> KEEP."""
    resp = llm.invoke([
        ("system", SENDER_TRIAGE_PROMPT),
        ("human", f'"{sender}" ->'),
    ])
    text = (resp.content or "").strip().lower()
    if "discard" in text:
        return False
    if "keep" in text:
        return True
    print(f"  [verifier] unclear LLM reply for {sender!r}: {text!r} — keeping",
          file=sys.stderr)
    return True


def quick_filter(state: AgentState) -> dict[str, dict]:
    """Step 1. Reads/trims backend/parsed_emails.json in place and returns the
    kept emails (keyed by Gmail message id). Uses `state` only to pick the LLM.
    Does not write the database; does not touch the cursor."""
    if not OUTPUT_PATH.exists():
        print("Verifier: no parsed_emails.json — nothing to verify.")
        return {}

    data = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    emails: dict[str, dict] = data.get("emails", {})

    if not emails:
        print("Verifier: parsed_emails.json has no emails — nothing to verify.")
        return {}

    # Already-verified guard: a no-new-mail incremental run leaves this file
    # untouched, so if we already filtered this exact batch don't redo the LLM
    # work. Same-format UTC ISO strings -> lexical > works.
    if data.get("quick_verified_at", "") > data.get("fetched_at", ""):
        print(f"Verifier: parsed_emails.json already filtered "
              f"({data['quick_verified_at']}) — reusing.")
        return emails

    llm = get_llm(
        provider=state.get("llm_provider"),
        model=state.get("llm_model"),
        temperature=state.get("llm_temperature"),
    )

    kept: dict[str, dict] = {}
    discarded: list[dict] = []
    n_sent = n_category = n_llm_keep = n_llm_discard = n_errors = 0
    first_llm_call = True

    for mid, email in emails.items():
        labels = email.get("label_ids", [])

        if "SENT" in labels:                       # 1. sent mail: always keep
            kept[mid] = email
            n_sent += 1
            continue

        if _JUNK_CATEGORY_LABELS.intersection(labels):   # 2. promo/social/updates
            discarded.append({
                "id": mid, "sender": email.get("sender", ""),
                "subject": email.get("subject", ""), "reason": "category",
            })
            n_category += 1
            continue

        sender = email.get("sender", "")           # 3. ask the LLM about the From
        try:
            keep = _classify_sender(llm, sender)
        except Exception as e:  # noqa: BLE001 — report any LLM failure
            if first_llm_call:
                raise RuntimeError(
                    f"Verifier: LLM call failed ({type(e).__name__}: {e}). "
                    "Is Ollama running and the model pulled (e.g. "
                    "`ollama pull gemma4:e4b`)? parsed_emails.json left untouched."
                ) from e
            print(f"  [verifier] LLM error on {sender!r}: {type(e).__name__}: {e}"
                  " — keeping", file=sys.stderr)
            kept[mid] = email
            n_errors += 1
            first_llm_call = False
            continue
        first_llm_call = False

        if keep:
            kept[mid] = email
            n_llm_keep += 1
        else:
            discarded.append({
                "id": mid, "sender": sender,
                "subject": email.get("subject", ""), "reason": "llm",
            })
            n_llm_discard += 1

    data["emails"] = kept
    data["quick_verified_at"] = datetime.now(timezone.utc).isoformat()
    data["quick_discarded"] = discarded
    OUTPUT_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8",
    )

    print(
        f"Verifier: kept {len(kept)} of {len(emails)} "
        f"({n_sent} sent, {n_category} category-filtered, "
        f"{n_llm_keep} kept / {n_llm_discard} discarded by LLM"
        + (f", {n_errors} LLM errors (kept)" if n_errors else "")
        + ")"
    )
    return kept


# ============================================================================
# STEP 2 — save to the SQLite conversation store
# ============================================================================

# Conversations shorter than this are kept whole (you want full context on a
# newish relationship). Past it, only emails with actual content are appended.
HISTORY_LLM_THRESHOLD = 6

# LLM prompt: does a snippet carry real content, or is it just a pleasantry?
IMPORTANCE_PROMPT = """\
You see a short preview of an email. Decide:
KEEP    - it carries real content: a question, request, decision, plan, fact, or update.
DISCARD - it's only a pleasantry or acknowledgement, no information ("thanks!",
          "got it", "sounds great", "looking forward to it", "see you soon").
When unsure, answer KEEP. Reply with exactly one word: KEEP or DISCARD.

Examples:
"Thanks for the reply!"                                  -> DISCARD
"Sounds great, excited to see you soon."                 -> DISCARD
"Got it, thanks — talk soon."                            -> DISCARD
"Can we move our call to 3pm? I have a conflict at 2."   -> KEEP
"Here's the revised draft — I changed section 2."        -> KEEP
"Quick question: do you want the report as PDF or Word?" -> KEEP"""


def _is_substantive(llm, snippet: str) -> bool:
    """True if `snippet` looks like it carries real content. Empty or
    ambiguous -> True (keep)."""
    snippet = (snippet or "").strip()
    if not snippet:
        return True
    resp = llm.invoke([
        ("system", IMPORTANCE_PROMPT),
        ("human", f'"{snippet}" ->'),
    ])
    text = (resp.content or "").strip().lower()
    if "discard" in text:
        return False
    return True  # "keep", or anything unclear


def _other_party(email: dict, direction: str) -> tuple[str, str]:
    """(address, display_name) of the person on the other end of `email`.
    received -> the From; sent -> the first To, then Cc as fallback. Returns
    ("", "") if no header yields a parseable address. Address is lowercased so
    contact lookups are case-insensitive."""
    if direction == "received":
        raw = [email.get("sender", "")]
    else:  # sent: parse To then Cc; getaddresses keeps that order
        raw = [email.get("to", ""), email.get("cc", "")]
    for name, addr in getaddresses(raw):
        if addr:
            return addr.strip().lower(), name.strip()
    return "", ""


def _iso_from_ms(ms: int) -> str:
    """Gmail internalDate (ms since epoch) -> ISO-8601 UTC string (sorts
    lexically, which is how the DB orders a conversation)."""
    return datetime.fromtimestamp((ms or 0) / 1000, tz=timezone.utc).isoformat()


def save_relevant_to_db(state: AgentState, kept: dict[str, dict]) -> int:
    """Step 2. Write `kept` emails into SQLite oldest-first, building per-contact
    conversation histories. Idempotent (skips Gmail ids already stored). Returns
    the number of rows newly inserted."""
    if not kept:
        return 0

    schema.init_db()                       # CREATE TABLE IF NOT EXISTS — safe every run
    conn = schema.get_connection()

    # Oldest -> newest: the >6 threshold needs each conversation's length to be
    # up to date when we reach the next email in it.
    ordered = sorted(kept.values(), key=lambda e: e.get("internal_date_ms", 0))

    llm = None                             # built lazily — only long threads need it
    importance_llm_broken = False
    saved = n_new_convo = n_appended = n_filler_skipped = 0

    try:
        for email in ordered:
            gid = email["id"]
            if queries.email_exists(conn, gid):
                continue                   # already stored; it still counts toward length

            direction = "sent" if "SENT" in email.get("label_ids", []) else "received"
            addr, name = _other_party(email, direction)
            if not addr:
                print(f"  [verifier/db] no conversation partner for {gid} — skipping",
                      file=sys.stderr)
                continue

            contact_id = queries.resolve_or_create_contact(conn, addr, name)
            prior = queries.count_emails_for_contact(conn, contact_id)

            should_save = True
            if prior == 0:
                n_new_convo += 1
            elif prior > HISTORY_LLM_THRESHOLD and not importance_llm_broken:
                if llm is None:
                    llm = get_llm(
                        provider=state.get("llm_provider"),
                        model=state.get("llm_model"),
                        temperature=state.get("llm_temperature"),
                    )
                try:
                    should_save = _is_substantive(llm, email.get("snippet", ""))
                except Exception as e:  # noqa: BLE001
                    print(f"  [verifier/db] importance LLM failed "
                          f"({type(e).__name__}: {e}) — keeping this and all later "
                          "ones without checking", file=sys.stderr)
                    importance_llm_broken = True
                    should_save = True
                if should_save:
                    n_appended += 1
                else:
                    n_filler_skipped += 1
            else:                          # short thread, or long but LLM unavailable
                n_appended += 1

            if not should_save:
                continue

            queries.insert_email(
                conn,
                contact_id=contact_id,
                direction=direction,
                subject=email.get("subject"),
                body=email.get("plain_text") or email.get("snippet") or None,
                timestamp=_iso_from_ms(email.get("internal_date_ms", 0)),
                gmail_message_id=gid,
            )
            saved += 1
        conn.commit()
    finally:
        conn.close()

    print(
        f"Verifier/DB: saved {saved} of {len(kept)} kept "
        f"({n_new_convo} new conversations, {n_appended} appended, "
        f"{n_filler_skipped} skipped as filler) -> {schema.DB_PATH}"
    )
    return saved


# ============================================================================
# Orchestration
# ============================================================================

def _mark_db_saved(saved_count: int) -> None:
    """After step 2, clear parsed_emails.json's `emails` (it's been consumed)
    while keeping the run's audit trail — quick_discarded, the counts, etc."""
    if not OUTPUT_PATH.exists():
        return
    data = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    data["emails"] = {}
    data["db_saved_at"] = datetime.now(timezone.utc).isoformat()
    data["db_saved_count"] = saved_count
    OUTPUT_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8",
    )


def run(state: AgentState) -> None:
    """The verifier: quick filter (step 1) then DB save (step 2), then clear
    the consumed batch from parsed_emails.json."""
    kept = quick_filter(state)
    if not kept:
        return
    saved = save_relevant_to_db(state, kept)
    _mark_db_saved(saved)
