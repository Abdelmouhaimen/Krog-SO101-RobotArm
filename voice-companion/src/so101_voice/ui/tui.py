"""Compact terminal UI: status + cost bar, live transcript, Start / Stop / Mute / Talk."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Footer, RichLog, Static

from so101_voice.app import Companion
from so101_voice.audio.gate import InputMode
from so101_voice.conversation import (
    CostUpdate,
    Notice,
    Phase,
    PhaseChanged,
    ToolActivity,
    Transcript,
    TurnLatency,
    UIEvent,
    _hms,
)
from so101_voice.cost import BudgetState, CostSnapshot
from so101_voice.motion.speech_motion import RobotLink, TalkStatus

PHASE_STYLE = {
    Phase.IDLE: "dim",
    Phase.CONNECTING: "yellow",
    Phase.LISTENING: "green",
    Phase.USER_SPEAKING: "bold green",
    Phase.THINKING: "cyan",
    Phase.SPEAKING: "magenta",
    Phase.INTERRUPTED: "yellow",
    Phase.ERROR: "bold red",
}
LINK_TEXT = {
    RobotLink.DISABLED: "[dim]robot: off[/]",
    RobotLink.UNKNOWN: "[dim]robot: idle[/]",
    RobotLink.OK: "[green]robot: ok[/]",
    RobotLink.UNAVAILABLE: "[red]robot: DISCONNECTED[/]",
    RobotLink.REJECTED: "[yellow]robot: refused[/]",
    RobotLink.AUTH: "[red]robot: bad token[/]",
    RobotLink.ERROR: "[red]robot: error[/]",
}
BUDGET_STYLE = {
    BudgetState.OK: "",
    BudgetState.DISABLED: "dim",
    BudgetState.WARN: "yellow",
    BudgetState.EXCEEDED: "bold red",
}

Build = Callable[[Callable[[UIEvent], None], Callable[[TalkStatus], None]], Companion]


def format_cost(s: CostSnapshot) -> str:
    cap = f" / cap ${s.limit_usd:.2f}" if s.limit_usd else ""
    return (
        f"est. ${s.session_usd:.4f}{cap}  (STT ${s.stt_usd:.4f} · LLM ${s.llm_usd:.4f} · "
        f"TTS ${s.tts_usd:.4f})  all sessions ${s.cumulative_usd:.4f}"
    )


class CompanionTUI(App[None]):
    TITLE = "SO-101 Voice Companion"
    CSS = """
    #status { height: 1; padding: 0 1; background: $panel; }
    #readiness-row { height: auto; }
    #readiness { height: auto; width: 1fr; padding: 0 1; }
    #screen-toggle { height: 1; width: 21; min-width: 21; margin: 0; padding: 0 1; border: none; }
    #cost { height: 1; padding: 0 1; background: $panel; }
    #hearing { height: 1; padding: 0 1; color: $text-muted; }
    #log { height: 1fr; border: round $primary; }
    #controls { height: 3; }
    #timer { width: 18; height: 3; content-align: center middle; padding: 0 1; }
    Button { margin: 0 1; min-width: 12; }
    """
    BINDINGS = [
        ("s", "start", "Start"),
        ("x", "stop", "Stop"),
        ("m", "mute", "Mute"),
        ("space", "talk", "Talk (push-to-talk)"),
        ("e", "extend", "Extend +2h"),
        ("r", "refresh_robot", "Refresh robot library"),
        ("v", "screen", "Screen ON/OFF"),
        ("q", "quit_app", "Quit"),
    ]

    def __init__(self, build: Build) -> None:
        """``build(on_event, on_talk_status) -> Companion`` (deferred so callbacks can target the UI)."""
        super().__init__()
        self._build = build
        self.companion: Companion | None = None
        self._phase = Phase.IDLE
        self._talk = TalkStatus(RobotLink.UNKNOWN, False)
        self._latency = ""
        self._cost: CostSnapshot | None = None
        self._ptt = False
        self._assistant_open = False
        self._timer_text = ""
        self._extend_enabled: bool | None = None
        self._readiness_statuses: tuple[tuple[str, str], ...] | None = None
        self._readiness_width = 0

    def compose(self) -> ComposeResult:
        yield Static("", id="status")
        yield Static("", id="cost")
        with Horizontal(id="readiness-row"):
            yield Static("Checking arm, screen and face angle…", id="readiness", markup=False)
            yield Button("Screen: OFF", id="screen-toggle")
        yield RichLog(id="log", wrap=True, markup=True, highlight=False)
        yield Static("", id="hearing")
        with Horizontal(id="controls"):
            yield Button("Start", id="start", variant="success")
            yield Button("Stop", id="stop", variant="error")
            yield Button("Mute", id="mute")
            yield Button("Talk", id="talk")
            yield Button("Quit", id="quit")
            yield Static("", id="timer")
            yield Button("+2h", id="extend")
        yield Footer()

    async def on_mount(self) -> None:
        self.companion = self._build(self.on_session_event, self.on_talk_status)
        self._talk = self.companion.motion.status
        self._cost = self.companion.ledger.snapshot()
        await self.companion.startup()
        self._render_status()
        self._render_readiness()
        self.set_interval(0.5, self._render_readiness)
        self.set_interval(1.0, self._render_timer)
        self._render_timer()
        mode = self.companion.conversation.mode
        ptt = mode is InputMode.PUSH_TO_TALK
        self.query_one("#talk", Button).display = ptt  # only meaningful in push-to-talk mode
        self.refresh_bindings()
        hint = "Press [b]space[/b] (or Talk) to talk, again to stop." if ptt else "After Start, just speak."
        self.query_one(RichLog).write(f"[dim]Press [b]s[/b] to start. {hint}[/]")

    def _render_readiness(self) -> None:
        if self.companion and self.companion.readiness:
            readiness = self.companion.readiness
            checks = readiness.checks
            robot = readiness.robot_status or {}
            fault = (
                robot.get("fault")
                or robot.get("emergency_hold_latched")
                or str(robot.get("connection", "")).startswith("FAULT")
            )
            arm = checks.get("arm")
            screen = checks.get("screen")
            face = checks.get("face")
            arm_status = "Fault" if fault else "Connected" if arm and arm.status == "OK" else "Disconnected"
            statuses = [
                ("Arm", arm_status),
                ("Screen feed", "Available" if screen and screen.status == "OK" else "Unavailable"),
                ("Face angle", "Available" if face and face.status == "OK" else "Unavailable"),
                ("Follow", self.companion.follow.status if self.companion.follow else "OFF"),
            ]
            conv = self.companion.conversation
            observing = conv.screen_status(bool(screen and screen.status == "OK"))
            button = self.query_one("#screen-toggle", Button)
            label = f"Screen: {observing}"
            if str(button.label) != label:
                button.label = label
                button.variant = "success" if observing == "ON" else "default"
            widget = self.query_one("#readiness", Static)
            width = max(1, widget.content_size.width)
            signature = tuple(statuses)
            if signature == self._readiness_statuses and width == self._readiness_width:
                return
            self._readiness_statuses = signature
            self._readiness_width = width
            text = Text()
            line_width = 0
            for label, status in statuses:
                item_width = len(label) + 2 + len(status)
                if text:
                    if line_width + 3 + item_width > width:
                        text.append("\n")
                        line_width = 0
                    else:
                        text.append(" | ")
                        line_width += 3
                text.append(label + ": ")
                style = (
                    "bold red"
                    if status == "Fault"
                    else (
                        "green"
                        if status in ("Connected", "Available", "TRACKING")
                        else "dim"
                        if status == "OFF"
                        else "yellow"
                    )
                )
                text.append(status, style=style)
                line_width += item_width
            widget.update(text)

    # ------------------------------------------------------------------ events from the pipeline
    def on_session_event(self, ev: UIEvent) -> None:
        if not self.is_running or not self.is_mounted(self.screen):
            return
        # The full backend checklist is for diagnostics/plain mode; the TUI has its own live row.
        if (
            isinstance(ev, Notice)
            and ev.level == "info"
            and self.companion
            and self.companion.readiness
            and ev.text == self.companion.readiness.text()
        ):
            return
        log = self.query_one(RichLog)
        hearing = self.query_one("#hearing", Static)
        match ev:
            case PhaseChanged(phase=phase):
                self._phase = phase
                if phase not in (Phase.SPEAKING, Phase.THINKING):
                    self._assistant_open = False
            case Transcript(role="user", text=text, final=False):
                hearing.update(f"… {text}")
            case Transcript(role="user", text=text):
                hearing.update("")
                log.write(f"[b green]You:[/] {text}")
                self._assistant_open = False
            case Transcript(role=_, text=text):
                log.write(("   " if self._assistant_open else "[b magenta]Krog:[/] ") + text)
                self._assistant_open = True
            case Notice(level="error", text=text):
                log.write(f"[red]✖ {text}[/]")
            case Notice(level="warning", text=text):
                log.write(f"[yellow]! {text}[/]")
            case Notice(text=text):
                log.write(f"[dim]{text}[/]")
            case TurnLatency(playback_start_s=t, llm_first_text_s=llm, stt_eot_s=stt):
                parts = [
                    f"reply {t:.2f}s" if t is not None else "",
                    f"llm {llm:.2f}s" if llm is not None else "",
                ]
                if stt is not None:
                    parts.append(f"eot {stt:.2f}s")
                self._latency = " ".join(p for p in parts if p)
            case ToolActivity(name=name, status=status):
                if status != "started":
                    log.write(f"[dim]tool {name}: {status}[/]")
            case CostUpdate(snapshot=snap):
                self._cost = snap
        self._render_status()
        self._render_timer()

    def on_talk_status(self, st: TalkStatus) -> None:
        changed = st.link is not self._talk.link
        self._talk = st
        if changed and st.detail and self.is_running:
            self.query_one(RichLog).write(f"[yellow]! {st.detail}[/]")
        self._render_status()

    def _render_status(self) -> None:
        c = self.companion
        if not self.is_mounted(self.screen):
            return
        conv = c.conversation if c is not None else None
        muted = conv is not None and conv.muted
        style = PHASE_STYLE.get(self._phase, "")
        mic = "[red]MUTED[/]" if muted else ("[b]TALKING[/]" if self._ptt else "mic on")
        if conv is None or not conv.running:
            mic = "mic off"
        elif conv.mode is not InputMode.CONTINUOUS and not self._ptt and not muted:
            mic = f"mic: {conv.mode.value}"
        audio_badge = {
            "aec": "[green]AEC[/]",
            "raw": "[yellow]RAW MIC[/]",
            "half_duplex": "[yellow]HALF-DUPLEX[/]",
        }
        parts = [
            f"[{style}]{self._phase.value.upper()}[/]",
            mic + (" " + audio_badge.get(conv.audio_mode, conv.audio_mode) if conv is not None else ""),
            LINK_TEXT.get(self._talk.link, "")
            + (" [magenta]gripper talking[/]" if self._talk.talk_on else ""),
        ]
        d = c.director if c is not None else None
        if d is not None and d.active is not None and not d.active.terminal:
            parts.append(f"[cyan]arm: {d.active.label}[/]")
        if self._latency:
            parts.append(self._latency)
        if c is not None:
            parts.append(f"[dim]{c.cfg.llm.model} · {c.cfg.tts.model}[/]")
        self.query_one("#status", Static).update("  │  ".join(parts))
        if self._cost is not None:
            bstyle = BUDGET_STYLE.get(self._cost.state, "")
            text = format_cost(self._cost)
            self.query_one("#cost", Static).update(f"[{bstyle}]{text}[/]" if bstyle else text)
        self.query_one("#mute", Button).label = "Unmute" if muted else "Mute"
        self.query_one("#talk", Button).label = "Release" if self._ptt else "Talk"

    def _render_timer(self) -> None:
        """Countdown to the automatic stop (default 2 h), shown right of Quit."""
        if not self.is_mounted(self.screen) or self.companion is None:
            return
        # Keep this cheap and layout-free: it runs every second. Toggling a widget's display here made
        # Textual re-layout the screen each tick, which starved the audio thread and stalled the speaker.
        conv = self.companion.conversation
        left = conv.remaining_s()
        if left is None:
            text = "⏱ limit " + _hms(self.companion.cfg.session.max_minutes * 60)
        else:
            warn = left <= self.companion.cfg.session.warn_minutes * 60
            text = ("[b yellow]" if warn else "") + "⏱ " + _hms(left) + ("[/]" if warn else "")
        if text != self._timer_text:
            self._timer_text = text
            self.query_one("#timer", Static).update(text)
        enabled = left is not None
        if enabled != self._extend_enabled:
            self._extend_enabled = enabled
            self.query_one("#extend", Button).disabled = not enabled

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Hide the push-to-talk key from the footer unless push-to-talk is the input mode."""
        if action == "talk":
            c = self.companion
            return True if c is not None and c.conversation.mode is InputMode.PUSH_TO_TALK else None
        return True

    # ------------------------------------------------------------------ actions
    async def on_button_pressed(self, event: Button.Pressed) -> None:
        actions: dict[str, Callable[[], Any]] = {
            "extend": self.action_extend,
            "start": self.action_start,
            "stop": self.action_stop,
            "mute": self.action_mute,
            "talk": self.action_talk,
            "screen-toggle": self.action_screen,
        }
        if event.button.id == "quit":
            await self.action_quit_app()
        elif event.button.id in actions:
            await actions[event.button.id]()

    async def action_screen(self) -> None:
        if self.companion is not None:
            conv = self.companion.conversation
            conv.set_screen_enabled(not conv.screen_enabled)
            self._render_readiness()

    async def action_start(self) -> None:
        if self.companion and not self.companion.conversation.running:
            await self.companion.conversation.start()
            self._render_timer()

    async def action_stop(self) -> None:
        if self.companion and self.companion.conversation.running:
            self._ptt = False
            await self.companion.conversation.stop()
            self._render_status()

    async def action_mute(self) -> None:
        if self.companion:
            conv = self.companion.conversation
            conv.set_muted(not conv.muted)
            self._render_status()

    async def action_refresh_robot(self) -> None:
        d = self.companion.director if self.companion is not None else None
        log = self.query_one(RichLog)
        if d is None:
            log.write("[dim]robot tools are off[/]")
            return
        try:
            change = await d.library.refresh()
        except Exception as e:
            log.write(f"[yellow]! robot library refresh failed: {e}[/]")
            return
        extra = f"; missing: {', '.join(change.missing_expected)}" if change.missing_expected else ""
        log.write(
            f"[dim]robot library: {len(d.library.behaviors)} behaviors, "
            f"{len(d.library.poses)} poses{extra}[/]"
        )

    async def action_extend(self) -> None:
        if self.companion is not None:
            self.companion.conversation.extend()
            self._render_timer()

    async def action_talk(self) -> None:
        """Push-to-talk toggle (terminals cannot report key release)."""
        if self.companion and self.companion.conversation.mode is InputMode.PUSH_TO_TALK:
            self._ptt = not self._ptt
            self.companion.conversation.set_push_to_talk(self._ptt)
            self._render_status()

    async def action_quit_app(self) -> None:
        if self.companion:
            await self.companion.shutdown()
        self.exit()
