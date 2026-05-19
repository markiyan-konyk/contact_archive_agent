# Your Email Agent — CLI

A terminal UI for the email agent. Drafts emails using the relationship
context the backend has built from your Gmail. Read-only on your inbox; the
agent never sends anything.

> This folder is intentionally separate from `backend/`. It will later move to
> its own repo (with the backend as an installed dependency); for now it adds
> the sibling `backend/` to `sys.path` automatically.

## Install

From this folder, in a Python 3.11+ environment:

```bash
pip install -r ../requirements.txt        # the backend deps
pip install -r requirements.txt           # rich + prompt_toolkit + pyperclip
```

You also need:

- A `credentials.json` (Gmail OAuth client, *Desktop* type) at the repo root —
  see [../Oauthsetup.md](../Oauthsetup.md).
- A local LLM running, by default [Ollama](https://ollama.com) with the model
  set by `OLLAMA_MODEL` (or your `LLM_PROVIDER` of choice — see
  [../README.md](../README.md)).

## ⚠ One-time: reset an old DB

If you ran an earlier version of this agent (before the contacts/people schema
was unified), the existing SQLite file is on the old schema and the new code
won't read it. Wipe it once:

```bash
python -m backend.cli reset
```

This deletes the DB, the mailparse cursor, and the parsed-mail handoff. On the
next launch the CLI will re-ingest from Gmail. Skip this on a fresh install.

## Run

```bash
python cli.py
```

First launch will open a browser for Google consent, then ask how far back to
backfill your mail (a count, or a date). Later launches just pull anything new
since you last opened the app, then drop you straight into the writing screen.

To run it like a command from anywhere, add this folder to your PATH (Windows:
"Edit the system environment variables" → Path; macOS/Linux: edit your shell
rc). After that, `cli.py` from any folder works.

## What you see

A rounded box titled **Your Email Agent** with the conversation inside, plus a
prompt line at the bottom — the same shape as a coding agent. The top of the
box always shows `to: …` so you know who the next draft is aimed at.

## Commands

Type these on the bottom prompt line.

### `:` view / admin commands

| Command            | What it does                                                |
| ------------------ | ----------------------------------------------------------- |
| `:emails`          | toggle the list of every email address you have on file     |
| `:contacts`        | toggle the list of contacts (people)                        |
| `:config`          | edit the **general** writing-style prompt (saved on Esc)    |
| `:addcon <name>`   | inside `:emails`, link the hovered address to that contact  |
| `:prompt`          | inside `:contacts`, edit the hovered contact's prompt       |
| `:exit`            | quit                                                        |

In a list: **↑/↓ or mouse wheel** to move, **Enter** to drill in,
**Esc** to back out one level.

A contact is a *person*, who may have several email addresses (linked via
`:addcon`). Their stored conversation is automatically aggregated across all
of those addresses when you draft for them.

### `/` writing-mode tools

| Command                   | What it does                                          |
| ------------------------- | ----------------------------------------------------- |
| `/to mail <email>`        | next draft uses the history attached to this address  |
| `/to contact <name>`      | next draft uses the history aggregated for this contact |
| `/copy`                   | copy the last draft to the clipboard                  |
| `/new`                    | clear the conversation and start fresh                |

Anything else you type while in writing mode is sent to the agent and it
drafts an email. The `to:` line at the top of the box reminds you who that
draft is meant for.

## Editors (`:prompt`, `:config`)

When you open one of these, the main area becomes an editable buffer
pre-filled with the current text. Type freely. **Esc** saves and closes. (The
spec described "type `:prompt` again to save" — we use Esc instead because a
free-text editor can't reliably grab `:prompt` out of the body.) The contact
prompt is saved into `contacts.notes` in the DB; the general one into the
single-row `app_config` table.

## The "I haven't opened this in months" cap

Open `cli.py` and look near the top:

```python
INCREMENTAL_MAX: int | None = None
# INCREMENTAL_MAX = 5      # <-- uncomment to cap a long-gap catch-up
```

Uncomment the second line (and pick a number) if you don't want a months-long
gap to fetch every email since the cursor — only the newest N are pulled.

## When something goes wrong

- **"is Ollama running?"** on a draft attempt — start Ollama and `ollama pull`
  the model in `OLLAMA_MODEL`. Browsing (`:emails`, `:contacts`, `:prompt`,
  `:config`) keeps working without it.
- **OAuth keeps asking for consent every week** — Google's "Testing"-mode apps
  expire refresh tokens after ~7 days. Either publish your Cloud app or just
  re-consent when prompted; the agent re-uses the existing data.
- **Clipboard unavailable on `/copy`** — over SSH or a stripped Linux box the
  draft is shown in the box instead; copy it manually.
