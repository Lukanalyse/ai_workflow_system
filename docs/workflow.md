# Workflow

## Batch mode (`app/gmail_cli.py`)
1. Load `.env` settings.
2. Validate startup config and local files.
3. Authenticate to Gmail.
4. Fetch latest unread Gmail messages.
5. For each unprocessed message:
   - run replyability filters (newsletter/automation/noreply/promotions),
   - clean content,
   - summarize,
   - classify intent + urgency + confidence,
   - generate reply text,
   - create Gmail draft only when `--create-drafts` is explicitly passed,
   - store in SQLite.

## UI mode (`app/ui/streamlit_app.py`)
1. Apply filters interactively.
2. Load Gmail candidates.
3. Analyze selected emails.
4. Review summary / intent / urgency / confidence.
5. Edit generated draft.
6. Approve to create Gmail draft.

## No auto-send guarantee
- The project only calls draft creation endpoints.
- No Gmail send endpoint is used.
- Sending remains manual in Gmail.
