# AI Email Workflow System (Outlook-first)

Production-oriented, modular Python project for:
- reading Outlook/Hotmail emails via Microsoft Graph,
- filtering and cleaning email content,
- analyzing with an OpenAI-compatible LLM,
- generating suggested replies,
- creating Outlook drafts only,
- storing outputs in SQLite for traceability.

> **Safety rule:** this project never auto-sends emails. Sending is always manual by the user in Outlook.

---

## 1. Architecture

```text
ai_email_workflow_system/
├── app/
│   ├── auth/microsoft_auth.py
│   ├── email/
│   │   ├── read_emails.py
│   │   ├── clean_email.py
│   │   ├── filters.py
│   │   ├── create_draft.py
│   │   └── thread_parser.py
│   ├── llm/
│   │   ├── summarize.py
│   │   ├── classify.py
│   │   ├── generate_reply.py
│   │   ├── prompt_loader.py
│   │   └── llm_client.py
│   ├── config/
│   │   ├── settings.py
│   │   └── prompts.yaml
│   ├── database/sqlite_manager.py
│   ├── ui/streamlit_app.py
│   └── main.py
├── docs/
│   ├── setup_outlook.md
│   ├── setup_gmail.md
│   ├── architecture.md
│   └── workflow.md
├── .env.example
├── requirements.txt
├── pyproject.toml
└── README.md
```

Detailed docs:
- `docs/architecture.md`
- `docs/workflow.md`

---

## 2. Features

### Email ingestion
- Outlook/Hotmail via Microsoft Graph API
- OAuth2 authentication via MSAL (device code flow)
- token cache persistence and refresh-aware silent acquisition

### Dynamic filtering
- unread only
- after a specific date
- sender whitelist / blacklist
- keyword filtering
- newsletter exclusion
- automated/no-reply exclusion

### Email preprocessing
- HTML cleanup
- signature trimming
- history/thread truncation

### LLM outputs
- summary
- intent classification
- urgency score
- confidence
- suggested reply draft

### Draft-only policy
- creates draft in Outlook Drafts folder
- no automatic send endpoint call

### Persistence
- SQLite records for processed emails:
  - message metadata
  - summary
  - intent
  - urgency
  - draft text
  - confidence
  - timestamps

### UI
- Streamlit review screen
- filters panel
- per-email analysis
- editable draft text
- approve/reject actions
- logs section

---

## 3. Setup (PyCharm recommended)

## 3.1 Create/open project
1. Open PyCharm.
2. Open folder: `PycharmProjects/ai_email_workflow_system`.
3. Set Python interpreter to **3.11+**.

## 3.2 Install dependencies
```bash
pip install -r requirements.txt
```

## 3.3 Configure environment
1. Copy `.env.example` to `.env`.
2. Fill at least:
   - `MICROSOFT__CLIENT_ID`
   - `MICROSOFT__TENANT_ID`
   - `LLM__API_KEY`
3. Keep prompt and paths defaults unless needed.

---

## 4. Outlook setup (required)

Follow:
- `docs/setup_outlook.md`

Includes:
1. Azure Portal and app registration
2. CLIENT_ID and TENANT_ID retrieval
3. Graph permissions (`Mail.Read`, `Mail.ReadWrite`, `Mail.Send`, etc.)
4. OAuth2 flow explanation
5. Token refresh behavior

---

## 5. Gmail setup (optional future provider)

Follow:
- `docs/setup_gmail.md`

This project is architected so Gmail modules can be added without changing business flow.

---

## 6. Run instructions

## 6.1 CLI batch workflow
```bash
python -m app.main
```

Generate analysis + drafts disabled:
```bash
python -m app.main --no-drafts
```

## 6.2 Streamlit app
```bash
streamlit run app/ui/streamlit_app.py
```

In UI:
1. set filters,
2. load emails,
3. analyze one email,
4. edit draft,
5. approve to create draft.

## 6.3 Gmail connectivity test (optional)
```bash
python -m app.email.test_gmail_connection --only-unread --max-results 5 --sender example@gmail.com
```

You can combine parameters such as `--query`, `--after-date YYYY-MM-DD`, `--label INBOX`, `--keyword`, and `--include-spam-trash`.

---

## 7. Environment variables

The configuration uses nested env names through pydantic settings:
- `MICROSOFT__*`
- `LLM__*`
- `FILTERS__*`
- `DATABASE__*`
- plus `GRAPH_BASE_URL`, `PROMPT_FILE`, `LOG_FILE`, `PROCESS_LIMIT`

See full template in `.env.example`.

---

## 8. Security notes

1. Never hardcode credentials.
2. Keep `.env` and token cache out of git.
3. Token cache path is local and file permissions are restricted by code.
4. Apply least-privilege Graph scopes.
5. This system does not auto-send emails.
6. Human validation is mandatory before sending.

---

## 9. Prompt engineering

Prompts are externalized in:
- `app/config/prompts.yaml`

Supports custom tone and language adaptation.

Preset tone styles:
- formal
- academic
- concise
- friendly
- recruiter
- research

---

## 10. Logging

Logs include:
- auth events
- Graph/email access
- LLM calls
- draft creation
- processing pipeline and errors

Default log file:
- `logs/app.log`

---

## 11. Database schema

SQLite table: `processed_emails`
- `message_id`
- `subject`
- `sender`
- `received_at`
- `summary`
- `intent_label`
- `urgency_score`
- `draft_text`
- `confidence_score`
- `draft_id`
- `created_at`

Default DB path:
- `data/email_workflow.db`

---

## 12. Future extensions (already anticipated by architecture)

- Local LLM providers
- RAG / memory layers
- vector database integration
- attachment parsing
- automatic categorization
- multi-agent orchestration
- calendar and task integration
- optional Gmail provider implementation
