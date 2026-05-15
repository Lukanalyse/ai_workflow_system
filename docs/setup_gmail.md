# Gmail Setup
git clone ...

cd ai_email_workflow_system

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python onboarding_setup.py
python -m app.gmail_cli --validate-config

streamlit :

cd file_way_project
  source .venv/bin/activate
  python -m streamlit run app/ui/streamlit_app.py


## 1. Create project in Google Cloud
1. Go to https://console.cloud.google.com
2. Create/select a project.
3. Enable billing if required.

## 2. Enable Gmail API
1. Open **APIs & Services** → **Library**.
2. Search **Gmail API**.
3. Click **Enable**.

## 3. OAuth consent screen
1. Open **APIs & Services** → **OAuth consent screen**.
2. Choose **External** (or Internal for Workspace org).
3. Fill app name, support email, developer contact.
4. Add scopes (minimum needed):
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/gmail.compose`
5. Add test users during development.

## 4. Create OAuth credentials
1. Open **Credentials** → **Create credentials** → **OAuth client ID**.
2. App type: Desktop app or Web app.
3. Download `credentials.json`.

## 5. Local token generation
Use the built-in auth flow (`app/auth/gmail_auth.py`) to get and persist local token state.
The workflow reuses `token.json` and refreshes automatically when possible.
Token is stored as JSON (not pickle) and should remain local-only.

## 6. Credential placement
Place your OAuth client file at:
- `credentials/credentials.json`

Token is persisted at:
- `token.json`

On macOS/Linux, permissions should be owner-only (`600`) for:
- `credentials/credentials.json`
- `token.json`

## 7. Run

```bash
python -m app.gmail_cli --max-emails 5
```

Drafts are created only in Gmail Drafts. No email is auto-sent.
Use `--no-drafts` if you want analyze-only mode.

Optional guided setup:

```bash
python onboarding_setup.py
```
