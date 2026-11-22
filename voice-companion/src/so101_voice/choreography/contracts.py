"""Typed speech-and-gesture choreography contract (foundation only; nothing here moves the robot).

A plan is an ordered list of actions. Speech is split into ``speak`` segments; ``behavior`` and
``pose`` gestures sit between them and are anchored to the **audio playback timeline**, never to the
moment their text was generated:

* ``anchor="after_previous"`` (default): fire when the previous ``speak`` segment has finished
  playing, i.e. when the next segment's first sample plays, or when playback drains if it was the last.
* ``anchor="with_next"``: fire when the next ``speak`` segment's first sample is played.
* ``anchor="immediate"``: fire when the plan starts playing (its first sample), for an opening gesture.

Gestures never block speech: speech keeps playing while a gesture runs. A newer gesture **supersedes**
a running one, matching the robot API, where a new command supersedes the previous one. Every action
belongs to one generation (one assistant response). Interrupting that response cancels its pending
gestures, and a cancelled gesture can never run later.

Example (the conceptual shape the LLM could produce later)::

    {"actions": [{"type": "speak", "text": "Wow, that was incredible!"},
                 {"type": "behavior", "behavior_id": "celebration"},
                 {"type": "speak", "text": "You should do that again!"}]}
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

ID_PATTERN = r"^[A-Za-z0-9_.-]{1,64}$"
Anchor = Literal["after_previous", "with_next", "immediate"]


class _A(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SpeakAction(_A):
    type: Literal["speak"] = "speak"
    text: str = Field(min_length=1, max_length=1000)


class BehaviorAction(_A):
    type: Literal["behavior"] = "behavior"
    behavior_id: str = Field(pattern=ID_PATTERN)
    anchor: Anchor = "after_previous"


class PoseAction(_A):
    type: Literal["pose"] = "pose"
    pose_id: str = Field(pattern=ID_PATTERN)
    anchor: Anchor = "after_previous"


GestureAction = BehaviorAction | PoseAction
Action = Annotated[SpeakAction | BehaviorAction | PoseAction, Field(discriminator="type")]


class ChoreographyPlan(_A):
    actions: list[Action] = Field(min_length=1, max_length=24)

    @property
    def speech(self) -> list[SpeakAction]:
        return [a for a in self.actions if isinstance(a, SpeakAction)]

    @property
    def gestures(self) -> list[GestureAction]:
        return [a for a in self.actions if not isinstance(a, SpeakAction)]
