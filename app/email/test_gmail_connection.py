from __future__ import annotations

import argparse
import json

from app.auth.gmail_auth import GmailAuthManager
from app.config.settings import get_settings
from app.security.startup_checks import validate_and_prepare_runtime


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate Gmail OAuth and list message metadata.")
    parser.add_argument("--max-results", type=int, default=5, help="Max messages to list")
    parser.add_argument("--query", default="in:inbox -in:spam -in:trash", help="Gmail query")
    return parser.parse_args()


def extract_headers(headers: list[dict[str, str]]) -> dict[str, str]:
    lookup = {h.get("name", "").lower(): h.get("value", "") for h in headers}
    return {
        "from": lookup.get("from", ""),
        "subject": lookup.get("subject", ""),
        "date": lookup.get("date", ""),
    }


def main() -> None:
    args = parse_args()
    try:
        from googleapiclient.discovery import build
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing dependency: google-api-python-client. Run `pip install -r requirements.txt`."
        ) from exc

    settings = get_settings()
    errors, _warnings = validate_and_prepare_runtime(settings)
    if errors:
        raise RuntimeError("Startup validation failed:\n- " + "\n- ".join(errors))

    auth = GmailAuthManager(
        credentials_path=settings.gmail.credentials_path,
        token_path=settings.gmail.token_path,
        scopes=settings.gmail.scopes,
    )
    service = build("gmail", "v1", credentials=auth.get_credentials())
    result = (
        service.users()
        .messages()
        .list(userId=settings.gmail.user_id, maxResults=max(1, min(args.max_results, 20)), q=args.query)
        .execute()
    )
    messages = result.get("messages", [])
    print(f"Fetched {len(messages)} messages.")

    for idx, msg in enumerate(messages, start=1):
        detail = (
            service.users()
            .messages()
            .get(userId=settings.gmail.user_id, id=msg["id"], format="metadata")
            .execute()
        )
        headers = extract_headers(detail.get("payload", {}).get("headers", []))
        row = {
            "index": idx,
            "message_id": detail.get("id"),
            "thread_id": detail.get("threadId"),
            "subject": headers["subject"],
            "from": headers["from"],
            "date": headers["date"],
        }
        print(json.dumps(row, ensure_ascii=False))


if __name__ == "__main__":
    main()
