from __future__ import annotations

from pathlib import Path

import pytest

from ai_harness.cli import _parse_answers
from ai_harness.config import HarnessConfig
from ai_harness.errors import AwaitingInput
from ai_harness.git import GitRepo
from ai_harness.slack import MAX_REPLY_CHARS, ChannelError, InboundMessage, Receipt
from ai_harness.state import RunStore, list_runs
from ai_harness.task import MAX_IGNORED_SLACK_MESSAGES, execute_task, start_task

QUESTION = {
    "id": "Q001",
    "question": "Should the output be JSON or text?",
    "why_blocking": "The public file format changes the implementation.",
    "suggested_default": "JSON",
}
SECOND_QUESTION = {
    "id": "Q002",
    "question": "Keep backward compatibility?",
    "why_blocking": "It decides whether the old reader stays.",
    "suggested_default": "Yes",
}


def _plan() -> dict[str, object]:
    return {
        "summary": "Add a file.",
        "assumptions": [],
        "questions": [],
        "preparation_commands": [],
        "steps": [
            {
                "id": "P001",
                "title": "Add file",
                "description": "Add result.txt.",
                "files": ["result.txt"],
                "verification": ["Inspect the file"],
            }
        ],
        "verification_commands": [],
        "risks": [],
    }


def _revised_plan() -> dict[str, object]:
    plan = _plan()
    plan.pop("questions")
    return {**plan, "review_resolutions": []}


def _slack_config(**overrides: object) -> HarnessConfig:
    base: dict[str, object] = {
        "slack_enabled": True,
        "slack_user": "U_HUMAN",
        "slack_wait_seconds": 600,
        "slack_max_clarifications": 3,
    }
    base.update(overrides)
    return HarnessConfig(**base)  # type: ignore[arg-type]


class FakeChannel:
    """Scripted question channel. Replies are strings, None for a wait that times out,
    or an exception to raise."""

    def __init__(self, replies: list, channel_id: str = "D1", start: float = 100.0) -> None:
        self.replies = list(replies)
        self.questions_posted: list[str] = []
        self.replies_posted: list[str] = []
        self.outbound_seen: list[list[str]] = []
        self.channel_id = channel_id
        self._clock = start

    def _ts(self) -> str:
        self._clock += 1
        return f"{self._clock:.1f}"

    def post_question(self, text: str) -> Receipt:
        self.questions_posted.append(text)
        return Receipt(channel_id=self.channel_id, message_ts=self._ts())

    def post_reply(self, receipt: Receipt, text: str) -> Receipt:
        self.replies_posted.append(text)
        return Receipt(channel_id=self.channel_id, message_ts=self._ts())

    def set_outbound(self, timestamps) -> None:
        self.outbound_seen.append(list(timestamps))

    def wait_for_reply(self, receipt, *, after_ts, deadline):
        if not self.replies:
            return None
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        if item is None:
            return None
        return InboundMessage(
            channel_id=self.channel_id,
            message_ts=self._ts(),
            thread_ts="",
            sender_id="U_HUMAN",
            text=item,
        )


def _runner_factory(calls: list, question_rounds: dict[str, list]):
    """Build a fake ProviderRunner whose plan stages emit the given questions."""

    class Runner:
        def __init__(self, config, store: RunStore):
            self.store = store

        def run(self, request):
            calls.append(request.stage)
            self.store.begin_stage(request.stage, request.family)
            if request.stage.startswith("clarify-"):
                value = {
                    "answer": f"Clarified for {request.stage}.",
                    "still_blocking": True,
                }
            elif request.stage == "plan-review":
                value = {
                    "verdict": "approve",
                    "summary": "No defects.",
                    "findings": [],
                    "required_changes": [],
                }
            elif request.stage == "revised-plan":
                value = _revised_plan()
            elif request.stage in question_rounds:
                value = {**_plan(), "questions": question_rounds[request.stage]}
            elif request.stage.startswith("plan"):
                value = _plan()
            else:
                (request.cwd / "result.txt").write_text("implemented\n", encoding="utf-8")
                value = {
                    "summary": "Implemented.",
                    "changed_files": ["result.txt"],
                    "verification_requested": [],
                    "notes": [],
                }
            self.store.complete_stage(request.stage, value)
            return value

    return Runner


def _install(monkeypatch, calls, question_rounds, channel) -> None:
    monkeypatch.setattr(
        "ai_harness.task.ProviderRunner", _runner_factory(calls, question_rounds)
    )
    monkeypatch.setattr(
        "ai_harness.task.build_question_channel",
        lambda config, store, budget: channel,
    )


def _start(git_repo: GitRepo, config: HarnessConfig):
    return start_task(git_repo, config=config, prompt="Add result.txt", references=[])


def test_slack_answer_resumes_planning_without_a_manual_resume(git_repo, monkeypatch) -> None:
    calls: list[str] = []
    channel = FakeChannel(["Use JSON"])
    _install(monkeypatch, calls, {"plan": [QUESTION]}, channel)

    store = _start(git_repo, _slack_config())

    assert channel.questions_posted and "Should the output be JSON" in channel.questions_posted[0]
    assert calls == ["plan", "plan-r2", "plan-review", "revised-plan", "implementation"]
    final = store.load()
    assert final["status"] == "completed"
    assert final["human_answers"][0]["answers"] == {"Q001": "Use JSON"}
    # Progress is cleared with the answers it produced.
    assert final["slack_progress"] is None
    assert Path(final["result"]["worktree"], "result.txt").is_file()


def test_answer_application_preserves_spend_recorded_during_slack_wait(
    git_repo, monkeypatch
) -> None:
    calls: list[str] = []

    class SpendingChannel(FakeChannel):
        def wait_for_reply(self, receipt, *, after_ts, deadline):
            self.budget.charge(1.25)
            return super().wait_for_reply(receipt, after_ts=after_ts, deadline=deadline)

    channel = SpendingChannel(["Use JSON"])
    monkeypatch.setattr(
        "ai_harness.task.ProviderRunner", _runner_factory(calls, {"plan": [QUESTION]})
    )

    def build_channel(config, store, budget):
        channel.budget = budget
        return channel

    monkeypatch.setattr("ai_harness.task.build_question_channel", build_channel)

    store = _start(git_repo, _slack_config())

    assert store.load()["slack_spent_usd"] == pytest.approx(1.25)


def test_reply_provenance_uses_the_normalized_slack_target(git_repo, monkeypatch) -> None:
    calls: list[str] = []
    channel = FakeChannel(["Use JSON"])
    _install(monkeypatch, calls, {"plan": [QUESTION]}, channel)

    store = _start(git_repo, _slack_config(slack_user="  U_HUMAN\t"))

    assert store.load()["human_answers"][0]["answers"] == {"Q001": "Use JSON"}


def test_question_reply_runs_a_clarify_stage_per_turn_without_reposting(
    git_repo, monkeypatch
) -> None:
    calls: list[str] = []
    channel = FakeChannel(["q: why not JSON", "what about CSV?", "Use JSON"])
    _install(monkeypatch, calls, {"plan": [QUESTION]}, channel)

    store = _start(git_repo, _slack_config())

    # The original question is posted exactly once across the whole conversation.
    assert len(channel.questions_posted) == 1
    # Per-turn stage names: a fixed name would hit the checksummed artifact cache and
    # silently skip the second model call.
    assert calls[:3] == ["plan", "clarify-r1-Q001-1", "clarify-r1-Q001-2"]
    assert channel.replies_posted == [
        "Clarified for clarify-r1-Q001-1.",
        "Clarified for clarify-r1-Q001-2.",
    ]
    final = store.load()
    assert final["status"] == "completed"
    assert final["human_answers"][0]["answers"] == {"Q001": "Use JSON"}


def test_long_reply_is_classified_before_its_payload_is_truncated(git_repo, monkeypatch) -> None:
    calls: list[str] = []
    long_question = "x" * (MAX_REPLY_CHARS + 100) + "?"
    channel = FakeChannel([long_question, "Use JSON"])
    _install(monkeypatch, calls, {"plan": [QUESTION]}, channel)

    store = _start(git_repo, _slack_config())

    assert calls[:2] == ["plan", "clarify-r1-Q001-1"]
    assert store.load()["human_answers"][0]["answers"] == {"Q001": "Use JSON"}


def test_clarification_answers_are_excluded_from_the_next_poll(git_repo, monkeypatch) -> None:
    calls: list[str] = []
    channel = FakeChannel(["q: why?", "Use JSON"])
    _install(monkeypatch, calls, {"plan": [QUESTION]}, channel)

    _start(git_repo, _slack_config())

    # Our own clarification post must be in the exclusion set before the next wait, or a
    # self-DM would read it back as the human's answer.
    assert len(channel.outbound_seen) >= 2
    assert len(channel.outbound_seen[-1]) > len(channel.outbound_seen[0])


def test_timeout_keeps_collected_answers_and_falls_back_to_manual_resume(
    git_repo, monkeypatch
) -> None:
    calls: list[str] = []
    channel = FakeChannel(["Use JSON", None])
    _install(monkeypatch, calls, {"plan": [QUESTION, SECOND_QUESTION]}, channel)

    with pytest.raises(AwaitingInput):
        _start(git_repo, _slack_config())

    state = list_runs(git_repo.common_git_dir)[0]
    assert state["status"] == "awaiting_input"
    assert state["slack_progress"]["answers"] == {"Q001": "Use JSON"}

    # The CLI merges what Slack collected, so only the outstanding question needs --answer,
    # and the shorthand form still works because one question remains.
    merged = _parse_answers(state, ["Keep it"])
    assert merged == {"Q001": "Use JSON", "Q002": "Keep it"}

    store = RunStore(git_repo.common_git_dir, state["id"])
    result = execute_task(store, git_repo, _slack_config(slack_enabled=False), answers=merged)
    assert Path(result["worktree"], "result.txt").is_file()
    assert store.load()["human_answers"][0]["answers"] == merged


def test_bare_resume_with_partial_slack_answers_pauses_cleanly_when_slack_is_disabled(
    git_repo, monkeypatch
) -> None:
    calls: list[str] = []
    channel = FakeChannel(["Use JSON", None])
    _install(monkeypatch, calls, {"plan": [QUESTION, SECOND_QUESTION]}, channel)

    with pytest.raises(AwaitingInput):
        _start(git_repo, _slack_config())

    state = list_runs(git_repo.common_git_dir)[0]
    answers = _parse_answers(state, [])
    assert answers is None

    monkeypatch.setattr("ai_harness.task.build_question_channel", lambda *args, **kwargs: None)
    store = RunStore(git_repo.common_git_dir, state["id"])
    with pytest.raises(AwaitingInput):
        execute_task(
            store,
            git_repo,
            _slack_config(slack_enabled=False),
            answers=answers,
        )


def test_explicit_answer_overrides_what_slack_collected(git_repo, monkeypatch) -> None:
    calls: list[str] = []
    channel = FakeChannel(["Use JSON", None])
    _install(monkeypatch, calls, {"plan": [QUESTION, SECOND_QUESTION]}, channel)

    with pytest.raises(AwaitingInput):
        _start(git_repo, _slack_config())

    state = list_runs(git_repo.common_git_dir)[0]
    merged = _parse_answers(state, ["Q001=Actually use text", "Q002=Keep it"])
    assert merged == {"Q001": "Actually use text", "Q002": "Keep it"}


def test_manual_answer_merge_rejects_slack_progress_for_a_changed_plan_artifact(
    git_repo, monkeypatch
) -> None:
    calls: list[str] = []
    channel = FakeChannel(["Use JSON", None])
    _install(monkeypatch, calls, {"plan": [QUESTION, SECOND_QUESTION]}, channel)

    with pytest.raises(AwaitingInput):
        _start(git_repo, _slack_config())

    state = list_runs(git_repo.common_git_dir)[0]
    state["stages"]["plan"]["sha256"] = "changed-artifact"

    assert _parse_answers(state, ["Q002=Keep it"]) == {"Q002": "Keep it"}


def test_resume_after_a_crash_continues_the_wait_instead_of_reposting(
    git_repo, monkeypatch
) -> None:
    calls: list[str] = []
    first = FakeChannel([None])
    _install(monkeypatch, calls, {"plan": [QUESTION]}, first)

    with pytest.raises(AwaitingInput):
        _start(git_repo, _slack_config())
    assert len(first.questions_posted) == 1

    state = list_runs(git_repo.common_git_dir)[0]
    assert state["slack_progress"]["receipt_ts"]
    assert state["slack_progress"]["question_id"] == "Q001"

    # Wall clock advances across the crash, so the reply is genuinely newer than the cursor.
    second = FakeChannel(["Use JSON"], start=500.0)
    _install(monkeypatch, calls, {"plan": [QUESTION]}, second)
    store = RunStore(git_repo.common_git_dir, state["id"])
    execute_task(store, git_repo, _slack_config())

    assert second.questions_posted == []
    assert store.load()["human_answers"][0]["answers"] == {"Q001": "Use JSON"}


def test_resume_refreshes_an_expired_slack_wait_deadline(git_repo, monkeypatch) -> None:
    calls: list[str] = []
    first = FakeChannel([None])
    _install(monkeypatch, calls, {"plan": [QUESTION]}, first)

    with pytest.raises(AwaitingInput):
        _start(git_repo, _slack_config())

    state = list_runs(git_repo.common_git_dir)[0]
    state["slack_progress"]["deadline_epoch"] = 0
    store = RunStore(git_repo.common_git_dir, state["id"])
    store.save(state)

    second = FakeChannel(["Use JSON"], start=500.0)
    _install(monkeypatch, calls, {"plan": [QUESTION]}, second)
    execute_task(store, git_repo, _slack_config(slack_wait_seconds=30))

    assert second.questions_posted == []
    assert store.load()["human_answers"][0]["answers"] == {"Q001": "Use JSON"}


def test_progress_bound_to_a_different_round_is_discarded(git_repo, monkeypatch) -> None:
    """A later round may legitimately reuse Q001 and answers validate on ID alone, so a
    stale answer must never satisfy the new round's question."""
    calls: list[str] = []
    repeated = {**QUESTION, "question": "Text or binary, now that JSON is settled?"}
    channel = FakeChannel(["Use JSON", "Use text"])
    _install(monkeypatch, calls, {"plan": [QUESTION], "plan-r2": [repeated]}, channel)

    store = _start(git_repo, _slack_config())

    assert len(channel.questions_posted) == 2
    assert "Text or binary" in channel.questions_posted[1]
    final = store.load()
    assert final["human_answers"][0]["answers"] == {"Q001": "Use JSON"}
    assert final["human_answers"][1]["answers"] == {"Q001": "Use text"}
    assert calls[:3] == ["plan", "plan-r2", "plan-r3"]


def test_cancel_falls_back_to_the_manual_path(git_repo, monkeypatch) -> None:
    calls: list[str] = []
    channel = FakeChannel(["cancel"])
    _install(monkeypatch, calls, {"plan": [QUESTION]}, channel)

    with pytest.raises(AwaitingInput):
        _start(git_repo, _slack_config())
    assert calls == ["plan"]


def test_clarification_ceiling_stops_the_conversation(git_repo, monkeypatch) -> None:
    calls: list[str] = []
    channel = FakeChannel(["q: one?", "q: two?"])
    _install(monkeypatch, calls, {"plan": [QUESTION]}, channel)

    with pytest.raises(AwaitingInput):
        _start(git_repo, _slack_config(slack_max_clarifications=1))

    assert calls == ["plan", "clarify-r1-Q001-1"]
    assert "clarification limit" in channel.replies_posted[-1]


def test_channel_failure_degrades_to_the_manual_pause(git_repo, monkeypatch) -> None:
    calls: list[str] = []
    channel = FakeChannel([ChannelError("missing scope im:history")])
    _install(monkeypatch, calls, {"plan": [QUESTION]}, channel)

    with pytest.raises(AwaitingInput) as paused:
        _start(git_repo, _slack_config())

    assert paused.value.questions == [QUESTION]
    state = list_runs(git_repo.common_git_dir)[0]
    assert state["status"] == "awaiting_input"
    assert state["pending_input"]["questions"] == [QUESTION]

    store = RunStore(git_repo.common_git_dir, state["id"])
    result = execute_task(store, git_repo, _slack_config(slack_enabled=False), answers={
        "Q001": "Use JSON"
    })
    assert Path(result["worktree"], "result.txt").is_file()


def test_failed_clarification_send_does_not_consume_a_clarification_turn(
    git_repo, monkeypatch
) -> None:
    calls: list[str] = []

    class FailingReplyChannel(FakeChannel):
        def post_reply(self, receipt: Receipt, text: str) -> Receipt:
            self.replies_posted.append(text)
            raise ChannelError("reply response was invalid")

    channel = FailingReplyChannel(["q: why JSON?"])
    _install(monkeypatch, calls, {"plan": [QUESTION]}, channel)

    with pytest.raises(AwaitingInput):
        _start(git_repo, _slack_config())

    state = list_runs(git_repo.common_git_dir)[0]
    assert state["slack_progress"]["clarifications"] == 0
    assert calls == ["plan", "clarify-r1-Q001-1"]


def test_uncertain_send_reposts_before_polling_so_our_message_cannot_be_an_answer(
    git_repo, monkeypatch
) -> None:
    calls: list[str] = []

    class AmbiguousReplyChannel(FakeChannel):
        def post_reply(self, receipt: Receipt, text: str) -> Receipt:
            self.replies_posted.append(text)
            self.unknown_ts = self._ts()
            raise ChannelError("reply was delivered but its response was invalid")

    first = AmbiguousReplyChannel(["q: why JSON?"])
    _install(monkeypatch, calls, {"plan": [QUESTION]}, first)
    with pytest.raises(AwaitingInput):
        _start(git_repo, _slack_config())

    state = list_runs(git_repo.common_git_dir)[0]

    class RecoveryChannel(FakeChannel):
        def __init__(self) -> None:
            super().__init__(["Use JSON"], start=500.0)
            self.returned_unknown_post = False

        def wait_for_reply(self, receipt, *, after_ts, deadline):
            if not self.returned_unknown_post:
                self.returned_unknown_post = True
                return InboundMessage(
                    channel_id=self.channel_id,
                    message_ts=first.unknown_ts,
                    thread_ts=state["slack_progress"]["receipt_ts"],
                    sender_id="U_HUMAN",
                    text=first.replies_posted[0],
                )
            return super().wait_for_reply(receipt, after_ts=after_ts, deadline=deadline)

    second = RecoveryChannel()
    _install(monkeypatch, calls, {"plan": [QUESTION]}, second)
    store = RunStore(git_repo.common_git_dir, state["id"])
    execute_task(store, git_repo, _slack_config())

    assert len(second.questions_posted) == 1
    assert store.load()["human_answers"][0]["answers"] == {"Q001": "Use JSON"}


def test_failed_clarification_send_does_not_reuse_its_stage_for_a_new_follow_up(
    git_repo, monkeypatch
) -> None:
    calls: list[str] = []

    class FailingReplyChannel(FakeChannel):
        def post_reply(self, receipt: Receipt, text: str) -> Receipt:
            self.replies_posted.append(text)
            raise ChannelError("reply response was invalid")

    first = FailingReplyChannel(["q: why JSON?"])
    _install(monkeypatch, calls, {"plan": [QUESTION]}, first)
    with pytest.raises(AwaitingInput):
        _start(git_repo, _slack_config())

    state = list_runs(git_repo.common_git_dir)[0]
    second = FakeChannel(["q: what about CSV?", "Use JSON"], start=500.0)
    _install(monkeypatch, calls, {"plan": [QUESTION]}, second)
    store = RunStore(git_repo.common_git_dir, state["id"])
    execute_task(store, git_repo, _slack_config())

    assert calls[:3] == ["plan", "clarify-r1-Q001-1", "clarify-r1-Q001-2"]
    assert second.replies_posted == [
        "Clarified for clarify-r1-Q001-1.",
        "Clarified for clarify-r1-Q001-2.",
    ]


def test_completed_clarification_is_delivered_after_a_crash_before_send_intent(
    git_repo, monkeypatch
) -> None:
    calls: list[str] = []
    base_runner = _runner_factory(calls, {"plan": [QUESTION]})

    class CrashingRunner(base_runner):
        def run(self, request):
            value = super().run(request)
            if request.stage.startswith("clarify-"):
                raise KeyboardInterrupt
            return value

    first = FakeChannel(["q: why JSON?"])
    monkeypatch.setattr("ai_harness.task.ProviderRunner", CrashingRunner)
    monkeypatch.setattr(
        "ai_harness.task.build_question_channel",
        lambda config, store, budget: first,
    )
    with pytest.raises(KeyboardInterrupt):
        _start(git_repo, _slack_config())

    state = list_runs(git_repo.common_git_dir)[0]
    second = FakeChannel(["Use JSON"], start=500.0)
    _install(monkeypatch, calls, {"plan": [QUESTION]}, second)
    store = RunStore(git_repo.common_git_dir, state["id"])
    execute_task(store, git_repo, _slack_config())

    assert second.questions_posted == []
    assert second.replies_posted == ["Clarified for clarify-r1-Q001-1."]


def test_legacy_attempted_clarification_is_migrated_as_unconfirmed_delivery(
    git_repo, monkeypatch
) -> None:
    calls: list[str] = []

    class FailingReplyChannel(FakeChannel):
        def post_reply(self, receipt: Receipt, text: str) -> Receipt:
            self.replies_posted.append(text)
            raise ChannelError("reply response was invalid")

    first = FailingReplyChannel(["q: why JSON?"])
    _install(monkeypatch, calls, {"plan": [QUESTION]}, first)
    with pytest.raises(AwaitingInput):
        _start(git_repo, _slack_config())

    state = list_runs(git_repo.common_git_dir)[0]
    progress = state["slack_progress"]
    progress["clarifications"] = 1
    progress.pop("clarification_attempts")
    progress.pop("send_in_flight")
    store = RunStore(git_repo.common_git_dir, state["id"])
    store.save(state)

    second = FakeChannel(["Use JSON"], start=500.0)
    _install(monkeypatch, calls, {"plan": [QUESTION]}, second)
    execute_task(store, git_repo, _slack_config())

    assert len(second.questions_posted) == 1
    assert second.replies_posted == ["Clarified for clarify-r1-Q001-1."]
    assert store.load()["human_answers"][0]["answers"] == {"Q001": "Use JSON"}


def test_unusable_messages_are_ignored_and_bounded(git_repo, monkeypatch) -> None:
    calls: list[str] = []

    class NoisyChannel(FakeChannel):
        def __init__(self) -> None:
            super().__init__([])
            self.waits = 0

        def wait_for_reply(self, receipt, *, after_ts, deadline):
            # Always replays one of our own posts, which can never become an answer.
            self.waits += 1
            return InboundMessage(
                channel_id=self.channel_id,
                message_ts=receipt.message_ts,
                thread_ts="",
                sender_id="U_HUMAN",
                text="Use JSON",
            )

    channel = NoisyChannel()
    _install(monkeypatch, calls, {"plan": [QUESTION]}, channel)

    with pytest.raises(AwaitingInput):
        _start(git_repo, _slack_config())

    # The bound is what stops this, not an early bail: assert the loop actually ran to its cap.
    assert channel.waits == MAX_IGNORED_SLACK_MESSAGES
    assert calls == ["plan"]


def test_empty_prefixed_answers_count_toward_the_ignored_message_limit(
    git_repo, monkeypatch
) -> None:
    calls: list[str] = []

    class EmptyAnswerChannel(FakeChannel):
        def __init__(self) -> None:
            super().__init__(["a:"] * MAX_IGNORED_SLACK_MESSAGES)
            self.waits = 0

        def wait_for_reply(self, receipt, *, after_ts, deadline):
            self.waits += 1
            return super().wait_for_reply(receipt, after_ts=after_ts, deadline=deadline)

    channel = EmptyAnswerChannel()
    _install(monkeypatch, calls, {"plan": [QUESTION]}, channel)

    with pytest.raises(AwaitingInput):
        _start(git_repo, _slack_config())

    state = list_runs(git_repo.common_git_dir)[0]
    assert channel.waits == MAX_IGNORED_SLACK_MESSAGES
    assert state["slack_ignored_messages"] == MAX_IGNORED_SLACK_MESSAGES


def test_ignored_message_limit_is_persisted_across_resumes(git_repo, monkeypatch) -> None:
    calls: list[str] = []

    class RejectedThenChannel(FakeChannel):
        def __init__(self, invalid: int, replies: list, *, start: float = 100.0) -> None:
            super().__init__(replies, start=start)
            self.invalid = invalid
            self.waits = 0

        def wait_for_reply(self, receipt, *, after_ts, deadline):
            self.waits += 1
            if self.invalid:
                self.invalid -= 1
                return InboundMessage(
                    channel_id=self.channel_id,
                    message_ts=receipt.message_ts,
                    thread_ts="",
                    sender_id="U_HUMAN",
                    text="Use JSON",
                )
            return super().wait_for_reply(receipt, after_ts=after_ts, deadline=deadline)

    first = RejectedThenChannel(MAX_IGNORED_SLACK_MESSAGES - 4, [None])
    _install(monkeypatch, calls, {"plan": [QUESTION]}, first)
    with pytest.raises(AwaitingInput):
        _start(git_repo, _slack_config())

    state = list_runs(git_repo.common_git_dir)[0]
    assert state["slack_ignored_messages"] == MAX_IGNORED_SLACK_MESSAGES - 4

    second = RejectedThenChannel(4, ["Use JSON"], start=500.0)
    _install(monkeypatch, calls, {"plan": [QUESTION]}, second)
    store = RunStore(git_repo.common_git_dir, state["id"])
    with pytest.raises(AwaitingInput):
        execute_task(store, git_repo, _slack_config())

    assert second.waits == 4
    assert store.load()["slack_ignored_messages"] == MAX_IGNORED_SLACK_MESSAGES
