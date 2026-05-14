# Workflow

## Batch mode (`app/main.py`)
1. Load `.env` settings.
2. Authenticate to Microsoft Graph.
3. Fetch filtered inbox messages.
4. For each unprocessed message:
   - clean content,
   - summarize,
   - classify intent + urgency + confidence,
   - generate reply text,
   - create Outlook draft,
   - store in SQLite.

## UI mode (`app/ui/streamlit_app.py`)
1. Apply filters interactively.
2. Load candidate emails.
3. Analyze selected emails.
4. Review summary / intent / urgency / confidence.
5. Edit generated draft.
6. Approve to create draft or reject.

## No auto-send guarantee
- The project only calls draft creation endpoints.
- No `sendMail` operation is used.
- Sending remains manual in Outlook.

