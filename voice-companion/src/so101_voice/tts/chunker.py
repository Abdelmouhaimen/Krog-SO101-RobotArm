"""Split streamed LLM text into TTS segments: early enough for low latency, whole enough to sound natural.

Rules, in order:
* **Sentence boundary**: ``. ! ? …`` followed by whitespace, unless the word before it is a known
  abbreviation ("Dr.", "e.g.") or a single letter (initials), or the dot sits inside a number ("3.5").
  The boundary is only certain once the following whitespace has arrived; ``finish()`` releases the
  rest at the end of the stream.
* **Short sentences are merged** into the next one (below ``min_chars``), except the very first
  segment, which goes out as soon as it is complete ("Hey!") so speech can start early.
* **Long sentences** (over ``soft_max_chars``) are split at the last clause boundary (``, ; : —``).
  With no clause boundary they are split at the last space once ``hard_max_chars`` is reached.
* Markdown that slipped through (``**``, ``#``, backticks, list bullets) is removed and whitespace is
  collapsed, because the voice would read it out loud.

Tokens are never sent one by one: a segment always ends at a boundary or a size limit.
"""

from __future__ import annotations

import re

ABBREVIATIONS = frozenset(
    "mr mrs ms dr prof sr jr st vs etc e.g i.e approx no fig dept est inc ltd co".split()
)
_SENT_END = re.compile(r"[.!?…]+[\"')\]]*(?=\s)")
_CLAUSE = re.compile(r"[,;:—–](?=\s)|\s[—–](?=\s)")
_MD = re.compile(r"(\*\*|__|`+|^#{1,6}\s*|^\s*[-*•]\s+)", re.MULTILINE)
_WS = re.compile(r"\s+")


def clean_for_speech(text: str) -> str:
    return _WS.sub(" ", _MD.sub("", text))


class TextChunker:
    def __init__(self, *, min_chars: int = 40, soft_max_chars: int = 160, hard_max_chars: int = 260) -> None:
        if not 0 < min_chars <= soft_max_chars < hard_max_chars:
            raise ValueError("need 0 < min_chars <= soft_max_chars < hard_max_chars")
        self.min_chars = min_chars
        self.soft_max_chars = soft_max_chars
        self.hard_max_chars = hard_max_chars
        self._buf = ""
        self._emitted = 0

    def feed(self, delta: str) -> list[str]:
        """Add streamed text; return the segments that are now complete."""
        self._buf += delta
        out: list[str] = []
        while True:
            seg = self._next_segment()
            if seg is None:
                break
            out.append(seg)
        return out

    def finish(self) -> list[str]:
        """End of the LLM stream: release whatever is left."""
        rest = clean_for_speech(self._buf).strip()
        self._buf = ""
        if not rest:
            return []
        self._emitted += 1
        return [rest]

    def reset(self) -> None:
        self._buf = ""
        self._emitted = 0

    @property
    def pending(self) -> str:
        return self._buf

    # ------------------------------------------------------------------ internals
    def _next_segment(self) -> str | None:
        text = self._buf
        cut = self._sentence_cut(text)
        if cut is None and len(text) > self.soft_max_chars:
            cut = self._clause_cut(text)
            if cut is None and len(text) >= self.hard_max_chars:
                cut = text.rfind(" ", 0, self.hard_max_chars)
                if cut <= 0:
                    cut = self.hard_max_chars
        if cut is None:
            return None
        seg = clean_for_speech(text[:cut]).strip()
        self._buf = text[cut:].lstrip()
        if not seg:
            return None if not self._buf else self._next_segment()
        self._emitted += 1
        return seg

    def _sentence_cut(self, text: str) -> int | None:
        """End index of the first acceptable sentence boundary, merging short sentences."""
        need = 1 if self._emitted == 0 else self.min_chars
        for m in _SENT_END.finditer(text):
            end = m.end()
            if self._is_false_boundary(text, m.start()):
                continue
            if len(text[:end].strip()) >= need:
                return end
        return None

    def _clause_cut(self, text: str) -> int | None:
        best = None
        for m in _CLAUSE.finditer(text, 0, self.hard_max_chars):
            if m.end() >= self.min_chars:
                best = m.end()
        return best

    @staticmethod
    def _is_false_boundary(text: str, idx: int) -> bool:
        if text[idx] != ".":
            return False
        start = idx
        while start > 0 and not text[start - 1].isspace():
            start -= 1
        word = text[start:idx].lower().strip("\"'(")
        if word in ABBREVIATIONS or (len(word) == 1 and word.isalpha()):
            return True
        return bool(word) and word[-1].isdigit() and idx + 1 < len(text) and text[idx + 1].isdigit()
