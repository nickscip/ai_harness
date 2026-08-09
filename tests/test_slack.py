from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ai_harness.config import HarnessConfig
from ai_harness.errors import CommandError
from ai_harness.process import CommandResult
from ai_harness.slack import (
    MAX_POLLS_PER_CALL,
    MAX_REPLY_CHARS,
    READ_TOOL,
    SEND_TOOL,
    SLEEP_TOOL,
    ChannelBudget,
    ChannelBudgetExhausted,
    ChannelError,
    ClaudeSlackChannel,
    InboundMessage,
    Receipt,
    Routing,
    build_question_channel,
    classify_reply,
    parse_ts,
    poll_schedule,
    reject_reason,
    truncate_reply,
)
from ai_harness.state import RunStore, new_run_id


def _config(**overrides: object) -> HarnessConfig:
    base = {
        "slack_enabled": True,
        "slack_user": "U_HUMAN",
        "slack_budget_usd": 5.0,
        "slack_poll_seconds": 20,
        "stage_timeout": 900,
    }
    base.update(overrides)
    return HarnessConfig(**base)  # type: ignore[arg-type]


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore.create(
        tmp_path / "git",
        run_id=new_run_id("task"),
        kind="task",
        source_repo=tmp_path,
        source_head="0" * 40,
        primary_family="claude",
        prompt="probe",
        options={},
    )


def _channel(store: RunStore, monkeypatch, results: list, **overrides) -> ClaudeSlackChannel:
    """Build a channel whose CLI calls are replaced by canned CommandResults."""
    monkeypatch.setattr("ai_harness.slack.find_claude", lambda: Path("/usr/bin/claude"))
    calls: list[list[str]] = []

    def fake_run_command(argv, **kwargs):
        calls.append(list(argv))
        outcome = results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return CommandResult(argv=tuple(argv), returncode=0, stdout=outcome, stderr="")

    monkeypatch.setattr("ai_harness.slack.run_command", fake_run_command)
    channel = ClaudeSlackChannel(
        _config(**overrides),
        store,
        budget=ChannelBudget(limit_usd=5.0),
        target="U_HUMAN",
    )
    channel.recorded_calls = calls  # type: ignore[attr-defined]
    return channel


def _post_payload(channel_id: str = "D1", ts: str = "100.1", cost: float = 0.5) -> str:
    return json.dumps(
        {
            "is_error": False,
            "total_cost_usd": cost,
            "structured_output": {
                "ok": True,
                "channel_id": channel_id,
                "message_ts": ts,
                "error": "",
            },
        }
    )


# --- argv contract -----------------------------------------------------------------


def test_argv_keeps_mcp_reachable_while_denying_every_other_surface(store, monkeypatch) -> None:
    channel = _channel(store, monkeypatch, [_post_payload()])
    channel.post_question("hello")
    argv = channel.recorded_calls[0]  # type: ignore[attr-defined]

    # --safe-mode would disable the connector, so each surface it normally covers is
    # disabled explicitly instead.
    assert "--safe-mode" not in argv
    assert "--add-dir" not in argv
    assert '{"disableAllHooks":true,"autoMemoryEnabled":false}' in argv
    assert "--disable-slash-commands" in argv
    assert argv[argv.index("--setting-sources") + 1] == ""

    # `--tools` controls availability and `--allowedTools` only grants permission, so an
    # empty --tools would leave the sleep-poll loop with no Bash at all.
    assert argv[argv.index("--tools") + 1] == "Bash"
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert allowed == [SLEEP_TOOL, SEND_TOOL, READ_TOOL]


def test_channel_never_runs_inside_the_run_artifact_directory(store, monkeypatch) -> None:
    seen: list[Path] = []
    monkeypatch.setattr("ai_harness.slack.find_claude", lambda: Path("/usr/bin/claude"))

    def fake_run_command(argv, **kwargs):
        seen.append(Path(kwargs["cwd"]))
        return CommandResult(argv=tuple(argv), returncode=0, stdout=_post_payload(), stderr="")

    monkeypatch.setattr("ai_harness.slack.run_command", fake_run_command)
    channel = ClaudeSlackChannel(
        _config(), store, budget=ChannelBudget(limit_usd=5.0), target="U_HUMAN"
    )
    channel.post_question("hello")

    assert store.root not in seen[0].parents
    assert seen[0] != store.root


# --- provenance --------------------------------------------------------------------


RECEIPT = Receipt(channel_id="D1", message_ts="100.1")


def _message(**overrides: str) -> InboundMessage:
    base = {
        "channel_id": "D1",
        "message_ts": "200.2",
        "thread_ts": "",
        "sender_id": "U_HUMAN",
        "text": "Use JSON",
    }
    base.update(overrides)
    return InboundMessage(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (_message(), None),
        (_message(channel_id="D_OTHER"), "another channel"),
        (_message(message_ts="100.1"), "not newer than cursor"),
        (_message(message_ts="99.0"), "not newer than cursor"),
        (_message(thread_ts="555.5"), "another thread"),
        (_message(sender_id="U_SOMEONE_ELSE"), "another sender"),
        (_message(text="   "), "empty"),
    ],
)
def test_reject_reason_enforces_provenance_in_python(message, expected) -> None:
    reason = reject_reason(
        message,
        receipt=RECEIPT,
        cursor_ts="100.1",
        outbound_ts=[],
        target_sender="U_HUMAN",
    )
    if expected is None:
        assert reason is None
    else:
        assert reason is not None and expected in reason


def test_our_own_clarification_cannot_become_an_answer(store) -> None:
    """The connector posts as the authenticated user, so sender id cannot separate the
    harness's own messages from the human's. Outbound timestamps are what does."""
    ours = _message(message_ts="300.3", sender_id="U_HUMAN", text="Because the parser is shared.")
    reason = reject_reason(
        ours,
        receipt=RECEIPT,
        cursor_ts="200.2",
        outbound_ts=["100.1", "300.3"],
        target_sender="U_HUMAN",
    )
    assert reason is not None and "own posts" in reason


def test_a_threaded_reply_meant_for_another_run_is_rejected() -> None:
    other_run = Receipt(channel_id="D1", message_ts="900.9")
    reply_to_other = _message(message_ts="950.5", thread_ts="900.9")
    assert (
        reject_reason(
            reply_to_other,
            receipt=RECEIPT,
            cursor_ts="100.1",
            outbound_ts=[],
            target_sender="U_HUMAN",
        )
        is not None
    )
    assert (
        reject_reason(
            reply_to_other,
            receipt=other_run,
            cursor_ts="900.9",
            outbound_ts=[],
            target_sender="U_HUMAN",
        )
        is None
    )


def test_provenance_alone_does_not_separate_two_runs_sharing_one_dm() -> None:
    """Documents the real limit rather than implying a guarantee that does not exist.

    outbound_ts is per-run, and in a self-DM every message carries the human's own sender id,
    so an unthreaded reply intended for another run satisfies every provenance check. What
    actually prevents this is controller_lock: one controller per repository at a time.
    """
    reply_meant_for_another_run = _message(message_ts="950.5", thread_ts="")
    assert (
        reject_reason(
            reply_meant_for_another_run,
            receipt=RECEIPT,
            cursor_ts="100.1",
            outbound_ts=["100.1"],
            target_sender="U_HUMAN",
        )
        is None
    )


@pytest.mark.parametrize(
    ("remaining", "interval", "expected"),
    [
        (60, 20, (20, 3)),
        (90, 15, (15, 6)),
        # A long wait widens the interval instead of adding round trips: 90 polls in one
        # process would cost more than the entire run allowance.
        (1800, 20, (90, 20)),
        (600, 20, (30, 20)),
        (5, 20, (20, 1)),
    ],
)
def test_poll_schedule_bounds_round_trips_per_call(remaining, interval, expected) -> None:
    assert poll_schedule(remaining, interval) == expected
    _seconds, polls = poll_schedule(remaining, interval)
    assert polls <= MAX_POLLS_PER_CALL


def test_truncate_reply_caps_untrusted_text() -> None:
    assert truncate_reply("short") == "short"
    long_reply = truncate_reply("x" * (MAX_REPLY_CHARS + 500))
    assert len(long_reply) < MAX_REPLY_CHARS + 100
    assert long_reply.endswith("[truncated by ai-harness]")


def test_parse_ts_sorts_unparseable_values_first() -> None:
    assert parse_ts("100.5") == 100.5
    assert parse_ts("not-a-timestamp") == -1.0


# --- routing -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "routing", "payload"),
    [
        ("Use JSON", Routing.ANSWER, "Use JSON"),
        ("q: why not JSON", Routing.QUESTION, "why not JSON"),
        ("Q: why not JSON", Routing.QUESTION, "why not JSON"),
        ("?why not JSON", Routing.QUESTION, "why not JSON"),
        ("why not JSON?", Routing.QUESTION, "why not JSON?"),
        ("a: Use JSON, okay?", Routing.ANSWER, "Use JSON, okay?"),
        ("cancel", Routing.CANCEL, "cancel"),
        ("CANCEL", Routing.CANCEL, "CANCEL"),
        # The documented failure modes of the bare heuristic, which the prefixes exist to fix.
        ("Use JSON, okay?", Routing.QUESTION, "Use JSON, okay?"),
        ("why not JSON", Routing.ANSWER, "why not JSON"),
    ],
)
def test_classify_reply(text: str, routing: Routing, payload: str) -> None:
    routed = classify_reply(text)
    assert routed.routing is routing
    assert routed.text == payload


# --- budget ------------------------------------------------------------------------


def test_budget_spans_processes_and_refuses_the_call_that_would_exceed_it() -> None:
    recorded: list[float] = []
    budget = ChannelBudget(limit_usd=1.0, spent_usd=0.0, on_spend=recorded.append)
    budget.charge(0.6)
    assert recorded == [0.6]
    assert budget.remaining() == pytest.approx(0.4)
    budget.charge(0.4)
    with pytest.raises(ChannelBudgetExhausted):
        budget.require()


def test_channel_charges_reported_cost_and_stops_when_exhausted(store, monkeypatch) -> None:
    channel = _channel(
        store,
        monkeypatch,
        [_post_payload(cost=4.6), _post_payload(ts="101.1", cost=0.4)],
    )
    channel.post_question("first")
    assert channel.budget.spent_usd == pytest.approx(4.6)
    channel.post_reply(Receipt("D1", "100.1"), "second")
    assert channel.budget.spent_usd == pytest.approx(5.0)
    with pytest.raises(ChannelBudgetExhausted):
        channel.post_question("third")


# --- failure classes ---------------------------------------------------------------


def test_process_timeout_becomes_a_channel_error(store, monkeypatch) -> None:
    """run_command raises rather than returning, so the channel must translate."""
    channel = _channel(store, monkeypatch, [CommandError("Command timed out after 30s: claude")])
    with pytest.raises(ChannelError, match="could not run"):
        channel.post_question("hello")


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        '{"is_error":true,"result":"revoked oauth token","total_cost_usd":0.1}',
        '{"is_error":false,"total_cost_usd":0.1,"result":"no structured output here"}',
        '{"is_error":false,"total_cost_usd":0.1,"structured_output":{"ok":false,'
        '"channel_id":"","message_ts":"","error":"missing scope im:write"}}',
        '{"is_error":false,"total_cost_usd":0.1,"structured_output":{"ok":true}}',
    ],
)
def test_every_transport_failure_class_becomes_a_channel_error(store, monkeypatch, payload) -> None:
    channel = _channel(store, monkeypatch, [payload])
    with pytest.raises(ChannelError):
        channel.post_question("hello")


def test_nonzero_exit_becomes_a_channel_error(store, monkeypatch) -> None:
    monkeypatch.setattr("ai_harness.slack.find_claude", lambda: Path("/usr/bin/claude"))
    monkeypatch.setattr(
        "ai_harness.slack.run_command",
        lambda argv, **kwargs: CommandResult(
            argv=tuple(argv), returncode=1, stdout="", stderr="permission denied"
        ),
    )
    channel = ClaudeSlackChannel(
        _config(), store, budget=ChannelBudget(limit_usd=5.0), target="U_HUMAN"
    )
    with pytest.raises(ChannelError, match="exited 1"):
        channel.post_question("hello")


def test_wait_reports_no_reply_without_raising(store, monkeypatch) -> None:
    payload = (
        '{"is_error":false,"total_cost_usd":0.2,"structured_output":'
        '{"found":false,"channel_id":"","message_ts":"","thread_ts":"",'
        '"sender_id":"","text":"","error":""}}'
    )
    channel = _channel(store, monkeypatch, [payload])
    deadline = time.monotonic() + 60
    assert channel.wait_for_reply(RECEIPT, after_ts="100.1", deadline=deadline) is None


def test_wait_past_its_deadline_makes_no_call(store, monkeypatch) -> None:
    channel = _channel(store, monkeypatch, [])
    deadline = time.monotonic() - 1
    assert channel.wait_for_reply(RECEIPT, after_ts="100.1", deadline=deadline) is None
    assert channel.recorded_calls == []  # type: ignore[attr-defined]


# --- construction ------------------------------------------------------------------


def test_build_question_channel_is_off_by_default(store) -> None:
    assert build_question_channel(
        HarnessConfig(), store, budget=ChannelBudget(limit_usd=5.0)
    ) is None


def test_build_question_channel_requires_a_target(store) -> None:
    with pytest.raises(ChannelError, match="AI_HARNESS_SLACK_USER"):
        build_question_channel(
            _config(slack_user=""), store, budget=ChannelBudget(limit_usd=5.0)
        )
