"""Live Kanban status report for gateway slash commands."""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Iterable

_STATUS_ORDER = (
    "triage",
    "todo",
    "scheduled",
    "ready",
    "running",
    "review",
    "blocked",
    "done",
)

_STATUS_LABELS = {
    "triage": "🧭 Triage",
    "todo": "🟦 Todo",
    "scheduled": "📅 Scheduled",
    "ready": "🟦 Ready",
    "running": "🟡 Running",
    "review": "🔎 Review",
    "blocked": "🔴 Blocked",
    "done": "✅ Done",
    "archived": "🗄️ Archived",
}

_ATTENTION_STATUSES = {"blocked", "review", "running"}
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"\s+")
_URL_RE = re.compile(r"https?://[^\s)]+", re.IGNORECASE)

_MAX_TASK_TITLE = 180
_MAX_NOTE = 220
_DEFAULT_MAX_CHARS = 28_000
_DEFAULT_MAX_TASKS_PER_STATUS = 40
_DONE_RETENTION = timedelta(days=30)


@dataclass(frozen=True)
class BoardReport:
    slug: str
    name: str
    task_count: int
    counts: Counter[str]
    sections: dict[str, list[object]]
    warning: str | None = None
    error: str | None = None


def _clean_display_text(value: object, *, max_len: int = 240) -> str:
    """Return one-line inert display text for untrusted board/task fields."""
    text = "" if value is None else str(value)
    text = _CONTROL_CHARS_RE.sub("", text)
    text = text.replace("`", "'").replace("<", "‹").replace(">", "›")
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if len(text) > max_len:
        return text[: max_len - 1].rstrip() + "…"
    return text


def _local_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now().astimezone()
    if now.tzinfo is None:
        return now.astimezone()
    return now


def _fmt_timestamp(now: datetime | None = None) -> str:
    return _local_now(now).strftime("%Y-%m-%d %H:%M %Z")


def _parse_timestamp(value: object, *, local_tz: tzinfo | None) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        timestamp = float(value)
        if not math.isfinite(timestamp):
            return None
        return datetime.fromtimestamp(timestamp, tz=local_tz)
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _compact_age(when: datetime, *, now: datetime) -> str:
    seconds = now.timestamp() - when.timestamp()
    future = seconds < 0
    seconds = abs(seconds)
    if seconds >= 86_400:
        amount = f"{int(seconds // 86_400)}d"
    elif seconds >= 3_600:
        amount = f"{int(seconds // 3_600)}h"
    elif seconds >= 60:
        amount = f"{int(seconds // 60)}m"
    else:
        return "just now"
    return f"in {amount}" if future else f"{amount} ago"


def _task_date_detail(task: object, status: str, *, now: datetime) -> str | None:
    if status == "done":
        label = "completed"
        value = getattr(task, "completed_at", None)
    elif status == "running":
        started = _parse_timestamp(
            getattr(task, "started_at", None), local_tz=now.tzinfo
        )
        if started is not None:
            return f"started: {started:%Y-%m-%d} • {_compact_age(started, now=now)}"
        label = "created"
        value = getattr(task, "created_at", None)
    elif status == "scheduled":
        for field in ("scheduled_at", "schedule_at", "schedule_timestamp"):
            scheduled = _parse_timestamp(
                getattr(task, field, None), local_tz=now.tzinfo
            )
            if scheduled is not None:
                return f"scheduled: {scheduled:%Y-%m-%d} • {_compact_age(scheduled, now=now)}"
        label = "created"
        value = getattr(task, "created_at", None)
    else:
        label = "created"
        value = getattr(task, "created_at", None)

    when = _parse_timestamp(value, local_tz=now.tzinfo)
    if when is None:
        return None
    return f"{label}: {when:%Y-%m-%d} • {_compact_age(when, now=now)}"


def _status_label(status: str) -> str:
    return _STATUS_LABELS.get(status, f"• {status.title()}")


def _inline_code(value: object, *, max_len: int = 80) -> str:
    """Return Telegram/Slack-friendly inline-code text for a display token."""
    return f"`{_clean_display_text(value, max_len=max_len)}`"


def _status_sort_key(status: str) -> tuple[int, str]:
    try:
        return (_STATUS_ORDER.index(status), status)
    except ValueError:
        return (len(_STATUS_ORDER), status)


def _extract_task_cues(task: object, comments: Iterable[object]) -> list[str]:
    cues: list[str] = []
    status = str(getattr(task, "status", "") or "")
    failure = _clean_display_text(
        getattr(task, "last_failure_error", None), max_len=_MAX_NOTE
    )
    if failure:
        cues.append(f"⚠️ last failure: {failure}")
    if status == "blocked":
        for comment in reversed(list(comments)):
            body = _clean_display_text(getattr(comment, "body", ""), max_len=_MAX_NOTE)
            if body:
                cues.append(f"note: {body}")
                break
    for source in (getattr(task, "result", None), getattr(task, "body", None)):
        text = str(source or "")
        if not text:
            continue
        url_match = _URL_RE.search(text)
        if url_match:
            cues.append(f"🔗 {url_match.group(0)}")
            break
    return cues[:2]


def _explicit_board_db_path(slug: str) -> Path:
    """Resolve a board DB path without honoring inherited worker/gateway env pins."""
    from hermes_cli.kanban_db import DEFAULT_BOARD, board_dir, kanban_home

    if slug == DEFAULT_BOARD:
        return kanban_home() / "kanban.db"
    return board_dir(slug) / "kanban.db"


def _should_archive_done_task(task: object, *, now: datetime) -> bool:
    if getattr(task, "status", None) != "done":
        return False
    completed = _parse_timestamp(
        getattr(task, "completed_at", None), local_tz=now.tzinfo
    )
    return (
        completed is not None
        and now.timestamp() - completed.timestamp() > _DONE_RETENTION.total_seconds()
    )


def collect_kanban_status_data(
    *,
    include_archived_tasks: bool = False,
    now: datetime | None = None,
) -> list[BoardReport]:
    """Collect live status data for every active/non-archived Kanban board."""
    from hermes_cli.kanban_db import (
        archive_task,
        connect_closing,
        list_boards,
        list_tasks,
    )

    now = _local_now(now)
    reports: list[BoardReport] = []
    for meta in list_boards(include_archived=False):
        slug = _clean_display_text(meta.get("slug") or "default", max_len=80)
        name = _clean_display_text(meta.get("name") or slug, max_len=120)
        try:
            with connect_closing(db_path=_explicit_board_db_path(slug)) as conn:
                cleanup_errors: list[str] = []
                active_tasks = list_tasks(
                    conn, include_archived=False, order_by="status"
                )
                for task in active_tasks:
                    if _should_archive_done_task(task, now=now):
                        try:
                            archive_task(
                                conn,
                                getattr(task, "id", ""),
                                expected_status="done",
                            )
                        except Exception as exc:
                            cleanup_errors.append(
                                f"{type(exc).__name__}: {_clean_display_text(exc, max_len=160)}"
                            )
                tasks = list_tasks(
                    conn,
                    include_archived=include_archived_tasks,
                    order_by="status",
                )
                sections: dict[str, list[object]] = defaultdict(list)
                counts: Counter[str] = Counter()
                for task in tasks:
                    status = str(getattr(task, "status", "") or "unknown")
                    counts[status] += 1
                    sections[status].append(task)
                reports.append(
                    BoardReport(
                        slug=slug,
                        name=name,
                        task_count=len(tasks),
                        counts=counts,
                        sections=dict(sections),
                        warning=(
                            f"Could not archive {len(cleanup_errors)} expired Done task(s): "
                            f"{cleanup_errors[0]}"
                            if cleanup_errors
                            else None
                        ),
                    )
                )
        except Exception as exc:
            reports.append(
                BoardReport(
                    slug=slug,
                    name=name,
                    task_count=0,
                    counts=Counter(),
                    sections={},
                    error=f"{type(exc).__name__}: {_clean_display_text(exc, max_len=220)}",
                )
            )
    return reports


def render_kanban_status_report(
    reports: list[BoardReport] | None = None,
    *,
    now: datetime | None = None,
    max_chars: int = _DEFAULT_MAX_CHARS,
    max_tasks_per_status: int = _DEFAULT_MAX_TASKS_PER_STATUS,
) -> str:
    """Render a Markdown-friendly Kanban report for Telegram/Slack."""
    now = _local_now(now)
    if reports is None:
        reports = collect_kanban_status_data(now=now)

    total_boards = len(reports)
    total_tasks = sum(report.task_count for report in reports)
    lines: list[str] = [
        f"📋 Kanban Status — live as of {_fmt_timestamp(now)}",
        f"Boards: {total_boards} active/non-archived • Tasks shown: {total_tasks} non-archived",
        "",
    ]

    if not reports:
        lines.append("No active Kanban boards found.")
        return "\n".join(lines).strip()

    truncated = False
    for report in reports:
        lines.append(f"## 📁 {report.name} ({report.slug})")
        if report.error:
            lines.append(f"⚠️ Could not read board: {report.error}")
            lines.append("")
            continue
        if report.warning:
            lines.append(
                f"⚠️ Cleanup warning: {_clean_display_text(report.warning, max_len=240)}"
            )
        counts_text = (
            ", ".join(
                f"{status}: {report.counts.get(status, 0)}"
                for status in _STATUS_ORDER
                if report.counts.get(status, 0)
            )
            or "no tasks"
        )
        attention = sum(report.counts.get(status, 0) for status in _ATTENTION_STATUSES)
        lines.append(f"Total: {report.task_count} • {counts_text}")
        if attention:
            lines.append(
                f"⚠️ Attention: {attention} task(s) running, in review, or blocked"
            )

        statuses_to_show = sorted(
            set(_STATUS_ORDER) | set(report.sections), key=_status_sort_key
        )
        for status in statuses_to_show:
            tasks = report.sections.get(status, [])
            lines.append("")
            lines.append(f"{_status_label(status)} ({len(tasks)})")
            if not tasks:
                lines.append("- none")
                continue
            shown = tasks[:max_tasks_per_status]
            overflow = len(tasks) - len(shown)
            for task in shown:
                task_id = _inline_code(getattr(task, "id", ""), max_len=32)
                title = _clean_display_text(
                    getattr(task, "title", ""), max_len=_MAX_TASK_TITLE
                )
                assignee = _clean_display_text(
                    getattr(task, "assignee", None) or "unassigned", max_len=80
                )
                priority = getattr(task, "priority", 0)
                tenant = _clean_display_text(getattr(task, "tenant", None), max_len=80)
                lines.append(f"- {task_id} — {title}")
                detail = f"  👤 assignee: {assignee} • status: {status} • priority: {priority}"
                if tenant:
                    detail += f" • tenant: {tenant}"
                date_detail = _task_date_detail(task, status, now=now)
                if date_detail:
                    detail += f" • {date_detail}"
                lines.append(detail)
                comments = []
                if status in _ATTENTION_STATUSES:
                    try:
                        from hermes_cli.kanban_db import connect_closing, list_comments

                        with connect_closing(
                            db_path=_explicit_board_db_path(report.slug)
                        ) as conn:
                            comments = list_comments(conn, getattr(task, "id", ""))
                    except Exception:
                        comments = []
                for cue in _extract_task_cues(task, comments):
                    lines.append(f"  {cue}")
            if overflow > 0:
                lines.append(f"- … {overflow} more {status} task(s) not shown")

        lines.append("")
        current = "\n".join(lines)
        if len(current) > max_chars:
            truncated = True
            break

    text = "\n".join(lines).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 240].rstrip()
        truncated = True
    if truncated:
        text += "\n\n⚠️ Report truncated to fit gateway message limits. Use `hermes kanban --board <slug> list` locally for the full board."
    return text


def build_kanban_status_report() -> str:
    """Gateway command entrypoint for `/kanban_status`."""
    return render_kanban_status_report()
