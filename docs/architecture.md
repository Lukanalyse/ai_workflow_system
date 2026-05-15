# Architecture

## Overview
This project implements an AI-assisted email workflow with a strict **draft-only** policy:
- read Gmail emails via Gmail API,
- filter and clean content,
- analyze with an OpenAI-compatible LLM,
- generate suggested replies,
- create Gmail drafts (never auto-send),
- store processing results in SQLite.

## Module Responsibilities

### `app/auth`
- `gmail_auth.py`: OAuth2 flow for Gmail with token persistence and refresh support.

### `app/email`
- `gmail_reader.py`: Gmail inbox reader + replyability heuristics.
- `gmail_draft_creator.py`: Gmail draft creation only.
- `filters.py`: local filtering helpers.
- `clean_email.py`: HTML/text cleanup and signature removal.
- `thread_parser.py`: thread/history trimming.
- `attachment_detector.py`: metadata-only attachment detection (no attachment content read).

### `app/llm`
- `llm_client.py`: OpenAI-compatible `/chat/completions` client.
- `prompt_loader.py`: external prompt loading from YAML.
- `summarize.py`, `classify.py`, `generate_reply.py`: modular LLM tasks.

### `app/security`
- `startup_checks.py`: startup validation, local file permission hardening, and persistence minimization helpers.

### `app/database`
- `sqlite_manager.py`: persistence for processed emails, drafts, confidence, and timestamps.

### `app/ui`
- `streamlit_app.py`: manual review UI (analyze, inspect confidence, approve/reject draft creation).

### `app/gmail_cli.py`
- Gmail-native non-UI entrypoint for batch workflow execution.

## Data Flow
1. Acquire Gmail OAuth token.
2. Fetch inbox messages with Gmail query + local replyability filtering.
3. Clean/trim content.
4. Execute LLM tasks.
5. Create draft in Gmail.
6. Save metadata and outputs in SQLite.
7. Human validates and manually sends from Gmail UI.

## Extensibility
- Add LLM backends by implementing a compatible client adapter.
- Add RAG/memory by inserting retrieval step before prompt generation.
- Add vector DB, attachment parsing, calendar hooks as isolated modules.
