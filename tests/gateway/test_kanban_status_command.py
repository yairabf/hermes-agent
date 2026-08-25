from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import threading
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest


def test_kanban_status_command_is_gateway_registered():
    from hermes_cli.commands import is_gateway_known_command, resolve_command

    cmd = resolve_command("kanban_status")
    alias = resolve_command("kanban-status")

    assert cmd is not None
    assert cmd.name == "kanban_status"
    assert cmd.gateway_only is True
    assert "project" in cmd.description.casefold()
    assert alias is cmd
    assert is_gateway_known_command("kanban_status") is True


def test_kanban_status_report_includes_active_boards_and_grouped_tasks(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

    from hermes_cli import kanban_db as kb
    from gateway.kanban_status import render_kanban_status_report

    kb.create_board("yair-general", name="Yair General")
    kb.create_board("archived-board", name="Archived Board")
    kb.write_board_metadata("archived-board", archived=True)

    with kb.connect_closing(board="yair-general") as conn:
        ready_id = kb.create_task(
            conn,
            title="Implement kanban_status",
            assignee="coder",
            priority=10,
            board="yair-general",
        )
        blocked_id = kb.create_task(
            conn,
            title="Wait for Yair approval <unsafe>`tick`",
            assignee="reviewer",
            priority=5,
            initial_status="blocked",
            board="yair-general",
        )
        kb.add_comment(
            conn, blocked_id, "reviewer", "review-required: needs eyes before merge"
        )
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (ready_id,))
        conn.commit()

    report = render_kanban_status_report(max_chars=20_000)

    assert "📋 Kanban Status" in report
    assert "Yair General (yair-general)" in report
    assert "Archived Board" not in report
    assert "Review (1)" in report
    assert "Blocked (1)" in report
    assert f"`{ready_id}`" in report
    assert f"`{blocked_id}`" in report
    assert "Implement kanban_status" in report
    assert "👤 assignee: coder" in report
    assert "status: review" in report
    assert "priority: 10" in report
    assert "review-required: needs eyes before merge" in report
    assert "‹unsafe›'tick'" in report
    assert "<unsafe>`tick`" not in report


def test_kanban_status_report_cleans_task_id_before_inline_code():
    from gateway.kanban_status import BoardReport, render_kanban_status_report

    task = SimpleNamespace(
        id="t_bad`id<raw>\x07",
        title="Unsafe id fixture",
        assignee="coder",
        priority=0,
        tenant=None,
        status="ready",
    )
    report = render_kanban_status_report(
        reports=[
            BoardReport(
                slug="demo",
                name="Demo",
                task_count=1,
                counts=Counter({"ready": 1}),
                sections={"ready": [task]},
            )
        ],
        max_chars=20_000,
    )

    assert "- `t_bad'id‹raw›` — Unsafe id fixture" in report
    assert "`t_bad`id<raw>" not in report


def test_kanban_status_report_shows_done_completion_date_and_age():
    from gateway.kanban_status import BoardReport, render_kanban_status_report

    local_tz = timezone(timedelta(hours=3))
    now = datetime(2026, 8, 14, 12, 0, tzinfo=local_tz)
    completed = datetime(2026, 7, 15, 12, 0, tzinfo=local_tz)
    task = SimpleNamespace(
        id="t_done",
        title="Finished task",
        assignee="coder",
        priority=0,
        tenant=None,
        status="done",
        created_at=completed.timestamp() - 86_400,
        started_at=None,
        completed_at=completed.timestamp(),
    )

    report = render_kanban_status_report(
        reports=[
            BoardReport(
                slug="demo",
                name="Demo",
                task_count=1,
                counts=Counter({"done": 1}),
                sections={"done": [task]},
            )
        ],
        now=now,
        max_chars=20_000,
    )

    assert "completed: 2026-07-15 • 30d ago" in report


def test_kanban_status_report_uses_relevant_dates_for_active_statuses():
    from gateway.kanban_status import BoardReport, render_kanban_status_report

    local_tz = timezone(timedelta(hours=3))
    now = datetime(2026, 8, 14, 12, 0, tzinfo=local_tz)

    def task(task_id, status, *, created_days, started_hours=None):
        return SimpleNamespace(
            id=task_id,
            title=task_id,
            assignee="coder",
            priority=0,
            tenant=None,
            status=status,
            created_at=(now - timedelta(days=created_days)).timestamp(),
            started_at=(now - timedelta(hours=started_hours)).timestamp()
            if started_hours is not None
            else None,
            completed_at="not-a-timestamp",
        )

    tasks = [
        task("t_running", "running", created_days=5, started_hours=2),
        task("t_running_fallback", "running", created_days=3),
        task("t_scheduled", "scheduled", created_days=4),
        task("t_ready", "ready", created_days=5),
    ]
    sections: dict[str, list[object]] = {
        status: [item for item in tasks if item.status == status]
        for status in {str(t.status) for t in tasks}
    }
    report = render_kanban_status_report(
        reports=[
            BoardReport(
                slug="demo",
                name="Demo",
                task_count=len(tasks),
                counts=Counter(t.status for t in tasks),
                sections=sections,
            )
        ],
        now=now,
        max_chars=20_000,
    )

    assert "started: 2026-08-14 • 2h ago" in report
    assert "created: 2026-08-11 • 3d ago" in report
    assert "created: 2026-08-10 • 4d ago" in report
    assert "created: 2026-08-09 • 5d ago" in report


def test_done_retention_boundary_uses_elapsed_hours_across_dst():
    from gateway.kanban_status import _should_archive_done_task

    local_tz = ZoneInfo("America/New_York")
    now = datetime(2026, 4, 1, 12, 0, tzinfo=local_tz)
    exact_boundary = SimpleNamespace(
        status="done",
        completed_at=now.timestamp() - (30 * 24 * 60 * 60),
    )
    one_second_older = SimpleNamespace(
        status="done",
        completed_at=exact_boundary.completed_at - 1,
    )

    assert _should_archive_done_task(exact_boundary, now=now) is False
    assert _should_archive_done_task(one_second_older, now=now) is True


def test_compact_age_uses_elapsed_time_across_dst():
    from gateway.kanban_status import _compact_age

    local_tz = ZoneInfo("America/New_York")
    now = datetime(2026, 3, 8, 4, 0, tzinfo=local_tz)
    two_hours_ago = datetime.fromtimestamp(now.timestamp() - (2 * 60 * 60), tz=local_tz)

    assert _compact_age(two_hours_ago, now=now) == "2h ago"


def test_invalid_out_of_range_timestamp_stays_visible_without_crashing(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

    from hermes_cli import kanban_db as kb
    from gateway.kanban_status import (
        collect_kanban_status_data,
        render_kanban_status_report,
    )

    kb.create_board("invalid-time", name="Invalid Time")
    with kb.connect_closing(board="invalid-time") as conn:
        task_id = kb.create_task(
            conn, title="Invalid completion", assignee="coder", board="invalid-time"
        )
        conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
            (1e308, task_id),
        )

    reports = {report.slug: report for report in collect_kanban_status_data()}
    rendered = render_kanban_status_report(list(reports.values()))

    assert reports["invalid-time"].error is None
    assert task_id in {
        task.id for tasks in reports["invalid-time"].sections.values() for task in tasks
    }
    assert "Invalid completion" in rendered


def test_kanban_status_archives_only_done_tasks_strictly_older_than_retention(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

    from hermes_cli import kanban_db as kb
    from gateway.kanban_status import (
        collect_kanban_status_data,
        render_kanban_status_report,
    )

    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    kb.create_board("retention", name="Retention")
    with kb.connect_closing(board="retention") as conn:
        expired_id = kb.create_task(
            conn, title="Expired Done", assignee="coder", board="retention"
        )
        boundary_id = kb.create_task(
            conn, title="Boundary Done", assignee="coder", board="retention"
        )
        missing_id = kb.create_task(
            conn, title="Missing Completion", assignee="coder", board="retention"
        )
        blocked_id = kb.create_task(
            conn,
            title="Old Blocked",
            assignee="coder",
            initial_status="blocked",
            board="retention",
        )
        already_archived_id = kb.create_task(
            conn, title="Already Archived", assignee="coder", board="retention"
        )
        conn.executemany(
            "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
            [
                ((now - timedelta(days=31)).timestamp(), expired_id),
                ((now - timedelta(days=30)).timestamp(), boundary_id),
                (None, missing_id),
            ],
        )
        conn.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?",
            ((now - timedelta(days=31)).timestamp(), blocked_id),
        )
        kb.archive_task(conn, already_archived_id)

    reports = {report.slug: report for report in collect_kanban_status_data(now=now)}
    shown_ids = {
        task.id for tasks in reports["retention"].sections.values() for task in tasks
    }

    assert expired_id not in shown_ids
    assert boundary_id in shown_ids
    assert missing_id in shown_ids
    assert blocked_id in shown_ids
    assert already_archived_id not in shown_ids
    assert reports["retention"].counts["done"] == 2
    rendered = render_kanban_status_report(list(reports.values()), now=now)
    assert "Missing Completion" in rendered
    assert (
        "completed: 2026" in rendered
    )  # Boundary task still has a valid completion date.
    with kb.connect_closing(board="retention") as conn:
        stored = {task.id: task for task in kb.list_tasks(conn, include_archived=True)}
        assert stored[expired_id].status == "archived"
        assert stored[boundary_id].status == "done"
        assert stored[missing_id].status == "done"
        assert stored[blocked_id].status != "archived"
        assert [event.kind for event in kb.list_events(conn, expired_id)].count(
            "archived"
        ) == 1


def test_kanban_status_cleanup_failure_warns_and_continues(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

    from hermes_cli import kanban_db as kb
    from gateway.kanban_status import (
        collect_kanban_status_data,
        render_kanban_status_report,
    )

    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    expired_at = (now - timedelta(days=31)).timestamp()
    ids = {}
    for board in ("failing", "healthy"):
        kb.create_board(board, name=board.title())
        with kb.connect_closing(board=board) as conn:
            ids[board] = kb.create_task(
                conn, title=f"{board} expired", assignee="coder", board=board
            )
            conn.execute(
                "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
                (expired_at, ids[board]),
            )
    with kb.connect_closing(board="failing") as conn:
        ids["failing-second"] = kb.create_task(
            conn, title="second expired", assignee="coder", board="failing"
        )
        conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
            (expired_at, ids["failing-second"]),
        )

    real_archive_task = kb.archive_task

    def selective_failure(conn, task_id, **kwargs):
        if task_id == ids["failing"]:
            raise RuntimeError("unsafe <cleanup>\nfailed")
        return real_archive_task(conn, task_id, **kwargs)

    monkeypatch.setattr(kb, "archive_task", selective_failure)
    reports = {report.slug: report for report in collect_kanban_status_data(now=now)}
    rendered = render_kanban_status_report(list(reports.values()), now=now)

    assert reports["failing"].error is None
    assert reports["failing"].warning
    assert ids["failing"] in {
        task.id for tasks in reports["failing"].sections.values() for task in tasks
    }
    assert ids["failing-second"] not in {
        task.id for tasks in reports["failing"].sections.values() for task in tasks
    }
    assert reports["healthy"].task_count == 0
    assert "Could not archive 1 expired Done task(s)" in rendered
    assert "‹cleanup› failed" in rendered
    assert "<cleanup>\nfailed" not in rendered


def test_kanban_status_cleanup_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

    from hermes_cli import kanban_db as kb
    from gateway.kanban_status import collect_kanban_status_data

    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    kb.create_board("repeat", name="Repeat")
    with kb.connect_closing(board="repeat") as conn:
        task_id = kb.create_task(
            conn, title="Expired", assignee="coder", board="repeat"
        )
        conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
            ((now - timedelta(days=31)).timestamp(), task_id),
        )

    first = {report.slug: report for report in collect_kanban_status_data(now=now)}
    second = {report.slug: report for report in collect_kanban_status_data(now=now)}

    assert first["repeat"].task_count == 0
    assert second["repeat"].task_count == 0
    assert first["repeat"].warning is None
    assert second["repeat"].warning is None
    with kb.connect_closing(board="repeat") as conn:
        assert [event.kind for event in kb.list_events(conn, task_id)].count(
            "archived"
        ) == 1


def test_kanban_status_does_not_archive_task_changed_from_done_concurrently(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

    from hermes_cli import kanban_db as kb
    from gateway.kanban_status import collect_kanban_status_data

    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    kb.create_board("race", name="Race")
    with kb.connect_closing(board="race") as conn:
        task_id = kb.create_task(
            conn, title="Racing task", assignee="coder", board="race"
        )
        conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
            ((now - timedelta(days=31)).timestamp(), task_id),
        )

    real_archive_task = kb.archive_task

    def move_before_archive(conn, candidate_id, **kwargs):
        conn.execute(
            "UPDATE tasks SET status = 'running' WHERE id = ?", (candidate_id,)
        )
        conn.commit()
        return real_archive_task(conn, candidate_id, **kwargs)

    monkeypatch.setattr(kb, "archive_task", move_before_archive)
    reports = {report.slug: report for report in collect_kanban_status_data(now=now)}

    assert reports["race"].counts["running"] == 1
    with kb.connect_closing(board="race") as conn:
        stored = {task.id: task for task in kb.list_tasks(conn, include_archived=True)}
        assert stored[task_id].status == "running"
        assert all(event.kind != "archived" for event in kb.list_events(conn, task_id))


def test_kanban_status_report_ignores_inherited_env_board_pins(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

    from hermes_cli import kanban_db as kb
    from gateway.kanban_status import (
        collect_kanban_status_data,
        render_kanban_status_report,
    )

    kb.create_board("alpha", name="Alpha Board")
    kb.create_board("beta", name="Beta Board")
    with kb.connect_closing(board="alpha") as conn:
        alpha_id = kb.create_task(
            conn,
            title="Alpha pinned-env task",
            assignee="coder",
            initial_status="blocked",
            board="alpha",
        )
        kb.add_comment(conn, alpha_id, "coder", "alpha-only blocked note")
    with kb.connect_closing(board="beta") as conn:
        beta_id = kb.create_task(
            conn,
            title="Beta real board task",
            assignee="reviewer",
            initial_status="blocked",
            board="beta",
        )
        kb.add_comment(conn, beta_id, "reviewer", "beta-only blocked note")

    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path(board="alpha")))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "alpha")

    reports = {report.slug: report for report in collect_kanban_status_data()}
    rendered = render_kanban_status_report(max_chars=20_000)

    assert [
        getattr(task, "title", "") for task in reports["alpha"].sections["blocked"]
    ] == ["Alpha pinned-env task"]
    assert [
        getattr(task, "title", "") for task in reports["beta"].sections["blocked"]
    ] == ["Beta real board task"]
    beta_section = rendered.split("## 📁 Beta Board (beta)", 1)[1]
    assert "Beta real board task" in beta_section
    assert "beta-only blocked note" in beta_section
    assert "Alpha pinned-env task" not in beta_section
    assert "alpha-only blocked note" not in beta_section


def test_kanban_status_report_truncates_with_clear_notice(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

    from hermes_cli import kanban_db as kb
    from gateway.kanban_status import render_kanban_status_report

    kb.create_board("big", name="Big Board")
    with kb.connect_closing(board="big") as conn:
        for idx in range(20):
            kb.create_task(
                conn,
                title=f"Long task {idx} with enough title text to overflow the report",
                assignee="coder",
                board="big",
            )

    report = render_kanban_status_report(max_chars=900, max_tasks_per_status=20)

    assert "Report truncated" in report
    assert len(report) < 1200


def _make_status_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = {}
    runner.adapters = {}
    runner.hooks = SimpleNamespace(emit_collect=lambda *_args, **_kwargs: [])
    runner.session_store = SimpleNamespace(
        get_or_create_session=lambda *_args, **_kwargs: None
    )
    runner.pairing_store = SimpleNamespace()
    runner._update_prompt_pending = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._draining = False
    runner._is_user_authorized = lambda _source: True
    runner._session_key_for_source = lambda _source: "telegram:chat:user"
    runner._check_slash_access = lambda _source, _canonical: None
    runner._active_profile_name = lambda: "coordinator"
    runner._is_telegram_topic_root_lobby = lambda _source: False
    runner._begin_session_run_generation = lambda _key: 1
    runner._post_turn_goal_continuation = lambda **_kwargs: None
    return runner


def _status_event(text: str, platform=None):
    from gateway.config import Platform
    from gateway.platforms.base import MessageEvent
    from gateway.session import SessionSource

    platform = platform or Platform.TELEGRAM
    source = SessionSource(
        platform=platform,
        chat_id="chat",
        user_id="user",
        user_name="Yair",
    )
    return MessageEvent(text=text, source=source)


@pytest.mark.asyncio
async def test_kanban_status_routes_from_telegram(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

    from hermes_cli import kanban_db as kb

    kb.create_board("telegram-board", name="Telegram Board")
    with kb.connect_closing(board="telegram-board") as conn:
        kb.create_task(
            conn, title="Telegram status task", assignee="coder", board="telegram-board"
        )

    runner = _make_status_runner()
    result = await runner._handle_message(_status_event("/kanban_status"))

    assert "Telegram Board (telegram-board)" in result
    assert "Telegram status task" in result


@pytest.mark.asyncio
async def test_kanban_status_routes_from_slack(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

    from gateway.config import Platform
    from hermes_cli import kanban_db as kb

    kb.create_board("slack-board", name="Slack Board")
    with kb.connect_closing(board="slack-board") as conn:
        kb.create_task(
            conn, title="Slack status task", assignee="reviewer", board="slack-board"
        )

    runner = _make_status_runner()
    result = await runner._handle_message(
        _status_event("/kanban_status", platform=Platform.SLACK)
    )

    assert "Slack Board (slack-board)" in result
    assert "Slack status task" in result
    assert "👤 assignee: reviewer" in result


def _pager_report(slug: str, name: str, *, status: str = "ready"):
    from gateway.kanban_status import BoardReport

    task = SimpleNamespace(
        id=f"t_{slug}",
        title=f"Task for {name}",
        assignee="coder",
        priority=0,
        tenant=None,
        status=status,
        created_at=None,
        started_at=None,
        completed_at=None,
    )
    return BoardReport(
        slug=slug,
        name=name,
        task_count=1,
        counts=Counter({status: 1}),
        sections={status: [task]},
    )


def test_project_page_renders_exactly_one_project_and_clamps_bounds():
    from gateway.kanban_status import render_kanban_project_page

    reports = [_pager_report("alpha", "Alpha"), _pager_report("beta", "Beta")]

    first = render_kanban_project_page(reports, page=-99, max_chars=4_000)
    last = render_kanban_project_page(reports, page=99, max_chars=4_000)

    assert "Alpha (alpha)" in first
    assert "Beta (beta)" not in first
    assert "Project 1 of 2" in first
    assert "Beta (beta)" in last
    assert "Alpha (alpha)" not in last
    assert "Project 2 of 2" in last


def test_project_page_hides_empty_projects_and_handles_empty_and_single_cases():
    from gateway.kanban_status import BoardReport, active_project_reports, render_kanban_project_page

    empty = BoardReport(
        slug="empty",
        name="Empty",
        task_count=0,
        counts=Counter(),
        sections={},
    )
    single = _pager_report("active", "Active")

    assert active_project_reports([empty, single]) == [single]
    assert "Project 1 of 1" in render_kanban_project_page([single])
    assert "No active Kanban projects found" in render_kanban_project_page([])


def test_project_page_keeps_board_errors_while_hiding_genuinely_empty_projects():
    from gateway.kanban_status import BoardReport, active_project_reports

    empty = BoardReport(
        slug="empty",
        name="Empty",
        task_count=0,
        counts=Counter(),
        sections={},
    )
    unreadable = BoardReport(
        slug="broken",
        name="Broken",
        task_count=0,
        counts=Counter(),
        sections={},
        error="PermissionError: unreadable board",
    )

    assert active_project_reports([empty, unreadable]) == [unreadable]


def test_project_page_escapes_untrusted_board_text_and_enforces_message_limit():
    from gateway.kanban_status import BoardReport, render_kanban_project_page

    unsafe = BoardReport(
        slug="bad`slug<raw>\x07",
        name="Bad <board>\n`name`",
        task_count=1,
        counts=Counter({"ready": 1}),
        sections={"ready": [_pager_report("nested", "Nested").sections["ready"][0]]},
    )
    rendered = render_kanban_project_page([unsafe], max_chars=500)

    assert "<board>" not in rendered
    assert "`name`" not in rendered
    assert "\x07" not in rendered
    assert len(rendered) <= 500


def test_project_page_extracts_review_qa_and_deploy_gate_cues():
    from gateway.kanban_status import _extract_task_cues

    task = SimpleNamespace(
        status="review",
        result="Implementation complete; PR #42 is awaiting exact-head review.",
        body="QA must pass before deploy.",
        last_failure_error=None,
    )
    comments = [
        SimpleNamespace(body="stage:qa — QA passed; coordinator deploy gate remains")
    ]

    cues = _extract_task_cues(task, comments)

    assert any("PR #42" in cue for cue in cues)
    assert any("QA passed" in cue and "deploy gate" in cue for cue in cues)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("kstatus:Abc123_-:n", ("Abc123_-", "n")),
        ("kstatus:token_1:p", ("token_1", "p")),
        ("kstatus:token-2:r", ("token-2", "r")),
        ("kstatus:token-2:x", None),
        ("kstatus:../../etc:n", None),
        ("kstatus:token:n:extra", None),
        ("other:token:n", None),
    ],
)
def test_project_pager_callback_payload_validation(payload, expected):
    from gateway.kanban_status import parse_pager_callback

    assert parse_pager_callback(payload) == expected


def test_kanban_status_full_preserves_all_project_dump(monkeypatch):
    from gateway import kanban_status

    reports = [_pager_report("alpha", "Alpha"), _pager_report("beta", "Beta")]
    monkeypatch.setattr(kanban_status, "collect_kanban_status_data", lambda **_kwargs: reports)

    full = kanban_status.build_kanban_status_report(mode="full")
    page = kanban_status.build_kanban_status_report(mode="page", page=0)

    assert "Alpha (alpha)" in full and "Beta (beta)" in full
    assert "Alpha (alpha)" in page and "Beta (beta)" not in page


@pytest.mark.asyncio
async def test_kanban_status_pre_dispatch_falls_back_to_numbered_text(monkeypatch):
    from gateway import kanban_status

    reports = [_pager_report("alpha", "Alpha"), _pager_report("beta", "Beta")]
    monkeypatch.setattr(kanban_status, "collect_kanban_status_data", lambda **_kwargs: reports)
    gateway = SimpleNamespace(adapters={}, _is_user_authorized=lambda _source: True)

    result = await kanban_status.handle_pre_gateway_dispatch(
        event=_status_event("/kanban_status 2"), gateway=gateway
    )
    text = kanban_status.build_kanban_status_report(mode="page", page=1)

    assert result is None
    assert "Beta (beta)" in text
    assert "/kanban_status 1" in text
    assert "/kanban_status full" in text


@pytest.mark.asyncio
async def test_non_telegram_pre_dispatch_does_not_collect_kanban_data(monkeypatch):
    from gateway import kanban_status
    from gateway.config import Platform

    def unexpected_collection(**_kwargs):
        raise AssertionError("non-Telegram pre-dispatch must not read Kanban data")

    monkeypatch.setattr(
        kanban_status, "collect_kanban_status_data", unexpected_collection
    )
    gateway = SimpleNamespace(
        adapters={Platform.SLACK: SimpleNamespace(_app=object())},
        _is_user_authorized=lambda _source: True,
    )

    result = await kanban_status.handle_pre_gateway_dispatch(
        event=_status_event("/kanban_status", platform=Platform.SLACK), gateway=gateway
    )

    assert result is None


@pytest.mark.asyncio
async def test_telegram_without_callback_support_does_not_collect_kanban_data(
    monkeypatch,
):
    from gateway import kanban_status
    from gateway.config import Platform

    def unexpected_collection(**_kwargs):
        raise AssertionError("non-native Telegram fallback must not read Kanban data")

    monkeypatch.setattr(
        kanban_status, "collect_kanban_status_data", unexpected_collection
    )
    gateway = SimpleNamespace(
        adapters={Platform.TELEGRAM: SimpleNamespace(_bot=object(), _app=None)},
        _is_user_authorized=lambda _source: True,
    )

    result = await kanban_status.handle_pre_gateway_dispatch(
        event=_status_event("/kanban_status"), gateway=gateway
    )

    assert result is None


@pytest.mark.asyncio
async def test_telegram_pre_dispatch_collects_kanban_data_off_event_loop(monkeypatch):
    from gateway import kanban_status
    from gateway.config import Platform

    event_loop_thread = threading.get_ident()
    collection_threads = []
    reports = [_pager_report("alpha", "Alpha")]

    def collect(**_kwargs):
        collection_threads.append(threading.get_ident())
        return reports

    async def deliver(_gateway, _event, delivered_reports, _page):
        assert delivered_reports == reports
        return True

    monkeypatch.setattr(kanban_status, "collect_kanban_status_data", collect)
    monkeypatch.setattr(kanban_status, "_deliver_native_pager", deliver)
    monkeypatch.setattr(
        kanban_status, "_ensure_telegram_callbacks", lambda *_args: True
    )
    gateway = SimpleNamespace(
        adapters={Platform.TELEGRAM: SimpleNamespace(_bot=object())},
        _is_user_authorized=lambda _source: True,
    )

    result = await kanban_status.handle_pre_gateway_dispatch(
        event=_status_event("/kanban_status"), gateway=gateway
    )

    assert result == {"action": "skip", "reason": "kanban-status-project-pager"}
    assert collection_threads
    assert collection_threads[0] != event_loop_thread


@pytest.mark.parametrize("path", ["initial", "callback"])
@pytest.mark.asyncio
async def test_native_pager_comment_render_runs_off_event_loop(
    monkeypatch, tmp_path, path
):
    from collections import OrderedDict
    from dataclasses import replace

    from gateway import kanban_status
    from gateway.config import Platform
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(kanban_status, "_PAGER_SESSIONS", OrderedDict())

    kb.create_board("render-thread", name="Render Thread")
    with kb.connect_closing(board="render-thread") as conn:
        task_id = kb.create_task(
            conn,
            title="Needs comment cues",
            assignee="reviewer",
            initial_status="blocked",
            board="render-thread",
        )
        kb.add_comment(conn, task_id, "reviewer", "blocked: waiting for approval")

    event_loop_thread = threading.get_ident()
    comment_read_threads = []
    real_list_comments = kb.list_comments

    def tracked_list_comments(conn, current_task_id):
        comment_read_threads.append(threading.get_ident())
        return real_list_comments(conn, current_task_id)

    monkeypatch.setattr(kb, "list_comments", tracked_list_comments)
    reports = kanban_status.active_project_reports(
        kanban_status.collect_kanban_status_data(maintain_done_retention=False)
    )
    event = _status_event("/kanban_status")
    event = replace(event, source=replace(event.source, chat_id="12345"))

    if path == "initial":
        class FakeBot:
            async def send_message(self, **_kwargs):
                return None

        class FakeApp:
            def add_handler(self, *_args, **_kwargs):
                return None

        adapter = SimpleNamespace(
            _bot=FakeBot(),
            _app=FakeApp(),
            format_message=lambda text: text,
        )
        gateway = SimpleNamespace(adapters={Platform.TELEGRAM: adapter})
        assert await kanban_status._deliver_native_pager(gateway, event, reports, 0)
    else:
        gateway = SimpleNamespace()
        token = kanban_status._create_pager_session(
            gateway, event.source, reports, 0
        )
        result = await kanban_status._handle_pager_action(
            token=token,
            action="r",
            owner_key=kanban_status._owner_key(gateway, event.source),
        )
        assert result is not None

    assert comment_read_threads
    assert all(thread_id != event_loop_thread for thread_id in comment_read_threads)


@pytest.mark.asyncio
async def test_denied_telegram_status_uses_normal_slash_gate_without_collecting(monkeypatch):
    from gateway import kanban_status
    from gateway.config import Platform

    collections = []

    def unexpected_collection(**_kwargs):
        collections.append(True)
        return [_pager_report("secret", "Secret")]

    monkeypatch.setattr(
        kanban_status, "collect_kanban_status_data", unexpected_collection
    )
    monkeypatch.setattr(
        kanban_status, "_ensure_telegram_callbacks", lambda *_args: True
    )

    class FakeApp:
        def add_handler(self, *_args, **_kwargs):
            return None

    runner = _make_status_runner()
    runner.adapters = {
        Platform.TELEGRAM: SimpleNamespace(_bot=object(), _app=FakeApp())
    }
    denial = "⛔ /kanban_status is admin-only here."
    runner._check_slash_access = lambda _source, command: (
        denial if command == "kanban_status" else None
    )

    result = await runner._handle_message(_status_event("/kanban_status"))

    assert result == denial
    assert collections == []
    assert kanban_status._callback_is_authorized(
        runner, _status_event("/kanban_status").source
    ) is False


@pytest.mark.parametrize(
    ("plugin_result", "expected"),
    [
        ({"action": "skip", "reason": "plugin-owned"}, None),
        ({"action": "rewrite", "text": "/kanban_status full"}, "rewritten"),
    ],
)
@pytest.mark.asyncio
async def test_plugin_dispatch_intercepts_before_native_kanban_pager(
    monkeypatch, plugin_result, expected
):
    from unittest.mock import AsyncMock

    from gateway import kanban_status
    from gateway.config import Platform
    from hermes_cli import lifecycle

    native_calls = []

    def unexpected_collection(**_kwargs):
        native_calls.append("collect")
        return [_pager_report("secret", "Secret")]

    async def unexpected_delivery(*_args, **_kwargs):
        native_calls.append("deliver")
        return True

    monkeypatch.setattr(
        lifecycle,
        "invoke_hook",
        lambda name, **_kwargs: [plugin_result]
        if name == "pre_gateway_dispatch"
        else [],
    )
    monkeypatch.setattr(
        kanban_status, "collect_kanban_status_data", unexpected_collection
    )
    monkeypatch.setattr(kanban_status, "_deliver_native_pager", unexpected_delivery)
    monkeypatch.setattr(
        kanban_status, "_ensure_telegram_callbacks", lambda *_args: True
    )
    runner = _make_status_runner()
    runner.adapters = {Platform.TELEGRAM: SimpleNamespace(_bot=object())}
    runner._handle_kanban_status_command = AsyncMock(return_value="rewritten")

    result = await runner._handle_message(_status_event("/kanban_status"))

    assert result == expected
    assert native_calls == []
    if plugin_result["action"] == "rewrite":
        rewritten_event = runner._handle_kanban_status_command.await_args.args[0]
        assert rewritten_event.text == "/kanban_status full"
    else:
        runner._handle_kanban_status_command.assert_not_awaited()


@pytest.mark.parametrize(
    ("hook_result", "expected"),
    [
        ({"decision": "deny", "message": "blocked by middleware"}, "blocked by middleware"),
        ({"decision": "handled", "message": "handled by middleware"}, "handled by middleware"),
        (
            {
                "decision": "rewrite",
                "command_name": "kanban_status",
                "raw_args": "full",
            },
            "rewritten full report",
        ),
    ],
)
@pytest.mark.asyncio
async def test_command_middleware_intercepts_before_native_kanban_pager(
    monkeypatch, hook_result, expected
):
    from unittest.mock import AsyncMock

    from gateway import kanban_status
    from gateway.config import Platform

    native_calls = []

    def unexpected_collection(**_kwargs):
        native_calls.append("collect")
        raise AssertionError("middleware must run before native Kanban collection")

    async def unexpected_delivery(*_args, **_kwargs):
        native_calls.append("deliver")
        raise AssertionError("middleware must run before native pager delivery")

    runner = _make_status_runner()
    runner.adapters = {Platform.TELEGRAM: SimpleNamespace(_bot=object(), _app=object())}
    runner.hooks = SimpleNamespace(emit_collect=AsyncMock(return_value=[hook_result]))
    runner._handle_kanban_status_command = AsyncMock(return_value="rewritten full report")
    monkeypatch.setattr(
        kanban_status, "collect_kanban_status_data", unexpected_collection
    )
    monkeypatch.setattr(kanban_status, "_deliver_native_pager", unexpected_delivery)
    monkeypatch.setattr(
        kanban_status, "_ensure_telegram_callbacks", lambda *_args: True
    )

    result = await runner._handle_message(_status_event("/kanban_status"))

    assert result == expected
    assert native_calls == []
    runner.hooks.emit_collect.assert_awaited_once()
    if hook_result["decision"] == "rewrite":
        rewritten_event = runner._handle_kanban_status_command.await_args.args[0]
        assert rewritten_event.text == "/kanban_status full"
    else:
        runner._handle_kanban_status_command.assert_not_awaited()


def test_general_topic_callback_source_uses_adapter_effective_thread_id():
    from dataclasses import replace

    from gateway.kanban_status import _callback_source_thread_id, _owner_key

    message = SimpleNamespace(message_thread_id=None)
    adapter = SimpleNamespace(
        _effective_message_thread_id=lambda value: (
            "1" if value is message else pytest.fail("wrong callback message")
        )
    )

    callback_thread = _callback_source_thread_id(adapter, message)
    initial_source = replace(_status_event("/kanban_status").source, thread_id="1")
    callback_source = replace(initial_source, thread_id=callback_thread)

    assert callback_thread == "1"
    assert _owner_key(None, callback_source) == _owner_key(None, initial_source)


@pytest.mark.asyncio
async def test_full_capacity_callback_preserves_valid_sessions_until_creation(
    monkeypatch,
):
    from collections import OrderedDict
    import time

    from gateway import kanban_status

    now = time.monotonic()
    sessions = OrderedDict(
        (
            f"token-{index}",
            kanban_status.PagerSession(
                owner_key="owner",
                slugs=("alpha",),
                page=0,
                created_at=now,
            ),
        )
        for index in range(kanban_status._MAX_PAGER_SESSIONS)
    )
    monkeypatch.setattr(kanban_status, "_PAGER_SESSIONS", sessions)
    reports = [_pager_report("alpha", "Alpha")]
    monkeypatch.setattr(
        kanban_status,
        "collect_kanban_status_data",
        lambda **_kwargs: reports,
    )

    callback_result = await kanban_status._handle_pager_action(
        token="token-0", action="r", owner_key="owner"
    )

    assert callback_result is not None
    assert len(sessions) == kanban_status._MAX_PAGER_SESSIONS
    assert set(sessions) == {
        f"token-{index}" for index in range(kanban_status._MAX_PAGER_SESSIONS)
    }

    token = kanban_status._create_pager_session(
        SimpleNamespace(),
        _status_event("/kanban_status").source,
        reports,
        0,
    )

    assert len(sessions) == kanban_status._MAX_PAGER_SESSIONS
    assert "token-0" in sessions
    assert "token-1" not in sessions
    assert token in sessions


def test_gateway_status_build_is_read_only_even_for_expired_done(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

    from gateway.kanban_status import build_kanban_status_report
    from hermes_cli import kanban_db as kb

    kb.create_board("read-only", name="Read Only")
    with kb.connect_closing(board="read-only") as conn:
        task_id = kb.create_task(
            conn, title="Old Done", assignee="coder", board="read-only"
        )
        conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
            ((datetime.now(timezone.utc) - timedelta(days=90)).timestamp(), task_id),
        )

    rendered = build_kanban_status_report()

    assert "Old Done" in rendered
    with kb.connect_closing(board="read-only") as conn:
        stored = {task.id: task for task in kb.list_tasks(conn, include_archived=True)}
    assert stored[task_id].status == "done"


@pytest.mark.asyncio
async def test_telegram_native_pager_sends_valid_buttons_and_single_project_refresh_only():
    from dataclasses import replace

    from gateway.config import Platform
    from gateway.kanban_status import (
        _deliver_native_pager,
        _pager_button_specs,
        parse_pager_callback,
    )

    class FakeBot:
        def __init__(self):
            self.kwargs = None

        async def send_message(self, **kwargs):
            self.kwargs = kwargs

    class FakeApp:
        def add_handler(self, *_args, **_kwargs):
            return None

    bot = FakeBot()
    adapter = SimpleNamespace(
        _bot=bot,
        _app=FakeApp(),
        format_message=lambda text: text,
    )
    gateway = SimpleNamespace(
        adapters={Platform.TELEGRAM: adapter},
        _session_key_for_source=lambda _source: "telegram:chat:user",
    )

    event = _status_event("/kanban_status")
    event = replace(event, source=replace(event.source, chat_id="12345"))
    delivered = await _deliver_native_pager(
        gateway, event, [_pager_report("only", "Only")], 0
    )

    assert delivered is True
    assert bot.kwargs["reply_markup"] is not None
    specs = _pager_button_specs("abcdef", 1)
    assert [label for label, _data in specs] == ["Refresh"]
    assert parse_pager_callback(specs[0][1])[1] == "r"


def test_telegram_callback_registration_follows_rebuilt_application():
    from gateway.kanban_status import _ensure_telegram_callbacks

    class FakeApp:
        def __init__(self):
            self.handlers = []

        def add_handler(self, handler, **kwargs):
            self.handlers.append((handler, kwargs))

    first_app = FakeApp()
    adapter = SimpleNamespace(_app=first_app)

    assert _ensure_telegram_callbacks(SimpleNamespace(), adapter) is True
    assert len(first_app.handlers) == 1
    assert _ensure_telegram_callbacks(SimpleNamespace(), adapter) is True
    assert len(first_app.handlers) == 1

    replacement_app = FakeApp()
    adapter._app = replacement_app

    assert _ensure_telegram_callbacks(SimpleNamespace(), adapter) is True
    assert len(first_app.handlers) == 1
    assert len(replacement_app.handlers) == 1

    adapter._app = first_app

    assert _ensure_telegram_callbacks(SimpleNamespace(), adapter) is True
    assert len(first_app.handlers) == 1
    assert len(replacement_app.handlers) == 1


@pytest.mark.asyncio
async def test_telegram_native_pager_uses_adapter_thread_send_kwargs():
    from dataclasses import replace

    from gateway.config import Platform
    from gateway.kanban_status import _deliver_native_pager

    class FakeBot:
        def __init__(self):
            self.kwargs = None

        async def send_message(self, **kwargs):
            self.kwargs = kwargs

    class FakeApp:
        def add_handler(self, *_args, **_kwargs):
            return None

    metadata = {"telegram_message_thread_id": "general"}
    bot = FakeBot()
    adapter = SimpleNamespace(
        _bot=bot,
        _app=FakeApp(),
        format_message=lambda text: text,
        _metadata_thread_id=lambda value: (
            "general" if value is metadata else pytest.fail("wrong metadata")
        ),
        _thread_kwargs_for_send=lambda chat_id, thread_id, value: {
            "direct_messages_topic_id": 77
            if (chat_id, thread_id, value) == ("12345", "general", metadata)
            else pytest.fail("wrong thread helper arguments")
        },
    )
    gateway = SimpleNamespace(
        adapters={Platform.TELEGRAM: adapter},
        _thread_metadata_for_source=lambda _source: metadata,
    )
    event = _status_event("/kanban_status")
    event = replace(event, source=replace(event.source, chat_id="12345"))

    delivered = await _deliver_native_pager(
        gateway, event, [_pager_report("only", "Only")], 0
    )

    assert delivered is True
    assert bot.kwargs["direct_messages_topic_id"] == 77
    assert "message_thread_id" not in bot.kwargs


@pytest.mark.asyncio
async def test_pager_action_enforces_owner_bounds_and_refreshes_live_data(monkeypatch):
    from gateway import kanban_status

    source = _status_event("/kanban_status").source
    gateway = SimpleNamespace(_session_key_for_source=lambda _source: "owner")
    reports = [_pager_report("alpha", "Alpha"), _pager_report("beta", "Beta")]
    token = kanban_status._create_pager_session(gateway, source, reports, 0)
    refreshed = [_pager_report("alpha", "Alpha Live"), _pager_report("beta", "Beta Live")]
    monkeypatch.setattr(
        kanban_status,
        "collect_kanban_status_data",
        lambda **_kwargs: refreshed,
    )

    assert (
        await kanban_status._handle_pager_action(
            token=token, action="n", owner_key="someone-else"
        )
        is None
    )
    owner = kanban_status._owner_key(gateway, source)
    state, live, text = await kanban_status._handle_pager_action(
        token=token, action="n", owner_key=owner
    )
    assert state.page == 1
    assert [report.slug for report in live] == ["alpha", "beta"]
    assert "Beta Live (beta)" in text

    state, _live, _text = await kanban_status._handle_pager_action(
        token=token, action="n", owner_key=owner
    )
    assert state.page == 1


@pytest.mark.asyncio
async def test_native_pager_falls_back_when_callback_registration_fails():
    from dataclasses import replace

    from gateway.config import Platform
    from gateway.kanban_status import _deliver_native_pager

    class BrokenApp:
        def add_handler(self, *_args, **_kwargs):
            raise RuntimeError("cannot register")

    class FakeBot:
        async def send_message(self, **_kwargs):
            raise AssertionError("message must not be sent with dead buttons")

    adapter = SimpleNamespace(
        _bot=FakeBot(),
        _app=BrokenApp(),
        format_message=lambda text: text,
    )
    gateway = SimpleNamespace(adapters={Platform.TELEGRAM: adapter})
    event = _status_event("/kanban_status")
    event = replace(event, source=replace(event.source, chat_id="12345"))

    delivered = await _deliver_native_pager(
        gateway, event, [_pager_report("only", "Only")], 0
    )

    assert delivered is False


@pytest.mark.asyncio
async def test_slack_degrades_to_numbered_text_fallback(monkeypatch):
    from gateway import kanban_status
    from gateway.config import Platform

    reports = [_pager_report("alpha", "Alpha"), _pager_report("beta", "Beta")]
    monkeypatch.setattr(
        kanban_status, "collect_kanban_status_data", lambda **_kwargs: reports
    )
    gateway = SimpleNamespace(
        adapters={Platform.SLACK: SimpleNamespace(_app=object())},
        _is_user_authorized=lambda _source: True,
    )

    result = await kanban_status.handle_pre_gateway_dispatch(
        event=_status_event("/kanban_status", platform=Platform.SLACK), gateway=gateway
    )
    text = kanban_status.build_kanban_status_report(mode="page", page=0)

    assert result is None
    assert "Alpha (alpha)" in text
    assert "/kanban_status 2" in text


def test_pager_owner_identity_includes_scope_thread_and_user():
    from gateway.config import Platform
    from gateway.kanban_status import _owner_key
    from gateway.session import SessionSource

    base = SessionSource(
        platform=Platform.SLACK,
        chat_id="C1",
        chat_type="group",
        user_id="U1",
        thread_id="T1",
        scope_id="W1",
    )

    assert _owner_key(None, base) == _owner_key(None, base)
    assert _owner_key(None, base) != _owner_key(
        None, SimpleNamespace(**{**base.__dict__, "user_id": "U2"})
    )
    assert _owner_key(None, base) != _owner_key(
        None, SimpleNamespace(**{**base.__dict__, "thread_id": "T2"})
    )
    assert _owner_key(None, base) != _owner_key(
        None, SimpleNamespace(**{**base.__dict__, "scope_id": "W2"})
    )
    assert _owner_key(None, base) != _owner_key(
        None, SimpleNamespace(**{**base.__dict__, "profile": "other"})
    )


def test_telegram_payload_enforces_limit_after_markdown_expansion():
    from gateway.kanban_status import _telegram_payload
    from gateway.platforms.base import utf16_len

    adapter = SimpleNamespace(format_message=lambda text: text.replace("a", "\\a"))

    payload, use_markdown = _telegram_payload(adapter, "a" * 3_800)
    astral_payload, astral_markdown = _telegram_payload(
        SimpleNamespace(format_message=lambda text: text), "😀" * 3_000
    )

    assert utf16_len(payload) <= 4_096
    assert use_markdown is False
    assert utf16_len(astral_payload) <= 4_096
    assert astral_markdown is False
