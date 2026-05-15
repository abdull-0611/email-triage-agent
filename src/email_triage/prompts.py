"""Prompt templates and Claude tool definitions for classification and reply drafting."""

from typing import Any

CLASSIFICATION_SYSTEM_PROMPT = """\
You are an expert email triage assistant. Your job is to classify incoming emails
accurately and concisely so a busy professional can process their inbox efficiently.

Classification categories:
- urgent-action  : Requires immediate response or action (deadlines, emergencies, blocking issues)
- needs-reply    : Expects a response but is not time-critical (questions, invitations, follow-ups)
- reference-only : Informational only — no reply needed (receipts, notifications, confirmations)
- newsletter     : Marketing, digest, or bulk-sent content the user subscribed to
- spam-likely    : Unsolicited, suspicious, or clearly unwanted email

Rules:
1. Be conservative with confidence — only score ≥0.9 when the signal is unambiguous.
2. If the email could reasonably belong to two categories, pick the higher-priority one
   (urgent-action > needs-reply > reference-only > newsletter > spam-likely).
3. Keep reasoning to one sentence focused on the deciding signal.
4. suggested_action must be an imperative phrase under 12 words.\
"""

CLASSIFICATION_TOOL: dict[str, Any] = {
    "name": "classify_email",
    "description": (
        "Classify an email and return structured triage metadata. "
        "Call this tool exactly once per email."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": [
                    "urgent-action",
                    "needs-reply",
                    "reference-only",
                    "newsletter",
                    "spam-likely",
                ],
                "description": "The triage category that best fits this email.",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "How confident you are in this classification (0–1).",
            },
            "reasoning": {
                "type": "string",
                "description": "One sentence explaining the deciding signal for this category.",
            },
            "suggested_action": {
                "type": "string",
                "description": "Short imperative action phrase (≤12 words).",
            },
        },
        "required": ["category", "confidence", "reasoning", "suggested_action"],
    },
}

DRAFT_REPLY_SYSTEM_PROMPT = """\
You are a professional email assistant. Write a concise, polite reply to the email below.

Guidelines:
- Match the tone of the original (formal if formal, casual if casual).
- Be direct — no filler phrases like "I hope this email finds you well".
- Keep the reply under 150 words unless the original demands more detail.
- Do not invent facts or commitments; use placeholders like [DATE] or [DETAILS] where needed.
- Output only the reply body — no subject line, no greeting prefix, no sign-off instructions.\
"""


def build_classification_message(subject: str, sender: str, body: str) -> str:
    """Format an email into the user-turn message for the classification call.

    Args:
        subject: Email subject line.
        sender: Sender display name and/or address.
        body: Plain-text email body (truncated upstream if necessary).

    Returns:
        Formatted string ready to pass as a user message to Claude.
    """
    return (
        f"From: {sender}\n"
        f"Subject: {subject}\n"
        f"---\n"
        f"{body.strip()}"
    )


def build_draft_reply_message(subject: str, sender: str, body: str) -> str:
    """Format an email into the user-turn message for the draft-reply call.

    Args:
        subject: Email subject line.
        sender: Sender display name and/or address.
        body: Plain-text email body.

    Returns:
        Formatted string ready to pass as a user message to Claude.
    """
    return (
        f"Original email\n"
        f"From: {sender}\n"
        f"Subject: {subject}\n"
        f"---\n"
        f"{body.strip()}\n\n"
        f"Write a reply to this email."
    )
