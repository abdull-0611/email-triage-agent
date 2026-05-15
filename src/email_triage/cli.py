"""Typer CLI entry point — init, process, and stats commands."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Optional

import typer
from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.table import Table

from .classifier import EmailClassifier
from .db import get_manual_review_count, get_stats, log_result, make_session_factory
from .gmail_client import GmailClient, authenticate

load_dotenv()

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="email-triage",
    help="Claude-powered Gmail triage: classify, label, and draft replies.",
    add_completion=False,
    no_args_is_help=True,
)
console = Console()

_DEFAULT_CREDS = Path("credentials.json")
_DEFAULT_TOKEN = Path("token.json")
_DEFAULT_DB = Path("triage.db")

# ---------------------------------------------------------------------------
# Category colour map for the results table
# ---------------------------------------------------------------------------

_CATEGORY_STYLE: dict[str, str] = {
    "urgent-action": "bold red",
    "needs-reply": "bold yellow",
    "reference-only": "cyan",
    "newsletter": "blue",
    "spam-likely": "dim",
}


def _style_category(category: str) -> str:
    style = _CATEGORY_STYLE.get(category, "white")
    return f"[{style}]{category}[/{style}]"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def init(
    credentials: Annotated[
        Path,
        typer.Option("--credentials", "-c", help="Path to credentials.json from Google Cloud Console."),
    ] = _DEFAULT_CREDS,
    token: Annotated[
        Path,
        typer.Option("--token", "-t", help="Where to save the OAuth token."),
    ] = _DEFAULT_TOKEN,
) -> None:
    """Authenticate with Gmail and save an OAuth token for future runs.

    On first run a browser window opens for authorisation.
    Subsequent runs refresh the token silently if needed.
    """
    if not credentials.exists():
        console.print(
            f"[red]credentials.json not found at {credentials}[/red]\n"
            "Download it from Google Cloud Console → APIs & Services → Credentials."
        )
        raise typer.Exit(code=1)

    if token.exists():
        console.print(f"[dim]Token already exists at {token}. Skipping OAuth flow.[/dim]")
    else:
        console.print("Opening browser for Gmail authorisation…")
        authenticate(credentials_path=credentials, token_path=token)
        console.print(f"[green]Token saved to {token}.[/green]")

    console.print("[bold green]Ready.[/bold green]")


@app.command()
def process(
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="Maximum number of unread emails to fetch.", min=1, max=500),
    ] = 20,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Classify only — no Gmail mutations, no DB writes."),
    ] = False,
    credentials: Annotated[
        Path,
        typer.Option("--credentials", "-c", help="Path to credentials.json."),
    ] = _DEFAULT_CREDS,
    token: Annotated[
        Path,
        typer.Option("--token", "-t", help="Path to token.json."),
    ] = _DEFAULT_TOKEN,
    db: Annotated[
        Path,
        typer.Option("--db", help="Path to the SQLite database."),
    ] = _DEFAULT_DB,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show reasoning and suggested action for each email."),
    ] = False,
) -> None:
    """Fetch unread emails, classify with Claude, and apply Gmail labels.

    Without --dry-run the command also:
    - Applies AI/* labels to each message in Gmail
    - Saves a draft reply for needs-reply messages (confidence >= 0.7)
    - Logs every result to the SQLite database atomically
    """
    if dry_run:
        console.print("[yellow bold]Dry-run mode[/yellow bold] — no Gmail changes, no DB writes.")

    console.print("Connecting to Gmail…")
    try:
        gmail = GmailClient(credentials_path=credentials, token_path=token)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    console.print(f"Fetching up to [bold]{limit}[/bold] unread messages…")
    emails = gmail.get_unread_messages(limit=limit)

    if not emails:
        console.print("[dim]Inbox is empty — nothing to process.[/dim]")
        raise typer.Exit()

    console.print(f"Found [bold]{len(emails)}[/bold] message(s). Classifying…\n")

    api_key = os.getenv("ANTHROPIC_API_KEY")
    classifier = EmailClassifier(api_key=api_key)

    results: list[dict] = []
    errors: list[str] = []

    for idx, email in enumerate(emails, start=1):
        short_subject = email.subject[:55] + ("…" if len(email.subject) > 55 else "")
        with console.status(f"[{idx}/{len(emails)}] {short_subject}"):
            try:
                classification = classifier.classify(email)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to classify %s: %s", email.message_id, exc)
                errors.append(f"{email.subject[:40]}: {exc}")
                continue

        label_applied = "(dry-run)"
        had_draft = False

        if not dry_run:
            try:
                label_applied = gmail.apply_triage_label(
                    message_id=email.message_id,
                    category=classification.category,
                    needs_human_review=classification.needs_human_review,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Label failed for %s: %s", email.message_id, exc)
                label_applied = "(label-error)"

            if classification.draft_reply:
                try:
                    gmail.create_draft_reply(
                        message_id=email.message_id,
                        thread_id=email.thread_id,
                        to_address=email.sender,
                        subject=email.subject,
                        reply_body=classification.draft_reply,
                    )
                    had_draft = True
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Draft failed for %s: %s", email.message_id, exc)
        else:
            had_draft = classification.draft_reply is not None

        results.append(
            {
                "email": email,
                "result": classification,
                "label_applied": label_applied,
                "had_draft": had_draft,
            }
        )

    # ------------------------------------------------------------------
    # Results table
    # ------------------------------------------------------------------
    table = Table(
        title=f"Triage Results — {len(results)} email(s)",
        box=box.ROUNDED,
        show_lines=True,
        header_style="bold",
    )
    table.add_column("#", style="dim", justify="right", width=3)
    table.add_column("Subject", max_width=38)
    table.add_column("Sender", style="blue", max_width=26)
    table.add_column("Category", no_wrap=True)
    table.add_column("Conf", justify="right", width=5)
    table.add_column("Review?", justify="center", width=7)
    table.add_column("Draft?", justify="center", width=6)
    table.add_column("Label", style="green", max_width=26)

    if verbose:
        table.add_column("Reasoning", max_width=42)

    for i, row in enumerate(results, start=1):
        r = row["result"]
        cells = [
            str(i),
            row["email"].subject,
            row["email"].sender,
            _style_category(r.category),
            f"{r.confidence:.2f}",
            "[red]YES[/red]" if r.needs_human_review else "[dim]no[/dim]",
            "[green]yes[/green]" if row["had_draft"] else "[dim]no[/dim]",
            row["label_applied"],
        ]
        if verbose:
            cells.append(r.reasoning)
        table.add_row(*cells)

    console.print(table)

    if errors:
        console.print(f"\n[red]Errors ({len(errors)}):[/red]")
        for err in errors:
            console.print(f"  [dim]•[/dim] {err}")

    # ------------------------------------------------------------------
    # Persist to DB (live mode only, atomic commit)
    # ------------------------------------------------------------------
    if not dry_run and results:
        Session = make_session_factory(db_path=db)
        with Session() as session:
            for row in results:
                log_result(
                    session,
                    message_id=row["email"].message_id,
                    thread_id=row["email"].thread_id,
                    subject=row["email"].subject,
                    sender=row["email"].sender,
                    category=row["result"].category,
                    confidence=row["result"].confidence,
                    reasoning=row["result"].reasoning,
                    suggested_action=row["result"].suggested_action,
                    label_applied=row["label_applied"],
                    had_draft_reply=row["had_draft"],
                    dry_run=False,
                )
            session.commit()
        console.print(f"\n[green]Logged {len(results)} result(s) to {db}.[/green]")
    elif dry_run:
        console.print(
            f"\n[yellow]Dry-run complete — {len(results)} classified, nothing written.[/yellow]"
        )


@app.command()
def stats(
    db: Annotated[
        Path,
        typer.Option("--db", help="Path to the SQLite database."),
    ] = _DEFAULT_DB,
    days: Annotated[
        int,
        typer.Option("--days", "-d", help="Number of past days to include.", min=1),
    ] = 30,
    include_dry_runs: Annotated[
        bool,
        typer.Option("--include-dry-runs", help="Include dry-run rows in the breakdown."),
    ] = False,
) -> None:
    """Show a per-category triage breakdown for the past N days."""
    if not db.exists():
        console.print(f"[dim]No database found at {db}. Run 'process' first.[/dim]")
        raise typer.Exit()

    since = datetime.now(timezone.utc) - timedelta(days=days)
    Session = make_session_factory(db_path=db)

    with Session() as session:
        stat_rows = get_stats(session, include_dry_runs=include_dry_runs, since=since)
        manual_count = get_manual_review_count(
            session, include_dry_runs=include_dry_runs, since=since
        )

    if not stat_rows:
        console.print(f"[dim]No triage data in the past {days} day(s).[/dim]")
        raise typer.Exit()

    total = sum(r.count for r in stat_rows)
    total_drafts = sum(r.draft_count for r in stat_rows)

    table = Table(
        title=f"Triage Stats — last {days} day(s)",
        box=box.ROUNDED,
        header_style="bold",
    )
    table.add_column("Category", style="magenta", no_wrap=True)
    table.add_column("Count", justify="right")
    table.add_column("%", justify="right", width=6)
    table.add_column("Avg Confidence", justify="right")
    table.add_column("Drafts", justify="right")

    for row in stat_rows:
        pct = f"{row.count / total * 100:.1f}%" if total else "—"
        table.add_row(
            _style_category(row.category),
            str(row.count),
            pct,
            f"{row.avg_confidence:.2f}",
            str(row.draft_count),
        )

    table.add_section()
    table.add_row("[bold]Total[/bold]", str(total), "100%", "", str(total_drafts))

    console.print(table)
    if manual_count:
        console.print(f"[yellow]Manual review flagged:[/yellow] {manual_count} message(s)")
    if include_dry_runs:
        console.print("[dim]Note: dry-run rows are included in these stats.[/dim]")
