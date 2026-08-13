from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

import pytest


def test_kanban_status_command_is_gateway_registered():
    from hermes_cli.commands import is_gateway_known_command, resolve_command

    cmd = resolve_command("kanban_status")
    alias = resolve_command("kanban-status")

    assert cmd is not None
    assert cmd.name == "kanban_status"
    assert cmd.gateway_only is True
    assert alias is cmd
    assert is_gateway_known_command("kanban_status") is True


def test_kanban_status_report_includes_active_boards_and_grouped_tasks(monkeypatch, tmp_path):
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
        kb.add_comment(conn, blocked_id, "reviewer", "review-required: needs eyes before merge")
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


def test_kanban_status_report_ignores_inherited_env_board_pins(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

    from hermes_cli import kanban_db as kb
    from gateway.kanban_status import collect_kanban_status_data, render_kanban_status_report

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

    assert [getattr(task, "title", "") for task in reports["alpha"].sections["blocked"]] == [
        "Alpha pinned-env task"
    ]
    assert [getattr(task, "title", "") for task in reports["beta"].sections["blocked"]] == [
        "Beta real board task"
    ]
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
    runner.session_store = SimpleNamespace(get_or_create_session=lambda *_args, **_kwargs: None)
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
        kb.create_task(conn, title="Telegram status task", assignee="coder", board="telegram-board")

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
        kb.create_task(conn, title="Slack status task", assignee="reviewer", board="slack-board")

    runner = _make_status_runner()
    result = await runner._handle_message(_status_event("/kanban_status", platform=Platform.SLACK))

    assert "Slack Board (slack-board)" in result
    assert "Slack status task" in result
    assert "👤 assignee: reviewer" in result
