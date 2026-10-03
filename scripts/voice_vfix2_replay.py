"""vfix2 text replays of the talker (report 47: N0, N1, N7, N9, N10), invented names, on the real talker model.

The machinery is ``voice_vfix_replay``'s (each request built the way a conversation builds it: the cached layers, the
real tool schemas, the conversation with its markers, the volatile block with a stand-in desktop, read by the same speech
channel); only the cases and their checks are this report's. Nothing opens a device or touches the desktop.

    .venv\\Scripts\\python.exe scripts\\voice_vfix2_replay.py [--reps 3] [--variant new|head] [--budget 0.5]

``--variant head`` runs the same cases on main's talker (``--head-ref``). Writes ``<out>/vfix-replay-<variant>-<time>.json``
(the base script's name).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import voice_vfix_replay as base  # noqa: E402
from voice_vfix_replay import a, call, result, u  # noqa: E402

LIST = ("open chrome, open youtube, scroll the recommendations, then message sam on discord asking if he's coming "
        "on tonight")
WORK = ('task 3 "Open Chrome, open YouTube, scroll the recommendations, then message Sam on Discord asking if he is '
        'coming on tonight" running for 40 s, now: scrolling the page; still to do on its list: message Sam on '
        'Discord asking if he is coming on tonight')
HANDED = [u(LIST), call("tell_yuki", {"screen": False, "say_first": "On it.", "app_named": "Discord",
                                      "request": "Open Chrome, open YouTube, scroll the recommendations, then message "
                                                 "Sam on Discord asking if he's coming on tonight",
                                      "acts": True, "intent": "new_task"}, "t1"),
          result("t1", {"status": "started"})]
PEOPLE_N9 = ("Hemanth (best friend): Discord DM 'hemanth_k' (usual, most used lately); WhatsApp 'Hemanth'.\n"
             "Nichol (coworker): Slack DM 'nichol.r' (usual).\n"
             "Kabir (friend): Slack DM 'kabir.r' (usual).")
HEARD_N9 = ("unsure of 'niche' (0.60); 'hey meant' sounds close to Hemanth, a name you know; 'niche' sounds close to "
            "Nichol, a name you know")
PEOPLE_N9B = ("Disha (sister): WhatsApp 'Disha' (usual, most used lately).\n"
              "Shaan (colleague): Slack DM 'shaan.m' (usual).\nKabir (friend): Slack DM 'kabir.r' (usual).")
HEARD_N9B = ("unsure of 'sha' (0.55); 'sha' sounds close to Shaan, a name you know; 'the sha' sounds close to Disha, "
             "a name you know")
NOOP_REPORT = ("No change: Spotify.exe's own level in the Windows volume mixer is already at 100% (its top), so there "
               "was nothing to raise; nothing was changed. That is only the mixer level: the app's own player volume "
               "(its slider in its window) and the system volume (now 35%) are separate, and one of those may be what "
               "is low. Say it as a no-op and offer the one that matters, never that it was turned up")


def cases() -> list[dict[str, Any]]:
    from yuki.voice.hands import _NOOP_NOTE

    return [
        # N10: a quick interjection never drops the rest of the list
        dict(id="n10_volume_mid_list", issue="N10", check="keeps_list_volume", work=WORK,
             messages=[*HANDED, u("turn the volume up")]),
        dict(id="n10_video_mid_list", issue="N10", check="keeps_list", work=WORK,
             messages=[*HANDED, u("open this video")]),
        dict(id="n10_forget", issue="N10 control", check="instead", work=WORK,
             messages=[*HANDED, u("forget the message, just play some lofi")]),
        # N7: the name heard while they dictate into another app
        dict(id="n7_dictating", issue="N7", check="quiet", named=True,
             state={"dictating": 'Code.exe "notes.md - Code" (the field Ask anything)'},
             messages=[u("so the pipeline makes it dumb, it should check the chat first before sending, and then")]),
        dict(id="n7_named_control", issue="N7 control", check="not_quiet", named=True,
             messages=[u("pause the music")]),
        # N9: a name split across two words, resolved by sound and situation
        dict(id="n9_hey_meant", issue="N9", check="hemanth", people=PEOPLE_N9, heard=HEARD_N9,
             messages=[u("message hey meant on discord that I fell asleep on the call, sorry")]),
        # N9 generalisation: other names, other app, not the guide's own example
        dict(id="n9_the_sha", issue="N9", check="name:disha:shaan:the sha", people=PEOPLE_N9B, heard=HEARD_N9B,
             messages=[u("text the sha on whatsapp that I'll be late for dinner")]),
        # N1: a volume no-op is said as one
        dict(id="n1_noop", issue="N1", check="noop_said", messages=[
            u("increase the spotify volume"),
            call("app_volume", {"app": "Spotify.exe", "change": 15, "say_when_done": "Turned it up."}, "v1"),
            result("v1", {"no_change": NOOP_REPORT, "how_to_say_it": _NOOP_NOTE})]),
        # N0: the hand-off carries their situation and words, not a bare order
        dict(id="n0_handoff", issue="N0", check="situation_kept", people=base.PEOPLE + "\nSam (best friend): "
             "Discord DM 'sam_99' (usual, most used lately).",
             messages=[u("tell sam I fell asleep on the call, my alarm didn't go off")]),
    ]


def check(case: dict[str, Any], row: dict[str, Any]) -> bool:
    """This report's checks (an eval's own scoring of the model's output, never a rule inside Yuki)."""
    calls = row["calls"]
    names = [c["name"] for c in calls]
    tells = [c["input"] for c in calls if c["name"] == "tell_yuki"]
    spoken = (row.get("spoken") or "").casefold()
    kind = case["check"]
    instead = any(t.get("intent") == "instead" for t in tells) or "cancel_tell_yuki" in names
    if kind == "keeps_list_volume":
        return not instead and ("app_volume" in names or "media_key" in names
                                or any(t.get("intent") == "follow_up" for t in tells))
    if kind == "keeps_list":
        return not instead and bool(names or spoken)
    if kind == "instead":   # dropped on their words AND what they want now is handed over (not a bare cancel)
        return any(t.get("intent") == "instead" and "lofi" in str(t.get("request") or "").casefold().replace("-", "")
                   for t in tells)
    if kind == "hemanth":
        kind = "name:hemanth:nichol:hey meant"
    if kind.startswith("name:"):
        # handed over with the right person, or with the words as heard for the thinker to resolve; never the
        # wrong person picked by spelling; a question puts the right one first
        _k, want, other, heard = kind.split(":")
        requests = " ".join(str(t.get("request") or "") for t in tells).casefold()
        contexts = " ".join(str(t.get("context") or "") for t in tells).casefold()
        if tells:
            return (want in requests or heard in requests + contexts) and other not in requests.replace(heard, "")
        return want in spoken and (other not in spoken or spoken.find(want) < spoken.find(other))
    if kind == "noop_said":
        return ("turned it up" not in spoken and "turned up" not in spoken and "turning it up" not in spoken
                and ("already" in spoken or "100" in spoken or "max" in spoken))
    if kind == "situation_kept":
        return any("asleep" in str(t.get("request") or "").casefold() for t in tells)
    return base_check(case, row)


base_check = base.check
base.check = check
base.cases = cases

if __name__ == "__main__":
    raise SystemExit(base.main())
