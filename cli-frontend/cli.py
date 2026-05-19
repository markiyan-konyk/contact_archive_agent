"""
Your Email Agent — terminal UI.

Run it:  python cli.py     (from this folder)

On launch it behaves like `python -m backend.main`: first run asks how far back
to backfill, later runs pull mail incrementally, then it drops you into a
writing screen. Borrowed look from coding agents: a rounded box titled "Your
Email Agent" with the conversation above and a command/prompt line at the
bottom.

Two command families, both typed on the bottom line:

  :  view/admin commands (use these on an empty prompt line)
       :emails              toggle the list of every email address
       :contacts            toggle the list of contacts (people)
       :config              edit the general writing-style prompt
       :addcon <name>       (in :emails) put the hovered address under <name>
       :prompt              (in :contacts) edit the hovered contact's prompt
       :exit                quit

  /  writing-mode tools
       /to mail <email>     write to whoever owns this address (uses history)
       /to contact <name>   write to this contact (history across ALL their
                             addresses)
       /copy                copy the last drafted email to the clipboard
       /new                 start a fresh conversation

Anything else typed on the prompt line in writing mode is sent to the agent and
it drafts an email.

Navigation in list/history screens: ↑/↓ or mouse wheel to move, Enter to open,
Esc to go back. In the prompt/config editors, Esc saves & closes.

This file lives in cli-frontend/ so it can later be split into its own repo; it
only depends on the sibling `backend/` package, which it adds to sys.path
below. When it becomes a standalone repo, replace that shim with an installed
dependency.
"""

from __future__ import annotations

import io
import sys
from enum import Enum, auto
from pathlib import Path

# --- make the sibling backend/ importable (temporary; see module docstring) --
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from prompt_toolkit import Application
from prompt_toolkit.application import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    BufferControl,
    ConditionalContainer,
    FormattedTextControl,
    HSplit,
    Layout,
    Window,
)
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.styles import Style
from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

from backend.client import get_service
from backend.database import queries, schema
from backend.node import mailparse, verifier, writer
from backend.node.mailparse import FirstRunNeeded
from backend.state import AgentState, initial_state

# ===========================================================================
# Long-gap catch-up cap.
#
# By default an incremental run pulls EVERY email since you last opened the
# app. If you might not open it for months and don't want thousands of emails
# fetched at once, uncomment the line below (and tune the number) — only the
# newest N messages since the cursor are then fetched.
# ===========================================================================
INCREMENTAL_MAX: int | None = None
# INCREMENTAL_MAX = 5      # <-- uncomment to cap a long-gap catch-up


class Mode(Enum):
    WRITING = auto()
    EMAILS = auto()
    CONTACTS = auto()
    ADDRESSES = auto()      # one contact's addresses
    HISTORY = auto()        # a conversation
    PROMPT_EDITOR = auto()  # editing a contact's notes/prompt
    CONFIG_EDITOR = auto()  # editing the general prompt


class UI:
    """All mutable UI state. One instance for the whole session."""

    def __init__(self, state: AgentState) -> None:
        self.state = state
        self.mode = Mode.WRITING
        self.status = "Writing mode. Type an instruction, or :emails / :contacts / :config."
        # list views
        self.rows: list = []          # current list rows
        self.sel = 0                  # selected index
        self.scroll = 0               # top visible row
        # drill-down context
        self.contact_id: int | None = None      # for ADDRESSES/HISTORY
        self.contact_label = ""                  # header for HISTORY
        self.history: list = []
        self.prev_mode = Mode.WRITING            # where Esc returns to
        # editor target
        self.edit_contact_id: int | None = None
        self.busy = False             # writer call in flight


# ---------------------------------------------------------------------------
# Rich rendering — the main area is rendered by Rich, shown via ANSI()
# ---------------------------------------------------------------------------

_GREY = "grey50"


def _term_size() -> tuple[int, int]:
    try:
        size = get_app().output.get_size()
        return max(40, size.columns), max(10, size.rows)
    except Exception:  # noqa: BLE001 — before the app is running
        return 100, 30


def _render(renderable, width: int, height: int) -> str:
    """Render any Rich renderable to an ANSI string at a fixed size."""
    buf = io.StringIO()
    Console(file=buf, width=width, height=height, color_system="truecolor",
            force_terminal=True, legacy_windows=False).print(renderable)
    return buf.getvalue()


def _selected_to_label(ui: UI) -> str:
    st = ui.state
    cid = st.get("selected_contact_id")
    if cid is not None:
        conn = schema.get_connection()
        try:
            c = queries.get_contact(conn, cid)
            addrs = [r["email"] for r in queries.get_addresses_for_contact(conn, cid)]
        finally:
            conn.close()
        name = (c["name"] if c and c["name"] else "") or "(unnamed)"
        return f"{name} <{', '.join(addrs)}>" if addrs else name
    if st.get("selected_contact_email"):
        return st["selected_contact_email"]
    return "— no recipient selected —"


def _writing_body(ui: UI, height: int) -> Group:
    msgs = ui.state.get("messages") or []
    lines: list[Text] = []
    if not msgs:
        lines.append(Text("Type what you want to say and the agent drafts the "
                           "email.", style="grey62"))
        lines.append(Text(""))
        lines.append(Text("Tip: /to contact <name> first to pull that "
                           "relationship's history.", style="grey50"))
    for m in msgs:
        role = getattr(m, "type", "")
        content = getattr(m, "content", "")
        if role == "human":
            lines.append(Text(f"› {content}", style="bold cyan"))
        else:
            lines.append(Text("── draft " + "─" * 40, style="grey42"))
            for ln in str(content).splitlines() or [""]:
                lines.append(Text(ln, style="white"))
            lines.append(Text("─" * 49, style="grey42"))
        lines.append(Text(""))
    if ui.busy:
        lines.append(Text("✶ writing…", style="yellow"))
    # keep the tail visible
    body = lines[-(height):] if len(lines) > height else lines
    return Group(*body)


def _list_body(ui: UI, height: int) -> Group:
    """Render ui.rows with a selection cursor and a scroll window."""
    visible = max(1, height)
    if ui.sel < ui.scroll:
        ui.scroll = ui.sel
    elif ui.sel >= ui.scroll + visible:
        ui.scroll = ui.sel - visible + 1
    out: list[Text] = []
    if not ui.rows:
        return Group(Text("(nothing here yet)", style="grey50"))
    for i in range(ui.scroll, min(len(ui.rows), ui.scroll + visible)):
        r = ui.rows[i]
        cursor = "❯ " if i == ui.sel else "  "
        style = "reverse" if i == ui.sel else ""
        if ui.mode == Mode.EMAILS:
            owner = r["contact_name"] or "(unnamed)"
            txt = f"{r['email']:<38.38}  {owner:<22.22}  {r['email_count']:>3} msgs"
        elif ui.mode == Mode.CONTACTS:
            nm = r["name"] or "(unnamed)"
            txt = (f"{nm:<26.26}  {r['address_count']:>2} addr  "
                   f"{r['email_count']:>3} msgs"
                   + ("   ✎ has prompt" if r["notes"] else ""))
        else:  # ADDRESSES
            txt = f"{r['email']:<40.40}  {r['display_name'] or ''}"
        out.append(Text(cursor + txt, style=style))
    return Group(*out)


def _history_body(ui: UI, height: int) -> Group:
    rows = ui.history
    rendered: list[Text] = []
    for m in rows:
        when = (m["timestamp"] or "")[:16].replace("T", " ")
        tag = "SENT" if m["direction"] == "sent" else "RECEIVED"
        tag_style = "green" if m["direction"] == "sent" else "magenta"
        rendered.append(Text.assemble(
            (f"{tag:<8}", tag_style), ("  ", ""), (when, "grey62"),
            ("  ", ""), (m["subject"] or "(no subject)", "bold")))
        for ln in str(m["body"] or "").splitlines():
            rendered.append(Text("    " + ln, style="grey78"))
        rendered.append(Text(""))
    if not rendered:
        rendered = [Text("(no emails in this conversation)", style="grey50")]
    total = len(rendered)
    visible = max(1, height)
    ui.scroll = max(0, min(ui.scroll, max(0, total - visible)))
    return Group(*rendered[ui.scroll:ui.scroll + visible])


def get_main_text():
    """prompt_toolkit pulls this every render."""
    ui = _UI
    width, rows = _term_size()
    main_h = max(6, rows - 2)          # 1 prompt line + 1 footer line
    inner_h = main_h - 4               # panel border (2) + header line (2)

    header = Text.assemble(("to: ", "grey50"), (_selected_to_label(ui), "bold white"))

    if ui.mode == Mode.WRITING:
        title, body = "Your Email Agent", _writing_body(ui, inner_h - 1)
    elif ui.mode in (Mode.EMAILS, Mode.CONTACTS, Mode.ADDRESSES):
        names = {Mode.EMAILS: "Email addresses", Mode.CONTACTS: "Contacts",
                 Mode.ADDRESSES: f"Addresses · {ui.contact_label}"}
        title = f"Your Email Agent · {names[ui.mode]}"
        body = _list_body(ui, inner_h - 1)
    else:  # HISTORY
        title = f"Your Email Agent · {ui.contact_label}"
        body = _history_body(ui, inner_h - 1)

    inner = Group(header, Text(""), body)
    panel = Panel(inner, box=box.ROUNDED, border_style=_GREY,
                  title=f"[white]{title}[/]", title_align="center",
                  height=main_h, padding=(0, 1))
    return ANSI(_render(panel, width, main_h).rstrip("\n"))


def get_footer_text():
    ui = _UI
    hints = {
        Mode.WRITING: "Enter: send  ·  :emails :contacts :config  ·  /to /copy /new  ·  :exit",
        Mode.EMAILS: "↑↓/wheel move · Enter open history · :addcon <name> · :emails/Esc back",
        Mode.CONTACTS: "↑↓/wheel move · Enter addresses · :prompt edit · :contacts/Esc back",
        Mode.ADDRESSES: "↑↓/wheel move · Enter open history · Esc back to contacts",
        Mode.HISTORY: "↑↓/wheel scroll · Esc back",
        Mode.PROMPT_EDITOR: "Editing contact prompt — Esc to save & close",
        Mode.CONFIG_EDITOR: "Editing general config prompt — Esc to save & close",
    }
    return ANSI(_render(Text(" " + ui.status + "\n " + hints[ui.mode],
                              style="grey62"), _term_size()[0], 2).rstrip("\n"))


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------

def _load_list(ui: UI) -> None:
    conn = schema.get_connection()
    try:
        if ui.mode == Mode.EMAILS:
            ui.rows = list(queries.list_addresses(conn))
        elif ui.mode == Mode.CONTACTS:
            ui.rows = list(queries.list_contacts(conn))
        elif ui.mode == Mode.ADDRESSES:
            ui.rows = list(queries.get_addresses_for_contact(conn, ui.contact_id))
    finally:
        conn.close()
    ui.sel = 0
    ui.scroll = 0


def _open_history(ui: UI, contact_id: int, label: str, came_from: Mode) -> None:
    conn = schema.get_connection()
    try:
        ui.history = list(queries.get_conversation_by_contact_id(conn, contact_id))
    finally:
        conn.close()
    ui.contact_id = contact_id
    ui.contact_label = label
    ui.prev_mode = came_from
    ui.mode = Mode.HISTORY
    ui.scroll = 0


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------

def _do_writer(ui: UI, text: str) -> None:
    ui.busy = True
    ui.status = "writing…"
    app = get_app()

    async def run() -> None:
        import asyncio
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, writer.respond, ui.state, text)
            ui.status = "draft ready. /copy to copy it, or refine it."
        except Exception as e:  # noqa: BLE001
            ui.status = f"LLM error: {type(e).__name__}: {e} (is Ollama running?)"
        finally:
            ui.busy = False
            app.invalidate()

    app.create_background_task(run())


def _select(ui: UI, *, email: str | None = None, name: str | None = None) -> None:
    conn = schema.get_connection()
    try:
        if email is not None:
            cid = queries.get_contact_id_by_email(conn, email)
        else:
            row = queries.get_contact_by_name(conn, name or "")
            cid = int(row["id"]) if row else None
        n = (len(queries.get_conversation_by_contact_id(conn, cid))
             if cid is not None else 0)
    finally:
        conn.close()
    ui.state["selected_contact_id"] = cid
    ui.state["selected_contact_email"] = email
    who = email or name
    ui.status = (f"now writing to {who} ({n} msgs of history)."
                 if cid is not None else
                 f"{who} not in DB — will write fresh (no history).")


def _enter_editor(ui: UI, mode: Mode) -> None:
    conn = schema.get_connection()
    try:
        if mode == Mode.PROMPT_EDITOR:
            text = queries.get_contact_notes(conn, ui.edit_contact_id) or ""
        else:
            text = queries.get_general_prompt(conn) or ""
    finally:
        conn.close()
    ui.prev_mode = ui.mode
    ui.mode = mode
    _EDITOR.text = text
    _EDITOR.cursor_position = len(text)
    get_app().layout.focus(_editor_window)


def _save_editor(ui: UI) -> None:
    conn = schema.get_connection()
    try:
        if ui.mode == Mode.PROMPT_EDITOR:
            queries.set_contact_notes(conn, ui.edit_contact_id, _EDITOR.text.strip())
            ui.status = "contact prompt saved."
        else:
            queries.set_general_prompt(conn, _EDITOR.text.strip())
            ui.status = "general config saved."
        conn.commit()
    finally:
        conn.close()
    ui.mode = ui.prev_mode if ui.prev_mode != Mode.HISTORY else Mode.CONTACTS
    if ui.mode in (Mode.CONTACTS, Mode.EMAILS, Mode.ADDRESSES):
        _load_list(ui)
    get_app().layout.focus(_cmd_window)


def _dispatch(ui: UI, text: str) -> None:
    text = text.strip()
    if not text:
        # Empty Enter in a list = "open the selected row".
        if ui.mode in (Mode.EMAILS, Mode.CONTACTS, Mode.ADDRESSES):
            _open_selected(ui)
        return

    if text.startswith(":"):
        parts = text[1:].split(maxsplit=1)
        cmd = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "exit":
            get_app().exit()
        elif cmd == "emails":
            if ui.mode == Mode.EMAILS:
                ui.mode = Mode.WRITING
                ui.status = "back to writing."
            else:
                ui.mode = Mode.EMAILS
                _load_list(ui)
                ui.status = "all email addresses. ↑↓ to move, Enter to open."
        elif cmd == "contacts":
            if ui.mode == Mode.CONTACTS:
                ui.mode = Mode.WRITING
                ui.status = "back to writing."
            else:
                ui.mode = Mode.CONTACTS
                _load_list(ui)
                ui.status = "contacts. ↑↓ to move, Enter for addresses."
        elif cmd == "config":
            _enter_editor(ui, Mode.CONFIG_EDITOR)
        elif cmd == "addcon":
            if ui.mode != Mode.EMAILS or not ui.rows:
                ui.status = "use :addcon <name> while hovering an address in :emails."
            elif not arg:
                ui.status = "usage: :addcon <contact name>"
            else:
                email = ui.rows[ui.sel]["email"]
                conn = schema.get_connection()
                try:
                    queries.assign_email_to_contact(conn, email, arg)
                    conn.commit()
                finally:
                    conn.close()
                _load_list(ui)
                ui.status = f"{email} grouped under “{arg}”."
        elif cmd == "prompt":
            if ui.mode != Mode.CONTACTS or not ui.rows:
                ui.status = "use :prompt while hovering a contact in :contacts."
            else:
                ui.edit_contact_id = ui.rows[ui.sel]["id"]
                _enter_editor(ui, Mode.PROMPT_EDITOR)
        else:
            ui.status = f"unknown command :{cmd}"
        return

    if text.startswith("/"):
        low = text.lower()
        if low.startswith("/to mail"):
            email = text[len("/to mail"):].strip().lower()
            if email:
                _select(ui, email=email)
            else:
                ui.status = "usage: /to mail <email>"
        elif low.startswith("/to contact"):
            name = text[len("/to contact"):].strip()
            if name:
                _select(ui, name=name)
            else:
                ui.status = "usage: /to contact <name>"
        elif low == "/copy":
            draft = ui.state.get("draft")
            if not draft:
                ui.status = "nothing to copy yet."
            else:
                try:
                    import pyperclip
                    pyperclip.copy(draft)
                    ui.status = "last draft copied to clipboard."
                except Exception:  # noqa: BLE001 — headless / no clipboard
                    ui.status = "clipboard unavailable — the draft is shown above."
        elif low == "/new":
            ui.state["messages"] = []
            ui.state["draft"] = None
            ui.status = "new conversation."
        else:
            ui.status = f"unknown command {text.split()[0]}"
        return

    # plain text
    if ui.mode == Mode.WRITING:
        _do_writer(ui, text)
    else:
        ui.status = "type a command here, or Esc to go back to writing."


def _open_selected(ui: UI) -> None:
    if not ui.rows:
        return
    r = ui.rows[ui.sel]
    if ui.mode == Mode.EMAILS:
        cid = r["contact_id"]
        _open_history(ui, cid, r["contact_name"] or r["email"], Mode.EMAILS)
    elif ui.mode == Mode.CONTACTS:
        ui.contact_id = r["id"]
        ui.contact_label = r["name"] or "(unnamed)"
        ui.mode = Mode.ADDRESSES
        _load_list(ui)
    elif ui.mode == Mode.ADDRESSES:
        _open_history(ui, ui.contact_id, ui.contact_label, Mode.ADDRESSES)


# ---------------------------------------------------------------------------
# Key bindings
# ---------------------------------------------------------------------------

_kb = KeyBindings()
_in_list = Condition(lambda: _UI.mode in (Mode.EMAILS, Mode.CONTACTS, Mode.ADDRESSES))
_in_scroll = Condition(lambda: _UI.mode == Mode.HISTORY)
_in_editor = Condition(lambda: _UI.mode in (Mode.PROMPT_EDITOR, Mode.CONFIG_EDITOR))


@_kb.add("up", filter=_in_list)
def _(event):
    _UI.sel = max(0, _UI.sel - 1)


@_kb.add("down", filter=_in_list)
def _(event):
    _UI.sel = min(len(_UI.rows) - 1, _UI.sel + 1) if _UI.rows else 0


@_kb.add("up", filter=_in_scroll)
def _(event):
    _UI.scroll = max(0, _UI.scroll - 1)


@_kb.add("down", filter=_in_scroll)
def _(event):
    _UI.scroll += 1


@_kb.add("pageup", filter=_in_scroll)
def _(event):
    _UI.scroll = max(0, _UI.scroll - 10)


@_kb.add("pagedown", filter=_in_scroll)
def _(event):
    _UI.scroll += 10


@_kb.add("escape", filter=_in_editor, eager=True)
def _(event):
    _save_editor(_UI)


@_kb.add("escape", filter=_in_list | _in_scroll, eager=True)
def _(event):
    ui = _UI
    if ui.mode == Mode.ADDRESSES:
        ui.mode = Mode.CONTACTS
        _load_list(ui)
    elif ui.mode == Mode.HISTORY:
        ui.mode = ui.prev_mode
        if ui.mode in (Mode.EMAILS, Mode.CONTACTS, Mode.ADDRESSES):
            _load_list(ui)
    else:
        ui.mode = Mode.WRITING
        ui.status = "back to writing."


@_kb.add("c-c")
@_kb.add("c-q")
def _(event):
    event.app.exit()


def _wheel(mouse_event):
    """Mouse-wheel scroll for the main area."""
    ui = _UI
    if mouse_event.event_type == MouseEventType.SCROLL_UP:
        if ui.mode in (Mode.EMAILS, Mode.CONTACTS, Mode.ADDRESSES):
            ui.sel = max(0, ui.sel - 1)
        else:
            ui.scroll = max(0, ui.scroll - 3)
    elif mouse_event.event_type == MouseEventType.SCROLL_DOWN:
        if ui.mode in (Mode.EMAILS, Mode.CONTACTS, Mode.ADDRESSES):
            ui.sel = min(len(ui.rows) - 1, ui.sel + 1) if ui.rows else 0
        else:
            ui.scroll += 3
    else:
        return NotImplemented


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def _accept(buf: Buffer) -> bool:
    text = buf.text
    buf.text = ""
    _dispatch(_UI, text)
    return False  # keep the buffer (don't add to history)


_cmd_buffer = Buffer(accept_handler=_accept, multiline=False)
_EDITOR = Buffer(multiline=True)

_main_window = Window(
    FormattedTextControl(get_main_text, focusable=False,
                         show_cursor=False),
    wrap_lines=False,
)
# attach wheel handler
_main_window.content.mouse_handler = _wheel  # type: ignore[attr-defined]

_editor_window = Window(BufferControl(_EDITOR), wrap_lines=True)
_cmd_window = Window(
    BufferControl(_cmd_buffer),
    height=1,
    get_line_prefix=lambda *_: [("class:prompt", " › ")],
)
_footer_window = Window(FormattedTextControl(get_footer_text), height=2)

_root = HSplit([
    ConditionalContainer(_main_window, filter=~_in_editor),
    ConditionalContainer(_editor_window, filter=_in_editor),
    ConditionalContainer(_cmd_window, filter=~_in_editor),
    _footer_window,
])

_style = Style.from_dict({
    "prompt": "#5fafff bold",
})

# The Application is built in main() — its constructor probes the console,
# which fails when this module is merely imported outside a real TTY.
_app: Application | None = None

# Singleton UI, created in main() once state exists.
_UI: UI = None  # type: ignore[assignment]


def _build_app() -> Application:
    return Application(
        layout=Layout(_root, focused_element=_cmd_window),
        key_bindings=_kb,
        style=_style,
        full_screen=True,
        mouse_support=True,
    )


# ---------------------------------------------------------------------------
# Startup: auth -> ingest -> verify -> launch
# ---------------------------------------------------------------------------

def _prompt_backfill() -> tuple[str, str]:
    """Plain-terminal first-run prompt (before the full-screen UI starts),
    same choice as `python -m backend.main`."""
    print("\nFirst run — no mail has been ingested yet.")
    while True:
        mode = input("Backfill by 'number' of recent emails or by 'date'? "
                      "[number/date] > ").strip().lower()
        if mode == "number":
            n = input("How many recent emails? > ").strip()
            if n.isdigit() and int(n) > 0:
                return ("number", n)
        elif mode == "date":
            d = input("Since when? (yyyy-mm-dd) > ").strip()
            if len(d) == 10 and d[4] == "-" and d[7] == "-":
                return ("date", d)
        print("  please answer 'number' then a count, or 'date' then yyyy-mm-dd.")


def _old_schema_present() -> bool:
    """True if the DB file exists on the previous (pre-refactor) schema,
    which the new queries can't read. Cheap: just inspects column names of the
    existing `contacts` table without touching anything."""
    if not schema.DB_PATH.exists():
        return False
    conn = schema.get_connection()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(contacts)")}
    finally:
        conn.close()
    return bool(cols) and "name" not in cols


def _offer_reset() -> None:
    """Detect old-schema DBs and offer to wipe them so the new code can run.
    All data is reconstructable from Gmail by re-ingesting, so a reset is
    cheap. If the user declines, exit with instructions."""
    if not _old_schema_present():
        return
    print()
    print("This DB is on the previous schema (contacts/people split). The new")
    print("code can't read it. Resetting wipes the local SQLite + the mailparse")
    print("cursor; the next launch re-ingests from Gmail. (Your mailbox is")
    print("untouched — this is read-only on Gmail's side.)")
    ans = input("Reset now and continue? [Y/n] > ").strip().lower()
    if ans not in ("", "y", "yes"):
        print("OK — run `python -m backend.cli reset` when ready, then relaunch.")
        sys.exit(0)
    schema.reset_db()
    for p in (mailparse.STATE_PATH, mailparse.OUTPUT_PATH):
        if p.exists():
            p.unlink()
    print("DB reset. Continuing…\n")


def _startup() -> AgentState:
    print("Your Email Agent — starting up.")
    _offer_reset()
    schema.init_db()        # safe-to-call; ensures tables exist for browsing
    print("Authenticating with Gmail (a browser may open on first run)…")
    service = get_service()
    state = initial_state()

    try:
        mailparse.ingest(service, incremental_cap=INCREMENTAL_MAX)
    except FirstRunNeeded:
        backfill = _prompt_backfill()
        mailparse.ingest(service, backfill=backfill)

    print("Scoring relevance and updating your conversation history…")
    try:
        verifier.run(state)
    except Exception as e:  # noqa: BLE001 — Ollama down etc.; browsing still works
        print(f"\n[!] Verifier skipped: {type(e).__name__}: {e}")
        print("    Browsing works; drafting needs the local LLM running.\n")
    return state


def main() -> None:
    global _UI, _app
    state = _startup()
    _UI = UI(state)
    _app = _build_app()
    _app.run()
    print("Bye.")


if __name__ == "__main__":
    main()
