from __future__ import annotations

from dataclasses import dataclass, field

from app.config.user_config import UserConfig
from app.email.gmail_reader import AUTOMATION_SENDER_MARKERS, NOREPLY_PATTERN
from app.providers.base import EmailMessage

# Built-in marketing/automation markers always considered (in addition to the
# user's editable ignore_keywords).
BUILTIN_NEWSLETTER_MARKERS = {
    "unsubscribe",
    "view in browser",
    "manage preferences",
    "you are receiving this email",
}


@dataclass(slots=True)
class ScoreResult:
    score: int
    replyable: bool
    classification: str
    reasons: list[str] = field(default_factory=list)
    reply_reason: str = ""


def _matches(sender: str, patterns: list[str]) -> bool:
    sender = sender.lower()
    return any(p and p in sender for p in patterns)


class ReplyabilityScorer:
    """Config-driven replyability scoring.

    Produces a numeric score, a binary classification (score >= threshold), and
    human-readable reasons. Banned senders are hard-filtered; allowed senders
    are always replyable (whitelist overrides heuristics).
    """

    def __init__(self, config: UserConfig) -> None:
        self._c = config

    def score(
        self,
        email: EmailMessage,
        *,
        known_sender: bool = False,
        thread_seen: bool = False,
    ) -> ScoreResult:
        c = self._c
        w = c.replyability_weights
        sender = (email.sender_email or "").lower()
        subject = (email.subject or "").lower()
        snippet = (email.snippet or "").lower()
        body = (email.body_text or "").lower()
        content = f"{subject}\n{snippet}\n{body}"
        labels = {label.upper() for label in email.label_ids}

        # --- Hard rules --------------------------------------------------------
        if _matches(sender, c.banned_senders):
            return ScoreResult(
                score=0,
                replyable=False,
                classification="Filtered (banned sender)",
                reasons=["✗ Banned sender"],
                reply_reason="banned_sender",
            )

        whitelisted = _matches(sender, c.allowed_senders)

        # --- Rule signals ------------------------------------------------------
        has_question = "?" in subject or "?" in body or "?" in snippet
        keyword_hits = [kw for kw in c.ignore_keywords if kw in content]
        builtin_hits = any(m in content for m in BUILTIN_NEWSLETTER_MARKERS)
        promo_label = bool(labels & {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "SPAM"})
        is_newsletter = bool(keyword_hits) or builtin_hits or promo_label

        is_noreply = bool(NOREPLY_PATTERN.search(sender))
        is_automation = any(domain in sender for domain in AUTOMATION_SENDER_MARKERS)
        negative_sender = is_noreply or is_automation

        known = known_sender or thread_seen or whitelisted
        valid_personal = "@" in sender and not negative_sender and not is_newsletter

        score = 0
        reasons: list[str] = []

        if whitelisted:
            reasons.append("✓ Whitelisted sender")

        if has_question:
            score += w.question_detected
            reasons.append(f"✓ Question detected ({_fmt(w.question_detected)})")

        if known:
            score += w.known_contact
            if thread_seen:
                reasons.append(f"✓ Previous conversation ({_fmt(w.known_contact)})")
            else:
                reasons.append(f"✓ Known contact ({_fmt(w.known_contact)})")

        if valid_personal:
            score += w.personal_sender
            reasons.append(f"✓ Personal sender ({_fmt(w.personal_sender)})")

        if is_newsletter:
            score += w.contains_newsletter
            label = keyword_hits[0] if keyword_hits else "marketing/newsletter"
            reasons.append(f"✗ Newsletter keyword: {label} ({_fmt(w.contains_newsletter)})")

        if negative_sender:
            score += w.noreply_sender
            tag = "No-reply sender" if is_noreply else "Automated/marketing sender"
            reasons.append(f"✗ {tag} ({_fmt(w.noreply_sender)})")

        replyable = whitelisted or score >= c.replyability_threshold
        classification = "Reply required" if replyable else "No reply needed"
        if not reasons:
            reasons.append("• No strong signals detected")

        return ScoreResult(
            score=score,
            replyable=replyable,
            classification=classification,
            reasons=reasons,
            reply_reason=classification.lower().replace(" ", "_"),
        )


def _fmt(value: int) -> str:
    return f"+{value}" if value >= 0 else str(value)
