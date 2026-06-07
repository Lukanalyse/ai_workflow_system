from __future__ import annotations

import logging
import time
import uuid
from typing import Iterator

from app.services.draft_service import DraftService
from app.services.email_service import EmailService

logger = logging.getLogger(__name__)

BULK_MAX = 100


class BulkService:
    """Sequentially generates (and optionally saves) drafts for many emails.

    Design guarantees required by the spec:
    - Sequential, one email at a time.
    - Error isolation: a failure on one email is recorded and the run continues.
    - Respects replyability: non-replyable candidates are skipped.
    - In save mode, already-handled emails are skipped (no duplicate drafts).
    Emits a stream of progress events so the UI can render a live progress bar
    and a final report. Token usage is tracked automatically downstream
    (DraftService -> LLMService), tagged with this run's run_id.
    """

    def __init__(self, *, email_service: EmailService, draft_service: DraftService) -> None:
        self._emails = email_service
        self._drafts = draft_service

    def run(
        self,
        *,
        count: int,
        mode: str,
        tone: str | None = None,
        language: str | None = None,
    ) -> Iterator[dict]:
        count = max(1, min(int(count), BULK_MAX))
        save = mode == "generate_save"
        run_id = uuid.uuid4().hex
        started = time.monotonic()

        try:
            candidates = self._emails.list_candidates(max_emails=count)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Bulk run failed to list candidates")
            yield {"type": "error", "error": f"Failed to list emails: {exc}"}
            return

        replyable = [c for c in candidates if c.replyable]
        skipped_nonreplyable = len(candidates) - len(replyable)
        total = len(replyable)

        analyzed = generated = saved = 0
        skipped = skipped_nonreplyable
        failures: list[dict] = []

        yield {
            "type": "start",
            "total": total,
            "requested": count,
            "mode": mode,
            "skipped_nonreplyable": skipped_nonreplyable,
            "run_id": run_id,
        }

        for index, candidate in enumerate(replyable, start=1):
            yield {
                "type": "progress",
                "index": index,
                "total": total,
                "message_id": candidate.id,
                "subject": candidate.subject,
                "status": "processing",
            }

            if save and candidate.already_processed:
                skipped += 1
                yield {
                    "type": "item",
                    "index": index,
                    "message_id": candidate.id,
                    "status": "skipped_already_handled",
                }
                continue

            try:
                email = self._emails.get_message(candidate.id)
                result = self._drafts.generate(
                    email, tone=tone, language=language, run_id=run_id
                )
                analyzed += 1
                generated += 1
                status = "generated"
                if save:
                    self._drafts.save(
                        email, draft_text=result.draft, summary=result.summary
                    )
                    saved += 1
                    status = "saved"
                yield {
                    "type": "item",
                    "index": index,
                    "message_id": candidate.id,
                    "status": status,
                }
            except Exception as exc:  # noqa: BLE001 - isolate per-email failures
                logger.exception("Bulk: failed processing %s", candidate.id)
                failures.append({"message_id": candidate.id, "error": str(exc)})
                yield {
                    "type": "item",
                    "index": index,
                    "message_id": candidate.id,
                    "status": "failed",
                    "error": str(exc),
                }

        duration = round(time.monotonic() - started, 2)
        yield {
            "type": "done",
            "report": {
                "emails_analyzed": analyzed,
                "drafts_generated": generated,
                "drafts_saved": saved,
                "skipped": skipped,
                "failures": len(failures),
                "failure_details": failures,
                "duration_seconds": duration,
            },
        }
