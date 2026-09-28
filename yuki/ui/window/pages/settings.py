"""Settings: plain-language choices, each saved at once. Model and effort live in a fold marked Advanced.

Where each one is saved: voice (wake word, microphone, speaker, voice id) through
yuki-ui into ``voice.json``; the coach, quiet hours and blocked apps and sites in
``privacy.toml``; calls in memory (``set_call_pref``); Start with Windows in the
Run key; motion, the accent and the overlay's shortcut in ``ui.json``.

The look: shortcuts as keycaps, quiet hours on two small dot-matrix displays with arrows, the switches and
segmented choices of Horizon.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from yuki.ui.window.actions import NOOP, Actions
from yuki.ui.window.data import SettingsView
from yuki.ui.window.theme import PAGE_PAD, Theme
from yuki.ui.window.widgets import (
    Button,
    Card,
    Dropdown,
    Input,
    KeyCapture,
    Keycaps,
    Masonry,
    Panel,
    Segmented,
    SettingRow,
    TimeDisplay,
    Toggle,
    combo_words,
    page_header,
    text_label,
)

#: The overlay's shortcut when the user has not set one (``Settings.ui_hotkey``).
DEFAULT_OPEN_HOTKEY = "alt+shift"

#: Quiet-hour choices, every half hour.
TIMES = tuple(f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30))

#: The accent choices as the Settings page words them.
ACCENT_WORDS = {"green": "Green", "orange": "Orange"}


def _voice_name(voice_id: str) -> str:
    return "Your ElevenLabs voice" if voice_id else "The default voice"


def _chip_remove(t: Theme, text: str, on_remove) -> QWidget:
    """A blocked app or site: a pill with its name and a cross; pressing it takes it off the list."""
    x = Button(t, f"{text}  ✕", style="outline", size="small")
    x.setToolTip(f"Stop blocking {text}")
    x.clicked.connect(lambda _c=False: on_remove())
    return x


def _box(t: Theme, parent: QWidget | None = None) -> tuple[QWidget, QHBoxLayout]:
    w = QWidget(parent)
    w.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    row = QHBoxLayout(w)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(8)
    return w, row


def build(t: Theme, v: SettingsView, *, advanced: bool = False, act: Actions = NOOP, ui: Any = None,
          connected: bool | None = None) -> QWidget:
    """``ui``: the UI settings (``ui.json``); ``connected``: whether yuki-ui answers on the pipe (voice and
    model changes need it running)."""
    from yuki.ui.ui_settings import UiSettings

    ui = ui or UiSettings()
    page = QWidget()
    page.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    col = QVBoxLayout(page)
    col.setContentsMargins(*PAGE_PAD)
    col.setSpacing(24)
    note = "Plain choices. Every change applies at once."
    if connected is False:
        note += " Yuki isn't running, so voice and model changes are saved for its next start."
    col.addWidget(page_header(t, "Settings", note))

    grid = QWidget(page)
    grid.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    cards = Masonry(grid, columns=2, spacing=14, min_col=360)   # one column in a narrow window

    # -- voice
    voice = Panel(t, "Voice", "Talk to Yuki with headphones on.")
    wake = Toggle(t, v.wake_on)
    wake.toggled.connect(act.set_wake)
    voice.body.addWidget(SettingRow(t, "mic", "Listen for “Yuki”", "Say its name to start talking. It listens "
                                    "on this PC; nothing leaves it until you say “Yuki”.", wake))
    mic = Dropdown(t, v.mic, width=250, options=lambda: act.device_options("capture"))
    mic.changed.connect(lambda name: act.set_device("capture", name))
    voice.body.addWidget(SettingRow(t, "micro", "Microphone", "", mic))
    spk = Dropdown(t, v.speaker, width=250, options=lambda: act.device_options("render"))
    spk.changed.connect(lambda name: act.set_device("render", name))
    voice.body.addWidget(SettingRow(t, "headphones", "Speaker", "", spk))
    vid = Input(t, "Voice id (empty: the default)", action="Save", text=v.voice_id, height=34, pill=False)
    vid.setFixedWidth(260)
    vid.submitted.connect(lambda text: act.set_voice_id(text.strip()))
    voice.body.addWidget(SettingRow(t, "chat", "Yuki's voice", _voice_name(v.voice_id), vid))
    voice.body.addWidget(SettingRow(t, "keyboard", "Talk shortcut", "Start or stop talking from anywhere.",
                                    Keycaps(t, ["Ctrl", "Alt", "Y"])))
    voice.setParent(grid)
    cards.add(voice, 0)

    # -- coach
    coach = Panel(t, "Coach", "Short check-ins, reminders, and a nudge when you've been at it too long.")
    coach_on = Toggle(t, v.coach_on)
    coach_on.toggled.connect(act.set_coach)
    coach.body.addWidget(SettingRow(t, "heart", "Check-ins and reminders", "", coach_on))
    # quiet hours: a switch to turn them off (memory reads equal start and end as none), and two small
    # displays with arrows (a click opens the whole list, which scrolls)
    quiet_on = v.quiet_start != v.quiet_end
    last = (ui.quiet_last or [v.quiet_start, v.quiet_end]) if not quiet_on else [v.quiet_start, v.quiet_end]
    if last[0] == last[1]:
        last = ["00:30", "08:00"]
    q_toggle = Toggle(t, quiet_on)
    q_row = SettingRow(t, "moon", "Quiet hours", "Nothing at all during these hours.", q_toggle)
    coach.body.addWidget(q_row)
    quiet, qrow = _box(t)
    q_start = TimeDisplay(t, last[0], times=TIMES)
    q_end = TimeDisplay(t, last[1], times=TIMES)
    q_start.changed.connect(lambda value: act.set_quiet(str(value), q_end.text()))
    q_end.changed.connect(lambda value: act.set_quiet(q_start.text(), str(value)))
    qrow.setSpacing(12)
    qrow.addWidget(text_label(t, "From", "body", "text_3"))
    qrow.addWidget(q_start)
    qrow.addWidget(text_label(t, "to", "body", "text_3"))
    qrow.addWidget(q_end)
    qrow.addStretch(1)
    q_row.add_below(quiet)
    quiet.setVisible(quiet_on)

    def quiet_toggled(on: bool) -> None:
        quiet.setVisible(on)
        if on:
            act.set_quiet(q_start.text(), q_end.text())
        else:
            act.set_quiet_enabled(False)

    q_toggle.toggled.connect(quiet_toggled)
    questions = Toggle(t, v.questions_on)
    questions.toggled.connect(act.set_questions)
    coach.body.addWidget(SettingRow(t, "question", "Questions about people and things",
                                    "Yuki asks instead of guessing, a few times a day at most.", questions))
    coach.setParent(grid)
    cards.add(coach, 0)

    # -- general
    general = Panel(t, "General")
    startup = Toggle(t, bool(v.start_with_windows))
    startup.toggled.connect(act.set_startup)
    general.body.addWidget(SettingRow(t, "power", "Start with Windows", "Yuki and its memory start when you sign in.",
                                      startup))
    hot = KeyCapture(t, ui.open_hotkey or DEFAULT_OPEN_HOTKEY, width=170)
    hot.setToolTip("Click, then press the keys you want, like Ctrl + Space, or two modifiers such as Alt + Shift")
    hot.captured.connect(act.set_open_hotkey)
    reset = Button(t, "Default", style="ghost", size="small")
    reset.setToolTip(f"Back to {combo_words(DEFAULT_OPEN_HOTKEY)}")

    def reset_hotkey() -> None:
        hot.combo = DEFAULT_OPEN_HOTKEY
        hot.update()
        act.set_open_hotkey("")

    reset.clicked.connect(reset_hotkey)
    hbox_w, hrow = _box(t)
    hrow.setSpacing(4)
    hrow.addWidget(hot)
    hrow.addWidget(reset, 0, Qt.AlignmentFlag.AlignVCenter)
    general.body.addWidget(SettingRow(t, "command", "Open Yuki anywhere", "Click the keys, then press your shortcut.",
                                      hbox_w))
    motion = Toggle(t, ui.reduce_motion)
    motion.toggled.connect(act.set_reduce_motion)
    general.body.addWidget(SettingRow(t, "pause", "Reduce motion", "No sliding or fading: things just appear, and the "
                                      "waves hold still.", motion))
    accent = Segmented(t, tuple(ACCENT_WORDS.values()), ACCENT_WORDS.get(getattr(ui, "accent", "green"), "Green"),
                       small=True)
    accent.changed.connect(lambda word: act.set_accent({w: k for k, w in ACCENT_WORDS.items()}.get(word, "green")))
    general.body.addWidget(SettingRow(t, "palette", "Accent", "The one colour Yuki uses: live states, the displays "
                                      "and what's selected.", accent))
    general.setParent(grid)
    cards.add(general, 0)

    # -- calls
    calls = Panel(t, "Calls", "Yuki can remember your own side of a call: only your microphone, only with "
                              "headphones, turned into text on this PC later. Nobody else is recorded.")
    for pref in v.call_prefs:
        choice = {"always": "Always", "never": "Never"}.get(pref["decision"], "Ask")
        seg = Segmented(t, ("Always", "Ask", "Never"), choice, small=True)
        seg.changed.connect(lambda word, app=pref["app"]: act.set_call_pref(
            app, {"Always": "always", "Never": "never"}.get(word)))
        calls.body.addWidget(SettingRow(t, "phone", pref.get("name") or pref["app"], "", seg))
    calls.body.addWidget(text_label(t, "Every other app: Yuki asks at the start of each call.", "micro", "text_3",
                                    wrap=True))
    calls.setParent(grid)
    cards.add(calls, 1)

    # -- privacy
    privacy = Panel(t, "Privacy")
    paused = Toggle(t, v.paused)
    paused.toggled.connect(act.set_paused)
    privacy.body.addWidget(SettingRow(t, "pause", "Pause memory", "Stop remembering your screen until you turn it "
                                      "back on.", paused))
    for glyph, label, kind, items, add, hint in (
            ("lock", "Apps Yuki never looks at", "apps", v.blocked_apps, "Add app", "e.g. discord.exe or Discord"),
            ("globe", "Sites Yuki never looks at", "sites", v.blocked_sites, "Add site", "e.g. example.com")):
        add_pill = Button(t, add, glyph="add", style="outline", size="small")
        row = SettingRow(t, glyph, label, "None added yet." if not items else "", add_pill)
        privacy.body.addWidget(row)
        if items:
            chips, crow = _box(t)
            for name in items[:6]:
                crow.addWidget(_chip_remove(t, name, lambda k=kind, n=name: act.remove_blocked(k, n)))
            crow.addStretch(1)
            row.add_below(chips)
        entry = Input(t, hint, action="Block", height=34)
        entry.setVisible(False)
        entry.submitted.connect(lambda text, k=kind, e=entry: (act.add_blocked(k, text.strip()), e.setVisible(False))
                                if text.strip() else None)
        add_pill.clicked.connect(lambda _c=False, e=entry: (e.setVisible(not e.isVisible()), e.edit.setFocus()))
        row.add_below(entry)
    always = Card(t, level="muted", padding=0)
    always.body.setContentsMargins(14, 12, 14, 12)
    always.body.setSpacing(3)
    always.body.addWidget(text_label(t, "Always private, whatever you choose", "caption_strong", "text"))
    always.body.addWidget(text_label(t, "Password managers, banking and sign-in pages, password fields, private "
                                        "browser windows, and what's said or shown in meetings.", "micro", "text_3",
                                     wrap=True))
    privacy.body.addSpacing(4)
    privacy.body.addWidget(always)
    privacy.setParent(grid)
    cards.add(privacy, 1)

    # -- advanced: one line, folded
    adv = Card(t, padding=0)
    adv.body.setContentsMargins(22, 16, 16, 16)
    adv.body.setSpacing(8)
    head = QHBoxLayout()
    head.setSpacing(12)
    head.addWidget(text_label(t, "Advanced", "headline"))
    summary = text_label(t, f"{v.model}, {v.effort.lower()} effort. The defaults are fine.", "body", "text_3",
                         wrap=True)
    head.addWidget(summary, 1)
    fold = Button(t, glyph="down" if advanced else "next", style="ghost", size="small")
    fold.setToolTip("Model, effort and logs")
    head.addWidget(fold)
    adv.body.addLayout(head)
    inner = QWidget()
    inner.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    adv.body.addWidget(inner)
    ilay = QVBoxLayout(inner)
    ilay.setContentsMargins(0, 0, 6, 0)
    ilay.setSpacing(0)
    model = Segmented(t, ("Sonnet", "Opus"), v.model, small=True)
    model.changed.connect(lambda word: act.set_model(word.lower()))
    ilay.addWidget(SettingRow(t, "sparkle", "Model", "Opus is smarter and slower; Sonnet is quick.", model))
    effort = Segmented(t, ("Low", "Medium", "High", "Max"), v.effort, small=True)
    effort.changed.connect(lambda word: act.set_effort(word.lower()))
    ilay.addWidget(SettingRow(t, "bulb", "Thinking effort", "More effort: better on hard tasks, slower and "
                              "costlier.", effort))
    logs = Button(t, "Open logs folder", size="small")
    logs.clicked.connect(act.open_logs)
    ilay.addWidget(SettingRow(t, "book", "Logs", "", logs))
    if not advanced:
        inner.hide()

    def toggle_fold() -> None:
        inner.setVisible(not inner.isVisible())
        fold.glyph = "down" if inner.isVisible() else "next"
        fold.update()

    fold.clicked.connect(toggle_fold)
    adv.setParent(grid)
    cards.add(adv, 1)

    col.addWidget(grid)
    col.addStretch(1)
    return page
