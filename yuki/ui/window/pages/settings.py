"""Settings: plain-language choices, each saved at once. Model and effort live in a fold marked Advanced.

Where each one is saved: voice (wake word, microphone, speaker, voice id) through
yuki-ui into ``voice.json``; the coach, quiet hours and blocked apps and sites in
``privacy.toml``; calls in memory (``set_call_pref``); Start with Windows in the
Run key; the look (appearance, glass, motion) in ``ui.json``.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from yuki.ui.window.actions import NOOP, Actions
from yuki.ui.window.data import SettingsView
from yuki.ui.window.theme import SPACE, Theme
from yuki.ui.window.widgets import (
    Divider,
    Dropdown,
    Glass,
    GlassSlider,
    KeyCapture,
    Panel,
    Pill,
    Segmented,
    SettingRow,
    TextPill,
    Toggle,
    combo_words,
    page_header,
    text_label,
)

#: The overlay's shortcut when the user has not set one (``Settings.ui_hotkey``).
DEFAULT_OPEN_HOTKEY = "alt+shift"

#: Quiet-hour choices, every half hour.
TIMES = tuple(f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30))
GLASS_WORDS = {"off": "Off", "light": "Light", "full": "Full"}


def _voice_name(voice_id: str) -> str:
    return f"Your ElevenLabs voice ({voice_id[:8]}…)" if voice_id else "Default voice (Jessica)"


def _chip_remove(t: Theme, text: str, on_remove) -> QWidget:
    """A blocked app or site: a button with its name and a cross; pressing it takes it off the list."""
    x = Pill(t, f"{text}  ✕", style="quiet", size="small")
    x.setToolTip(f"Stop blocking {text}")
    x.clicked.connect(lambda _c=False: on_remove())
    return x


def build(t: Theme, v: SettingsView, *, advanced: bool = False, act: Actions = NOOP, ui: Any = None,
          connected: bool | None = None) -> QWidget:
    """``ui``: the UI settings (``ui.json``); ``connected``: whether yuki-ui answers on the pipe (voice and
    model changes need it running)."""
    from yuki.ui.ui_settings import UiSettings

    ui = ui or UiSettings()
    page = QWidget()
    page.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    col = QVBoxLayout(page)
    col.setContentsMargins(SPACE["xl"], SPACE["l"], SPACE["xl"], SPACE["xl"])
    col.setSpacing(SPACE["l"])
    note = "Plain choices. Every change applies at once."
    if connected is False:
        note += " Yuki isn't running, so voice and model changes are saved for its next start."
    col.addWidget(page_header(t, "Settings", note))

    grid = QHBoxLayout()
    grid.setSpacing(SPACE["l"])
    left = QVBoxLayout()
    left.setSpacing(SPACE["l"])
    right = QVBoxLayout()
    right.setSpacing(SPACE["l"])

    # -- voice
    voice = Panel(t, "Voice", "Talk to Yuki with headphones on.", glyph="headphones")
    wake = Toggle(t, v.wake_on)
    wake.toggled.connect(act.set_wake)
    voice.body.addWidget(SettingRow(t, "wave", "Listen for “Yuki”", "Say its name to start talking. It listens "
                                    "on this PC; nothing leaves it until you say “Yuki”.", wake))
    voice.body.addWidget(Divider(t))
    mic = Dropdown(t, v.mic, width=250, options=lambda: act.device_options("capture"))
    mic.changed.connect(lambda name: act.set_device("capture", name))
    voice.body.addWidget(SettingRow(t, "mic", "Microphone", "", mic))
    spk = Dropdown(t, v.speaker, width=250, options=lambda: act.device_options("render"))
    spk.changed.connect(lambda name: act.set_device("render", name))
    voice.body.addWidget(SettingRow(t, "speaker", "Speaker", "", spk))
    vid = TextPill(t, "ElevenLabs voice id (empty: the default)", action="Save", text=v.voice_id, height=34)
    vid.setFixedWidth(250)
    vid.submitted.connect(lambda text: act.set_voice_id(text.strip()))
    voice.body.addWidget(SettingRow(t, "chat", "Yuki's voice", _voice_name(v.voice_id), vid))
    voice.body.addWidget(SettingRow(t, "link", "Talk shortcut", "Press Ctrl + Alt + Y to start or stop talking.",
                                    None))
    left.addWidget(voice)

    # -- coach
    coach = Panel(t, "Coach", "Short check-ins, reminders and a nudge when you've been at it too long.",
                  glyph="heart")
    coach_on = Toggle(t, v.coach_on)
    coach_on.toggled.connect(act.set_coach)
    coach.body.addWidget(SettingRow(t, "heart", "Check-ins and reminders", "", coach_on))
    # quiet hours: a switch to turn them off (memory reads equal start and end as none), and two compact
    # pickers that scroll (a menu of 48 times filled the screen)
    quiet_on = v.quiet_start != v.quiet_end
    last = (ui.quiet_last or [v.quiet_start, v.quiet_end]) if not quiet_on else [v.quiet_start, v.quiet_end]
    if last[0] == last[1]:
        last = ["00:30", "08:00"]
    q_toggle = Toggle(t, quiet_on)
    coach.body.addWidget(SettingRow(t, "moon", "Quiet hours", "Nothing at all during these hours.", q_toggle))
    quiet = QWidget()
    quiet.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    qrow = QHBoxLayout(quiet)
    qrow.setContentsMargins(34, 0, 0, 4)
    qrow.setSpacing(8)
    times = [(x, x) for x in TIMES]
    q_start = Dropdown(t, last[0], width=110, options=times)
    q_end = Dropdown(t, last[1], width=110, options=times)
    q_start.changed.connect(lambda value: act.set_quiet(str(value), q_end.text()))
    q_end.changed.connect(lambda value: act.set_quiet(q_start.text(), str(value)))
    qrow.addWidget(text_label(t, "From", "callout", "text_2"))
    qrow.addWidget(q_start)
    qrow.addWidget(text_label(t, "to", "callout", "text_2"))
    qrow.addWidget(q_end)
    qrow.addStretch(1)
    coach.body.addWidget(quiet)
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
    left.addWidget(coach)

    # -- general
    general = Panel(t, "General", "", glyph="settings")
    startup = Toggle(t, bool(v.start_with_windows))
    startup.toggled.connect(act.set_startup)
    general.body.addWidget(SettingRow(t, "power", "Start with Windows", "Yuki and its memory start when you sign in.",
                                      startup))
    hot = KeyCapture(t, ui.open_hotkey or DEFAULT_OPEN_HOTKEY, width=190)
    hot.setToolTip("Click, then press the keys you want, like Ctrl + Space, or two modifiers such as Alt + Shift")
    hot.captured.connect(act.set_open_hotkey)
    reset = Pill(t, "Default", style="quiet", size="small")
    reset.setToolTip(f"Back to {combo_words(DEFAULT_OPEN_HOTKEY)}")

    def reset_hotkey() -> None:
        hot.combo = DEFAULT_OPEN_HOTKEY
        hot.update()
        act.set_open_hotkey("")

    reset.clicked.connect(reset_hotkey)
    hbox_w = QWidget()
    hbox_w.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    hrow = QHBoxLayout(hbox_w)
    hrow.setContentsMargins(0, 0, 0, 0)
    hrow.setSpacing(6)
    hrow.addWidget(hot)
    hrow.addWidget(reset, 0, Qt.AlignmentFlag.AlignVCenter)
    general.body.addWidget(SettingRow(t, "link", "Open Yuki anywhere", "Click the box, then press your shortcut.",
                                      hbox_w))
    left.addWidget(general)

    # -- appearance (the look, ui.json)
    look = Panel(t, "Appearance", "How Yuki looks everywhere: this window, the chat box, the cards and the pills.",
                 glyph="sparkle")
    mode = Segmented(t, ("System", "Light", "Dark"), ui.appearance.capitalize(), small=True)
    mode.changed.connect(lambda word: act.set_appearance(word.lower()))
    look.body.addWidget(SettingRow(t, "sparkle", "Light or dark", "System follows Windows.", mode))
    background = Segmented(t, ("Plain", "See-through"),
                           "See-through" if ui.background == "see-through" else "Plain", small=True)
    background.changed.connect(lambda word: act.set_background("see-through" if word == "See-through" else "solid"))
    look.body.addWidget(SettingRow(t, "globe", "Background",
                                   "Plain: black in dark, white in light. See-through: your desktop shows behind "
                                   "the window, blurred by Windows.", background))
    glass = Segmented(t, tuple(GLASS_WORDS.values()), GLASS_WORDS.get(ui.glass, "Light"), small=True)
    look.body.addWidget(SettingRow(t, "bulb", "Glass effect",
                                   "Off is plain and clear. Light bends light at the edge of every card, cheaply. "
                                   "Full bends the most, with colour at the edges, and uses the graphics card.",
                                   glass))
    sliders = QWidget()
    sliders.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    look.body.addWidget(sliders)   # parented before anything shows it (a parentless shown widget is a window)
    srow = QVBoxLayout(sliders)
    srow.setContentsMargins(0, 0, 0, 0)
    srow.setSpacing(2)
    bend = GlassSlider(t, ui.bend, width=200)
    bend.committed.connect(act.set_bend)
    srow.addWidget(SettingRow(t, "wave", "Glass bending", "How strongly the edges bend light.", bend))
    blur = GlassSlider(t, ui.blur, width=200)
    blur.committed.connect(act.set_blur)
    srow.addWidget(SettingRow(t, "cloud", "Blur", "How frosted the glass is.", blur))
    if ui.glass == "off":
        sliders.hide()
    clear = GlassSlider(t, ui.transparency, width=200)
    clear.committed.connect(act.set_transparency)
    look.body.addWidget(SettingRow(t, "globe", "Transparency", "How see-through the glass is.", clear))

    def glass_changed(word: str) -> None:
        mode_key = next((k for k, w in GLASS_WORDS.items() if w == word), "light")
        sliders.setVisible(mode_key != "off")
        act.set_glass(mode_key)

    glass.changed.connect(glass_changed)
    motion = Toggle(t, ui.reduce_motion)
    motion.toggled.connect(act.set_reduce_motion)
    look.body.addWidget(SettingRow(t, "pause", "Reduce motion", "No sliding or fading: things just appear.", motion))
    left.addWidget(look)
    left.addStretch(1)

    # -- calls
    calls = Panel(t, "Calls", "Yuki can remember your own side of a call: only your microphone, only with "
                              "headphones, turned into text on this PC later. Nobody else is recorded.",
                  glyph="phone")
    for pref in v.call_prefs:
        choice = {"always": "Always", "never": "Never"}.get(pref["decision"], "Ask")
        seg = Segmented(t, ("Always", "Ask", "Never"), choice, small=True)
        seg.changed.connect(lambda word, app=pref["app"]: act.set_call_pref(
            app, {"Always": "always", "Never": "never"}.get(word)))
        calls.body.addWidget(SettingRow(t, "phone", pref.get("name") or pref["app"], "", seg))
    calls.body.addWidget(text_label(t, "Every other app: Yuki asks at the start of each call.", "caption", "text_3",
                                    wrap=True))
    right.addWidget(calls)

    # -- privacy
    privacy = Panel(t, "Privacy", "", glyph="shield")
    paused = Toggle(t, v.paused)
    paused.toggled.connect(act.set_paused)
    privacy.body.addWidget(SettingRow(t, "pause", "Pause memory", "Stop remembering your screen until you turn it "
                                      "back on.", paused))
    privacy.body.addWidget(Divider(t))
    for glyph, label, kind, items, add, hint in (
            ("lock", "Apps Yuki never looks at", "apps", v.blocked_apps, "Add app", "e.g. discord.exe or Discord"),
            ("globe", "Sites Yuki never looks at", "sites", v.blocked_sites, "Add site", "e.g. example.com")):
        box = QWidget()
        box.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        brow = QHBoxLayout(box)
        brow.setContentsMargins(0, 0, 0, 0)
        brow.setSpacing(6)
        for name in items[:6]:
            brow.addWidget(_chip_remove(t, name, lambda k=kind, n=name: act.remove_blocked(k, n)))
        add_pill = Pill(t, add, glyph="edit", style="quiet", size="small")
        brow.addWidget(add_pill)
        privacy.body.addWidget(SettingRow(t, glyph, label, "None added yet." if not items else "", box))
        entry = TextPill(t, hint, action="Block", height=34)
        entry.setVisible(False)
        entry.submitted.connect(lambda text, k=kind, e=entry: (act.add_blocked(k, text.strip()), e.setVisible(False))
                                if text.strip() else None)
        add_pill.clicked.connect(lambda _c=False, e=entry: (e.setVisible(not e.isVisible()), e.edit.setFocus()))
        privacy.body.addWidget(entry)
    always = Glass(t, level="inset", radius=16, padding=SPACE["m"])
    always.body.addWidget(text_label(t, "Always private, whatever you choose", "caption_strong", "text_2"))
    always.body.addWidget(text_label(t, "Password managers, banking and sign-in pages, password fields, private "
                                        "browser windows, and what's said or shown in meetings.", "caption", "text_3",
                                     wrap=True))
    privacy.body.addWidget(always)
    right.addWidget(privacy)

    # -- advanced
    adv = Glass(t, level="regular", radius=24, padding=SPACE["l"])
    head = QHBoxLayout()
    head.addWidget(text_label(t, "Advanced", "headline"))
    head.addWidget(text_label(t, "for tinkering; the defaults are fine", "caption", "text_3"))
    head.addStretch(1)
    fold = Pill(t, glyph="down" if advanced else "next", style="quiet", size="small")
    head.addWidget(fold)
    adv.body.addLayout(head)
    inner = QWidget()
    inner.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    adv.body.addWidget(inner)
    ilay = QVBoxLayout(inner)
    ilay.setContentsMargins(0, 0, 0, 0)
    model = Segmented(t, ("Sonnet", "Opus"), v.model, small=True)
    model.changed.connect(lambda word: act.set_model(word.lower()))
    ilay.addWidget(SettingRow(t, "sparkle", "Model", "Opus is smarter and slower; Sonnet is quick.", model))
    effort = Segmented(t, ("Low", "Medium", "High", "Max"), v.effort, small=True)
    effort.changed.connect(lambda word: act.set_effort(word.lower()))
    ilay.addWidget(SettingRow(t, "bulb", "Thinking effort", "More effort: better on hard tasks, slower and "
                              "costlier.", effort))
    logs = Pill(t, "Open logs folder", size="small")
    logs.clicked.connect(act.open_logs)
    ilay.addWidget(SettingRow(t, "book", "Logs", "", logs))
    if not advanced:
        inner.hide()

    def toggle_fold() -> None:
        inner.setVisible(not inner.isVisible())
        fold.glyph = "down" if inner.isVisible() else "next"
        fold.update()

    fold.clicked.connect(toggle_fold)
    right.addWidget(adv)
    right.addStretch(1)

    grid.addLayout(left, 1)
    grid.addLayout(right, 1)
    col.addLayout(grid)
    col.addStretch(1)
    return page
