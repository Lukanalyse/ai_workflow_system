# AI Email Assistant

A small self-hosted web app that reads your Gmail, generates AI reply drafts, and
saves them straight into **Gmail Drafts**. Nothing is ever auto-sent.

- Clean web UI on `http://localhost:3000`
- **Plug-and-play setup wizard** — pick a provider, paste an API key, connect Gmail, done
- **Connect Gmail from the browser** (OAuth in the UI; no `login.py`, no terminal)
- In-app **Settings** (AI · Filtering · Gmail · Usage) — no file editing required
- Provider architecture (Gmail today; Outlook scaffolded)
- Draft-only by default: read + compose scopes only, never send
- **Bulk draft generation** (last 10/20/50/100 or all replyable) with live progress + report
- **Token & cost tracking** with a Usage dashboard (today / month, per provider & model, daily)
- **Pre-run cost estimate** + **monthly budget** monitoring
- **Optional manual sending** — OFF by default, explicit per-email confirmation

---

## Quick Start (plug-and-play)

```bash
git clone <repo-url> && cd ai_email_workflow_system
cp .env_EXAMPLE .env        # no edits required — the wizard fills it in
docker compose up
# open http://localhost:3000
```

On first launch the app shows a **setup wizard**:

1. **Choose provider** — OpenAI or Anthropic
2. **Enter API key** — stored server-side, never shown again
3. **Connect Gmail** — opens Google sign-in, grants read + draft access, stores
   the token in `storage/tokens/<email>.json`
4. **Finish** — start generating drafts

No `python login.py`, no manual `token.json`, no `.env` editing. The Gmail OAuth
flow runs entirely through the FastAPI server, so it works the same locally and
in Docker, and the token persists across restarts via the `storage/` volume.

### Google OAuth client (`credentials.json`)

You still need a Google OAuth client (the project's identity with Google). Create
one in Google Cloud Console (Gmail API enabled, OAuth client → *Desktop app*) and
either place it at `credentials/credentials.json` **or upload it from
Settings → Gmail** in the UI. Keep the default redirect URI
`http://localhost:3000/api/gmail/callback` (already a loopback URI accepted by
Desktop clients).

---

## Configuration

Only these go in `.env` (all also editable from the in-app **Settings** page):

```env
OPENAI_API_KEY=          # set one of these two (or via the wizard)
ANTHROPIC_API_KEY=
LLM_PROVIDER=            # optional: openai | anthropic (auto by key if blank)
LLM_MODEL=              # optional: defaults to gpt-4.1-mini / claude-opus-4-8
LLM_TEMPERATURE=        # optional: 0.0–1.0 (slider in Settings → AI)
GOOGLE_CREDENTIALS_PATH=credentials/credentials.json
OAUTH_REDIRECT_URI=http://localhost:3000/api/gmail/callback
```

---

## Architecture

```
web/                     FastAPI backend + static frontend (port 3000)
  server.py              API: /api/health, /api/emails, .../draft, .../save-draft, /api/settings
  static/                index.html + app.js + style.css (vanilla, no build step)
app/
  providers/             EmailProvider abstraction
    gmail/               GmailProvider (read + draft, reuses existing reader/auth)
    outlook/             OutlookProvider (scaffold)
  services/              email_service · draft_service · llm_service
  agents/                base_agent · draft_agent · summarize_agent · classify_agent (scaffold)
  llm/                   provider router · OpenAI client · Anthropic SDK client
  auth/                  gmail_auth · oauth_flow (web OAuth) · token_store (storage/tokens/)
  email/ database/ security/         (reused core)
  logging_config.py      structured JSON logs -> logs/app.log + logs/error.log
storage/tokens/          OAuth tokens, one file per account (multi-account ready)
login.py                 optional legacy host OAuth (UI connect is preferred)
```

Request flow: **browser → FastAPI → EmailService (provider) / DraftService (agents) → Gmail**.

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET  | `/api/health` | Gmail / LLM / database status |
| GET  | `/api/emails?max=N` | List unread candidates (max 100) |
| POST | `/api/emails/{id}/draft` | Generate summary + reply draft |
| POST | `/api/emails/{id}/save-draft` | Save edited draft into Gmail Drafts |
| POST | `/api/emails/{id}/send` | **Send** a reply (only if `ENABLE_EMAIL_SENDING=true`, requires `confirm`) |
| POST | `/api/bulk/preview` | Estimate tokens/cost + budget check before a bulk run |
| GET  | `/api/bulk/stream` | Run bulk generation, streaming progress via SSE |
| GET  | `/api/usage/summary` | Token usage, cost, budget, per-provider/model breakdown |
| GET/POST | `/api/settings` | Read / update provider, key, model, temperature, budget, sending |
| GET  | `/api/setup/status` | First-run wizard state (LLM ready, Gmail connected, configured) |
| GET  | `/api/gmail/status` | Connected account, token validity, last refresh (no secrets) |
| GET  | `/api/gmail/connect` | Returns the Google consent URL to open |
| GET  | `/api/gmail/callback` | OAuth redirect target; stores the token, bounces back to the UI |
| POST | `/api/gmail/disconnect` | Remove the stored token (requires UI confirmation) |
| POST | `/api/gmail/credentials` | Upload the Google OAuth client file from the UI |

### Bulk generation

Pick a count (10/20/50/100) or "All Replyable", then **Generate Drafts** or
**Generate + Save**. A cost estimate is shown first (Cancel/Continue). Processing
is sequential and error-isolated — one failing email never aborts the run — and a
final report shows analyzed / generated / saved / skipped / failures / elapsed time.

### Manual sending (optional, off by default)

Sending is disabled by default. Toggle it on from **Settings → Gmail** (or set
`ENABLE_EMAIL_SENDING=true`). Enabling it adds the `gmail.send` scope, so the UI
asks you to **reconnect Gmail** to re-consent. Only then does a **Send email**
button appear, and every send requires an explicit confirmation popup.
Draft-first remains the default.

---

## Security Model

1. Draft-first: sending is **off by default**; the send scope and Send button only
   exist when `ENABLE_EMAIL_SENDING=true`, and every send needs explicit confirmation.
2. Least-privilege scopes: `gmail.readonly` + `gmail.compose` (adds `gmail.send` only when enabled).
3. Attachment content is never downloaded (metadata only).
4. Email bodies are wrapped as untrusted input before reaching the LLM.
5. Tokens never reach the browser: the UI only sees the connected email, token
   validity, and last-refresh time — refresh/access tokens and OAuth secrets stay
   server-side. Token files are written `0600` under a `0700` `storage/tokens/` dir.
6. Secrets stay local and git-ignored: `.env`, `token.json`, `storage/tokens/`,
   `credentials/`, `data/`, `logs/`.
7. No autonomous behavior: bulk generation produces drafts for human review; nothing is sent automatically.

**Never commit** `.env`, `token.json`, `storage/tokens/`, `credentials/credentials.json`, `data/*.db`, `logs/*.log`.

---

## Without Docker (dev)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn web.server:app --port 3000 --reload
# then open http://localhost:3000 and connect Gmail from the setup wizard
```

The legacy CLI (`python -m app.gmail_cli --max-emails 5`) and the older Streamlit UI
(`streamlit run app/ui/streamlit_app.py`) still work for batch / manual use.
