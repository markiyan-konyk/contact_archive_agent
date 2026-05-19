"""
Mail-parse node.

Responsibility: turn a Gmail mailbox into parsed-email dicts on disk. Nothing
more. It does NOT do OAuth (the service is handed in by the caller -- in the
future, by backend/client.py) and it does NOT decide which emails are
"relevant" (that is the verifier node's job; this node feeds it raw material).

Outputs (both live in backend/):

    parsed_emails.json     transient handoff to the verifier; overwritten on
                           every run; the verifier is expected to consume and
                           clear it.
    mailparse_state.json   persistent cursor -- the internalDate of the newest
                           email we have ever parsed. Survives the verifier
                           clearing parsed_emails.json.

"Mailbox" here = INBOX + SENT (not Spam, not Trash). Sent mail matters: it's
the user's own writing, the verifier always keeps it as a relationship-tone
sample.

Two modes for the FIRST-run backfill, chosen interactively in the terminal:

    number   ->  fetch the last N messages (inbox + sent)
    date     ->  fetch every message (inbox + sent) on or after yyyy-mm-dd
    esc      ->  do nothing, exit

On every subsequent run the prompt is skipped: we read the cursor and pull
everything Gmail received after it. Delete mailparse_state.json to force a
fresh first-run prompt.

Later this same module will be driven by a timer instead of being called once
per `python -m backend.main`. The parsing helpers stay the same; only the
trigger changes.
"""

from __future__ import annotations

import base64
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from googleapiclient.discovery import Resource

# Both files live in backend/ (one level above this file).
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "parsed_emails.json"
STATE_PATH = Path(__file__).resolve().parent.parent / "mailparse_state.json"


class FirstRunNeeded(Exception):
    """Raised by `ingest()` when this is a first run (no cursor yet) and the
    caller didn't supply a backfill window. A non-interactive front end (the
    CLI) catches this, asks the user how far back to go, then calls `ingest()`
    again with `backfill=`."""


# ============================================================================
# Parsed-email shape (what we hand to the verifier)
# ============================================================================

@dataclass
class ParsedEmail:
    id: str
    thread_id: str
    label_ids: list[str]
    snippet: str
    sender: str
    to: str
    cc: str
    subject: str
    date: str                # raw RFC 2822 date header
    internal_date_ms: int    # Gmail's own receive timestamp
    plain_text: str
    html: str
    attachments: list[dict] = field(default_factory=list)


# ============================================================================
# MIME / header helpers (same shape as the test/ learning code, trimmed)
# ============================================================================

def _header(headers: list[dict], name: str, default: str = "") -> str:
    """Case-insensitive header lookup. headers is a list of {name,value} dicts."""
    target = name.lower()
    for h in headers:
        if h["name"].lower() == target:
            return h["value"]
    return default


def _walk(payload: dict) -> Iterator[dict]:
    """Pre-order traversal of the MIME tree."""
    yield payload
    for child in payload.get("parts") or []:
        yield from _walk(child)


def _decode_body(part: dict) -> bytes:
    data = part.get("body", {}).get("data")
    if not data:
        return b""
    # Gmail uses URL-safe base64; pad defensively before decoding.
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _charset(part: dict) -> str:
    ct = _header(part.get("headers", []), "Content-Type", "")
    for token in ct.split(";"):
        token = token.strip()
        if token.lower().startswith("charset="):
            return token.split("=", 1)[1].strip().strip('"').strip("'")
    return "utf-8"


def _decode_text(part: dict) -> str:
    raw = _decode_body(part)
    if not raw:
        return ""
    try:
        return raw.decode(_charset(part), errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def parse_message(service: Resource, message_id: str) -> ParsedEmail:
    """Fetch one message in `full` form and flatten its MIME tree."""
    msg = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )
    return _parse_payload(msg)


def _parse_payload(msg: dict) -> ParsedEmail:
    """Flatten a `messages.get(format='full')` response dict into a ParsedEmail.

    Split out from parse_message so the batched fetch (_fetch_and_parse) can
    feed it the dicts that come back in a batch response, without a separate
    .get() per message.
    """
    payload = msg["payload"]
    headers = payload.get("headers", [])

    plain_chunks: list[str] = []
    html_chunks: list[str] = []
    attachments: list[dict] = []

    for part in _walk(payload):
        mime = part.get("mimeType", "")
        body = part.get("body", {})

        if mime.startswith("multipart/"):
            continue

        # Binary leaf: body lives behind attachmentId, not body.data.
        if body.get("attachmentId"):
            attachments.append({
                "filename": part.get("filename", ""),
                "mime_type": mime,
                "size": body.get("size", 0),
                "attachment_id": body["attachmentId"],
            })
            continue

        if mime == "text/plain":
            plain_chunks.append(_decode_text(part))
        elif mime == "text/html":
            html_chunks.append(_decode_text(part))

    return ParsedEmail(
        id=msg["id"],
        thread_id=msg["threadId"],
        label_ids=msg.get("labelIds", []),
        snippet=msg.get("snippet", ""),
        sender=_header(headers, "From"),
        to=_header(headers, "To"),
        cc=_header(headers, "Cc"),
        subject=_header(headers, "Subject"),
        date=_header(headers, "Date"),
        internal_date_ms=int(msg.get("internalDate", "0")),
        plain_text="\n".join(plain_chunks).strip(),
        html="\n".join(html_chunks).strip(),
        attachments=attachments,
    )


# ============================================================================
# ID listing -- the "what should I parse?" step
# ============================================================================

def _iter_ids(service: Resource, query: str, *, max_total: int | None) -> Iterator[str]:
    """
    Page through users.messages.list and yield message IDs.

    Caller decides when to stop via max_total -- important because a date
    filter on a 10-year-old mailbox could return tens of thousands of IDs.
    """
    seen = 0
    page_token: str | None = None
    while True:
        resp = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=100,
                pageToken=page_token,
            )
            .execute()
        )
        for ref in resp.get("messages", []):
            yield ref["id"]
            seen += 1
            if max_total is not None and seen >= max_total:
                return
        page_token = resp.get("nextPageToken")
        if not page_token:
            return


# Batched fetching. A `messages.get` per email is one HTTP round-trip each,
# which is the slow part of a backfill. Batch HTTP lets us bundle many gets
# into one request: Google replies with all of them at once. We chunk biggest-
# first -- as many full 50s as we can, then 20s for the remainder, then 1s --
# so e.g. 476 ids -> nine 50s + one 20 + six 1s (16 requests instead of 476).
# Just a for-loop over chunk sizes with // and %, really.
_BATCH_SIZES: tuple[int, ...] = (50, 20, 1)


def _chunked(items: list[str], sizes: tuple[int, ...] = _BATCH_SIZES) -> Iterator[list[str]]:
    """Yield consecutive slices of `items`: as many length-`sizes[0]` slices as
    fit, then length-`sizes[1]`, and so on. With 1 as the last size every list
    is fully consumed."""
    i, n = 0, len(items)
    for size in sizes:
        while n - i >= size:
            yield items[i:i + size]
            i += size
    if i < n:                      # only reachable if `sizes` doesn't end in 1
        yield items[i:]


def _fetch_and_parse(service: Resource, message_ids: list[str]) -> dict[str, dict]:
    """
    Fetch every message id (in batched HTTP requests, see _BATCH_SIZES) and
    parse it. A single message that fails to fetch or parse is logged and
    skipped; a batch-level failure (auth expired, network down) propagates.
    """
    parsed: dict[str, dict] = {}
    total = len(message_ids)
    if not total:
        return parsed

    def _collect(request_id: str, response: dict, exception) -> None:
        # Called once per sub-request. request_id is the Gmail message id we
        # passed to .add(); response is the parsed get() result; exception is
        # an HttpError if that one sub-request failed.
        if exception is not None:
            print(f"  [skip] {request_id}: {type(exception).__name__}: {exception}",
                  file=sys.stderr)
            return
        try:
            email = _parse_payload(response)
        except Exception as e:  # noqa: BLE001 — one bad message shouldn't kill the run
            print(f"  [skip] {request_id}: parse failed: {type(e).__name__}: {e}",
                  file=sys.stderr)
            return
        parsed[email.id] = asdict(email)

    done = 0
    for chunk in _chunked(message_ids):
        batch = service.new_batch_http_request(callback=_collect)
        for mid in chunk:
            batch.add(
                service.users().messages().get(userId="me", id=mid, format="full"),
                request_id=mid,
            )
        batch.execute()
        done += len(chunk)
        print(f"  ... fetched {done}/{total} ({len(parsed)} parsed)")

    failed = total - len(parsed)
    print(f"  done: {total} fetched, {len(parsed)} parsed"
          + (f", {failed} failed" if failed else ""))
    return parsed


# ============================================================================
# Mode runners
# ============================================================================

# INBOX + SENT, excluding Spam and Trash. (in:sent is never in spam; -in:trash
# keeps deleted mail out of both. INBOX already excludes spam.)
_MAILBOX_QUERY = "(in:inbox OR in:sent) -in:trash"


def _list_then_fetch(service: Resource, query: str, *, max_total: int | None) -> dict[str, dict]:
    """List ids matching `query` (cheap), then batch-fetch and parse them."""
    ids = list(_iter_ids(service, query, max_total=max_total))
    print(f"  {len(ids)} message(s) to fetch")
    return _fetch_and_parse(service, ids)


def parse_by_count(service: Resource, n: int) -> dict[str, dict]:
    print(f"Fetching the last {n} messages (inbox + sent)...")
    return _list_then_fetch(service, _MAILBOX_QUERY, max_total=n)


def parse_since_date(service: Resource, iso_date: str) -> dict[str, dict]:
    # Gmail's search operator uses slashes, not dashes.
    gmail_date = iso_date.replace("-", "/")
    query = f"{_MAILBOX_QUERY} after:{gmail_date}"
    print(f"Fetching all messages (inbox + sent) since {iso_date}...")
    return _list_then_fetch(service, query, max_total=None)


def parse_since_unix(
    service: Resource, unix_seconds: int, *, max_total: int | None = None
) -> dict[str, dict]:
    """
    Fetch every message (inbox + sent, no spam/trash) received after a unix
    timestamp.

    Gmail's `after:` accepts a raw unix-seconds integer in addition to the
    YYYY/MM/DD form -- second granularity is exactly what we want for
    incremental polling so we don't re-fetch a whole day each run.

    `max_total` caps the fetch to the newest N messages (Gmail lists
    newest-first) -- used by the CLI's "long-gap catch-up" cap so a months-long
    absence doesn't pull thousands of emails at once.
    """
    query = f"{_MAILBOX_QUERY} after:{unix_seconds}"
    pretty = datetime.fromtimestamp(unix_seconds, tz=timezone.utc).isoformat()
    print(f"Fetching new messages (inbox + sent) since {pretty} (unix={unix_seconds})...")
    return _list_then_fetch(service, query, max_total=max_total)


# ============================================================================
# Persistence
# ============================================================================

def save(emails: dict[str, dict], *, mode: str, param: str) -> None:
    """
    Write parsed_emails.json AND advance the cursor in mailparse_state.json.

    parsed_emails.json format:

        {
          "fetched_at": ISO-8601 UTC,
          "mode":       "number" | "date" | "incremental",
          "param":      "50" | "2026-01-01" | "<unix_seconds>",
          "emails":     {message_id: {...}, ...}
        }

    The verifier will iterate over .emails.values() and decide relevance.
    """
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "param": param,
        "emails": emails,
    }
    OUTPUT_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Wrote {len(emails)} emails -> {OUTPUT_PATH}")

    # Advance the cursor based on what we just parsed. Done here -- not at
    # call sites -- so it's impossible to save a batch and forget the cursor.
    state = _load_state() or {}
    _advance_cursor(state, emails)
    _save_state(state)


def _load_state() -> dict | None:
    """Return the cursor state, or None if no state file exists yet."""
    if not STATE_PATH.exists():
        return None
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _advance_cursor(state: dict, emails: dict[str, dict]) -> None:
    """
    Update `state` in place to reflect the newest email we just parsed.

    If `emails` is empty, only last_run_at is touched -- we mustn't advance
    the cursor on an empty run, otherwise a hiccup would silently skip a
    window of mail that *will* show up on the next poll.
    """
    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    if not emails:
        return
    newest_ms = max(e["internal_date_ms"] for e in emails.values())
    state["last_parsed_internal_date_ms"] = newest_ms
    state["last_parsed_at"] = datetime.fromtimestamp(
        newest_ms / 1000, tz=timezone.utc
    ).isoformat()


# ============================================================================
# Terminal prompt (debug-mode UX)
# ============================================================================

def _prompt_number() -> int | None:
    while True:
        raw = input("How many emails? (positive integer, or 'esc' to cancel) > ").strip()
        if raw.lower() == "esc":
            return None
        try:
            n = int(raw)
            if n > 0:
                return n
        except ValueError:
            pass
        print("  not a positive integer, try again")


def _prompt_date() -> str | None:
    while True:
        raw = input("Since when? (yyyy-mm-dd, or 'esc' to cancel) > ").strip()
        if raw.lower() == "esc":
            return None
        try:
            datetime.strptime(raw, "%Y-%m-%d")
            return raw
        except ValueError:
            print("  not a valid yyyy-mm-dd date, try again")


def _prompt_mode() -> str | None:
    """Return 'number', 'date', or None for esc."""
    while True:
        raw = input("Choose: 'number', 'date', or 'esc' > ").strip().lower()
        if raw in ("number", "date"):
            return raw
        if raw == "esc":
            return None
        print("  unrecognized; type 'number', 'date', or 'esc'")


# ============================================================================
# Entry point called by main.py
# ============================================================================

def run(service: Resource) -> None:
    """
    Drive the node once.

    Branches on mailparse_state.json:
      - missing  -> first run; prompt the user for a backfill window.
      - present  -> incremental run; pull everything received after the
                    cursor and advance it.
    """
    state = _load_state()

    if state is None:
        _run_first_time(service)
    else:
        _run_incremental(service, state)


def ingest(
    service: Resource,
    *,
    backfill: tuple[str, str] | None = None,
    incremental_cap: int | None = None,
) -> None:
    """Non-interactive driver for front ends that supply their own UI (the
    CLI) instead of the terminal prompts in `run()`.

      - First run (no cursor yet):
            backfill is None         -> raise FirstRunNeeded (ask the user)
            backfill ("number","50") -> fetch the last 50 messages
            backfill ("date","Y-M-D")-> fetch everything since that date
      - Subsequent runs: incremental from the cursor. `incremental_cap` (if
        set) keeps only the newest N messages — the CLI's "I haven't opened
        this in months, don't pull thousands" safety valve.
    """
    state = _load_state()
    first_run = state is None or state.get("last_parsed_internal_date_ms") is None

    if first_run:
        if backfill is None:
            raise FirstRunNeeded
        mode, param = backfill
        if mode == "number":
            emails = parse_by_count(service, int(param))
            save(emails, mode="number", param=str(param))
        elif mode == "date":
            emails = parse_since_date(service, param)
            save(emails, mode="date", param=param)
        else:
            raise ValueError(f"backfill mode must be 'number' or 'date', got {mode!r}")
        return

    _run_incremental(service, state, max_total=incremental_cap)


def _run_first_time(service: Resource) -> None:
    print()
    print("=" * 60)
    print("First run detected. Backfill needed.")
    print("=" * 60)
    print("  number  - parse the last N messages (inbox + sent)")
    print("  date    - parse every message since yyyy-mm-dd (inbox + sent)")
    print("  esc     - skip and exit")
    print()

    mode = _prompt_mode()
    if mode is None:
        print("Skipped. No backfill performed.")
        return

    if mode == "number":
        n = _prompt_number()
        if n is None:
            print("Cancelled.")
            return
        emails = parse_by_count(service, n)
        save(emails, mode="number", param=str(n))
    else:  # mode == "date"
        d = _prompt_date()
        if d is None:
            print("Cancelled.")
            return
        emails = parse_since_date(service, d)
        save(emails, mode="date", param=d)


def _run_incremental(
    service: Resource, state: dict, *, max_total: int | None = None
) -> None:
    last_ms = state.get("last_parsed_internal_date_ms")
    if last_ms is None:
        # State file exists but no cursor was ever recorded (e.g. the
        # first-run prompt was cancelled). Treat this like a first run.
        print("State file present but cursor is empty -- prompting for backfill.")
        _run_first_time(service)
        return

    # Gmail's `after:<unix>` operator is DAY-granularity even when given a
    # precise timestamp -- it rounds down to the start of that calendar day.
    # So the server-side query is just a cheap narrowing; the cursor itself
    # is enforced client-side below.
    cursor_seconds = last_ms // 1000
    print(f"Incremental run. Last parsed: {state.get('last_parsed_at', '?')}")

    raw = parse_since_unix(service, cursor_seconds, max_total=max_total)

    # Drop anything at or before the cursor. internal_date_ms is Gmail's
    # own per-message timestamp, so a strict `>` comparison is reliable.
    emails = {
        mid: e for mid, e in raw.items()
        if e["internal_date_ms"] > last_ms
    }
    dropped = len(raw) - len(emails)
    if dropped:
        print(f"  filtered out {dropped} already-parsed message(s) "
              "(Gmail's after: operator is day-granularity)")

    if not emails:
        print("No new emails since last run.")
        # Still record that we ran -- useful for debugging poll cadence.
        _advance_cursor(state, {})
        _save_state(state)
        return

    save(emails, mode="incremental", param=str(cursor_seconds))
