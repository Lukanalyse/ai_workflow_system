# Architecture

A self-hosted local web app: read Gmail, generate AI reply drafts, save them into
Gmail Drafts. **Draft-only** — nothing is ever auto-sent.

## Layers

```
Browser (http://localhost:3000)
   │
   ▼
web/server.py  ── FastAPI: REST API + static frontend (web/static/*)
   │
   ├── services/email_service.py   list/filter messages (via provider)
   ├── services/draft_service.py   generate (agents) + save draft + persist
   └── services/llm_service.py     provider router + agents wiring
          │                              │
          ▼                              ▼
   providers/                        agents/
     base.py (EmailProvider)           base_agent.py
     gmail/provider.py                 draft_agent.py
     outlook/provider.py (scaffold)    summarize_agent.py
          │                            classify_agent.py (scaffold)
          ▼                              │
   email/ · auth/ (reused Gmail core)    ▼
                                      llm/provider.py → OpenAI | Anthropic client
```

## Module Responsibilities

### `web`
- `server.py`: FastAPI app. `ServiceContainer` builds/reloads the service graph.
  Endpoints: `/api/health`, `/api/emails`, `/api/emails/{id}/draft`,
  `/api/emails/{id}/save-draft`, `/api/settings`. Serves the static frontend.
- `static/`: single-page UI (vanilla JS, no build step).

### `app/providers`
- `base.py`: `EmailProvider` interface + neutral `EmailMessage` / `DraftResult`.
- `gmail/provider.py`: Gmail backend; composes the existing reader, draft creator,
  and OAuth manager. Builds the API service lazily.
- `outlook/provider.py`: scaffold for a future Microsoft Graph backend.

### `app/services`
- `email_service.py`: list/filter candidates, replyability + dedup.
- `draft_service.py`: orchestrates generation and Gmail draft creation + persistence.
- `llm_service.py`: selects the provider, owns the agents, exposes summarize/draft.

### `app/agents`
- `base_agent.py`: prompt + LLM-call scaffold. `draft_agent` and `summarize_agent`
  are implemented; `classify_agent` is scaffolded.

### `app/llm`
- `provider.py`: returns the right client from settings.
- `llm_client.py`: OpenAI-compatible client. `anthropic_client.py`: Claude via the
  official Anthropic SDK (no sampling params — valid on Opus 4.7+/4.8).

### Reused core
- `app/auth`, `app/email`, `app/database`, `app/security` are unchanged.
- `app/logging_config.py`: structured JSON logs → `logs/app.log` + `logs/error.log`.

## Configuration & OAuth flow

- `app/config/settings.py` folds simple flat env vars (`OPENAI_API_KEY`,
  `ANTHROPIC_API_KEY`, `LLM_PROVIDER`, `LLM_MODEL`, `GOOGLE_CREDENTIALS_PATH`) into
  the structured config. Legacy `LLM__*` / `GMAIL__*` still work.
- `app/config/env_writer.py` backs the Settings page (writes `.env` + `os.environ`).
- `login.py` runs the browser OAuth flow once on the host and writes `token.json`,
  which the container mounts read-only — no in-container browser needed.

## Data flow
1. Host: `python login.py` → `token.json`.
2. `docker compose up` → FastAPI on :3000.
3. UI loads `/api/emails` → GmailProvider lists unread, applies replyability + dedup.
4. UI requests a draft → summarize + draft agents via the active LLM provider.
5. User edits, saves → GmailProvider creates a Gmail draft; result persisted to SQLite.
6. Human reviews and sends from Gmail.

## Extensibility
- New mailbox: implement `EmailProvider` (see `outlook/`).
- New LLM provider: add a client + branch in `llm/provider.py`.
- New agent: subclass `BaseAgent`; wire into `LLMService`.
