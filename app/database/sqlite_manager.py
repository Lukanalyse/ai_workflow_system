from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# SQLite caps the number of bound variables per statement (default 999). Stay
# well under it when expanding ``IN (...)`` clauses for large inbox listings.
_SQL_VAR_CHUNK = 500


def _chunks(items: list[str], size: int = _SQL_VAR_CHUNK):
    for start in range(0, len(items), size):
        yield items[start : start + size]


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
class UsageEventRecord:
    """One LLM call. One email typically produces several (summarize + draft)."""

    timestamp: str
    provider: str
    model: str
    operation: str  # "summarize" | "draft" | ...
    email_message_id: str | None
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost: float
    currency: str
    run_id: str | None


@dataclass(slots=True)
class UsageSummary:
    emails_analyzed: int
    drafts_generated: int
    drafts_sent: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost: float


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
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        except (FileExistsError, NotADirectoryError, OSError) as exc:
            raise RuntimeError(
                f"Database folder {self.db_path.parent} could not be created "
                f"({exc}). Make sure data/ is a folder, not a file."
            ) from exc
        if self.db_path.is_dir():
            raise RuntimeError(
                f"The database path {self.db_path} is a directory, not a file. "
                "Remove the folder so the database file can be created."
            )
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        # A short busy timeout makes concurrent access (FastAPI runs sync
        # endpoints in a threadpool) wait briefly for a lock instead of failing
        # with "database is locked". ``synchronous=NORMAL`` is the recommended,
        # safe pairing with WAL and noticeably speeds up writes.
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            # Write-Ahead Logging: readers don't block the writer (and vice
            # versa), so the inbox listing's reads no longer contend with a
            # draft/analysis write. Persisted in the DB file header (set once).
            conn.execute("PRAGMA journal_mode = WAL")
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    email_message_id TEXT,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_cost REAL NOT NULL DEFAULT 0,
                    currency TEXT NOT NULL DEFAULT 'USD',
                    run_id TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_events_ts ON usage_events(timestamp)"
            )
            # Cache of the AI understanding layer — one row per analyzed message.
            # Keyed by message_id so an email is never re-analyzed unnecessarily.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS email_ai_analysis (
                    message_id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT 'Other',
                    priority TEXT NOT NULL DEFAULT 'Medium',
                    needs_reply INTEGER NOT NULL DEFAULT 0,
                    action_recommended TEXT NOT NULL DEFAULT 'Other',
                    confidence REAL NOT NULL DEFAULT 0,
                    model TEXT NOT NULL DEFAULT '',
                    analyzed_at TEXT NOT NULL
                )
                """
            )
            # Filing history — one row per filed email. Feeds the Learning
            # Engine (sender/domain -> label habits) and makes every decision
            # retrievable.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS filing_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL,
                    sender TEXT NOT NULL DEFAULT '',
                    domain TEXT NOT NULL DEFAULT '',
                    label TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_filing_sender ON filing_history(sender)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_filing_domain ON filing_history(domain)")
            # Expression index matching how senders are queried — known_senders()
            # and sender_seen() filter on LOWER(sender), so a plain column index
            # wouldn't be used. This turns those lookups from a full table scan
            # into an index seek as the processed-email history grows.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_gmail_sender_lower "
                "ON gmail_processed_emails(LOWER(sender))"
            )
            # Schema version marker for future migrations (idempotent set).
            conn.execute("PRAGMA user_version = 4")
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
                status = str(message_row["processed_status"])
                draft_created = bool(message_row["draft_created"])
                if draft_created or status in {"processed", "skipped"}:
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

    def sender_seen(self, sender_email: str) -> bool:
        """True if we have ever processed an email from this sender (known contact)."""
        sender = (sender_email or "").strip().lower()
        if not sender:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM gmail_processed_emails WHERE LOWER(sender) = ? LIMIT 1",
                (sender,),
            ).fetchone()
        return row is not None

    def seen_status_bulk(
        self, message_ids: list[str], thread_ids: list[str]
    ) -> tuple[set[str], set[str]]:
        """Bulk variant of :meth:`already_processed_gmail` for a whole list.

        Returns ``(seen_message_ids, threads_with_draft)`` so the caller can
        decide ``seen = id in seen_message_ids or thread_id in threads`` in
        memory — replacing N per-email queries with two batched ones. The
        per-email semantics are preserved exactly: a message counts as seen
        when a draft was created for it or its status is processed/skipped, and
        a thread counts when any of its rows already produced a draft.
        """
        ids = [m for m in dict.fromkeys(message_ids) if m]
        threads = [t for t in dict.fromkeys(thread_ids) if t]
        seen_messages: set[str] = set()
        draft_threads: set[str] = set()
        if not ids and not threads:
            return seen_messages, draft_threads
        with self._connect() as conn:
            for chunk in _chunks(ids):
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"""
                    SELECT message_id, processed_status, draft_created
                    FROM gmail_processed_emails
                    WHERE message_id IN ({placeholders})
                    """,
                    chunk,
                ).fetchall()
                for row in rows:
                    if bool(row["draft_created"]) or str(row["processed_status"]) in {
                        "processed",
                        "skipped",
                    }:
                        seen_messages.add(str(row["message_id"]))
            for chunk in _chunks(threads):
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"""
                    SELECT DISTINCT thread_id
                    FROM gmail_processed_emails
                    WHERE draft_created = 1 AND thread_id IN ({placeholders})
                    """,
                    chunk,
                ).fetchall()
                for row in rows:
                    draft_threads.add(str(row["thread_id"]))
        return seen_messages, draft_threads

    def known_senders(self, sender_emails: list[str]) -> set[str]:
        """Return the subset of senders we have ever processed (lowercased).

        Bulk variant of :meth:`sender_seen` — one query per chunk instead of one
        per email.
        """
        senders = [s.strip().lower() for s in sender_emails if (s or "").strip()]
        senders = list(dict.fromkeys(senders))
        known: set[str] = set()
        if not senders:
            return known
        with self._connect() as conn:
            for chunk in _chunks(senders):
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"""
                    SELECT DISTINCT LOWER(sender) AS sender
                    FROM gmail_processed_emails
                    WHERE LOWER(sender) IN ({placeholders})
                    """,
                    chunk,
                ).fetchall()
                for row in rows:
                    known.add(str(row["sender"]))
        return known

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

    def record_usage_event(self, record: UsageEventRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO usage_events (
                    timestamp, provider, model, operation, email_message_id,
                    input_tokens, output_tokens, total_tokens, estimated_cost,
                    currency, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.timestamp,
                    record.provider,
                    record.model,
                    record.operation,
                    record.email_message_id,
                    record.input_tokens,
                    record.output_tokens,
                    record.total_tokens,
                    record.estimated_cost,
                    record.currency,
                    record.run_id,
                ),
            )
            conn.commit()

    def usage_summary(self, *, since_iso: str) -> UsageSummary:
        """Aggregate usage from `since_iso` (inclusive) to now.

        Boundaries are passed as ISO-UTC strings produced by `now_iso`, so a
        lexical `timestamp >= ?` comparison is correct (identical format/tz).
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN operation = 'summarize' THEN 1 ELSE 0 END), 0) AS analyzed,
                    COALESCE(SUM(CASE WHEN operation = 'draft' THEN 1 ELSE 0 END), 0) AS generated,
                    COALESCE(SUM(CASE WHEN operation = 'send' THEN 1 ELSE 0 END), 0) AS sent,
                    COALESCE(SUM(input_tokens), 0) AS in_tok,
                    COALESCE(SUM(output_tokens), 0) AS out_tok,
                    COALESCE(SUM(total_tokens), 0) AS tot_tok,
                    COALESCE(SUM(estimated_cost), 0.0) AS cost
                FROM usage_events
                WHERE timestamp >= ?
                """,
                (since_iso,),
            ).fetchone()
        return UsageSummary(
            emails_analyzed=int(row["analyzed"]),
            drafts_generated=int(row["generated"]),
            drafts_sent=int(row["sent"]),
            input_tokens=int(row["in_tok"]),
            output_tokens=int(row["out_tok"]),
            total_tokens=int(row["tot_tok"]),
            cost=float(row["cost"]),
        )

    def usage_averages(self) -> tuple[float, float]:
        """Return (avg_input, avg_output) tokens per *email* across all history.

        One email = one summarize + one draft call, so averages for those two
        operations are summed. Returns (0, 0) when there is no history yet.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT operation,
                       AVG(input_tokens) AS avg_in,
                       AVG(output_tokens) AS avg_out
                FROM usage_events
                WHERE operation IN ('summarize', 'draft')
                GROUP BY operation
                """
            ).fetchall()
        avg_in = sum(float(r["avg_in"] or 0.0) for r in rows)
        avg_out = sum(float(r["avg_out"] or 0.0) for r in rows)
        return avg_in, avg_out

    def usage_breakdown(self, *, since_iso: str) -> dict:
        """Per-provider and per-model token/cost totals since `since_iso`."""
        with self._connect() as conn:
            providers = conn.execute(
                """
                SELECT provider,
                       COALESCE(SUM(total_tokens), 0) AS tokens,
                       COALESCE(SUM(estimated_cost), 0.0) AS cost
                FROM usage_events
                WHERE timestamp >= ? AND model != '-'
                GROUP BY provider
                ORDER BY cost DESC
                """,
                (since_iso,),
            ).fetchall()
            models = conn.execute(
                """
                SELECT provider, model,
                       COALESCE(SUM(total_tokens), 0) AS tokens,
                       COALESCE(SUM(estimated_cost), 0.0) AS cost
                FROM usage_events
                WHERE timestamp >= ? AND model != '-'
                GROUP BY provider, model
                ORDER BY cost DESC
                """,
                (since_iso,),
            ).fetchall()
        return {
            "providers": [
                {"provider": r["provider"], "tokens": int(r["tokens"]), "cost": round(float(r["cost"]), 4)}
                for r in providers
            ],
            "models": [
                {
                    "provider": r["provider"],
                    "model": r["model"],
                    "tokens": int(r["tokens"]),
                    "cost": round(float(r["cost"]), 4),
                }
                for r in models
            ],
        }

    def daily_costs(self, *, since_iso: str) -> list[dict]:
        """Daily cost totals since `since_iso` (ISO date prefix grouping)."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT substr(timestamp, 1, 10) AS day,
                       COALESCE(SUM(estimated_cost), 0.0) AS cost
                FROM usage_events
                WHERE timestamp >= ?
                GROUP BY day
                ORDER BY day
                """,
                (since_iso,),
            ).fetchall()
        return [{"date": r["day"], "cost": round(float(r["cost"]), 4)} for r in rows]

    def usage_cost_since(self, *, since_iso: str) -> float:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(estimated_cost), 0.0) AS cost FROM usage_events WHERE timestamp >= ?",
                (since_iso,),
            ).fetchone()
        return float(row["cost"])

    # --- AI analysis cache ---------------------------------------------------
    @staticmethod
    def _row_to_analysis(row: sqlite3.Row) -> dict:
        return {
            "summary": str(row["summary"]),
            "category": str(row["category"]),
            "priority": str(row["priority"]),
            "needs_reply": bool(row["needs_reply"]),
            "action_recommended": str(row["action_recommended"]),
            "confidence": float(row["confidence"]),
            "model": str(row["model"]),
            "analyzed_at": str(row["analyzed_at"]),
        }

    def save_email_analysis(self, message_id: str, analysis: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO email_ai_analysis (
                    message_id, summary, category, priority, needs_reply,
                    action_recommended, confidence, model, analyzed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    summary = excluded.summary,
                    category = excluded.category,
                    priority = excluded.priority,
                    needs_reply = excluded.needs_reply,
                    action_recommended = excluded.action_recommended,
                    confidence = excluded.confidence,
                    model = excluded.model,
                    analyzed_at = excluded.analyzed_at
                """,
                (
                    message_id,
                    str(analysis.get("summary", "")),
                    str(analysis.get("category", "Other")),
                    str(analysis.get("priority", "Medium")),
                    int(bool(analysis.get("needs_reply", False))),
                    str(analysis.get("action_recommended", "Other")),
                    float(analysis.get("confidence", 0.0)),
                    str(analysis.get("model", "")),
                    str(analysis.get("analyzed_at", "")),
                ),
            )
            conn.commit()

    def get_email_analysis(self, message_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM email_ai_analysis WHERE message_id = ? LIMIT 1",
                (message_id,),
            ).fetchone()
        return self._row_to_analysis(row) if row is not None else None

    def get_email_analysis_many(self, message_ids: list[str]) -> dict[str, dict]:
        ids = [m for m in dict.fromkeys(message_ids) if m]
        out: dict[str, dict] = {}
        if not ids:
            return out
        with self._connect() as conn:
            for chunk in _chunks(ids):
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT * FROM email_ai_analysis WHERE message_id IN ({placeholders})",
                    chunk,
                ).fetchall()
                for row in rows:
                    out[str(row["message_id"])] = self._row_to_analysis(row)
        return out

    # --- Filing history / learning ------------------------------------------
    def record_filing(
        self,
        *,
        message_id: str,
        sender: str,
        domain: str,
        label: str,
        source: str,
        confidence: float,
        created_at: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO filing_history (
                    message_id, sender, domain, label, source, confidence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (message_id, sender, domain, label, source, float(confidence), created_at),
            )
            conn.commit()

    def filing_label_counts_for_sender(self, sender: str) -> dict[str, int]:
        s = (sender or "").strip().lower()
        if not s:
            return {}
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT label, COUNT(*) AS n FROM filing_history
                WHERE sender = ? GROUP BY label
                """,
                (s,),
            ).fetchall()
        return {str(r["label"]): int(r["n"]) for r in rows}

    def filing_label_counts_for_domain(self, domain: str) -> dict[str, int]:
        d = (domain or "").strip().lower()
        if not d:
            return {}
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT label, COUNT(*) AS n FROM filing_history
                WHERE domain = ? GROUP BY label
                """,
                (d,),
            ).fetchall()
        return {str(r["label"]): int(r["n"]) for r in rows}

    def recent_filing_history(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT message_id, sender, domain, label, source, confidence, created_at
                FROM filing_history ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "message_id": str(r["message_id"]),
                "sender": str(r["sender"]),
                "domain": str(r["domain"]),
                "label": str(r["label"]),
                "source": str(r["source"]),
                "confidence": float(r["confidence"]),
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ]

    @staticmethod
    def now_iso() -> str:
        return datetime.now(tz=timezone.utc).isoformat()
