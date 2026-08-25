"""Live Kanban status report for gateway slash commands."""

from __future__ import annotations

import asyncio
import math
import re
import secrets
import time
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Iterable

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
_PAGER_CALLBACK_RE = re.compile(r"^kstatus:([A-Za-z0-9_-]{6,32}):([npr])$")
_KANBAN_STATUS_COMMAND_RE = re.compile(
    r"^/kanban(?:_|-)status(?:@[A-Za-z0-9_]+)?(?:\s+(full|[0-9]{1,6}))?\s*$",
    re.IGNORECASE,
)
_PAGER_SESSION_TTL = 30 * 60
_MAX_PAGER_SESSIONS = 128


@dataclass(frozen=True)
class BoardReport:
    slug: str
    name: str
    task_count: int
    counts: Counter[str]
    sections: dict[str, list[object]]
    warning: str | None = None
    error: str | None = None


@dataclass
class PagerSession:
    owner_key: str
    slugs: tuple[str, ...]
    page: int
    created_at: float
    profile: str | None = None


_PAGER_SESSIONS: OrderedDict[str, PagerSession] = OrderedDict()


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
    comment_list = list(comments)
    status = str(getattr(task, "status", "") or "")
    failure = _clean_display_text(
        getattr(task, "last_failure_error", None), max_len=_MAX_NOTE
    )
    if failure:
        cues.append(f"⚠️ last failure: {failure}")
    if status == "blocked":
        for comment in reversed(comment_list):
            body = _clean_display_text(getattr(comment, "body", ""), max_len=_MAX_NOTE)
            if body:
                cues.append(f"note: {body}")
                break
    gate_pattern = re.compile(
        r"\b(?:pr|pull request|review|qa|finali[sz](?:e|ation)|deploy(?:ment)?|release)\b",
        re.IGNORECASE,
    )
    gate_sources = [getattr(task, "result", None)]
    gate_sources.extend(getattr(comment, "body", None) for comment in reversed(comment_list))
    gate_sources.append(getattr(task, "body", None))
    for source in gate_sources:
        raw = str(source or "")
        if not raw:
            continue
        matching_lines = [line for line in raw.splitlines() if gate_pattern.search(line)]
        if not matching_lines:
            continue
        excerpt = _clean_display_text(matching_lines[-1], max_len=_MAX_NOTE)
        cue = f"🚦 gate: {excerpt}"
        if excerpt and cue not in cues:
            cues.append(cue)
        if len(cues) >= 2:
            return cues[:2]
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
    maintain_done_retention: bool = True,
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
                    if maintain_done_retention and _should_archive_done_task(task, now=now):
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


def active_project_reports(reports: Iterable[BoardReport]) -> list[BoardReport]:
    """Return stable reports for active boards and boards with collection errors."""
    active = [report for report in reports if report.task_count > 0 or report.error]
    return sorted(active, key=lambda report: (report.slug.casefold(), report.name.casefold()))


def _bounded_text(text: str, *, max_chars: int) -> str:
    max_chars = max(120, int(max_chars))
    if len(text) <= max_chars:
        return text
    suffix = "\n\n⚠️ Project page truncated to fit message limits."
    return text[: max_chars - len(suffix)].rstrip() + suffix


def render_kanban_project_page(
    reports: list[BoardReport],
    *,
    page: int = 0,
    now: datetime | None = None,
    max_chars: int = 3_800,
) -> str:
    """Render one project from a stable report sequence."""
    if not reports:
        return "📋 Kanban Status\n\nNo active Kanban projects found."
    page = min(max(int(page), 0), len(reports) - 1)
    report = reports[page]
    safe_report = BoardReport(
        slug=_clean_display_text(report.slug, max_len=80),
        name=_clean_display_text(report.name, max_len=120),
        task_count=report.task_count,
        counts=report.counts,
        sections=report.sections,
        warning=report.warning,
        error=report.error,
    )
    body = render_kanban_status_report(
        [safe_report],
        now=now,
        max_chars=max(400, max_chars - 120),
        max_tasks_per_status=20,
    )
    body_lines = body.splitlines()
    if len(body_lines) >= 2:
        body_lines = body_lines[2:]
    text = "\n".join(
        [
            f"📋 Kanban Status — Project {page + 1} of {len(reports)}",
            f"Live as of {_fmt_timestamp(now)}",
            "",
            *body_lines,
        ]
    ).strip()
    return _bounded_text(text, max_chars=max_chars)


def parse_pager_callback(payload: object) -> tuple[str, str] | None:
    """Validate and decode an opaque pager callback payload."""
    match = _PAGER_CALLBACK_RE.fullmatch(str(payload or ""))
    if match is None:
        return None
    return match.group(1), match.group(2)


def _parse_command(text: object) -> tuple[str, int] | None:
    match = _KANBAN_STATUS_COMMAND_RE.fullmatch(str(text or "").strip())
    if match is None:
        return None
    argument = (match.group(1) or "").casefold()
    if argument == "full":
        return "full", 0
    if argument:
        return "page", max(0, int(argument) - 1)
    return "page", 0


def _owner_key(_gateway: object, source: object) -> str:
    """Bind callbacks to the exact platform/chat/thread/user identity."""
    platform = getattr(getattr(source, "platform", None), "value", getattr(source, "platform", ""))
    parts = (
        platform,
        getattr(source, "scope_id", None) or "",
        getattr(source, "chat_id", None) or "",
        getattr(source, "thread_id", None) or "",
        getattr(source, "user_id", None) or "",
        getattr(source, "profile", None) or "",
    )
    return "\x1f".join(str(part) for part in parts)


def _callback_is_authorized(gateway: object, source: object) -> bool:
    check_slash_access = getattr(gateway, "_check_slash_access", None)
    if callable(check_slash_access):
        try:
            if check_slash_access(source, "kanban_status") is not None:
                return False
        except Exception:
            return False
    authorize = getattr(gateway, "_is_user_authorized", None)
    if not callable(authorize):
        return True
    try:
        return bool(authorize(source))
    except Exception:
        return False


def _callback_source_thread_id(adapter: object, message: object) -> str | None:
    """Normalize callback routing exactly like an inbound Telegram message."""
    effective_thread_id = getattr(adapter, "_effective_message_thread_id", None)
    if callable(effective_thread_id):
        try:
            normalized = effective_thread_id(message)
            return str(normalized) if normalized is not None else None
        except Exception:
            return None
    return str(getattr(message, "message_thread_id", "") or "") or None


def _prune_pager_sessions() -> None:
    cutoff = time.monotonic() - _PAGER_SESSION_TTL
    for token, state in list(_PAGER_SESSIONS.items()):
        if state.created_at < cutoff:
            _PAGER_SESSIONS.pop(token, None)


def _create_pager_session(gateway: object, source: object, reports: list[BoardReport], page: int) -> str:
    _prune_pager_sessions()
    while len(_PAGER_SESSIONS) >= _MAX_PAGER_SESSIONS:
        _PAGER_SESSIONS.popitem(last=False)
    token = secrets.token_urlsafe(9)
    page = min(max(page, 0), max(0, len(reports) - 1))
    _PAGER_SESSIONS[token] = PagerSession(
        owner_key=_owner_key(gateway, source),
        slugs=tuple(report.slug for report in reports),
        page=page,
        created_at=time.monotonic(),
        profile=getattr(source, "profile", None),
    )
    return token


def _reports_for_session(state: PagerSession) -> list[BoardReport]:
    live = {
        report.slug: report
        for report in collect_kanban_status_data(maintain_done_retention=False)
    }
    reports: list[BoardReport] = []
    for slug in state.slugs:
        report = live.get(slug)
        if report is None:
            report = BoardReport(
                slug=slug,
                name=slug,
                task_count=0,
                counts=Counter(),
                sections={},
                error="Project is no longer available.",
            )
        reports.append(report)
    return reports


def _pager_button_specs(token: str, count: int) -> list[tuple[str, str]]:
    buttons: list[tuple[str, str]] = []
    if count > 1:
        buttons.extend([("◀ Previous", "p"), ("Next ▶", "n")])
    buttons.append(("Refresh", "r"))
    return [(label, f"kstatus:{token}:{action}") for label, action in buttons]


def _telegram_payload(adapter: object, text: str) -> tuple[str, bool]:
    """Format within Telegram's UTF-16 payload limit, or use bounded plain text."""
    from gateway.platforms.base import _prefix_within_utf16_limit, utf16_len

    formatter = getattr(adapter, "format_message", None)
    formatted = formatter(text) if callable(formatter) else text
    if utf16_len(formatted) <= 4_096:
        return formatted, True
    suffix = "\n\n⚠️ Project page truncated to fit message limits."
    prefix = _prefix_within_utf16_limit(text, 4_096 - utf16_len(suffix)).rstrip()
    return prefix + suffix, False


def _adapter_for_source(gateway: object, source: object) -> object | None:
    resolver = getattr(gateway, "_adapter_for_source", None)
    if callable(resolver):
        try:
            resolved = resolver(source)
            if resolved is not None:
                return resolved
        except Exception:
            return None
    adapters = getattr(gateway, "adapters", None)
    if not isinstance(adapters, dict):
        return None
    platform = getattr(source, "platform", None)
    name = str(getattr(platform, "value", platform) or "").lower()
    return adapters.get(platform) or adapters.get(name)


async def _handle_pager_action(
    *, token: str, action: str, owner_key: str
) -> tuple[PagerSession, list[BoardReport], str] | None:
    _prune_pager_sessions()
    state = _PAGER_SESSIONS.get(token)
    if state is None or state.owner_key != owner_key:
        return None
    if action == "n":
        state.page = min(state.page + 1, len(state.slugs) - 1)
    elif action == "p":
        state.page = max(state.page - 1, 0)
    elif action != "r":
        return None
    state.created_at = time.monotonic()
    _PAGER_SESSIONS.move_to_end(token)
    reports = await asyncio.to_thread(_reports_for_session, state)
    text = await asyncio.to_thread(
        render_kanban_project_page, reports, page=state.page
    )
    return state, reports, text


def _ensure_telegram_callbacks(gateway: object, adapter: object) -> bool:
    if getattr(adapter, "_kanban_status_callbacks", False):
        return True
    app = getattr(adapter, "_app", None)
    if app is None:
        return False
    try:
        from gateway.config import Platform
        from gateway.session import SessionSource
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from telegram.constants import ParseMode
        from telegram.error import BadRequest
        from telegram.ext import CallbackQueryHandler
    except Exception:
        return False

    async def _callback(update: Any, _context: Any) -> None:
        query = getattr(update, "callback_query", None)
        parsed = parse_pager_callback(getattr(query, "data", "")) if query else None
        if query is None or parsed is None:
            return
        message = getattr(query, "message", None)
        user = getattr(query, "from_user", None)
        chat = getattr(message, "chat", None)
        thread_id = _callback_source_thread_id(adapter, message)
        raw_chat_type = str(getattr(chat, "type", "private") or "private")
        chat_type = "dm" if raw_chat_type == "private" else "group"
        if raw_chat_type == "supergroup" and thread_id:
            chat_type = "forum"
        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id=str(getattr(chat, "id", getattr(message, "chat_id", "")) or ""),
            chat_type=chat_type,
            user_id=str(getattr(user, "id", "") or ""),
            user_name=getattr(user, "username", None),
            thread_id=thread_id,
            message_id=str(getattr(message, "message_id", "") or "") or None,
        )
        pending = _PAGER_SESSIONS.get(parsed[0])
        if pending is None:
            await query.answer("This Kanban pager expired.", show_alert=True)
            return
        source.profile = pending.profile
        if not _callback_is_authorized(gateway, source):
            await query.answer("Not authorized.", show_alert=True)
            return
        result = await _handle_pager_action(
            token=parsed[0], action=parsed[1], owner_key=_owner_key(gateway, source)
        )
        if result is None:
            await query.answer("This Kanban pager expired.", show_alert=True)
            return
        _state, reports, text = result
        await query.answer()
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(label, callback_data=data) for label, data in _pager_button_specs(parsed[0], len(reports))]]
        )
        formatted, use_markdown = _telegram_payload(adapter, text)
        try:
            await query.edit_message_text(
                text=formatted,
                parse_mode=ParseMode.MARKDOWN_V2 if use_markdown else None,
                reply_markup=keyboard,
            )
        except BadRequest as exc:
            if "message is not modified" not in str(exc).casefold():
                raise

    try:
        app.add_handler(CallbackQueryHandler(_callback, pattern=r"^kstatus:"), group=-1)
        setattr(adapter, "_kanban_status_callbacks", True)
        return True
    except Exception:
        return False


async def _deliver_native_pager(
    gateway: object, event: object, reports: list[BoardReport], page: int
) -> bool:
    source = getattr(event, "source", None)
    if source is None:
        return False
    adapter = _adapter_for_source(gateway, source)
    if adapter is None:
        return False
    platform = str(
        getattr(getattr(source, "platform", None), "value", getattr(source, "platform", ""))
        or ""
    ).lower()
    if platform != "telegram" or getattr(adapter, "_bot", None) is None:
        return False
    if not _ensure_telegram_callbacks(gateway, adapter):
        return False
    token = _create_pager_session(gateway, source, reports, page)
    state = _PAGER_SESSIONS[token]
    text = await asyncio.to_thread(
        render_kanban_project_page, reports, page=state.page
    )
    specs = _pager_button_specs(token, len(reports))
    if platform == "telegram" and getattr(adapter, "_bot", None) is not None:
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            from telegram.constants import ParseMode
        except Exception:
            return False
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(label, callback_data=data) for label, data in specs]]
        )
        formatted, use_markdown = _telegram_payload(adapter, text)
        kwargs: dict[str, Any] = {
            "chat_id": int(getattr(source, "chat_id")),
            "text": formatted,
            "parse_mode": ParseMode.MARKDOWN_V2 if use_markdown else None,
            "reply_markup": keyboard,
        }
        metadata: Any = None
        metadata_fn = getattr(gateway, "_thread_metadata_for_source", None)
        if callable(metadata_fn):
            try:
                metadata = metadata_fn(source)
            except Exception:
                metadata = None
        thread_id_fn = getattr(adapter, "_metadata_thread_id", None)
        thread_kwargs_fn = getattr(adapter, "_thread_kwargs_for_send", None)
        thread_id = (
            thread_id_fn(metadata)
            if callable(thread_id_fn)
            else getattr(source, "thread_id", None)
        )
        if callable(thread_kwargs_fn):
            helper_kwargs = thread_kwargs_fn(
                str(getattr(source, "chat_id")), thread_id, metadata
            )
            if isinstance(helper_kwargs, dict):
                kwargs.update(helper_kwargs)
        elif thread_id:
            kwargs["message_thread_id"] = int(str(thread_id))
        await adapter._bot.send_message(**kwargs)
        return True
    return False


async def handle_pre_gateway_dispatch(
    *, event: object, gateway: object, **_kwargs: object
) -> dict[str, str] | None:
    """Send the native project pager before normal slash-command dispatch."""
    parsed = _parse_command(getattr(event, "text", ""))
    if parsed is None or parsed[0] == "full":
        return None
    source = getattr(event, "source", None)
    adapter = _adapter_for_source(gateway, source) if source is not None else None
    platform = str(
        getattr(getattr(source, "platform", None), "value", getattr(source, "platform", ""))
        or ""
    ).lower()
    native_supported = bool(
        adapter is not None
        and platform == "telegram"
        and getattr(adapter, "_bot", None) is not None
    )
    if not native_supported:
        return None
    check_slash_access = getattr(gateway, "_check_slash_access", None)
    if source is not None and callable(check_slash_access):
        try:
            if check_slash_access(source, "kanban_status") is not None:
                return None
        except Exception:
            return None
    authorize = getattr(gateway, "_is_user_authorized", None)
    if source is not None and callable(authorize):
        try:
            if not authorize(source):
                return None
        except Exception:
            return None
    if not _ensure_telegram_callbacks(gateway, adapter):
        return None
    reports = active_project_reports(
        await asyncio.to_thread(
            collect_kanban_status_data, maintain_done_retention=False
        )
    )
    delivered = await _deliver_native_pager(gateway, event, reports, parsed[1])
    if delivered:
        return {"action": "skip", "reason": "kanban-status-project-pager"}
    return None


def build_kanban_status_report(*, mode: str = "page", page: int = 0) -> str:
    """Gateway command entrypoint for `/kanban_status` and explicit full mode."""
    reports = collect_kanban_status_data(maintain_done_retention=False)
    if mode == "full":
        return render_kanban_status_report(reports)
    active = active_project_reports(reports)
    text = render_kanban_project_page(active, page=page)
    if len(active) > 1:
        current = min(max(page, 0), len(active) - 1)
        commands = []
        if current > 0:
            commands.append(f"Previous: `/kanban_status {current}`")
        if current + 1 < len(active):
            commands.append(f"Next: `/kanban_status {current + 2}`")
        commands.append("All projects: `/kanban_status full`")
        text = _bounded_text(text + "\n\n" + " • ".join(commands), max_chars=3_800)
    return text
