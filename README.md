# Gmail AI Email Assistant

Draft-only Gmail assistant with OpenAI-compatible analysis.

## Security Model

1. Draft creation only. No Gmail send endpoint is used.
2. Attachment content is never downloaded or parsed (metadata only).
3. Email body is treated as untrusted input for LLM prompts.
4. Local secrets, tokens, DB, and logs are git-ignored by default.
5. CLI default can create drafts (configurable), while `--no-drafts` forces analyze-only mode.

## What Must Never Be Committed

- `.env` (real API keys)
- `token.json` (OAuth access/refresh tokens)
- `credentials/credentials.json` (Google OAuth client secret)
- `data/*.db` (email-derived local state)
- `logs/*.log` (runtime traces)

## Quick Start (Portable)

1. Install Python `3.11+`.
2. Create virtual env and install deps:
   - `python -m venv .venv`
   - `source .venv/bin/activate` (Windows PowerShell: `.\.venv\Scripts\Activate.ps1`)
   - `pip install -r requirements.txt`
3. Copy config template:
   - `cp .env_EXAMPLE .env`
4. Set at least:
   - `LLM__API_KEY=...`
5. Put Gmail OAuth Desktop app file at:
   - `credentials/credentials.json`
6. Run the guided setup wizard:
   - `python onboarding_setup.py`

## Validate Setup

Run validation only (no email processing):

```bash
python -m app.gmail_cli --validate-config
```

This checks required config, files, and minimal runtime setup.

## Guided Onboarding Wizard

Run:

```bash
python onboarding_setup.py
```

The wizard performs:
1. environment checks (Python, dependencies, required folders),
2. beginner-friendly Gmail OAuth instructions,
3. interactive LLM/API key setup,
4. draft default choice (enabled/disabled),
5. safe `.env` creation/update,
6. automatic `--validate-config` execution.

## Run

Analyze only (no draft creation):

```bash
python -m app.gmail_cli --max-emails 5 --no-drafts
```

Create Gmail drafts:

```bash
python -m app.gmail_cli --max-emails 5
```

Streamlit manual review UI:

```bash
streamlit run app/ui/streamlit_app.py
```

## Gmail OAuth Scopes (Least Privilege)

- `https://www.googleapis.com/auth/gmail.readonly`
- `https://www.googleapis.com/auth/gmail.compose`

`gmail.modify` is intentionally excluded by default.

## Persistence Defaults

To reduce local privacy risk, DB persistence is minimal by default:

- `DATABASE__PERSIST_SNIPPET=false`
- `DATABASE__PERSIST_AI_OUTPUTS=false`

You can opt in to store snippets/AI outputs in `.env` for debugging.

## Sharing Checklist

Before sharing publicly or with colleagues:

1. Ensure `.env`, `token.json`, `credentials/`, `data/`, and `logs/` are absent from commits.
2. Rotate API keys if there is any doubt.
3. Remove local runtime artifacts:
   - `rm -f token.json`
   - `rm -rf data logs`
4. Share `.env_EXAMPLE`, never real `.env`.
