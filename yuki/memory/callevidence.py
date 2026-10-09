"""Whether the user has really joined a call: judged from the screen, never from the microphone alone.

Live 2026-09-28 09:01-09:11 the Zen browser held the microphone (a tab with microphone permission) while it
played YouTube, and the call worker took that for a call: voice went into call mode and refused the user's
own commands. Earlier, a Google Meet pre-join lobby (which holds the microphone for its preview) counted as a
meeting too. The user: "call detection must use the screen, which Yuki already captures ... a mic held by an
app is only a hint, never proof."

So a call needs **evidence from the screen**. The watcher (:mod:`yuki.memory.watcher`) hands every window it
extracts to :meth:`CallEvidence.seen`: the app, the page host, and the names of the visible controls and
headings (:func:`yuki.memory.extract.screen_controls`, structural, capped). For an app that holds the
microphone now (the hint), the latest screen of each of its sites is judged once by a small model (Claude
Haiku, forced tool ``judge_screen``, worked examples): does it show a call the user has joined (call controls,
participants, a leave or disconnect button, a voice-connected panel) or not (a pre-join lobby, a text chat,
a video page)? A verdict is cached by the screen's content, so an unchanged window costs nothing again.

:meth:`CallEvidence.state` then says, for an app and its current microphone hold:

* ``"fresh"``: a screen taken during this hold was judged in a call, within ``fresh_s`` (default 900 s);
  seeing the same screen again renews it;
* ``"stale"``: the in-call screen was taken during this hold but longer ago than ``fresh_s`` (the user joined,
  then went full screen in a game for an hour): probably still in the call, not certain;
* ``None``: no in-call screen during this hold (mic only, a lobby, a video page): not a call.

A later screen of the same app and site judged not in a call means that site's evidence can no longer
*start* a call (they left: the Discord panel is gone, the Meet page shows "You left the meeting"); a screen of
another site in the same browser does not (a Meet call goes on while they look at YouTube in another tab).

**Starting is not continuing** (2026-10-03, ``docs/research/revamp-2026-10-02/05-call-pill-timer.md``): one
Discord frame read in the 6-21 s of an alt-tab from a game and judged "not in a call" used to delete the
evidence, and 30 s later the call row ended (six calls cut that way in 3.5 days, 32 minutes lost on 2 Oct).
Those frames were half drawn: 0, 0, 18, 21, 21 and 22 controls against at least 68 in all 183 in-call reads. So:

* the judge has a third answer, ``unclear`` (an alt-tab or half-drawn window, a worked example in its
  prompt), which counts as neither a call nor no call;
* a frame is only **healthy** when its control count is at least :data:`HEALTH_RATIO` times the median of the
  site's own last :data:`HEALTH_WINDOW` in-call frames (a measured number, no words): an unhealthy frame
  counts as neither, whatever the verdict;
* a healthy "not in a call" is a **reliable negative**; every sight of it is counted (the watcher re-reads a
  window that stays in front), and the call worker's :class:`yuki.memory.calls.CallLife` ends a call whose
  microphone is still held only on a persistent run of them (:meth:`life`), never on one.

**A meeting site the privacy rules keep unread** (2026-10-05, ``docs/research/revamp-2026-10-02/84-build-meetFix.md``):
live, the user sat in a Google Meet call in the Zen browser and no call was ever seen, because the privacy file
blocks ``meet.google.com`` and ``zoom.us`` (and Teams' meeting paths): the watcher never reads those pages, so
no screen of the call ever reached the judge. A blocked page's content stays unread; the watcher hands over
only its **site identity** (:meth:`seen` with ``blocked``: the app, the page's host, when; never a title, a path
or a control), and it stays in this process. Which of those sites are calling services is decided from the
privacy file itself, never from a visit:

* the **shipped** list of blocked sites (``privacy_default.toml`` ``[web] blocked_host_suffixes`` and the hosts of
  ``blocked_host_paths``; public data, the same for every user) is judged **once, as a whole**, by the same small
  model (``judge_sites``, worked examples) the first time a site sighting of a microphone holder needs it: which
  entries are voice or video calling services. No request ever names a page the user visited (review F1 of
  2026-10-05: an earlier version of this change sent each visited blocked host, a bank or a sign-in site too);
* the user's own ``[meetings] always_sites`` are calling sites by their word (named by ``[meetings.names]``), and
  ``never_sites`` never are;
* a blocked site the user added themselves (not in the shipped list, not in ``always_sites``) never counts and is
  never sent anywhere.

A sighting of a calling site during the hold is site evidence: it **starts** a call (``fresh`` / ``stale`` like a
screen; a lobby holds the microphone too, so a false start costs at most the Ask card, which still decides any
recording, and the standing answer is the service's, never the browser's), unless a newer readable screen of the
same app and site was judged not in a call (Teams' readable pre-join lobby after its blocked join link). A site
call's life (:attr:`Life.site`, :class:`yuki.memory.calls.CallLife`): while the app holds the microphone it goes
on, unless readable screens of the same site persistently say otherwise, or the app's newest frame is another page
judged not in a call and the site has not been seen for ``screen_alive_s`` (one glance at a meeting page during a
long unrelated hold, review F4); once the app lets go (Meet releases the microphone on mute) it goes on while the
app still plays sound and the site was seen within ``screen_alive_s`` (review F3), else it ends by the release
grace. A browser holding the microphone for a video site is still no call: that page is read and judged as a screen.

**Kept across restarts and asked without a microphone** (PLAN.md F5, D8): the shipped list's verdicts are kept in
``nudge_state['call_evidence:sites']`` with a digest of the list (``state``: the store), reused while the list is
unchanged and judged again only when it changes, so a service start costs no model call and the first sighting is
instant. :meth:`CallEvidence.calling_service` answers "which calling service is this blocked page's host?" for any
sighting, with no microphone holder needed (the ``site_front`` moment and the Meet tab), never blocking its caller: a
list not judged yet is judged once on a short-lived thread, and the answer is None until then.

**The Meet tab** (:class:`SiteTabs`, PLAN.md D8): on a sighting of a calling site, the selected tab of that window is
read as identities (its RuntimeId with the browser's pid, a keyed digest of its name, its place outside the main
strip; :func:`yuki.memory.extract.uia.tab_identities`) at most once per 10 min per window and site, kept in
``nudge_state['site_tabs:<service>']``, and served on the Now board (``calling_tabs``). The census
(:meth:`SiteTabs.census`) finds the tab again in every window of the browser when an offer is due. Content-free logs
(``tab_strip``, ``tab_census``): group sizes, counts, ms, reason codes, never a tab name.

Logs are content-free (``call_evidence_judged`` / ``call_evidence_applied`` for a cached verdict: app, the
verdict, the frame's control count, whether it was healthy, the site's median, ms, cost;
``call_evidence_sites``: the shipped list's size, how many are calling services, their product names, ms, cost;
``call_evidence_site``: apps, whether the site counts, its product name, by what): never a control name, never a
blocked page's host. Nothing here decides from words in code: the model judges the screen and the site list.
"""

from __future__ import annotations

import hashlib
import json
import statistics
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Any

#: An in-call screen stays "fresh" evidence this long after it was last seen (s); then "stale".
FRESH_S = 900.0
#: A screen taken this long before the microphone hold began still counts for it (s): the two clocks are
#: read by different processes (the ConsentStore's start time, the capture's wall clock).
HOLD_TOLERANCE_S = 5.0
#: Verdicts kept (by screen content).
VERDICT_CACHE = 256
#: Controls sent to the model at most (names; the extraction already caps them).
MAX_CONTROLS = 80
#: A site with live in-call evidence is judged again at most this often (s) while its screen keeps changing (a
#: busy chat changes with every message): a cost guard only (a judgement costs ~$0.003; 195 in 3.5 days, measured
#: 2 Oct), 60 s until 2026-10-03, when the wait itself let a re-opened microphone hold end a call (05 §4 M3). A
#: site with none (joining), or known content, is answered at once.
REJUDGE_S = 20.0
#: A site whose last verdict said the user is alone in the call (nobody else joined yet) is judged again this soon
#: on a change: someone joining is what the change most likely is, and voice waits on it (v3.15).
ALONE_REJUDGE_S = 8.0
#: A frame is healthy (its verdict counts) only with at least this share of the median control count of the
#: site's own recent in-call frames. Measured 28 Sept-2 Oct on Discord: 183 in-call reads had 68-80 controls;
#: the six "not in a call" reads that ended live calls had 0, 0, 18, 21, 21 and 22 (half-drawn, mid alt-tab);
#: real "not in a call" screens (before joining) had 80.
HEALTH_RATIO = 0.5
#: ... over this many of the site's latest in-call frames.
HEALTH_WINDOW = 20
#: Reliable negative sights kept per site (the call worker needs a few, spanning minutes).
MAX_NEGATIVES = 32

#: Site verdicts kept (by host).
SITE_CACHE = 128
#: Blocked-site sightings kept (per app and host).
MAX_SITES = 256
#: A failed judgement of the shipped site list is tried again after this long (s).
SITES_RETRY_S = 60.0
#: Where the shipped list's verdicts are kept across restarts (``nudge_state``), with a digest of the list.
SITES_STATE_KEY = "call_evidence:sites"
#: A calling site's tab is read again at most this often per window and site (s).
TAB_READ_GAP_S = 600.0

#: The judge's three answers.
IN_CALL, NOT_IN_CALL, UNCLEAR = "in_call", "not_in_call", "unclear"

JUDGE_TOOL: dict[str, Any] = {
    "name": "judge_screen",
    "description": "Say whether this window shows a voice or video call that the user has joined right now.",
    "input_schema": {
        "type": "object",
        "properties": {
            "state": {
                "type": "string",
                "enum": [IN_CALL, NOT_IN_CALL, UNCLEAR],
                "description": "in_call only when the window shows a call the user is in now (joined): call "
                               "controls such as leave, hang up, end call or disconnect, a voice-connected panel, "
                               "participant tiles. not_in_call for a fully drawn window that shows no joined call: "
                               "a pre-join lobby or waiting screen, a call that ended, a text chat, a video or any "
                               "other page. unclear when the window cannot tell either way because it is not drawn "
                               "yet or only partly: only its frame buttons, almost no controls, or the app's outer "
                               "navigation without its main content (a window caught mid alt-tab or while it "
                               "repaints).",
            },
            "service": {
                "type": "string",
                "description": "When in_call: the product's common name (Google Meet, Discord, Zoom, Microsoft "
                               "Teams, WhatsApp, Slack). Otherwise empty.",
            },
            "others": {
                "type": "integer",
                "description": "When in_call: how many OTHER people are in the call with the user right now, as "
                               "the screen shows it (participant tiles, the names listed under the voice channel "
                               "the user is connected to, a participant count). 0 when the user is alone in it "
                               "(only their own name or tile, waiting for someone, still ringing, 'no one else "
                               "is here'). -1 when the screen does not show who is in the call. Otherwise -1.",
            },
        },
        "required": ["state", "service", "others"],
    },
}

JUDGE_PROMPT = """\
You look at one window of the user's PC, given as its title, its web address host (if it is a web page) and \
the names of the controls and headings visible in it, in on-screen order. Decide whether it shows a voice or \
video call that the user has actually joined right now. The microphone being in use is not proof: a meeting's \
pre-join lobby, a browser tab that was once given the microphone, or a game also use it. Only the screen says \
whether they are in the call.

Three answers. in_call: the controls of a live call (leave call, hang up, end call, disconnect, mute and \
camera toggles of the call, share screen, participants or people in the call, meeting details), or a panel \
saying the voice is connected. not_in_call: a fully drawn window that shows no joined call - a lobby that asks \
them to join (join now, ask to join, ready to join?), a screen saying the call ended or they left, a chat or \
channel with messages only, a video page, an editor, a list of chats, settings. unclear: the window cannot say \
either way because it is not drawn yet or only partly - the user is switching to it (alt-tab from a game) or it \
is repainting, so you see only its frame buttons, no controls at all, or the app's outer navigation without its \
main content. A window that is half drawn is unclear, never not_in_call: the missing part may be the call.

Examples:
- title "Meet - abc-defg-hij", host meet.google.com, controls: "Ready to join?", "Join now", "Other ways to \
join", "Turn off microphone", "Turn off camera" -> not_in_call (the pre-join lobby)
- title "Meet - abc-defg-hij", host meet.google.com, controls: "Leave call", "Turn off microphone", "Turn off \
camera", "Share screen", "Meeting details", "People", "Chat with everyone", "Rohan", "You" -> in_call, \
Google Meet
- title "#general | Friends - Discord", host discord.com, controls: "Voice Connected", "General / Friends", \
"Disconnect", "Noise Suppression", "Mute", "Deafen", "#general", "Message #general" -> in_call, Discord
- title "#memes | Friends - Discord", host discord.com, controls: "#general", "#memes", "Message #memes", \
"Mute", "Deafen", "User Settings" -> not_in_call (a text channel; no voice-connected panel)
- title "Discord", host discord.com, controls: "Minimize", "Maximize", "Close" -> unclear (only the window's \
frame: it is still being drawn)
- title "#general | Friends - Discord", host discord.com, controls: (none visible) -> unclear (nothing drawn yet)
- title "Friends - Discord", host discord.com, controls: "Minimize", "Maximize", "Close", "Servers", "Direct \
Messages", "Friends", "Add a Server" -> unclear (the server list is drawn but not the channels or the voice \
panel: half a window, caught while switching to it)
- title "Meet - abc-defg-hij", host meet.google.com, controls: "Meet", "Loading" -> unclear (the page is \
still loading)
- title "Zoom Meeting", controls: "Mute", "Stop Video", "Participants", "Share Screen", "End" -> in_call, Zoom
- title "Microsoft Teams", controls: "Join now", "Choose your video and audio options", "Computer audio" -> \
not_in_call (Teams pre-join)
- title "(1558) YouTube", host youtube.com, controls: "Search", "Home", "Shorts", "Subscriptions", video \
titles -> not_in_call
- title "WhatsApp", controls: "Chats", "Search", "Bro", "Res", "Elina", "Archived" -> not_in_call (a chat list)
- title "Meet", host meet.google.com, controls: "You left the meeting", "Rejoin", "Return to home screen" -> \
not_in_call (the call ended for them)
- title "Voice call", controls: "End call", "Mute", "Speaker", "Video" -> in_call, WhatsApp, others -1
- title "#general | Friends - Discord", controls: "General", "sam", "Rohan", "Voice Connected", "General / \
Friends", "Disconnect", "Mute", "Deafen" -> in_call, Discord, others 1 (two names under the voice channel: \
the user and Rohan)
- title "@Priya - Discord", controls: "Priya, Ongoing Call", "Voice Connected", "Disconnect", "sam", \
"Ringing", "Mute", "Deafen" -> in_call, Discord, others 0 (only the user has joined; the other is still \
being rung)
- title "Meet - abc-defg-hij", controls: "Leave call", "Turn off microphone", "Share screen", "People 1", \
"You", "No one else is here" -> in_call, Google Meet, others 0
- title "Zoom Meeting", controls: "Mute", "Stop Video", "Participants 4", "End" -> in_call, Zoom, others 3

The user's own name or tile never counts in others; a call whose screen does not show who is in it is -1.

Answer with the judge_screen tool only."""

SITES_TOOL: dict[str, Any] = {
    "name": "judge_sites",
    "description": "Say, for each web site in the list, whether it is a voice or video calling service.",
    "input_schema": {
        "type": "object",
        "properties": {
            "sites": {
                "type": "array",
                "description": "One item per site of the list, in its order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string", "description": "The site exactly as listed."},
                        "calling": {
                            "type": "boolean",
                            "description": "true when the site is a voice or video calling or meeting service: a "
                                           "page of it that uses the microphone is a call or a meeting, or its "
                                           "waiting room. false for any other site (sign-in, password manager, "
                                           "bank, payment, shop, video, mail, documents).",
                        },
                        "service": {
                            "type": "string",
                            "description": "When calling: the product's common name (Google Meet, Zoom, Microsoft "
                                           "Teams). Otherwise empty.",
                        },
                    },
                    "required": ["host", "calling", "service"],
                },
            },
        },
        "required": ["sites"],
    },
}

SITES_PROMPT = """\
You get a list of web sites (address hosts; a site also covers its subdomains) that a privacy setting keeps \
unread. Nothing of any page is given, and nobody visited them for this question: it is the setting's own list. \
Say for each whether it is a voice or video calling service - a site whose pages use the microphone for calls or \
meetings (or the waiting room before one) - or any other kind of site (signing in, password managers, banking, \
payments, shopping, video, mail, documents), which has no calls of its own.

Examples:
- meet.google.com -> calling, Google Meet
- zoom.us -> calling, Zoom
- teams.microsoft.com -> calling, Microsoft Teams
- teams.live.com -> calling, Microsoft Teams
- whereby.com -> calling, Whereby
- accounts.google.com -> not calling (signing in)
- login.microsoftonline.com -> not calling (signing in)
- paypal.com -> not calling (payments)
- netbanking.examplebank.com -> not calling (a bank)
- bitwarden.com -> not calling (a password manager)

Answer with the judge_sites tool only, one item for every site of the list."""


def shipped_site_entries() -> tuple[str, ...]:
    """The blocked sites of the packaged privacy file (``[web] blocked_host_suffixes`` and the hosts of
    ``blocked_host_paths``), sorted: public data, the same for every user. () when unreadable."""
    global _SHIPPED
    if _SHIPPED is None:
        try:
            from yuki.memory.privacy import DEFAULT_PATH, PrivacyRules

            rules = PrivacyRules.load(DEFAULT_PATH)
            _SHIPPED = tuple(sorted(set(rules.blocked_host_suffixes) | set(rules.blocked_host_paths)))
        except Exception:  # noqa: BLE001 - no shipped list: no site counts but the user's always_sites
            _SHIPPED = ()
    return _SHIPPED


_SHIPPED: tuple[str, ...] | None = None


def _host_matches(host: str, entry: str) -> bool:
    return host == entry or host.endswith("." + entry)


@dataclass
class Screen:
    key: str               # app key of the process ("zen", "discord")
    host: str | None       # page host, or None for an app window without a page
    hwnd: int
    exe: str               # the process's executable (matched to a microphone holder)
    at: float              # wall clock of the capture
    title: str
    controls: tuple[str, ...]
    digest: str


@dataclass
class Verdict:
    in_call: bool
    service: str = ""
    #: Other people in the call with the user, as the screen shows (0: alone in it), or None when it does not say.
    others: int | None = None
    #: The window could not say (half drawn, caught mid alt-tab): neither a call nor no call.
    unclear: bool = False

    @property
    def state(self) -> str:
        return UNCLEAR if self.unclear else IN_CALL if self.in_call else NOT_IN_CALL


@dataclass
class Life:
    """What the screen says about a call that is going on, for :class:`yuki.memory.calls.CallLife` (measured,
    content-free): the newest in-call frame of the call's site and the reliable "not in a call" sights after it."""

    #: Capture time of the site's newest in-call frame (renewed whenever the same screen is seen again); None:
    #: no in-call frame of this app at all.
    positive_at: float | None = None
    #: Capture times of the reliable not-in-call sights of that site since it, oldest first.
    negatives: tuple[float, ...] = ()
    host: str | None = None
    #: Control count of the site's latest frame, and the median of its recent in-call frames (for the log).
    last_controls: int | None = None
    median_controls: float | None = None
    #: The newest positive is a sighting of a calling site whose page is never read (the module docstring);
    #: ``negatives`` are then the reliable not-in-call sights of readable pages of the same site since it.
    site: bool = False
    #: For a site call: capture time of the app's newest frame when that is another page, judged reliably not in
    #: a call, captured after the sighting (the browser shows something else now); None otherwise.
    away_at: float | None = None


def _digest(title: str, host: str | None, controls: Iterable[str]) -> str:
    raw = json.dumps([title, host or "", list(controls)], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()


class CallEvidence:
    """The screens of the user's apps, judged for a joined call (see the module docstring). Thread-safe.

    Args:
        judge_fn: ``(screen) -> Verdict``; default: the model (Haiku, forced tool). Tests pass a stub.
        settings: :class:`yuki.config.Settings` for the model client and costs (default ``Settings()``).
        log: ``log(type, **fields)`` (content-free), or None.
        fresh_s: How long an in-call screen stays fresh evidence (``[calls] evidence_fresh_s``).
        client: A model client (tests); default ``yuki.agent.failover.make_client(settings)``.
        sites_fn: ``(entries) -> {entry: (calling, service)}`` for the shipped list of blocked sites (see the
            module docstring); default: the model (Haiku, forced tool ``judge_sites``, the list only). Tests pass a
            stub.
        privacy: The service's :class:`yuki.memory.privacy.PrivacyConfig` (its ``[meetings]`` table: the user's
            ``always_sites``, ``never_sites`` and ``names``); None: the packaged defaults.
        entries: The shipped list of blocked sites (tests); default :func:`shipped_site_entries`.
        state: Where the shipped list's verdicts are kept across restarts (an object with ``nudge_state(name)`` and
            ``set_nudge_state(name, value)``: the store); None: in RAM only.
    """

    def __init__(self, *, judge_fn: Callable[[Screen], Verdict] | None = None, settings: Any = None,
                 log: Callable[..., Any] | None = None, fresh_s: float = FRESH_S, client: Any = None,
                 model: str | None = None, rejudge_s: float = REJUDGE_S,
                 sites_fn: Callable[[tuple[str, ...]], dict[str, tuple[bool, str]]] | None = None,
                 privacy: Any = None, entries: Iterable[str] | None = None, state: Any = None) -> None:
        self._judge_fn = judge_fn
        self._state = state
        #: One judgement of the shipped list at a time (the call worker's pass, or calling_service's thread).
        self._entries_lock = threading.Lock()
        self._entries_thread: threading.Thread | None = None
        self._kept_checked = False
        self._sites_fn = sites_fn
        self._privacy = privacy
        self._entries = tuple(sorted({str(e).strip().casefold() for e in entries if str(e).strip()})) \
            if entries is not None else None
        #: The privacy file's [meetings] table (built on first use), and the value the site verdicts were made with.
        self._meetings: Any = None
        self._meetings_used: Any = None
        #: The shipped list's verdicts per entry (None: not judged yet), and when judging it last failed (monotonic).
        self._entry_verdicts: dict[str, tuple[bool, str]] | None = None
        self._entries_failed_at = float("-inf")
        #: Sightings of blocked pages per (app key, host): the last capture time (the host only, never content; in
        #: this process only).
        self._sites: dict[tuple[str, str], float] = {}
        #: Sites whose host has no verdict yet, per (app key, host).
        self._site_pending: set[tuple[str, str]] = set()
        #: Per host: ``(a calling site, its product name)``.
        self._site_verdicts: OrderedDict[str, tuple[bool, str]] = OrderedDict()
        self._settings = settings
        self._log_fn = log
        self.fresh_s = float(fresh_s)
        self._client = client
        self._model = model
        self._lock = threading.Lock()
        #: The latest screen per (app key, host).
        self._screens: dict[tuple[str, str | None], Screen] = {}
        #: Screens whose content has no verdict applied yet, per (app key, host).
        self._pending: set[tuple[str, str | None]] = set()
        #: The digest of the screen each (app key, host) last had a verdict applied to ...
        self._applied: dict[tuple[str, str | None], str] = {}
        #: ... and what that verdict counted as: "pos" (in a call), "neg" (a reliable not-in-a-call) or "none"
        #: (unclear, or an unhealthy frame: neither).
        self._kind: dict[tuple[str, str | None], str] = {}
        #: In-call evidence per (app key, host): ``(last seen at, service, others)`` (others: see :class:`Verdict`).
        #: Kept after a reliable negative (a call goes on through one); :attr:`_negs` says whether it is contradicted.
        self._evidence: dict[tuple[str, str | None], tuple[float, str, int | None]] = {}
        #: Capture times of the reliable not-in-call sights of each site since its last in-call frame.
        self._negs: dict[tuple[str, str | None], list[float]] = {}
        #: Control counts of each site's latest in-call frames (the measured health line).
        self._pos_counts: dict[tuple[str, str | None], deque[int]] = {}
        #: The executable each app key was last seen running as (to match a microphone holder by path or package).
        self._exes: dict[str, str] = {}
        self._verdicts: OrderedDict[str, Verdict] = OrderedDict()
        self.rejudge_s = float(rejudge_s)
        #: When the model last judged a screen of each (app key, host) (monotonic).
        self._judged_at: dict[tuple[str, str | None], float] = {}
        self.counters = {"seen": 0, "judged": 0, "cached": 0, "errors": 0, "sites_seen": 0, "sites_judged": 0}
        self.cost_usd = 0.0

    # -- input (the watcher's worker thread) -------------------------------------------------

    def seen(self, screen: dict[str, Any]) -> None:
        """A window was extracted: keep it as its app's (and site's) latest screen. Cheap; never raises.

        ``blocked`` set (a privacy reason): a page the privacy rules keep unread; only its app, host and time
        are kept (a site sighting, see the module docstring), whatever else the dict carries.
        """
        try:
            key = str(screen.get("process") or "").strip().casefold()
            if not key:
                return
            host = (str(screen.get("host") or "").strip().casefold() or None)
            if screen.get("blocked"):
                if host:
                    self._seen_site(key, host, str(screen.get("exe") or ""), float(screen.get("at") or time.time()))
                return
            controls = tuple(str(c) for c in (screen.get("controls") or [])[:MAX_CONTROLS] if str(c).strip())
            title = str(screen.get("title") or "")
            shot = Screen(key=key, host=host, hwnd=int(screen.get("hwnd") or 0), exe=str(screen.get("exe") or ""),
                          at=float(screen.get("at") or time.time()), title=title, controls=controls,
                          digest=_digest(title, host, controls))
        except Exception:  # noqa: BLE001 - a hint only
            return
        where = (shot.key, shot.host)
        with self._lock:
            self.counters["seen"] += 1
            self._screens[where] = shot
            if shot.exe:
                self._exes[shot.key] = shot.exe
            if self._applied.get(where) == shot.digest:
                # The same screen again: its verdict stands; in-call evidence is renewed, and a reliable
                # "not in a call" is counted once more (a "You left the meeting" page left in front).
                kind = self._kind.get(where)
                if kind == "pos" and where in self._evidence:
                    self._evidence[where] = (shot.at, *self._evidence[where][1:])
                elif kind == "neg":
                    self._add_negative(where, shot.at)
                return
            self._pending.add(where)
            if len(self._screens) > 512:   # bounded: forget the oldest screens
                for old in sorted(self._screens, key=lambda w: self._screens[w].at)[:256]:
                    self._screens.pop(old, None)
                    self._pending.discard(old)

    def _seen_site(self, key: str, host: str, exe: str, at: float) -> None:
        """A sighting of a page the privacy rules keep unread: its app, host and time only."""
        where = (key, host)
        with self._lock:
            self.counters["sites_seen"] += 1
            if exe:
                self._exes[key] = exe
            if at > self._sites.get(where, float("-inf")):
                self._sites[where] = at
            if host not in self._site_verdicts:
                self._site_pending.add(where)
            if len(self._sites) > MAX_SITES:   # bounded: forget the oldest sightings
                for old in sorted(self._sites, key=self._sites.__getitem__)[: MAX_SITES // 2]:
                    self._sites.pop(old, None)
                    self._site_pending.discard(old)

    def _site_hits(self, holder: Any) -> list[tuple[float, str, str, tuple[float, ...]]]:
        """``(last sighting, host, service, negatives)`` of the calling sites an app showed (under the lock).

        ``negatives``: the reliable not-in-call sights of readable pages of the same app and site captured after
        the sighting (no in-call frame of it since; review F2): such a hit can no longer start a call, and a call
        it started ends on a persistent run of them, like a screen's."""
        out = []
        for (k, h), at in self._sites.items():
            verdict = self._site_verdicts.get(h)
            if verdict is not None and verdict[0] and self._matches(holder, k):
                out.append((at, h, verdict[1], tuple(t for t in self._negs.get((k, h), ()) if t > at)))
        return out

    def _away_at(self, holder: Any, site_at: float, site_host: str) -> float | None:
        """Capture time of the app's newest frame when it is a readable page of another site, judged reliably not
        in a call (its verdict applied to that very screen), captured after the site sighting; else None
        (under the lock). A newer sighting of any unread page is not "away": nothing is known of it."""
        shots = [s for w, s in self._screens.items() if self._matches(holder, w[0])]
        if not shots:
            return None
        newest = max(shots, key=lambda s: s.at)
        where = (newest.key, newest.host)
        unread = [at for (k, _h), at in self._sites.items() if self._matches(holder, k)]
        if (newest.host == site_host or newest.at <= site_at or (unread and max(unread) > newest.at)
                or self._applied.get(where) != newest.digest or self._kind.get(where) != "neg"):
            return None
        negs = self._negs.get(where)
        return max(newest.at, negs[-1]) if negs else newest.at

    def _add_negative(self, where: tuple[str, str | None], at: float) -> None:
        """One more reliable not-in-call sight of a site (under the lock); only a newer one counts."""
        negs = self._negs.setdefault(where, [])
        if negs and at <= negs[-1]:
            return
        negs.append(at)
        if len(negs) > MAX_NEGATIVES:
            del negs[: len(negs) - MAX_NEGATIVES]

    def _live(self, where: tuple[str, str | None]) -> bool:
        """The site has in-call evidence that no reliable negative followed (under the lock)."""
        return where in self._evidence and not self._negs.get(where)

    def _median(self, where: tuple[str, str | None]) -> float | None:
        counts = self._pos_counts.get(where)
        return float(statistics.median(counts)) if counts else None

    def _apply(self, where: tuple[str, str | None], shot: Screen, verdict: Verdict) -> tuple[str, bool, float | None]:
        """Count a verdict for a site's screen (under the lock): ``(kind, healthy, median)``.

        A frame with fewer controls than :data:`HEALTH_RATIO` of the site's recent in-call median is not
        healthy (half drawn): like an ``unclear`` verdict it counts as neither a call nor no call. Before the
        site's first in-call frame there is no median and every frame counts.
        """
        n = len(shot.controls)
        median = self._median(where)
        healthy = median is None or n >= HEALTH_RATIO * median
        self._applied[where] = shot.digest
        if verdict.unclear or not healthy:
            kind = "none"
        elif verdict.in_call:
            kind = "pos"
            self._evidence[where] = (shot.at, verdict.service or "", verdict.others)
            self._negs.pop(where, None)
            self._pos_counts.setdefault(where, deque(maxlen=HEALTH_WINDOW)).append(n)
        else:
            kind = "neg"
            self._add_negative(where, shot.at)
        self._kind[where] = kind
        return kind, healthy, median

    # -- judging (the call worker's thread) ----------------------------------------------------

    def _matches(self, holder: Any, key: str) -> bool:
        """Whether screen app ``key`` is ``holder`` (an app key string, or a :class:`~yuki.memory.avsignals.Holder`:
        same key, same executable, or a packaged app whose install folder is its package family's)."""
        if isinstance(holder, str):
            return holder.casefold() == key
        if getattr(holder, "key", None) == key:
            return True
        exe = self._exes.get(key, "")
        try:
            parts = tuple(part.casefold() for part in PureWindowsPath(exe).parts) if exe else ()
            return bool(holder.matches(process_key=key, exe=exe, ids=parts))
        except Exception:  # noqa: BLE001
            return False

    def judge(self, holders: Iterable[Any]) -> int:
        """Judge the unjudged latest screens of these apps (the microphone holders now: keys or ``Holder``).

        Returns how many verdicts were applied. A verdict already known for the same content costs nothing.
        """
        holders = [h for h in holders if h]
        if not holders:
            return 0
        with self._lock:
            todo = [self._screens[w] for w in list(self._pending)
                    if w in self._screens and any(self._matches(h, w[0]) for h in holders)]
        applied = 0
        for shot in todo:
            where = (shot.key, shot.host)
            with self._lock:
                cached = shot.digest in self._verdicts
                # A site in a call judged moments ago keeps its evidence while its screen changes (a busy chat):
                # the newest screen waits for the next pass (a cost guard). Joining is never delayed (no live
                # evidence yet). Alone in the call at the last verdict: a change is judged again sooner.
                live = self._live(where)
                wait_s = ALONE_REJUDGE_S if live and self._evidence[where][2] == 0 else self.rejudge_s
                recent = live and time.monotonic() - self._judged_at.get(where, -1e9) < wait_s
            if recent and not cached:
                continue
            got = self._verdict(shot)
            if got is None:
                continue
            verdict, was_cached, ms, cost = got
            kind, healthy, median = "", True, None
            with self._lock:
                latest = self._screens.get(where)
                superseded = latest is not None and latest.digest != shot.digest
                if not superseded:   # else a newer screen arrived meanwhile: it is judged next time
                    self._pending.discard(where)
                    kind, healthy, median = self._apply(where, shot, verdict)
            fields = dict(app=shot.key, page=shot.host is not None, state=verdict.state, others=verdict.others,
                          controls=len(shot.controls), healthy=healthy,
                          median=None if median is None else round(median, 1), counted=kind or None)
            if was_cached:
                if not superseded:
                    self._log("call_evidence_applied", cached=True, **fields)
            else:
                self._log("call_evidence_judged", applied=not superseded, ms=ms, cost_usd=round(cost, 6), **fields)
            if not superseded:
                applied += 1
        return applied + self._judge_sites(holders)

    def _meeting_rules(self) -> Any:
        """The privacy file's ``[meetings]`` table (:class:`yuki.memory.timeline.MeetingRules`: the user's
        ``always_sites``, ``never_sites``, ``names``), re-read when the file changes; None when unreadable."""
        try:
            if self._meetings is None:
                from yuki.memory.timeline import MeetingRules, PrivacySection

                self._meetings = PrivacySection(self._privacy, "meetings", MeetingRules.from_dict)
            return self._meetings.get()
        except Exception:  # noqa: BLE001 - then only the shipped list counts
            return None

    def _entry_of(self, host: str) -> str | None:
        """The most specific entry of the shipped list of blocked sites that ``host`` falls under, or None."""
        entries = self._entries if self._entries is not None else shipped_site_entries()
        found = [e for e in entries if _host_matches(host, e)]
        return max(found, key=len) if found else None

    def _judge_sites(self, holders: list[Any]) -> int:
        """Decide, for the sites of unread pages these apps showed (the microphone holders now), whether each is a
        calling site (the module docstring): by the user's ``[meetings]`` word, else by the verdict on the
        shipped list's entry it falls under (that list judged once, as a whole, never per visit); a site the user
        blocked themselves never counts. One verdict per host, kept. Returns how many hosts were decided."""
        with self._lock:
            if not self._sites:
                return 0   # no unread page seen: nothing to read, not even the privacy file's stamp
        meetings = self._meeting_rules()
        with self._lock:
            if meetings is not self._meetings_used:   # the user changed [meetings]: decide every site again
                self._meetings_used = meetings
                self._site_verdicts.clear()
                self._site_pending |= set(self._sites)
            todo = sorted({w[1] for w in self._site_pending
                           if w[1] not in self._site_verdicts and any(self._matches(h, w[0]) for h in holders)})
        decided = 0
        for host in todo:
            calling, service, by = False, "", "unlisted"
            if meetings is not None and meetings.never("", host):
                by = "never_sites"
            elif meetings is not None and meetings.match_url(f"https://{host}/"):
                calling, service, by = True, meetings.match_url(f"https://{host}/"), "always_sites"
            else:
                entry = self._entry_of(host)
                if entry is not None:
                    verdicts = self._entry_verdicts if self._entry_verdicts is not None else self._judge_entries()
                    if verdicts is None:
                        break   # the list could not be judged now: these sites wait for a later pass
                    calling, service = verdicts.get(entry, (False, ""))
                    by = "shipped_list"
                    if calling and meetings is not None:
                        service = meetings.name_for(host, meetings.name_for(entry, service or entry))
            service = " ".join(str(service or "").split())[:40] if calling else ""
            with self._lock:
                self._site_verdicts[host] = (calling, service)
                while len(self._site_verdicts) > SITE_CACHE:
                    self._site_verdicts.popitem(last=False)
                apps = sorted({w[0] for w in self._site_pending if w[1] == host})
                self._site_pending = {w for w in self._site_pending if w[1] != host}
                self.counters["sites_judged"] += 1
            decided += 1
            self._log("call_evidence_site", apps=apps, calling=calling, service=service or None, by=by)
        return decided

    def _judge_entries(self) -> dict[str, tuple[bool, str]] | None:
        """Judge the whole shipped list of blocked sites once (``sites_fn`` or the model); None when that failed
        (tried again after :data:`SITES_RETRY_S`). Never names a page the user visited."""
        with self._entries_lock:
            if self._entry_verdicts is not None:
                return self._entry_verdicts
            entries = self._entries if self._entries is not None else shipped_site_entries()
            kept = self._kept_verdicts(entries)
            if kept is not None:
                with self._lock:
                    self._entry_verdicts = kept
                self._log("call_evidence_sites", entries=len(entries), calling=sum(1 for c, _s in kept.values() if c),
                          services=sorted({sv for c, sv in kept.values() if c and sv}), ms=0, cost_usd=0.0, kept=True)
                return kept
            if time.monotonic() - self._entries_failed_at < SITES_RETRY_S:
                return None
            return self._judge_entries_now(entries)

    def _judge_entries_now(self, entries: tuple[str, ...]) -> dict[str, tuple[bool, str]] | None:
        """Judge the shipped list (under ``_entries_lock``), keep the verdicts; None when that failed."""
        started = time.perf_counter()
        cost = 0.0
        try:
            if self._sites_fn is not None:
                got = self._sites_fn(entries)
            else:
                got, cost = self._model_sites(entries)
        except Exception as exc:  # noqa: BLE001
            self._entries_failed_at = time.monotonic()
            with self._lock:
                self.counters["errors"] += 1
            self._log("call_evidence_error", where="sites", error=f"{type(exc).__name__}: {str(exc)[:200]}")
            return None
        verdicts = {}
        for entry in entries:
            calling, service = (got or {}).get(entry, (False, ""))
            calling = calling is True
            verdicts[entry] = (calling, " ".join(str(service or "").split())[:40] if calling else "")
        with self._lock:
            self._entry_verdicts = verdicts
            self.cost_usd += cost
        self._keep_verdicts(entries, verdicts)
        services = sorted({s for c, s in verdicts.values() if c and s})
        self._log("call_evidence_sites", entries=len(entries), calling=sum(1 for c, _s in verdicts.values() if c),
                  services=services, ms=round((time.perf_counter() - started) * 1000), cost_usd=round(cost, 6))
        return verdicts

    @staticmethod
    def _list_digest(entries: tuple[str, ...]) -> str:
        """A digest of the shipped list (public data): its verdicts are reused while it is unchanged."""
        return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()

    def _kept_verdicts(self, entries: tuple[str, ...]) -> dict[str, tuple[bool, str]] | None:
        """The verdicts kept in ``nudge_state['call_evidence:sites']`` for this very list, or None."""
        if self._state is None:
            return None
        try:
            raw = self._state.nudge_state(SITES_STATE_KEY)
            data = json.loads(raw) if raw else None
        except Exception:  # noqa: BLE001 - unreadable: judged again
            return None
        if not isinstance(data, dict) or data.get("list") != self._list_digest(entries):
            return None
        got = data.get("verdicts") or {}
        out = {}
        for entry in entries:
            item = got.get(entry)
            calling = bool(item[0]) if isinstance(item, list) and item else False
            service = str(item[1]) if isinstance(item, list) and len(item) > 1 and calling else ""
            out[entry] = (calling, " ".join(service.split())[:40])
        return out

    def _keep_verdicts(self, entries: tuple[str, ...], verdicts: dict[str, tuple[bool, str]]) -> None:
        if self._state is None:
            return
        try:
            self._state.set_nudge_state(SITES_STATE_KEY, json.dumps(
                {"list": self._list_digest(entries), "at": round(time.time()),
                 "verdicts": {e: [bool(c), sv] for e, (c, sv) in verdicts.items()}}, separators=(",", ":")))
        except Exception:  # noqa: BLE001 - kept in RAM only then
            pass

    def _judge_entries_soon(self) -> None:
        """Judge the shipped list once on a short-lived thread (never on the caller's)."""
        with self._lock:
            running = self._entries_thread is not None and self._entries_thread.is_alive()
            if (running or self._entry_verdicts is not None
                    or time.monotonic() - self._entries_failed_at < SITES_RETRY_S):
                return   # judging now, judged, or failed moments ago (tried again after SITES_RETRY_S)
            self._entries_thread = threading.Thread(target=self._judge_entries, name="yuki-memory-sites-judge",
                                                    daemon=True)
            thread = self._entries_thread
        thread.start()

    def calling_service(self, host: str | None, *, judge: bool = True) -> str | None:
        """The calling service a blocked page's ``host`` is (its product name, e.g. "Google Meet"), or None: not a
        calling site, a site the user blocked themselves, or not known yet. The same decision as the microphone
        path (the user's ``[meetings]`` word, else the shipped list's verdict), with no holder needed. Never blocks:
        a list not judged yet is judged on a short-lived thread (``judge``), and None is the answer until then."""
        host = str(host or "").strip().casefold()
        if not host:
            return None
        meetings = self._meeting_rules()
        if meetings is not None and meetings.never("", host):
            return None
        if meetings is not None and meetings.match_url(f"https://{host}/"):
            return " ".join(str(meetings.match_url(f"https://{host}/") or host).split())[:40]
        entry = self._entry_of(host)
        if entry is None:
            return None
        with self._lock:
            verdicts = self._entry_verdicts
        if verdicts is None:
            kept = None
            if not self._kept_checked:   # the kept verdicts are read once per process (only this one writes them)
                self._kept_checked = True
                kept = self._kept_verdicts(self._entries if self._entries is not None else shipped_site_entries())
            if kept is not None:
                with self._lock:
                    self._entry_verdicts = verdicts = kept
            elif judge:
                self._judge_entries_soon()
        if verdicts is None:
            return None
        calling, service = verdicts.get(entry, (False, ""))
        if not calling:
            return None
        if meetings is not None:
            service = meetings.name_for(host, meetings.name_for(entry, service or entry))
        return " ".join(str(service or entry).split())[:40]

    def _model_sites(self, entries: tuple[str, ...]) -> tuple[dict[str, tuple[bool, str]], float]:
        """``({entry: (calling, service)}, cost)`` for the shipped list, from the model (the list only)."""
        from yuki.log.requests import usage_tokens

        if not entries:
            return {}, 0.0
        settings, model = self._model_client()
        response = self._client.messages.create(
            model=model, max_tokens=60 + 40 * len(entries), system=SITES_PROMPT, tools=[SITES_TOOL],
            tool_choice={"type": "tool", "name": SITES_TOOL["name"]},
            messages=[{"role": "user", "content": "sites:\n" + "\n".join(entries)}])
        block = next((b for b in response.content if getattr(b, "type", None) == "tool_use"), None)
        data = getattr(block, "input", None) or {}
        out: dict[str, tuple[bool, str]] = {}
        for item in data.get("sites") or []:
            if isinstance(item, dict):
                host = str(item.get("host") or "").strip().casefold()
                if host in entries:
                    out[host] = (item.get("calling") is True, str(item.get("service") or ""))
        try:
            cost = float(settings.estimate_cost(model, usage_tokens(response.usage)) or 0.0)
        except Exception:  # noqa: BLE001
            cost = 0.0
        return out, cost

    def _model_client(self) -> tuple[Any, str]:
        """``(settings, model)``, creating the settings and the client once."""
        from yuki.memory.journal import HAIKU_MODEL

        settings = self._settings
        if settings is None:
            from yuki.config import Settings

            settings = self._settings = Settings()
        if self._client is None:
            from yuki.agent.failover import make_client

            self._client = make_client(settings)
        return settings, self._model or HAIKU_MODEL

    def _verdict(self, shot: Screen) -> tuple[Verdict, bool, int, float] | None:
        """``(verdict, from the cache, ms, cost)`` for a screen, or None when the model failed (judged again on
        the next pass)."""
        with self._lock:
            known = self._verdicts.get(shot.digest)
            if known is not None:
                self._verdicts.move_to_end(shot.digest)
                self.counters["cached"] += 1
                return known, True, 0, 0.0
        started = time.perf_counter()
        cost = 0.0
        try:
            if self._judge_fn is not None:
                verdict = self._judge_fn(shot)
            else:
                verdict, cost = self._model_verdict(shot)
        except Exception as exc:  # noqa: BLE001 - judged again on the next pass
            with self._lock:
                self.counters["errors"] += 1
            self._log("call_evidence_error", app=shot.key, error=f"{type(exc).__name__}: {str(exc)[:200]}")
            return None
        ms = round((time.perf_counter() - started) * 1000)
        with self._lock:
            self._judged_at[(shot.key, shot.host)] = time.monotonic()
            self.counters["judged"] += 1
            self.cost_usd += cost
            self._verdicts[shot.digest] = verdict
            while len(self._verdicts) > VERDICT_CACHE:
                self._verdicts.popitem(last=False)
        return verdict, False, ms, cost

    def _model_verdict(self, shot: Screen) -> tuple[Verdict, float]:
        from yuki.log.requests import usage_tokens

        settings, model = self._model_client()
        lines = [f"title: {json.dumps(shot.title, ensure_ascii=False)}",
                 f"host: {shot.host or '(an app window, no web page)'}",
                 "controls, in on-screen order: " + " / ".join(json.dumps(c, ensure_ascii=False)
                                                               for c in shot.controls) if shot.controls
                 else "controls: (none visible)"]
        response = self._client.messages.create(
            model=model, max_tokens=120, system=JUDGE_PROMPT, tools=[JUDGE_TOOL],
            tool_choice={"type": "tool", "name": JUDGE_TOOL["name"]},
            messages=[{"role": "user", "content": "\n".join(lines)}])
        block = next((b for b in response.content if getattr(b, "type", None) == "tool_use"), None)
        data = getattr(block, "input", None) or {}
        state = str(data.get("state") or "").strip()
        if state not in (IN_CALL, NOT_IN_CALL, UNCLEAR):   # an answer in the old shape (a boolean) still reads
            state = IN_CALL if data.get("in_call") is True else NOT_IN_CALL if data.get("in_call") is False else UNCLEAR
        in_call = state == IN_CALL
        try:
            others = int(data.get("others")) if in_call and data.get("others") is not None else None
        except (TypeError, ValueError):
            others = None
        verdict = Verdict(in_call=in_call,
                          service=" ".join(str(data.get("service") or "").split())[:40] if in_call else "",
                          others=others if others is not None and others >= 0 else None,
                          unclear=state == UNCLEAR)
        try:
            cost = float(settings.estimate_cost(model, usage_tokens(response.usage)) or 0.0)
        except Exception:  # noqa: BLE001
            cost = 0.0
        return verdict, cost

    # -- output ------------------------------------------------------------------------------

    def state(self, holder: Any, mic_since: float, now: float | None = None) -> tuple[str | None, str]:
        """``(evidence, service)`` for an app (its key, or its ``Holder``) and its microphone hold that began at
        ``mic_since``: may this hold **start** a call?

        ``evidence`` is ``"fresh"``, ``"stale"`` or ``None`` (see the module docstring): only a site whose
        newest counted frame was in a call, captured during this hold, starts one. ``service`` is the call's
        product name from the verdict ("" when unknown). A call already going on is kept by
        :meth:`life` (no "newer than the hold" test: a re-grab of the microphone is the same call).
        A sighting of a calling site whose page is never read, during this hold, counts the same way
        (``service`` from the site verdict, else its host).
        """
        if not holder:
            return None, ""
        now = time.time() if now is None else float(now)
        floor = float(mic_since) - HOLD_TOLERANCE_S
        with self._lock:
            found = [(at, service) for (k, h), (at, service, _o) in self._evidence.items()
                     if self._matches(holder, k) and self._live((k, h)) and at >= floor]
            found += [(at, service or host) for at, host, service, negs in self._site_hits(holder)
                      if at >= floor and not negs]
        if not found:
            return None, ""
        at, service = max(found)
        return ("fresh" if now - at <= self.fresh_s else "stale"), service

    def life(self, holder: Any) -> Life:
        """What the screen says about an app's call that is going on (key or ``Holder``): the newest in-call
        frame of its site and the reliable not-in-call sights since (:class:`Life`; empty without one). When the
        newest positive is a sighting of a calling site whose page is never read, :attr:`Life.site` is set, with
        the readable negatives of the same site since it and :attr:`Life.away_at`."""
        if not holder:
            return Life()
        with self._lock:
            sites = [(at, w) for w, (at, _s, _o) in self._evidence.items() if self._matches(holder, w[0])]
            blocked = self._site_hits(holder)
            newest_site = max(blocked, key=lambda b: b[0]) if blocked else None
            if newest_site is not None and (not sites or newest_site[0] > max(s[0] for s in sites)):
                site_at, site_host, _service, negs = newest_site
                return Life(positive_at=site_at, negatives=negs, host=site_host, site=True,
                            away_at=self._away_at(holder, site_at, site_host))
            if not sites:
                return Life()
            at, where = max(sites, key=lambda s: s[0])
            shot = self._screens.get(where)
            return Life(positive_at=at, negatives=tuple(t for t in self._negs.get(where, ()) if t > at),
                        host=where[1], last_controls=len(shot.controls) if shot is not None else None,
                        median_controls=self._median(where))

    def front(self) -> tuple[str | None, float | None]:
        """``(app key, capture time)`` of the newest window the watcher handed over (the one in front, as last
        read), or ``(None, None)``; content-free, for the call log."""
        with self._lock:
            seen = [(s.at, s.key) for s in self._screens.values()] + [(at, w[0]) for w, at in self._sites.items()]
            if not seen:
                return None, None
            at, key = max(seen)
            return key, at

    def host_of(self, holder: Any) -> str | None:
        """The site of the newest live in-call evidence of an app (key or ``Holder``; None: an app window, or
        none)."""
        if not holder:
            return None
        with self._lock:
            found = [(at, h or "") for (k, h), (at, _s, _o) in self._evidence.items()
                     if self._matches(holder, k) and self._live((k, h))]
            found += [(at, host) for at, host, _service, negs in self._site_hits(holder) if not negs]
        return (max(found)[1] or None) if found else None

    def others_of(self, holder: Any) -> int | None:
        """How many other people the newest live in-call screen of an app (key or ``Holder``) showed with the
        user (0: alone in the call), or None when there is none or it did not say (v3.15)."""
        if not holder:
            return None
        with self._lock:
            found = [(at, others) for (k, h), (at, _s, others) in self._evidence.items()
                     if self._matches(holder, k) and self._live((k, h))]
        return max(found, key=lambda f: f[0])[1] if found else None

    def forget(self, key: str) -> None:
        """Drop an app's in-call evidence (a new hold then needs a new screen to start a call)."""
        with self._lock:
            for where in [w for w in self._evidence if w[0] == key.casefold()]:
                self._evidence.pop(where, None)
                self._negs.pop(where, None)
            for where in [w for w in self._sites if w[0] == key.casefold()]:
                self._sites.pop(where, None)
                self._site_pending.discard(where)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {**self.counters, "screens": len(self._screens), "evidence": len(self._evidence),
                    "sites": len(self._sites), "calling_sites": sum(1 for v in self._site_verdicts.values() if v[0]),
                    "contradicted": sum(1 for w in self._evidence if self._negs.get(w)),
                    "cost_usd": round(self.cost_usd, 5)}

    def _log(self, type: str, **fields: Any) -> None:
        if self._log_fn is not None:
            try:
                self._log_fn(type, **fields)
            except Exception:  # noqa: BLE001
                pass


class SiteTabs:
    """The tab of a calling site (the Meet tab in Zen) found by identity, never by title (PLAN.md D8, F5).

    :meth:`seen` is the watcher's ``on_screen`` hook (beside :meth:`CallEvidence.seen`, on the watcher's worker
    thread): for a sighting of a blocked page whose host is a calling service (:meth:`CallEvidence.calling_service`),
    at most once per :data:`TAB_READ_GAP_S` per window and service, the window's selected tab is read as identities
    (``reader``: :func:`yuki.memory.extract.uia.tab_identities`, every group of the strip, at most 0.2 s), learned
    into ``nudge_state['site_tabs:<service>']`` (:func:`yuki.perception.tabs.learn_identity`) and put on the Now
    board (``calling_tabs``). Non-calling blocked hosts (a bank) produce nothing. A strip that reads empty logs why
    (``tab_strip`` with its reason code), so the user's first live check can say whether Zen's strip is readable.

    :meth:`census` finds the tab again in every window of the browser (an offer is due, "open my meeting"):
    candidates in identity order, ``one`` / ``ask`` (two equal) / ``none``, logged ``tab_census`` content-free.

    Args:
        evidence: the service's :class:`CallEvidence` (its ``calling_service``).
        store: the memory store (``cipher`` for the keyed name digests, ``nudge_state`` for the identities).
        board: the :class:`yuki.memory.now.NowBoard` (``set_calling_tab``), or None.
        log: ``log(type, **fields)``, content-free.
        reader: ``reader(hwnd, digest_fn=...) -> dict`` (tests pass a stub); default ``tab_identities``.
        census_fn: ``census_fn(windows, identity, digest_fn=...) -> dict``; default ``tab_census``.
    """

    def __init__(self, evidence: Any, *, store: Any = None, board: Any = None, log: Callable[..., Any] | None = None,
                 reader: Callable[..., dict[str, Any]] | None = None,
                 census_fn: Callable[..., dict[str, Any]] | None = None, gap_s: float = TAB_READ_GAP_S,
                 clock: Callable[[], float] = time.time) -> None:
        self._evidence = evidence
        self._store = store
        self._board = board
        self._log_fn = log
        self._reader = reader
        self._census_fn = census_fn
        self.gap_s = float(gap_s)
        self._clock = clock
        self._lock = threading.Lock()
        #: When each (hwnd, service) was last read, and whether its selected tab was identified then.
        self._read_at: dict[tuple[int, str], float] = {}
        self._known: dict[int, bool] = {}
        self.counters = {"sightings": 0, "calling": 0, "reads": 0, "identified": 0, "empty": 0, "census": 0}

    @staticmethod
    def state_key(service: str) -> str:
        return f"site_tabs:{service}"

    def digest(self, process: str, name: str) -> str:
        """The keyed digest of a tab's name (``FieldCipher.digest("tab", process, name)``); ``""`` without a key."""
        cipher = getattr(self._store, "cipher", None)
        if cipher is None or not name:
            return ""
        return cipher.digest("tab", str(process or "").casefold(), " ".join(str(name).split()))

    def identities(self, service: str) -> dict[str, Any]:
        """What was learned of ``service``'s tab (``{"process", "runtime_ids", "names", "positions"}``)."""
        try:
            raw = self._store.nudge_state(self.state_key(service)) if self._store is not None else None
            got = json.loads(raw) if raw else {}
            return got if isinstance(got, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def known(self, hwnd: int) -> bool:
        """Whether the last read of this window's calling-site tab identified it (``site_front``'s ``tab_known``)."""
        with self._lock:
            return bool(self._known.get(int(hwnd)))

    def seen(self, screen: dict[str, Any]) -> None:
        """The watcher's ``on_screen`` hook. Cheap unless a read is due; never raises."""
        try:
            if not screen.get("blocked"):
                return
            self.counters["sightings"] += 1
            host = str(screen.get("host") or "").strip().casefold()
            service = self._evidence.calling_service(host) if host and self._evidence is not None else None
            if not service:
                return
            self.counters["calling"] += 1
            hwnd = int(screen.get("hwnd") or 0)
            process = str(screen.get("process") or "").strip().casefold()
            now = float(screen.get("at") or self._clock())
            with self._lock:
                last = self._read_at.get((hwnd, service))
                if last is not None and now - last < self.gap_s:
                    due = False
                else:
                    due = True
                    self._read_at[(hwnd, service)] = now
                    if len(self._read_at) > 64:
                        for old in sorted(self._read_at, key=self._read_at.__getitem__)[:32]:
                            self._read_at.pop(old, None)
            if due:
                self._read(hwnd, process, service, now)
        except Exception as exc:  # noqa: BLE001 - the Meet tab never breaks capture
            self._log("tab_strip", status="error", error=type(exc).__name__)

    def _read(self, hwnd: int, process: str, service: str, now: float) -> None:
        reader = self._reader
        if reader is None:
            from yuki.memory.extract.uia import tab_identities as reader
        self.counters["reads"] += 1
        result = reader(hwnd, digest_fn=lambda name: self.digest(process, name))
        seen = result.get("sighting")
        identified = bool(seen)
        with self._lock:
            self._known[hwnd] = identified
        if identified:
            self.counters["identified"] += 1
            stored = self.identities(service)
            from yuki.perception.tabs import learn_identity

            ident = learn_identity(stored, seen)
            ident["process"] = process
            try:
                if self._store is not None:
                    self._store.set_nudge_state(self.state_key(service), json.dumps(ident, separators=(",", ":")))
            except Exception:  # noqa: BLE001
                pass
        else:
            self.counters["empty"] += 1
        if self._board is not None:
            try:
                info = {"process": process, "service": service, "seen_at": now, "known": identified}
                if identified:
                    pos = seen.get("position") or {}
                    info.update(tab_id=seen.get("tab_id") or "", name_digest=seen.get("digest") or "",
                                group=pos.get("group"), index=pos.get("index"))
                self._board.set_calling_tab(hwnd, info)
            except Exception:  # noqa: BLE001
                pass
        self._log("tab_strip", app=process, service=service, tabs=result.get("tabs"),
                  groups=list(result.get("groups") or [])[:12], nodes=result.get("nodes"),
                  complete=result.get("complete"), ms=result.get("ms"), selected_group=result.get("selected_group"),
                  main_group=result.get("main_group"), reason=result.get("reason"), tab_known=identified,
                  pinned=bool(identified and seen.get("position")), shallow_documents=result.get("shallow_documents"),
                  tabitems_in_documents=result.get("tabitems_in_documents"), depth_capped=result.get("depth_capped"))

    def census(self, service: str, windows: list[tuple[int, int]] | None = None) -> dict[str, Any]:
        """Find ``service``'s tab in every window of its browser (``windows``: ``[(hwnd, pid)]``; default: the user
        windows of the process its identities were learned in). Content-free result (:func:`census_job`), logged."""
        ident = self.identities(service)
        process = str(ident.get("process") or "")
        if windows is None:
            windows = _windows_of(process)
        fn = self._census_fn
        if fn is None:
            from yuki.memory.extract.uia import tab_census as fn
        self.counters["census"] += 1
        result = fn(windows, ident, digest_fn=lambda name: self.digest(process, name))
        best = (result.get("chosen") or [None])[0]
        self._log("tab_census", app=process or None, service=service, windows=result.get("windows"),
                  tabs=result.get("tabs"), candidates=len(result.get("candidates") or []),
                  decision=result.get("decision"), match=getattr(best, "match", None), ms=result.get("ms"),
                  reasons=[d.get("reason") for d in result.get("diagnostics") or []][:8],
                  error=result.get("error"))
        return result

    def stats(self) -> dict[str, Any]:
        return dict(self.counters)

    def _log(self, type: str, **fields: Any) -> None:
        if self._log_fn is not None:
            try:
                self._log_fn(type, **fields)
            except Exception:  # noqa: BLE001
                pass


def _windows_of(process: str) -> list[tuple[int, int]]:
    """``[(hwnd, pid)]`` of the user windows whose program's app key is ``process`` (``zen``)."""
    if not process:
        return []
    try:
        from yuki.memory.store import app_key
        from yuki.perception.windows import list_windows

        return [(w.hwnd, w.pid) for w in list_windows() if app_key(w.process_name) == process]
    except Exception:  # noqa: BLE001
        return []


__all__ = ["CallEvidence", "Screen", "Verdict", "Life", "JUDGE_PROMPT", "JUDGE_TOOL", "SITES_PROMPT", "SITES_TOOL",
           "SiteTabs", "shipped_site_entries", "FRESH_S", "HEALTH_RATIO", "HEALTH_WINDOW", "IN_CALL", "NOT_IN_CALL",
           "UNCLEAR", "SITES_STATE_KEY", "TAB_READ_GAP_S"]
