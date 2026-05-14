# Optional Gmail Support Setup

Gmail is optional and not required for the current Outlook-first implementation.

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
   - `https://www.googleapis.com/auth/gmail.modify`
   - `https://www.googleapis.com/auth/gmail.compose`
5. Add test users during development.

## 4. Create OAuth credentials
1. Open **Credentials** → **Create credentials** → **OAuth client ID**.
2. App type: Desktop app or Web app.
3. Download `credentials.json`.

## 5. Local token generation
Use Google OAuth flow (installed app) to get and persist token cache locally.

## 6. Recommended provider abstraction
Add Gmail support by implementing:
- `app/auth/google_auth.py`
- `app/email/gmail_read_emails.py`
- `app/email/gmail_create_draft.py`

Keep the same interfaces used by Outlook modules to avoid UI/business logic changes.

