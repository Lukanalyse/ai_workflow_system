from __future__ import annotations

import argparse
import json
import os
import pickle
from datetime import datetime
from typing import Any

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
]

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]

TOKEN_PATH = BASE_DIR / "token.json"
CREDS_PATH = BASE_DIR / "credentials" / "credentials.json"

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test Gmail API connection and list emails.")

    parser.add_argument("--max-results", type=int, default=5)
    parser.add_argument("--only-unread", action="store_true")
    parser.add_argument("--sender", default="")
    parser.add_argument("--keyword", default="")
    parser.add_argument("--after-date", default="")

    return parser.parse_args()


def build_query(args: argparse.Namespace) -> str:
    query_parts = []

    if args.only_unread:
        query_parts.append("is:unread")

    if args.sender:
        query_parts.append(f"from:{args.sender}")

    if args.keyword:
        query_parts.append(args.keyword)

    if args.after_date:
        parsed = datetime.strptime(args.after_date, "%Y-%m-%d")
        query_parts.append(f"after:{parsed.strftime('%Y/%m/%d')}")

    return " ".join(query_parts)


def authenticate_gmail():
    creds = None

    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, "rb") as token:
            creds = pickle.load(token)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDS_PATH,
                SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open(TOKEN_PATH, "wb") as token:
            pickle.dump(creds, token)

    return creds


def extract_headers(headers):
    lookup = {
        h["name"].lower(): h["value"]
        for h in headers
    }

    return {
        "from": lookup.get("from", ""),
        "subject": lookup.get("subject", ""),
        "date": lookup.get("date", ""),
    }


def main():
    load_dotenv()

    args = parse_args()

    query = build_query(args)

    creds = authenticate_gmail()

    service = build("gmail", "v1", credentials=creds)

    results = service.users().messages().list(
        userId="me",
        maxResults=args.max_results,
        q=query
    ).execute()

    messages = results.get("messages", [])

    print(f"\nFetched {len(messages)} emails.\n")

    for idx, msg in enumerate(messages, start=1):
        detail = service.users().messages().get(
            userId="me",
            id=msg["id"],
            format="metadata"
        ).execute()

        headers = extract_headers(
            detail["payload"]["headers"]
        )

        row = {
            "index": idx,
            "subject": headers["subject"],
            "from": headers["from"],
            "date": headers["date"],
            "snippet": detail.get("snippet", ""),
        }

        print(json.dumps(row, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()