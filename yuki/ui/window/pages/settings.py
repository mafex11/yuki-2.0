"""Settings: plain-language choices. Model and effort live in a fold marked Advanced."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from yuki.ui.window.data import SettingsView
from yuki.ui.window.theme import SPACE, Theme
from yuki.ui.window.widgets import (
    Chip,
    Divider,
    Dropdown,
    Glass,
    Panel,
    Pill,
    Segmented,
    SettingRow,
    Toggle,
    page_header,
    text_label,
)


def _voice_name(voice_id: str) -> str:
    return f"Your ElevenLabs voice ({voice_id[:8]}…)" if voice_id else "Default voice (Jessica)"


def build(t: Theme, v: SettingsView, *, advanced: bool = False) -> QWidget:
    page = QWidget()
    page.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    col = QVBoxLayout(page)
    col.setContentsMargins(SPACE["xl"], SPACE["l"], SPACE["xl"], SPACE["xl"])
    col.setSpacing(SPACE["l"])
    col.addWidget(page_header(t, "Settings", "Plain choices. Every change applies at once."))

    grid = QHBoxLayout()
    grid.setSpacing(SPACE["l"])
    left = QVBoxLayout()
    left.setSpacing(SPACE["l"])
    right = QVBoxLayout()
    right.setSpacing(SPACE["l"])

    voice = Panel(t, "Voice", "Talk to Yuki with headphones on.", glyph="headphones")
    voice.body.addWidget(SettingRow(t, "wave", "Listen for “Yuki”", "Say its name to start talking. It listens "
                                    "on this PC; nothing leaves it until you say “Yuki”.", Toggle(t, v.wake_on)))
    voice.body.addWidget(Divider(t))
    voice.body.addWidget(SettingRow(t, "mic", "Microphone", "", Dropdown(t, v.mic, width=250)))
    voice.body.addWidget(SettingRow(t, "speaker", "Speaker", "", Dropdown(t, v.speaker, width=250)))
    voice.body.addWidget(SettingRow(t, "chat", "Yuki's voice", "", Dropdown(t, _voice_name(v.voice_id), width=250)))
    voice.body.addWidget(SettingRow(t, "link", "Shortcut", "Press to start or stop talking.",
                                    Pill(t, "Ctrl + Alt + Y", style="quiet", size="small")))
    left.addWidget(voice)

    coach = Panel(t, "Coach", "Short check-ins, reminders and a nudge when you've been at it too long.",
                  glyph="heart")
    coach.body.addWidget(SettingRow(t, "heart", "Check-ins and reminders", "", Toggle(t, v.coach_on)))
    quiet = QWidget()
    qrow = QHBoxLayout(quiet)
    qrow.setContentsMargins(0, 0, 0, 0)
    qrow.setSpacing(6)
    qrow.addWidget(Dropdown(t, v.quiet_start, width=96))
    qrow.addWidget(text_label(t, "to", "caption", "text_3"))
    qrow.addWidget(Dropdown(t, v.quiet_end, width=96))
    coach.body.addWidget(SettingRow(t, "moon", "Quiet hours", "Nothing at all during these hours.", quiet))
    coach.body.addWidget(SettingRow(t, "question", "Questions about people and things",
                                    "Yuki asks instead of guessing, a few times a day at most.",
                                    Toggle(t, v.questions_on)))
    left.addWidget(coach)

    general = Panel(t, "General", "", glyph="settings")
    general.body.addWidget(SettingRow(t, "power", "Start with Windows", "Yuki and its memory start when you sign in.",
                                      Toggle(t, bool(v.start_with_windows))))
    general.body.addWidget(SettingRow(t, "sparkle", "Appearance", "",
                                      Segmented(t, ("System", "Light", "Dark"), "System", small=True)))
    general.body.addWidget(SettingRow(t, "link", "Open Yuki anywhere", "", Pill(t, "Alt + Shift", style="quiet",
                                                                                 size="small")))
    left.addWidget(general)
    left.addStretch(1)

    calls = Panel(t, "Calls", "Yuki can remember your own side of a call: only your microphone, only with "
                              "headphones, turned into text on this PC later. Nobody else is recorded.",
                  glyph="phone")
    for pref in v.call_prefs:
        choice = {"always": "Always", "never": "Never"}.get(pref["decision"], "Ask")
        calls.body.addWidget(SettingRow(t, "phone", pref.get("name") or pref["app"], "",
                                        Segmented(t, ("Always", "Ask", "Never"), choice, small=True)))
    calls.body.addWidget(text_label(t, "Every other app: Yuki asks at the start of each call.", "caption", "text_3",
                                    wrap=True))
    right.addWidget(calls)

    privacy = Panel(t, "Privacy", "", glyph="shield")
    privacy.body.addWidget(SettingRow(t, "pause", "Pause memory", "Stop remembering your screen until you turn it "
                                      "back on.", Toggle(t, v.paused)))
    privacy.body.addWidget(Divider(t))
    for glyph, label, items, add in (("lock", "Apps Yuki never looks at", v.blocked_apps, "Add app"),
                                     ("globe", "Sites Yuki never looks at", v.blocked_sites, "Add site")):
        box = QWidget()
        brow = QHBoxLayout(box)
        brow.setContentsMargins(0, 0, 0, 0)
        brow.setSpacing(6)
        for name in items[:4]:
            brow.addWidget(Chip(t, "you", name))
        brow.addWidget(Pill(t, add, glyph="edit", style="quiet", size="small"))
        privacy.body.addWidget(SettingRow(t, glyph, label, "None added yet." if not items else "", box))
    always = Glass(t, level="inset", radius=16, padding=SPACE["m"])
    always.body.addWidget(text_label(t, "Always private, whatever you choose", "caption_strong", "text_2"))
    always.body.addWidget(text_label(t, "Password managers, banking and sign-in pages, password fields, private "
                                        "browser windows, and what's said or shown in meetings.", "caption", "text_3",
                                     wrap=True))
    privacy.body.addWidget(always)
    right.addWidget(privacy)

    adv = Glass(t, level="regular", radius=24, padding=SPACE["l"])
    head = QHBoxLayout()
    head.addWidget(text_label(t, "Advanced", "headline"))
    head.addWidget(text_label(t, "for tinkering; the defaults are fine", "caption", "text_3"))
    head.addStretch(1)
    head.addWidget(Pill(t, glyph="down" if advanced else "next", style="quiet", size="small"))
    adv.body.addLayout(head)
    if advanced:
        adv.body.addWidget(SettingRow(t, "sparkle", "Model", "Opus is smarter and slower; Sonnet is quick.",
                                      Segmented(t, ("Sonnet", "Opus"), v.model, small=True)))
        adv.body.addWidget(SettingRow(t, "bulb", "Thinking effort", "More effort: better on hard tasks, slower and "
                                      "costlier.", Segmented(t, ("Low", "Medium", "High", "Max"), v.effort,
                                                             small=True)))
        adv.body.addWidget(SettingRow(t, "book", "Logs", "", Pill(t, "Open logs folder", size="small")))
    right.addWidget(adv)
    right.addStretch(1)

    grid.addLayout(left, 1)
    grid.addLayout(right, 1)
    col.addLayout(grid)
    col.addStretch(1)
    return page
