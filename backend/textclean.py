"""
Text cleanup for displaying email bodies.

Right now this is used display-only, by backend/cli.py: stored email bodies
keep their full quoted history (so the UI can render it however it wants
later), but the terminal preview shows just the new text. Ported from the
test/ sandbox (test/clean_body.py) so the backend doesn't depend on test/.

`strip_quotes_and_signature` is a heuristic, not perfect — Mailgun's `talon`
is the production-grade option if this ever needs to be airtight. It only ever
*under*-strips (leaves some quoted text), never deletes new content above a
marker, so the worst case for a preview is "a bit noisy".
"""

from __future__ import annotations

import re

# Each pattern marks where quoted history begins.
_REPLY_MARKERS = [
    # "On Mon, May 6, 2026 at 10:14 AM, Alice <a@x.com> wrote:" — Gmail style.
    re.compile(r"^On .+ wrote:\s*$", re.MULTILINE),
    # Outlook / older clients.
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.MULTILINE | re.IGNORECASE),
    # "From: ... Sent: ... To: ... Subject: ..." block Outlook prepends.
    re.compile(r"^From:\s.+\nSent:\s.+\nTo:\s.+\nSubject:\s.+", re.MULTILINE),
]

_SIGNATURE_MARKERS = [
    # RFC 3676 signature separator: a line that is exactly "-- " (clients often
    # drop the trailing space).
    re.compile(r"^-- ?\s*$", re.MULTILINE),
    re.compile(r"^Sent from my (iPhone|iPad|Android|BlackBerry).*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^Get Outlook for (iOS|Android).*$", re.MULTILINE | re.IGNORECASE),
]

_BOTTOM_QUOTE_BLOCK = re.compile(r"(?:^>.*(?:\n|$))+", re.MULTILINE)
_MULTI_BLANK = re.compile(r"\n{3,}")


def strip_quotes_and_signature(body: str | None) -> str:
    """Return `body` with the quoted-reply tail and trailing signature removed.

    Cuts at the earliest marker found, keeping only the text before it — so it
    never eats new content that sits above a quote. May return "" if the body
    is nothing but a quote (a bare bottom-posted reply); callers should fall
    back to the raw body in that case.
    """
    if not body:
        return ""

    cut = len(body)
    for pattern in _REPLY_MARKERS + _SIGNATURE_MARKERS:
        m = pattern.search(body)
        if m and m.start() < cut:
            cut = m.start()

    # A run of '>'-quoted lines, but only cut on it if it starts past the first
    # quarter — otherwise we'd risk eating an inline quote the reply is about.
    m = _BOTTOM_QUOTE_BLOCK.search(body)
    if m and m.start() > len(body) // 4 and m.start() < cut:
        cut = m.start()

    cleaned = body[:cut].rstrip()
    return _MULTI_BLANK.sub("\n\n", cleaned)
