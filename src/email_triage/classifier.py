"""Claude-based email classifier using structured tool_use output."""

from __future__ import annotations

import logging
from typing import Literal, Optional

import anthropic
from pydantic import BaseModel, Field, field_validator

from .prompts import (
    CLASSIFICATION_SYSTEM_PROMPT,
    CLASSIFICATION_TOOL,
    DRAFT_REPLY_SYSTEM_PROMPT,
    build_classification_message,
    build_draft_reply_message,
)

logger = logging.getLogger(__name__)

# Model constants — changed here, changed everywhere
_CLASSIFY_MODEL = "claude-haiku-4-5-20251001"
_DRAFT_MODEL = "claude-sonnet-4-6"

# Body is truncated before sending to keep token cost low
_MAX_BODY_CHARS = 4_000


class EmailInput(BaseModel):
    """Minimal email data required for classification."""

    message_id: str
    subject: str
    sender: str
    body: str = Field(default="")

    @field_validator("body")
    @classmethod
    def truncate_body(cls, v: str) -> str:
        """Keep body within token budget."""
        return v[:_MAX_BODY_CHARS]


class ClassificationResult(BaseModel):
    """Structured output produced by the classifier for a single email."""

    category: Literal[
        "urgent-action",
        "needs-reply",
        "reference-only",
        "newsletter",
        "spam-likely",
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    suggested_action: str
    draft_reply: Optional[str] = None

    @property
    def needs_human_review(self) -> bool:
        """True when confidence is below the threshold for automatic labeling."""
        return self.confidence < 0.7


class EmailClassifier:
    """Classifies emails via Claude and optionally drafts replies.

    Uses ``claude-haiku-4-5-20251001`` for classification (fast, cheap) and
    ``claude-sonnet-4-6`` for reply drafting (higher quality prose).

    Args:
        api_key: Anthropic API key. Reads ``ANTHROPIC_API_KEY`` env var if omitted.
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)

    def classify(self, email: EmailInput) -> ClassificationResult:
        """Classify a single email and draft a reply if the category warrants one.

        The classification is obtained via Claude's tool_use feature so the
        response is always structured — no fragile JSON parsing of free text.
        A second Sonnet call is made only when ``category == "needs-reply"``.

        Args:
            email: The email to classify.

        Returns:
            A fully populated ``ClassificationResult``.

        Raises:
            ValueError: If Claude does not return a tool_use block (unexpected).
            anthropic.APIError: On any unrecoverable API error.
        """
        logger.debug("Classifying message_id=%s subject=%r", email.message_id, email.subject)

        result = self._call_classify(email)

        if result.category == "needs-reply" and not result.needs_human_review:
            result.draft_reply = self._call_draft_reply(email)

        logger.info(
            "message_id=%s category=%s confidence=%.2f review=%s",
            email.message_id,
            result.category,
            result.confidence,
            result.needs_human_review,
        )
        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _call_classify(self, email: EmailInput) -> ClassificationResult:
        """Run the Haiku classification call and parse the tool_use block."""
        response = self._client.messages.create(
            model=_CLASSIFY_MODEL,
            max_tokens=512,
            system=CLASSIFICATION_SYSTEM_PROMPT,
            tools=[CLASSIFICATION_TOOL],
            tool_choice={"type": "any"},
            messages=[
                {
                    "role": "user",
                    "content": build_classification_message(
                        email.subject, email.sender, email.body
                    ),
                }
            ],
        )

        for block in response.content:
            if block.type == "tool_use" and block.name == "classify_email":
                return ClassificationResult(**block.input)

        raise ValueError(
            f"Claude did not return a classify_email tool_use block. "
            f"stop_reason={response.stop_reason!r}"
        )

    def _call_draft_reply(self, email: EmailInput) -> str:
        """Generate a draft reply with Sonnet and return the reply body text."""
        response = self._client.messages.create(
            model=_DRAFT_MODEL,
            max_tokens=512,
            system=DRAFT_REPLY_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": build_draft_reply_message(
                        email.subject, email.sender, email.body
                    ),
                }
            ],
        )
        return response.content[0].text.strip()


# ---------------------------------------------------------------------------
# Smoke test — run with: uv run python -m email_triage.classifier
# ---------------------------------------------------------------------------

_TEST_EMAILS: list[dict[str, str]] = [
    {
        "message_id": "test-001",
        "subject": "URGENT: Production database is down",
        "sender": "ops-alerts@company.com",
        "body": (
            "Hi team,\n\n"
            "Our primary production database went offline at 14:32 UTC. "
            "Customer-facing services are impacted. Need all hands on deck immediately.\n\n"
            "— Ops Bot"
        ),
    },
    {
        "message_id": "test-002",
        "subject": "Coffee catch-up?",
        "sender": "sarah.chen@example.com",
        "body": (
            "Hey! It's been a while — would you be up for a coffee chat sometime next week? "
            "Happy to work around your schedule."
        ),
    },
    {
        "message_id": "test-003",
        "subject": "Your order #98432 has shipped",
        "sender": "no-reply@shopexample.com",
        "body": (
            "Great news! Your order has shipped via FedEx. "
            "Tracking number: 7489234892. Expected delivery: May 18."
        ),
    },
]


def _run_smoke_test() -> None:
    import os
    from dotenv import load_dotenv
    from rich.console import Console
    from rich.table import Table

    load_dotenv()
    console = Console()
    classifier = EmailClassifier(api_key=os.getenv("ANTHROPIC_API_KEY"))

    table = Table(title="Classifier smoke test", show_lines=True)
    table.add_column("Subject", style="cyan", max_width=35)
    table.add_column("Category", style="magenta")
    table.add_column("Conf", justify="right")
    table.add_column("Review?", justify="center")
    table.add_column("Reasoning", max_width=45)
    table.add_column("Draft reply", max_width=40)

    for raw in _TEST_EMAILS:
        email = EmailInput(**raw)
        result = classifier.classify(email)
        table.add_row(
            email.subject,
            result.category,
            f"{result.confidence:.2f}",
            "YES" if result.needs_human_review else "no",
            result.reasoning,
            (result.draft_reply or "")[:120] + ("…" if result.draft_reply and len(result.draft_reply) > 120 else ""),
        )

    console.print(table)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _run_smoke_test()
