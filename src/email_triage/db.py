"""SQLite persistence layer — schema, session factory, and query helpers."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, String, Boolean, func, select, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class TriageLog(Base):
    """One row per email processed by the triage pipeline."""

    __tablename__ = "triage_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    message_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    thread_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    subject: Mapped[str] = mapped_column(String, nullable=False)
    sender: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    reasoning: Mapped[str] = mapped_column(String, nullable=False)
    suggested_action: Mapped[str] = mapped_column(String, nullable=False)
    label_applied: Mapped[str] = mapped_column(String, nullable=False)
    had_draft_reply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    def __repr__(self) -> str:
        return (
            f"<TriageLog id={self.id} category={self.category!r} "
            f"confidence={self.confidence:.2f} message_id={self.message_id!r}>"
        )


def make_engine(db_path: str | Path = "triage.db"):
    """Create and return a SQLAlchemy engine pointed at *db_path*.

    The database file and all tables are created on first call if they do
    not already exist.

    Args:
        db_path: Path to the SQLite file.  Defaults to ``triage.db`` in the
            current working directory.

    Returns:
        A configured ``sqlalchemy.engine.Engine`` instance.
    """
    url = f"sqlite:///{Path(db_path).resolve()}"
    engine = create_engine(url, echo=False)
    Base.metadata.create_all(engine)
    logger.debug("Database ready at %s", url)
    return engine


def make_session_factory(db_path: str | Path = "triage.db") -> sessionmaker[Session]:
    """Return a session factory bound to the database at *db_path*.

    Args:
        db_path: Path to the SQLite file.

    Returns:
        A ``sessionmaker`` that produces ``Session`` objects.
    """
    engine = make_engine(db_path)
    return sessionmaker(bind=engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------


def log_result(
    session: Session,
    *,
    message_id: str,
    thread_id: str,
    subject: str,
    sender: str,
    category: str,
    confidence: float,
    reasoning: str,
    suggested_action: str,
    label_applied: str,
    had_draft_reply: bool,
    dry_run: bool,
) -> TriageLog:
    """Insert a single triage result into the database and return the row.

    The session is flushed but **not committed** — the caller controls the
    transaction so that a batch of results can be committed atomically.

    Args:
        session: An active SQLAlchemy session.
        message_id: Gmail message ID.
        thread_id: Gmail thread ID.
        subject: Email subject line.
        sender: Sender address/name.
        category: Triage category string.
        confidence: Classifier confidence (0–1).
        reasoning: One-sentence reasoning from the classifier.
        suggested_action: Short imperative action phrase.
        label_applied: The Gmail label name that was applied.
        had_draft_reply: Whether a draft reply was created.
        dry_run: True when no Gmail mutations were made.

    Returns:
        The newly inserted :class:`TriageLog` row (with ``id`` populated).
    """
    row = TriageLog(
        message_id=message_id,
        thread_id=thread_id,
        subject=subject,
        sender=sender,
        category=category,
        confidence=confidence,
        reasoning=reasoning,
        suggested_action=suggested_action,
        label_applied=label_applied,
        had_draft_reply=had_draft_reply,
        dry_run=dry_run,
    )
    session.add(row)
    session.flush()
    return row


# ---------------------------------------------------------------------------
# Read / stats helpers
# ---------------------------------------------------------------------------


class CategoryStats:
    """Aggregated stats for a single triage category."""

    def __init__(self, category: str, count: int, avg_confidence: float, draft_count: int) -> None:
        self.category = category
        self.count = count
        self.avg_confidence = avg_confidence
        self.draft_count = draft_count

    def __repr__(self) -> str:
        return (
            f"<CategoryStats category={self.category!r} count={self.count} "
            f"avg_confidence={self.avg_confidence:.2f} drafts={self.draft_count}>"
        )


def get_stats(
    session: Session,
    *,
    include_dry_runs: bool = False,
    since: Optional[datetime] = None,
) -> list[CategoryStats]:
    """Return per-category counts, average confidence, and draft totals.

    Args:
        session: An active SQLAlchemy session.
        include_dry_runs: If False (default), dry-run rows are excluded so
            stats reflect only real pipeline runs.
        since: If provided, only rows with ``processed_at >= since`` are included.

    Returns:
        List of :class:`CategoryStats` sorted by count descending.
    """
    stmt = (
        select(
            TriageLog.category,
            func.count(TriageLog.id).label("count"),
            func.avg(TriageLog.confidence).label("avg_confidence"),
            func.sum(TriageLog.had_draft_reply.cast(Integer)).label("draft_count"),
        )
        .group_by(TriageLog.category)
        .order_by(func.count(TriageLog.id).desc())
    )

    if not include_dry_runs:
        stmt = stmt.where(TriageLog.dry_run.is_(False))
    if since is not None:
        stmt = stmt.where(TriageLog.processed_at >= since)

    rows = session.execute(stmt).all()
    return [
        CategoryStats(
            category=row.category,
            count=row.count,
            avg_confidence=row.avg_confidence or 0.0,
            draft_count=row.draft_count or 0,
        )
        for row in rows
    ]


def get_manual_review_count(
    session: Session,
    *,
    include_dry_runs: bool = False,
    since: Optional[datetime] = None,
) -> int:
    """Return the number of messages flagged for manual review.

    Args:
        session: An active SQLAlchemy session.
        include_dry_runs: If False (default), dry-run rows are excluded.
        since: If provided, only rows with ``processed_at >= since`` are counted.

    Returns:
        Integer count of ``AI/Manual-Review`` labelled messages.
    """
    from .gmail_client import MANUAL_REVIEW_LABEL

    stmt = select(func.count(TriageLog.id)).where(
        TriageLog.label_applied == MANUAL_REVIEW_LABEL
    )
    if not include_dry_runs:
        stmt = stmt.where(TriageLog.dry_run.is_(False))
    if since is not None:
        stmt = stmt.where(TriageLog.processed_at >= since)

    return session.execute(stmt).scalar_one()
