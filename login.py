"""Gmail connection status helper (no browser, no local server).

The interactive desktop OAuth flow has been removed. Gmail is now connected
from the web UI — start the app and use Settings → Gmail → "Connect Gmail",
which runs a standard web OAuth flow through the FastAPI server and works the
same locally and inside Docker.

This script no longer authorizes anything; it just reports whether a token is
already present so you can confirm setup from the terminal.

Usage:
    python login.py
"""

from __future__ import annotations

from app.auth.gmail_auth import build_auth_manager
from app.config.settings import get_settings


def main() -> int:
    settings = get_settings()
    gmail = settings.gmail

    if not gmail.credentials_path.exists():
        print(
            f"[INFO] No Google OAuth client file at: {gmail.credentials_path}\n"
            "Add it (or upload it from the web app: Settings → Gmail), then connect."
        )

    creds = build_auth_manager(settings).load_credentials()
    if creds is not None:
        print("[OK] Gmail is already connected. Start the app: docker compose up")
        return 0

    print(
        "[ACTION NEEDED] Gmail is not connected.\n"
        "  1. Start the app:    docker compose up   (or: uvicorn web.server:app --port 3000)\n"
        "  2. Open:             http://localhost:3000\n"
        "  3. Click:            Settings → Gmail → Connect Gmail\n"
        "No browser is launched on the server; Google opens in YOUR browser."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
