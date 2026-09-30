"""The brain interface — reasoning, behind one small contract.

A Brain takes the conversation plus the available tools and returns either
something to say, or a request to call one or more tools. That is the entire
surface. Anthropic, a local model, and an offline rule engine all implement it,
so the agent loop never knows which is running and any of them can be swapped by
changing one line of config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class Turn:
    """A brain's response to one step of the conversation."""

    text: str = ""  # what to say, if anything
    tool_calls: list[ToolCall] = field(default_factory=list)
    done: bool = True  # False means the brain expects tool results and will continue
    # The provider's own content blocks, when it has them. The agent loop replays
    # these verbatim instead of rebuilding the turn from `text` and `tool_calls`,
    # which is what keeps provider-specific blocks — reasoning blocks above all —
    # intact across turns. Providers without such blocks leave it None.
    raw_blocks: list[Any] | None = None


class Brain:
    name = "base"

    def think(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
    ) -> Turn:
        raise NotImplementedError

    def available(self) -> bool:
        """Whether this brain can actually run right now."""
        return True
