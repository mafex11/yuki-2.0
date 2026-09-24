"""Yuki's desktop shell: a global hotkey, a glass overlay, and the agent runtime.

Nothing in here touches the desktop directly. The UI drives
:class:`yuki.agent.loop.Agent` instances that live on their own threads
(:mod:`yuki.ui.runtime`) and receives everything back as Qt signals, so the only
thread that ever paints is the GUI thread.

Modules:
    hotkey: global hotkeys via Win32 ``RegisterHotKey`` on its own message loop.
    glass: the frameless dark-glass window base, with the show/hide animation.
    overlay: the ask-Yuki overlay -- input plus a stack of reply cards.
    status: the bottom-right status strip shown while a task is running.
    nudges: memory's nudge cards (bottom-right, never focused) and their queue.
    memory: the tray's memory controls and the portrait / to-do panels.
    runtime: the two agent lanes (worker / front desk) and their queue.
    uilog: UI events written through the existing :class:`SessionLogger`.
    app: ``main()`` -- tray icon, single-instance guard, wiring.
"""

from __future__ import annotations
