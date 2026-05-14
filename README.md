# Gmail AI Email Assistant

Production-oriented Gmail-native assistant that:
- reads latest unread Gmail emails,
- filters non-replyable/automated content,
- generates AI reply drafts,
- creates Gmail drafts only (never sends),
- prevents duplicate processing with SQLite.

## Safety policy

1. Drafts only: no auto-send action exists in the codebase.
2. Human validation is required before sending from Gmail.
3. Attachments are acknowledged only; attachment contents are not read.

## Architecture

```text
app/
├── auth/
│   └── gmail_auth.py
├── email/
│   ├── gmail_reader.py
│   ├── gmail_draft_creator.py
│   ├── clean_email.py
│   ├── filters.py
│   ├── thread_parser.py
│   └── attachment_detector.py
├── llm/
├── database/
├── config/
├── ui/
└── gmail_cli.py
```

## Setup

### 1. Install

```bash
pip install -r requirements.txt
```

### 2. Configure Gmail OAuth

1. Create a Google Cloud project.
2. Enable Gmail API.
3. Configure OAuth consent screen.
4. Create Desktop OAuth credentials.
5. Save credentials file to:
   - `credentials/credentials.json`

Required scopes:
- `https://www.googleapis.com/auth/gmail.readonly`
- `https://www.googleapis.com/auth/gmail.modify`
- `https://www.googleapis.com/auth/gmail.compose`

### 3. Configure environment

1. Copy `.env_EXAMPLE` to `.env`.
2. Set at least:
   - `LLM__API_KEY`

## Run

### Gmail CLI

```bash
python -m app.gmail_cli
```

Run on latest 5 unread:

```bash
python -m app.gmail_cli --max-emails 5
```

Dry run (no draft creation):

```bash
python -m app.gmail_cli --no-drafts
```

### Streamlit

```bash
streamlit run app/ui/streamlit_app.py
```

## Deduplication behavior

SQLite table `gmail_processed_emails` prevents duplicates by:
1. `message_id` uniqueness,
2. skipping new messages in threads where a draft was already created.

Skip reasons and processing state are persisted for auditability.

## Attachment handling

Attachments are detected from Gmail metadata (filename + attachment id only).
Attachment bytes/content are never downloaded or parsed.

