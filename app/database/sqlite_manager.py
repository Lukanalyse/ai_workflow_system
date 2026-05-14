from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(slots=True)
class ProcessedEmailRecord:
    message_id: str
    subject: str
    sender: str
    received_at: str
    summary: str
    intent_label: str
    urgency_score: int
    draft_text: str
    confidence_score: float
    draft_id: str | None
    created_at: str


@dataclass(slots=True)
class GmailProcessedEmailRecord:
    message_id: str
    thread_id: str
    subject: str
    sender: str
    received_at: str
    snippet: str
    processed_status: str
    draft_created: bool
    draft_id: str | None
    skip_reason: str | None
    summary: str | None
    intent_label: str | None
    urgency_score: int | None
    confidence_score: float | None
    draft_text: str | None
    created_at: str
    updated_at: str


class SQLiteManager:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_emails (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL UNIQUE,
                    subject TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    intent_label TEXT NOT NULL,
                    urgency_score INTEGER NOT NULL,
                    draft_text TEXT NOT NULL,
                    confidence_score REAL NOT NULL,
                    draft_id TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gmail_processed_emails (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL UNIQUE,
                    thread_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    snippet TEXT NOT NULL,
                    processed_status TEXT NOT NULL,
                    draft_created INTEGER NOT NULL DEFAULT 0,
                    draft_id TEXT,
                    skip_reason TEXT,
                    summary TEXT,
                    intent_label TEXT,
                    urgency_score INTEGER,
                    confidence_score REAL,
                    draft_text TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_gmail_processed_thread
                ON gmail_processed_emails(thread_id)
                """
            )
            conn.commit()

    def already_processed(self, message_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_emails WHERE message_id = ? LIMIT 1",
                (message_id,),
            ).fetchone()
        return row is not None

    def save_processed_email(self, record: ProcessedEmailRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO processed_emails (
                    message_id, subject, sender, received_at, summary, intent_label,
                    urgency_score, draft_text, confidence_score, draft_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    summary = excluded.summary,
                    intent_label = excluded.intent_label,
                    urgency_score = excluded.urgency_score,
                    draft_text = excluded.draft_text,
                    confidence_score = excluded.confidence_score,
                    draft_id = excluded.draft_id
                """,
                (
                    record.message_id,
                    record.subject,
                    record.sender,
                    record.received_at,
                    record.summary,
                    record.intent_label,
                    record.urgency_score,
                    record.draft_text,
                    record.confidence_score,
                    record.draft_id,
                    record.created_at,
                ),
            )
            conn.commit()

    def already_processed_gmail(self, message_id: str, thread_id: str) -> tuple[bool, str | None]:
        with self._connect() as conn:
            message_row = conn.execute(
                """
                SELECT processed_status, draft_created
                FROM gmail_processed_emails
                WHERE message_id = ?
                LIMIT 1
                """,
                (message_id,),
            ).fetchone()
            if message_row is not None:
                return True, "message_already_seen"

            thread_row = conn.execute(
                """
                SELECT draft_id
                FROM gmail_processed_emails
                WHERE thread_id = ? AND draft_created = 1
                LIMIT 1
                """,
                (thread_id,),
            ).fetchone()
            if thread_row is not None:
                return True, "thread_draft_already_created"
        return False, None

    def save_gmail_processed_email(self, record: GmailProcessedEmailRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO gmail_processed_emails (
                    message_id, thread_id, subject, sender, received_at, snippet,
                    processed_status, draft_created, draft_id, skip_reason,
                    summary, intent_label, urgency_score, confidence_score, draft_text,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    processed_status = excluded.processed_status,
                    draft_created = excluded.draft_created,
                    draft_id = excluded.draft_id,
                    skip_reason = excluded.skip_reason,
                    summary = excluded.summary,
                    intent_label = excluded.intent_label,
                    urgency_score = excluded.urgency_score,
                    confidence_score = excluded.confidence_score,
                    draft_text = excluded.draft_text,
                    updated_at = excluded.updated_at
                """,
                (
                    record.message_id,
                    record.thread_id,
                    record.subject,
                    record.sender,
                    record.received_at,
                    record.snippet,
                    record.processed_status,
                    int(record.draft_created),
                    record.draft_id,
                    record.skip_reason,
                    record.summary,
                    record.intent_label,
                    record.urgency_score,
                    record.confidence_score,
                    record.draft_text,
                    record.created_at,
                    record.updated_at,
                ),
            )
            conn.commit()

    def list_recent(self, limit: int = 50) -> list[ProcessedEmailRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT message_id, subject, sender, received_at, summary, intent_label,
                       urgency_score, draft_text, confidence_score, draft_id, created_at
                FROM processed_emails
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [ProcessedEmailRecord(**dict(row)) for row in rows]

    def list_recent_gmail(self, limit: int = 50) -> list[GmailProcessedEmailRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    message_id, thread_id, subject, sender, received_at, snippet,
                    processed_status, draft_created, draft_id, skip_reason,
                    summary, intent_label, urgency_score, confidence_score, draft_text,
                    created_at, updated_at
                FROM gmail_processed_emails
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        records: list[GmailProcessedEmailRecord] = []
        for row in rows:
            payload = dict(row)
            payload["draft_created"] = bool(payload["draft_created"])
            records.append(GmailProcessedEmailRecord(**payload))
        return records

    @staticmethod
    def now_iso() -> str:
        return datetime.now(tz=timezone.utc).isoformat()
