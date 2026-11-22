"""Conversation history for the Responses API (``store: false``, so the client keeps the context).

The history is a list of input items, grouped by turn:

* a ``user`` message;
* zero or more tool rounds: the ``function_call`` items (and any reasoning items) replayed verbatim,
  each followed by its ``function_call_output``;
* one ``assistant`` message holding what was **actually spoken**. After a barge-in it holds only the
  text whose audio started playing, followed by a short ``developer`` note saying the user cut it off
  (a note, not a marker inside the assistant text: the model imitated an inline "…" when it was
  tried live). If nothing was heard, there is no assistant message at all. Unplayed text never enters
  the context.

Trimming drops whole turns from the oldest end, so a function call is never separated from its output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

Item = dict[str, Any]
INTERRUPTED_NOTE = (
    "The user interrupted your previous reply. Only any assistant text above is confirmed "
    "heard; otherwise the exact heard words are unknown. Do not repeat unheard speech unasked."
)


@dataclass
class Turn:
    items: list[Item] = field(default_factory=list)


class ConversationHistory:
    def __init__(self, max_items: int = 40) -> None:
        self.max_items = max_items
        self._turns: list[Turn] = []

    @property
    def items(self) -> list[Item]:
        return [
            it for t in self._turns for it in t.items if it.get("role") != "assistant" or it.get("content")
        ]

    def __len__(self) -> int:
        return sum(len(t.items) for t in self._turns)

    def add_user(self, text: str) -> None:
        self._turns.append(Turn([{"role": "user", "content": text}]))
        self._trim()

    def add_assistant(self, text: str, *, new_turn: bool = False) -> None:
        """Record spoken assistant text (a greeting opens its own turn)."""
        if not text.strip():
            return
        if new_turn or not self._turns:
            self._turns.append(Turn())
        self._turns[-1].items.append({"role": "assistant", "content": text.strip()})
        self._trim()

    def add_interrupted(self, heard: str, *, new_turn: bool = False) -> Item:
        """A mutable slot lets a later LOCAL-offset TTS report refine heard text safely."""
        if new_turn or not self._turns:
            self._turns.append(Turn())
        item: Item = {"role": "assistant", "content": heard.strip()}
        self._turns[-1].items.extend([item, {"role": "developer", "content": INTERRUPTED_NOTE}])
        self._trim()
        return item

    def add_tool_round(self, call_items: list[Item], outputs: list[Item]) -> None:
        if not self._turns:
            self._turns.append(Turn())
        # Keep the tool text in future turns, not captured images that would be billed again.
        items = []
        for item in call_items + outputs:
            if item.get("type") == "function_call_output" and isinstance(item.get("output"), list):
                text = [part for part in item["output"] if part.get("type") == "input_text"]
                item = dict(item, output=text or [{"type": "input_text", "text": "Image inspected earlier."}])
            items.append(item)
        self._turns[-1].items.extend(items)
        self._trim()

    def _trim(self) -> None:
        while len(self) > self.max_items and len(self._turns) > 1:
            self._turns.pop(0)
