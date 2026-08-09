"""Slack question channel driven through the Claude CLI's claude.ai Slack connector.

The harness runs as its own process and cannot reach an MCP server directly, but the Claude
CLI it already invokes is authenticated against the workspace connector. This module therefore
treats that CLI as a dumb transport for three discrete operations and keeps every piece of loop
state, cursor state, and trust decision in Python.

The connector posts as the authenticated user, so a message this harness sends into a self-DM
comes back with the human's own sender id. Sender identity alone cannot separate our own posts
from a human reply; recorded outbound timestamps plus a strictly monotonic cursor do.
"""

from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from .config import HarnessConfig
from .errors import CommandError, HarnessError, ProviderError
from .process import run_command, sanitized_model_env
from .providers import create_deny_bin, find_claude
from .schema import claude_schema, load_schema, validate_output
from .state import RunStore

SEND_TOOL = "mcp__claude_ai_Slack__slack_send_message"
READ_TOOL = "mcp__claude_ai_Slack__slack_read_channel"
SLEEP_TOOL = "Bash(sleep:*)"

MAX_REPLY_CHARS = 4_000
PROCESS_OVERHEAD_SECONDS = 120
# Each poll is a tool round trip inside the waiting process, and measured cost rises with the
# number of polls (~$0.64 for one read, ~$0.96 for six). Left uncapped, a 30-minute wait at the
# default interval would be 90 polls in one call and cost more than the whole run allowance, so
# a long wait stretches its interval instead of adding round trips.
MAX_POLLS_PER_CALL = 20


class ChannelError(HarnessError):
    """The Slack question channel could not complete an operation."""


class ChannelBudgetExhausted(ChannelError):
    """The run-wide Slack allowance cannot cover another channel call."""


@dataclass(frozen=True)
class Receipt:
    """A message this harness sent, used later to exclude it from inbound polling."""

    channel_id: str
    message_ts: str


@dataclass(frozen=True)
class InboundMessage:
    channel_id: str
    message_ts: str
    thread_ts: str
    sender_id: str
    text: str


class Routing(Enum):
    ANSWER = "answer"
    QUESTION = "question"
    CANCEL = "cancel"


@dataclass(frozen=True)
class RoutedReply:
    routing: Routing
    text: str


class QuestionChannel(Protocol):
    def post_question(self, text: str) -> Receipt: ...

    def wait_for_reply(
        self,
        receipt: Receipt,
        *,
        after_ts: str,
        deadline: float,
    ) -> InboundMessage | None: ...

    def post_reply(self, receipt: Receipt, text: str) -> Receipt: ...

    def set_outbound(self, timestamps: Sequence[str]) -> None: ...


class ChannelBudget:
    """Run-wide spend guard. `--max-budget-usd` is per process; this spans every process."""

    def __init__(
        self,
        *,
        limit_usd: float,
        spent_usd: float = 0.0,
        on_spend: Callable[[float], None] | None = None,
    ) -> None:
        self.limit_usd = limit_usd
        self.spent_usd = spent_usd
        self._on_spend = on_spend

    def remaining(self) -> float:
        return max(0.0, self.limit_usd - self.spent_usd)

    def require(self, minimum: float = 0.01) -> float:
        remaining = self.remaining()
        if remaining < minimum:
            raise ChannelBudgetExhausted(
                f"Slack channel allowance is exhausted (${self.spent_usd:.2f} of "
                f"${self.limit_usd:.2f} spent)"
            )
        return remaining

    def charge(self, usd: float) -> None:
        self.spent_usd += max(0.0, usd)
        if self._on_spend is not None:
            self._on_spend(self.spent_usd)


def poll_schedule(remaining_seconds: int, minimum_interval: int) -> tuple[int, int]:
    """Choose a poll interval and count that covers the wait without unbounded round trips.

    Returns (poll_seconds, max_polls). Short waits poll at the configured interval; long waits
    widen the interval so a single process never exceeds MAX_POLLS_PER_CALL.
    """
    poll_seconds = max(1, minimum_interval)
    max_polls = max(1, remaining_seconds // poll_seconds)
    if max_polls > MAX_POLLS_PER_CALL:
        poll_seconds = max(poll_seconds, remaining_seconds // MAX_POLLS_PER_CALL)
        max_polls = MAX_POLLS_PER_CALL
    return poll_seconds, max_polls


def parse_ts(value: str) -> float:
    """Slack timestamps are decimal strings. Unparseable values sort before everything."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return -1.0


def classify_reply(text: str) -> RoutedReply:
    """Explicit prefixes win; bare text falls back to the documented `?` heuristic."""
    stripped = text.strip()
    lowered = stripped.lower()
    if lowered == "cancel":
        return RoutedReply(Routing.CANCEL, stripped)
    if lowered.startswith("a:"):
        return RoutedReply(Routing.ANSWER, stripped[2:].strip())
    if lowered.startswith("q:"):
        return RoutedReply(Routing.QUESTION, stripped[2:].strip())
    if stripped.startswith("?"):
        return RoutedReply(Routing.QUESTION, stripped[1:].strip())
    if stripped.endswith("?"):
        return RoutedReply(Routing.QUESTION, stripped)
    return RoutedReply(Routing.ANSWER, stripped)


def reject_reason(
    message: InboundMessage,
    *,
    receipt: Receipt,
    cursor_ts: str,
    outbound_ts: Sequence[str],
    target_sender: str,
) -> str | None:
    """Controller-side provenance. Returns None when the message may be trusted as a reply.

    These fields are reported by a language model, so this raises the bar rather than making
    fabrication impossible. What it does guarantee is that a stale message, an unrelated DM, a
    concurrent run's traffic, or one of this harness's own posts cannot become an answer.
    """
    if message.channel_id != receipt.channel_id:
        return f"message is from another channel ({message.channel_id})"
    if parse_ts(message.message_ts) <= parse_ts(cursor_ts):
        return f"message ts {message.message_ts} is not newer than cursor {cursor_ts}"
    if message.message_ts in set(outbound_ts):
        return f"message ts {message.message_ts} is one of this run's own posts"
    if message.thread_ts and message.thread_ts != receipt.message_ts:
        return f"message belongs to another thread ({message.thread_ts})"
    if target_sender and message.sender_id != target_sender:
        return f"message is from another sender ({message.sender_id})"
    if not message.text.strip():
        return "message is empty"
    return None


def truncate_reply(text: str) -> str:
    if len(text) <= MAX_REPLY_CHARS:
        return text
    return text[:MAX_REPLY_CHARS] + "\n[truncated by ai-harness]"


def _post_prompt(*, target: str, text: str, thread_ts: str = "") -> str:
    threading = (
        f"\nSet thread_ts to {json.dumps(thread_ts)} so the message threads under the question."
        if thread_ts
        else ""
    )
    return f"""You are a message transport. Perform exactly one action and return structured output.

Send this exact text as a Slack direct message. Use {json.dumps(target)} as the channel_id.
{threading}

MESSAGE TEXT
{json.dumps(text)}

Call {SEND_TOOL} exactly once with that text verbatim. Do not rewrite, summarize, translate, or
append to it. Do not read any channel. Do not send any other message.

Return the channel_id and message_ts that the send tool reported, with ok=true and an empty error.
If the send fails, return ok=false, empty identifiers, and the failure detail in error."""


def _wait_prompt(
    *,
    channel_id: str,
    after_ts: str,
    outbound_ts: Sequence[str],
    poll_seconds: int,
    max_polls: int,
) -> str:
    excluded = json.dumps(list(outbound_ts))
    return f"""You are a message transport polling for one inbound reply. Never send a message.

Channel: {json.dumps(channel_id)}
A message qualifies only if its ts is numerically greater than {json.dumps(after_ts)} and its ts is
not in this list of messages already sent by this run: {excluded}

Procedure, repeated at most {max_polls} times:
1. Read the most recent messages in the channel with {READ_TOOL}.
2. If a qualifying message exists, stop immediately and return the OLDEST qualifying one.
3. Otherwise run the Bash command `sleep {poll_seconds}` and go back to step 1.

Report the message exactly as the read tool returned it. Copy its channel id, ts, thread ts, sender
id, and text verbatim. Never invent, guess, summarize, translate, or reformat any of these values;
if a field is absent, return an empty string for it. Reporting a message that does not exist is a
failure, not a fallback.

When a qualifying message is found return found=true with its fields and an empty error. When the
poll limit is reached without one, return found=false, empty fields, and an empty error. If a tool
fails, return found=false and put the failure detail in error."""


class ClaudeSlackChannel:
    """Drives the claude.ai Slack connector through one short-lived Claude CLI process per call.

    Deliberately does not reuse `build_claude_argv`: that builder hardcodes `--safe-mode` and an
    empty MCP config, which is exactly what makes every other stage unable to reach the network.
    """

    def __init__(
        self,
        config: HarnessConfig,
        store: RunStore,
        *,
        budget: ChannelBudget,
        target: str,
    ) -> None:
        self.config = config
        self.store = store
        self.budget = budget
        self.target = target
        self.deny_bin = create_deny_bin(store, name="deny-bin-slack")
        self._calls = 0
        self._outbound: tuple[str, ...] = ()

    def set_outbound(self, timestamps: Sequence[str]) -> None:
        """Timestamps this run has posted, excluded from polling so we never read ourselves."""
        self._outbound = tuple(timestamps)

    def _argv(self, *, schema_name: str, prompt: str, budget_usd: float) -> list[str]:
        return [
            str(find_claude()),
            "--permission-mode",
            "dontAsk",
            # `--tools` controls availability and `--allowedTools` only grants permission, so the
            # sleep-poll loop genuinely needs Bash present here. The allowlist keeps it to sleep.
            "--tools",
            "Bash",
            "--allowedTools",
            ",".join([SLEEP_TOOL, SEND_TOOL, READ_TOOL]),
            # `--safe-mode` would disable the connector along with everything else, so each
            # customization surface it normally covers is disabled explicitly instead.
            "--settings",
            '{"disableAllHooks":true,"autoMemoryEnabled":false}',
            "--setting-sources",
            "",
            "--disable-slash-commands",
            "--json-schema",
            json.dumps(_channel_schema(schema_name), separators=(",", ":")),
            "--max-budget-usd",
            f"{budget_usd:.2f}",
            "--no-session-persistence",
            "--model",
            self.config.model_for("claude"),
            "--output-format",
            "json",
            "-p",
            prompt,
        ]

    def _run(
        self,
        *,
        operation: str,
        schema_name: str,
        prompt: str,
        timeout: int,
    ) -> dict[str, Any]:
        budget_usd = self.budget.require()
        argv = self._argv(schema_name=schema_name, prompt=prompt, budget_usd=budget_usd)
        self._calls += 1
        label = f"slack-{operation}-{self._calls}"
        # The channel never sees the run artifacts: they hold the plan, the task prompt, and every
        # referenced context file, none of which belong in a process that can reach the network.
        with tempfile.TemporaryDirectory(prefix="ai-harness-slack-") as sandbox:
            try:
                result = run_command(
                    argv,
                    cwd=Path(sandbox),
                    timeout=timeout,
                    env=sanitized_model_env(self.deny_bin),
                    check=False,
                )
            except CommandError as exc:
                raise ChannelError(f"Slack {operation} could not run: {exc}") from exc
        self.store.write_text_artifact(f"logs/{label}.stdout", result.stdout)
        self.store.write_text_artifact(f"logs/{label}.stderr", result.stderr)
        if result.returncode != 0:
            detail = (result.stderr.strip() or result.stdout.strip())[-1_000:]
            raise ChannelError(f"Slack {operation} exited {result.returncode}: {detail}")
        try:
            outer = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ChannelError(f"Slack {operation} returned unparseable output") from exc
        self.budget.charge(float(outer.get("total_cost_usd") or 0.0))
        if outer.get("is_error"):
            raise ChannelError(f"Slack {operation} failed: {outer.get('result', 'unknown error')}")
        value = outer.get("structured_output")
        if not isinstance(value, dict):
            raw = outer.get("result")
            if isinstance(raw, str):
                try:
                    decoded = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ChannelError(f"Slack {operation} returned no structured object") from exc
                if isinstance(decoded, dict):
                    value = decoded
        if not isinstance(value, dict):
            raise ChannelError(f"Slack {operation} returned no structured object")
        try:
            return validate_output(schema_name, value)
        except ProviderError as exc:
            raise ChannelError(f"Slack {operation} returned invalid output: {exc}") from exc

    def post_question(self, text: str) -> Receipt:
        return self._post(text, thread_ts="", operation="post")

    def post_reply(self, receipt: Receipt, text: str) -> Receipt:
        return self._post(text, thread_ts=receipt.message_ts, operation="reply")

    def _post(self, text: str, *, thread_ts: str, operation: str) -> Receipt:
        value = self._run(
            operation=operation,
            schema_name="slack_post",
            prompt=_post_prompt(target=self.target, text=text, thread_ts=thread_ts),
            timeout=self.config.stage_timeout,
        )
        if not value["ok"] or not value["channel_id"] or not value["message_ts"]:
            detail = value["error"] or "no detail"
            raise ChannelError(f"Slack {operation} did not deliver: {detail}")
        return Receipt(channel_id=value["channel_id"], message_ts=value["message_ts"])

    def wait_for_reply(
        self,
        receipt: Receipt,
        *,
        after_ts: str,
        deadline: float,
    ) -> InboundMessage | None:
        remaining = int(deadline - time.monotonic())
        if remaining <= 0:
            return None
        poll_seconds, max_polls = poll_schedule(remaining, self.config.slack_poll_seconds)
        value = self._run(
            operation="wait",
            schema_name="slack_wait",
            prompt=_wait_prompt(
                channel_id=receipt.channel_id,
                after_ts=after_ts,
                outbound_ts=self._outbound,
                poll_seconds=poll_seconds,
                max_polls=max_polls,
            ),
            timeout=remaining + PROCESS_OVERHEAD_SECONDS,
        )
        if value["error"]:
            raise ChannelError(f"Slack wait failed: {value['error']}")
        if not value["found"]:
            return None
        return InboundMessage(
            channel_id=value["channel_id"],
            message_ts=value["message_ts"],
            thread_ts=value["thread_ts"],
            sender_id=value["sender_id"],
            text=value["text"],
        )

def _channel_schema(name: str) -> dict[str, Any]:
    return claude_schema(load_schema(name))


def build_question_channel(
    config: HarnessConfig,
    store: RunStore,
    *,
    budget: ChannelBudget,
) -> ClaudeSlackChannel | None:
    if not config.slack_enabled:
        return None
    target = config.slack_user.strip()
    if not target:
        raise ChannelError(
            "Slack questions are enabled but no target user is set. "
            "Set AI_HARNESS_SLACK_USER to a Slack user id such as U0123456789."
        )
    return ClaudeSlackChannel(config, store, budget=budget, target=target)
