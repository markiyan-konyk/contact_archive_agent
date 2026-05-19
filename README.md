# Email Agent

A personal, local email-writing assistant. It reads your Gmail (read-only),
builds a SQLite store of who you talk to and what about, then uses that
relationship context to help you draft relationship-aware emails with a local
LLM. **Not** a generic email client — it never sends anything.

> Prototype. Single user, runs entirely on your machine. Your mailbox data
> stays local and is gitignored.

## How it works

```
Gmail ──▶ mailparse ──▶ verifier ──▶ SQLite history ──▶ writer ──▶ draft text
          (fetch)        (filter +    (per-contact        (context-aware
                          save)        conversations)      LLM draft)
```

- **mailparse** — pulls INBOX + SENT (read-only), incrementally after the first backfill.
- **verifier** — drops bulk/automated/filler mail, saves real conversations per contact.
- **writer** — pulls a contact's stored history into the prompt so drafts match
  that relationship's tone, and won't fabricate replies that never happened.

## Setup

**1. Gmail OAuth.** Follow [`Oauthsetup.md`](Oauthsetup.md): create a Google
Cloud project, enable the Gmail API, make a **Desktop app** OAuth client,
download it as `credentials.json` in the project root. (Gitignored.)

**2. Install.**

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (source .venv/bin/activate on macOS/Linux)
pip install -r requirements.txt
```

**3. Local LLM (default).** Install [Ollama](https://ollama.com), then:

```bash
ollama pull gemma3:4b           # set OLLAMA_MODEL to match what you pulled
```

To use a cloud model instead, set `LLM_PROVIDER` (google/anthropic/openai) plus
its API key in `.env`, and uncomment the matching line in `requirements.txt`.

## Use

The full experience is the terminal UI — see [cli-frontend/README.md](cli-frontend/README.md):

```bash
cd cli-frontend
pip install -r requirements.txt
python cli.py
```

The backend-only entrypoints still work for scripting/debugging:

```bash
# Ingest mail (first run asks how far back to backfill; opens a browser once)
python -m backend.main

# See what was saved (uses the same SQLite store as the TUI)
python -m backend.cli contacts
python -m backend.cli convo someone@example.com

# Chat REPL (/to mail <email> | /to contact <name> | /no-contact | /reset | /quit)
python -m backend.node.writer

# Wipe everything and start over (asks first)
python -m backend.cli reset
```

Re-run `python -m backend.main` (or relaunch the TUI) any time to pull new
mail incrementally.

## Notes

- `backend/database/email_agent.db`, the parsed-mail handoff, and the OAuth
  files are all gitignored — your mailbox never leaves your machine.
- While the Google Cloud app is in "Testing" mode the OAuth token expires after
  ~7 days; just rerun and re-consent (or delete `token.json`).
- Writing-style preferences live in the database, edited through the TUI:
  `:config` for the general style, `:prompt` per contact. (Earlier versions
  used a `writer_config.json` file; that's gone.)
