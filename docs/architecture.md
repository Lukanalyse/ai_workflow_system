# Architecture

## Overview
This project implements an AI-assisted email workflow with a strict **draft-only** policy:
- read Outlook emails via Microsoft Graph,
- filter and clean content,
- analyze with an OpenAI-compatible LLM,
- generate suggested replies,
- create Outlook drafts (never auto-send),
- store processing results in SQLite.

## Module Responsibilities

### `app/auth`
- `microsoft_auth.py`: OAuth2 via MSAL device flow, token cache persistence, automatic refresh through cache-aware silent token acquisition.

### `app/email`
- `read_emails.py`: Graph API inbox reader.
- `filters.py`: provider query filter and local filtering.
- `clean_email.py`: HTML/text cleanup and signature removal.
- `thread_parser.py`: thread/history trimming.
- `create_draft.py`: draft creation endpoint wrapper.

### `app/llm`
- `llm_client.py`: OpenAI-compatible `/chat/completions` client.
- `prompt_loader.py`: external prompt loading from YAML.
- `summarize.py`, `classify.py`, `generate_reply.py`: modular LLM tasks.

### `app/database`
- `sqlite_manager.py`: persistence for processed emails, drafts, confidence, and timestamps.

### `app/ui`
- `streamlit_app.py`: manual review UI (analyze, inspect confidence, approve/reject draft creation).

### `app/main.py`
- non-UI entrypoint for batch workflow execution.

## Data Flow
1. Acquire Graph token.
2. Fetch inbox messages with Graph-side and local filters.
3. Clean/trim content.
4. Execute LLM tasks.
5. Create draft in Outlook.
6. Save metadata and outputs in SQLite.
7. Human validates and manually sends from Outlook.

## Extensibility
- Add providers by creating new auth/reader/draft modules with same interfaces.
- Add LLM backends by implementing a compatible client adapter.
- Add RAG/memory by inserting retrieval step before prompt generation.
- Add vector DB, attachment parsing, calendar hooks as isolated modules.

