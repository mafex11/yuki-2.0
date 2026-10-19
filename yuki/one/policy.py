"""The single policy gate of the ONE agent (phase 4 design §4, addendum must-fix 3, 4, 10): every act passes here.

``gate(tool, tool_input, target_facts, turn) -> Decision`` is the authorizer the verbs call once their target is known
(the runner before each act; the skill engine's ``SkillRunner._held`` and ``chat._judged`` through :func:`authorize`).
It is a thin wrapper on the table that already exists: ``brain.consent`` (rows 1-6) with the one-step commit check
(``brain.check.one``), plus the ONE agent's own facts. Rows, in order:

0. **Closed turn** (the budget's hard stop): refused silently. **Unknown tool: refused** (fail closed: the agent's own
   call is one of its ONE tools, never a Dispatcher verb's name; the skill engine's steps, ``inner``, are Dispatcher
   verbs); a typed-only tool (``keys``, ``shell``, ``fetch``, ``ask_user``) on voice: refused. **The budget**
   (:mod:`yuki.one.budget`: hard stop, soft time, reads, the same failing step, the retry window) is read here, before
   any row below, so a call it refuses leaves nothing behind (no question recorded, no text marked, no tidy step used,
   no commit check spent); a lasting act after one missed is refused once the decision shows it lasting.
1. **Controls, reads, memory writes**: allowed. ``quick_memory``/``memory`` reads, a file or the clipboard set the
   turn's ``private_read``; after it a ``fetch`` of an address and an ``open_url`` whose address carries a query go to
   the commit check as an outbound fetch (the old M9 rule).
2. **Game in front, always read**: the media session and the mixer (``media_key``, ``app_volume``) work with a game in
   front, as the hands do today; every tab action is refused (addendum: tab close is game-gated); a skill runs in its
   own game mode (its rungs' own rule; the decision carries ``game``); everything else is consent row 1.
3. **Act switch**: consent row 2, and for ``run_skill`` the skill's own app and the app its ``app`` slots name.
4. **Sends by code**: Enter (typed or pressed), a committing key or any click in a window holding text Yuki typed and
   has not sent is a send (``sends_for_certain`` widened to every click and key, addendum must-fix 4): refused, since
   raw verbs never send; a message goes through ``run_skill`` with a declaration. The skill run the gate itself granted
   (a counted send, :meth:`Turn.begin_skill`) may press its own send once, for that very text. The agent's own Enter
   (a key, a combination holding it, typing with Enter or a line break) in a chat's window (``consent.chat_window``;
   a program it can't tell counts as a chat) is a send by code too, whoever's text waits there (addendum: zero
   tolerance); the skill engine's own steps keep using Enter (a quick switcher), judged as before.
5. **Skill sends**: no declaration (``consent`` on ``run_skill``): the gate **rewrites the send slot to a draft**
   (``Decision.rewrite``; Yuki's own wording typed after, ``type_after``), never a hand-over. With one: ``check.one``
   with ``consent=``: the send rule unchanged (countdown, the quote in their words, the recipient named, their text,
   the near-duplicate ask, DM only and exactly that text through the grant, once per turn).
6. **Lasting** (close, delete, keys, a committing click, Enter, shell): the commit check, fail closed. A window close is
   judged on their newest turn alone (the hands' x3 rule). ``shell`` is shown whole to the check, at most
   ``tools.SHELL_CHARS`` characters, a plain refusal past it.
7. **Undoable**: allowed.
8. **Tidy** (a window move or focus_back while the turn holds a flagged act diff, on a window the turn's own acts
   brought up or took the front from): the tidy dial (``util.tidy_mode``): do it / ask once / off; at most two tidy
   steps per flagged diff.

A yes to a question the gate asked is recorded as a code fact (:meth:`Turn.answer`): that very step is allowed once,
not asked again (rows 1-2 read first; a send keeps its countdown), and the yes is used up (``consent.after_ask``:
"allowed this once"). A question nobody answered lasts one more turn (:meth:`Turn.carry`), a yes never carries, and a
send of the previous turn is asked before it goes again. A click whose target has neither role nor name is judged as a
committing click (the hands' rule for an unknown role). The second refusal of a turn settles it by code
(``Decision.settle``; :mod:`yuki.one.budget`). Content-free log, one line per decision.

The hands' consent state lives here now, as turn state: the newest-words close rule, the windows holding Yuki's unsent
text (process-wide, :func:`mark_typed`: a voice act and a typed act see the same desk), the join grant.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

from yuki.brain import check as _check
from yuki.brain import consent as rule
from yuki.brain import util
from yuki.brain.check import CommitCheck, OneVerdict
from yuki.one import tools as T

ALLOW, ASK, REFUSE = "allow", "ask", "refuse"
KINDS: tuple[str, ...] = (ALLOW, ASK, REFUSE)

#: Lines for the model (refusals it can route around or fix) and for the user (``final`` ones, said as they are).
LINE_UNKNOWN = "Not done: that is not one of your tools, so nothing was done."
LINE_CHANNEL = "Not done: {tool} works only on typed requests; nothing was done."
LINE_ERROR = "Not done: Yuki's check could not run, so nothing was done."
LINE_GAME = rule.LINE_GAME
LINE_SEND_BY_CODE = ("Not done: that would send the text Yuki typed there and has not sent; a message goes out only "
                     "through run_skill message with consent. Nothing was sent.")
LINE_SHELL_LONG = ("Not run: the command is longer than Yuki's check reads ({n} characters at most); split it into "
                   "shorter commands (the session keeps variables).")
LINE_ALREADY_SENT = "Not sent again: that same message already went to them in this request."
#: The same message went to them in the turn before: asked before it goes again (a yes: once, with the countdown).
LINE_SENT_AGAIN = "You just sent that to {who}. Send it again?"
#: Enter in a chat, or a step the check read as sending with nothing declared: a message goes only through a skill.
LINE_CHAT_SEND = ("Not done: that would send a message (Enter or a send button in a chat); a message goes out only "
                  "through run_skill message with consent. Nothing was sent.")
#: M9: an address carrying a query, after this request read their private things, that the check did not find asked.
LINE_M9_ASK = "Should I open that address? It may carry what I read of yours."
LINE_TIDY_OFF = ("Not done: their settings say no tidy-ups; leave the windows as they are and say in a few words what "
                 "changed.")
LINE_TIDY_STEPS = "Not done: that change got its tidy-up already; nothing more was moved."
#: The tidy dial says ask: the step in code's words (``executor.LINE_TIDY_ASK``).
LINE_TIDY_ASK: dict[str, str] = {"place": "Should I move {app} to your other screen?",
                                 "focus_back": "Should I put {app} back in front?"}
#: Row 4's refusals in the words the ONE agent writes (``run_skill``'s ``consent``), by consent's code word.
ONE_LINES: dict[str, str] = {
    "undeclared_send": LINE_CHAT_SEND,
    "not_capability": "Not done: messages go only through run_skill message with consent; nothing was sent.",
    "recipient": "Not sent: consent.to must name whom it goes to.",
    "recipient_slot": "Not sent: the skill's person and consent.to name different people; make them the same person.",
    "text_mismatch": ("Not sent: consent.text and the text slot differ; write the same message in both, in their words "
                      "and language."),
    "declared_draft": ("Not sent: consent declares a send but send is not \"send\"; set it, or drop consent for a "
                       "draft."),
    "send_slot": "Not sent: set the skill's send slot to \"send\", or drop consent for a draft.",
    "quote": "Not sent: consent.quote must be their own words asking for it, exactly as they said them.",
    "quote_turn": "Not sent: quote the turn of their words that names the person and says what to send.",
    "no_draft": "Not done: that skill has no draft form and nothing was declared to send; nothing was done.",
}
#: A tidy-up takes at most this many steps per flagged act diff (``executor.TIDY_STEPS``).
TIDY_STEPS = 2
#: A window Yuki typed into counts as holding its unsent text this long (``voice.hands.Hands.TYPED_WAIT_S``).
TYPED_WAIT_S = 600.0
#: Key names that submit (a fixed key vocabulary, never words).
ENTER_KEYS = frozenset({"enter", "return"})
#: Reads of the user's private content (M9): after one, an outbound address goes to the commit check.
PRIVATE_VERBS: dict[str, str] = {"read_file": "file", "read_clipboard": "clipboard", "memory": "memory",
                                 "quick_memory": "memory"}


# ---------------------------------------------------------------------------------------------------------------------
# The desk's unsent text (process-wide: voice and typed share one desk)
# ---------------------------------------------------------------------------------------------------------------------

_typed: dict[int, tuple[float, str]] = {}
_typed_lock = threading.Lock()


def _hwnd(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return out or None


def mark_typed(hwnd: Any, text: str = "", *, clock: Callable[[], float] = time.monotonic) -> None:
    """Yuki typed ``text`` into window ``hwnd`` and pressed nothing: it waits there unsent."""
    key = _hwnd(hwnd)
    if key is not None:
        with _typed_lock:
            _typed[key] = (clock(), " ".join(str(text or "").split()))


def adopt_typed(waiting: Any, *, clock: Callable[[], float] = time.monotonic) -> int:
    """A skill run's own record of the windows it typed Yuki's text into and did not send (``uia_route.DeskHands``'s
    ``_typed``: hwnd -> monotonic time typed) joins the desk's registry, the text not known (``""``): a later click,
    Enter or key there is a send by code. The runner calls it after every ``run_skill`` (a draft waits in the chat).
    Windows already registered keep their text. Returns how many it added. Never raises."""
    added = 0
    try:
        items = list(dict(waiting or {}).items())
    except Exception:  # noqa: BLE001 - nothing readable: nothing added
        return 0
    now = clock()
    with _typed_lock:
        for raw, at in items:
            key = _hwnd(raw)
            if key is None or key in _typed:
                continue
            try:
                when = float(at)
            except (TypeError, ValueError):
                when = now
            if now - when < TYPED_WAIT_S:
                _typed[key] = (when, "")
                added += 1
    return added


def clear_typed(hwnd: Any) -> None:
    """The window's waiting text went (submitted, or the user cleared it)."""
    key = _hwnd(hwnd)
    if key is not None:
        with _typed_lock:
            _typed.pop(key, None)


def typed_text(hwnd: Any, *, clock: Callable[[], float] = time.monotonic) -> str | None:
    """The text Yuki typed in ``hwnd`` and has not sent (``""``: some, not known), or ``None``: none waits there."""
    key = _hwnd(hwnd)
    if key is None:
        return None
    now = clock()
    with _typed_lock:
        for h in [h for h, (at, _t) in _typed.items() if now - at >= TYPED_WAIT_S]:
            _typed.pop(h, None)
        got = _typed.get(key)
    return None if got is None else got[1]


# ---------------------------------------------------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """The gate's answer for one call.

    ``kind``: ``allow`` | ``ask`` | ``refuse``. ``reason``: a code word for the logs (content-free). ``countdown``: an
    allowed send waits for the countdown first (:mod:`yuki.one.countdown`). ``rewrite``: the input to run instead (a
    send with no declaration, drafted); ``type_after``: Yuki's own wording typed without Enter once the chat is open.
    ``line``: a refusal's words: said as they are when ``final`` (a game, Act off, a join, "just suggest"), else for the
    model to route around or fix. ``question``: an ask's question (one short yes/no). ``row``: the consent row.
    ``verb``: the Dispatcher verb judged. ``focus``: a refused join's window, brought forward. ``join``: a join their
    plain words allowed (the run's own steps pass row 3). ``game``: a game is in front (a skill runs in its game mode).
    ``tidy``: a tidy-up step. ``key``: the step's identity (a yes is recorded by it). ``settle``: the turn ends by
    code, ``line`` said (rule #149), no model run; ``silent``: it ends with nothing said (a closed turn). ``act_off``:
    the app whose Act switch is off. ``yes``: allowed on the user's recorded yes (used up by this decision)."""

    kind: str
    reason: str
    countdown: bool = False
    rewrite: dict[str, Any] | None = None
    line: str = ""
    question: str = ""
    row: int = 0
    verb: str = ""
    final: bool = False
    type_after: str = ""
    focus: int | None = None
    join: bool = False
    game: bool = False
    tidy: bool = False
    key: str = ""
    settle: bool = False
    silent: bool = False
    act_off: str = ""
    yes: bool = False

    @property
    def allowed(self) -> bool:
        return self.kind == ALLOW

    def facts(self) -> dict[str, Any]:
        """Content-free fields for a log line."""
        return {"kind": self.kind, "reason": self.reason, "row": self.row, "verb": self.verb,
                "countdown": self.countdown, "rewrite": self.rewrite is not None, "final": self.final,
                "join": self.join, "game": self.game, "tidy": self.tidy, "settle": self.settle, "yes": self.yes}


# ---------------------------------------------------------------------------------------------------------------------
# Turn state
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class Tidy:
    """The runner's tidy facts for the turn (addendum should-fix: held in the turn): a flagged act diff is open, the
    windows the turn's own acts brought up or covered with (``own``), and those that held the user's attention or the
    front before them (``attention``)."""

    open: bool = False
    own: set[int] = field(default_factory=set)
    attention: set[int] = field(default_factory=set)
    acts: int = 0
    asked_yes: bool = False
    #: Final-B (one-desk D3/F6): the flagged act's own desk read before it and its scope (``acted``, ``touched``, the
    #: app whose mixer row was read), so a reply code would settle while this is still open with no tidy step taken
    #: is measured once against it (``Runner.tidy_debt``); ``given``: that measure was made (never twice a turn).
    before: Any = None
    acted: Any = None
    touched: Any = None
    sound_app: str = ""
    given: bool = False


class Turn:
    """One user turn (voice) or request (typed): the gate's state.

    Args:
        channel: ``voice`` | ``typed``.
        words: The user's own words for this request, oldest first (voice: the exchange the hands read, at most 4
            turns; typed: their words and their answers), never Yuki's wording.
        earlier: Their earlier turns (at most 3 are read; they authorise nothing by themselves).
        request_id: For logs and the send grant.
        checker: A ``brain.check.CommitCheck`` (else one is made on first use from ``client``/``settings``/``log``;
            one per turn: its memo is per request).
        policy: The autonomy policy reader (``util.PolicyReader``; ``None``: the shared one).
        board: The Now board (``None``: read at each decision, 50 ms at most).
        people_in: Memory's people index (``consent.Live.people_in``).
        recent: ``recent(to) -> [{"text", "at"}]``: the user's own last messages to that person (row 4's near-duplicate
            ask), or a list.
        budget: The turn's :class:`yuki.one.budget.TurnBudget` (``None``: no budget; refusals still counted here).
        private_read: The request starts holding their private content (the people block's own messages: M9).
        log: ``log(event, **fields)``, content-free.
    """

    def __init__(self, channel: str = "voice", words: Sequence[str] = (), earlier: Sequence[str] = (), *,
                 request_id: Any = "", checker: Any = None, client: Any = None, settings: Any = None,
                 policy: Any = None, board: Mapping[str, Any] | None = None,
                 people_in: Callable[[str], list[list[str]] | None] | None = None,
                 recent: Any = None, budget: Any = None, private_read: bool = False,
                 log: Callable[..., None] | None = None) -> None:
        self.channel = "voice" if channel == "voice" else "typed"
        self.words: list[str] = [" ".join(str(w).split()) for w in words or () if str(w or "").strip()]
        self.earlier: list[str] = [" ".join(str(w).split()) for w in earlier or () if str(w or "").strip()][-3:]
        self.request_id = request_id
        self._checker = checker
        self._client = client
        self._settings = settings
        self.policy = policy
        self.board = board
        self.people_in = people_in
        self._recent = recent
        self.budget = budget
        self.reads: set[str] = {"memory"} if private_read else set()
        self.log = log
        self.tidy = Tidy()
        self.refusals = 0
        #: Step identities the user said yes to, with the row that asked: that step is allowed once, then the yes is
        #: used up (:meth:`use_yes`). Never carried to a later turn.
        self.yes: dict[str, int] = {}
        #: Questions the gate asked and nobody answered yet: identity -> (question, row), newest last.
        self.asks: dict[str, tuple[str, int]] = {}
        #: The questions in :attr:`asks` that came from the turn before (they last one turn: not carried again).
        self._carried_asks: set[str] = set()
        #: Sends that went this turn (once per request): (person folded, text folded).
        self.sent: set[tuple[str, str]] = set()
        #: Sends that went in the turn before (voice, :meth:`carry`): the same message again is asked first.
        self.sent_before: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self._join: tuple[int, str, str] | None = None
        self._send: dict[str, Any] | None = None

    # -- facts --------------------------------------------------------------------------------------------------------

    @property
    def private_read(self) -> bool:
        return bool(self.reads)

    def note_read(self, kind: str) -> None:
        """The turn read the user's private content (``file``, ``clipboard``, ``memory``: M9)."""
        if kind:
            self.reads.add(str(kind))

    def checker(self) -> Any:
        """This turn's commit check (made once; no client: every check fails closed, an ask)."""
        if self._checker is None:
            self._checker = CommitCheck(self._client, self._settings, self.log)
        return self._checker

    def board_now(self) -> Mapping[str, Any] | None:
        return self.board if self.board is not None else _check._board()

    def recent_for(self, to: str) -> list[Mapping[str, Any]]:
        got = self._recent
        try:
            rows = got(to) if callable(got) else got
        except Exception:  # noqa: BLE001 - memory can't say: no near-duplicate known
            rows = None
        return [r for r in rows or [] if isinstance(r, Mapping)]

    def words_for(self, verb: str, vinput: Mapping[str, Any]) -> tuple[list[str], list[str]]:
        """The words the commit check reads for this step, and the earlier ones. A window close is judged on their
        newest turn alone, the rest as earlier words (``voice.hands`` x3: a whole window holds every tab in it)."""
        words, earlier = list(self.words), list(self.earlier)
        if verb == "window_action" and str(vinput.get("action") or "") == "close" and len(words) > 1:
            earlier = (earlier + words[:-1])[-3:]
            words = words[-1:]
        return words, earlier

    # -- the user's answers -------------------------------------------------------------------------------------------

    def answer(self, yes: bool, key: str | None = None) -> None:
        """The user answered the gate's question (the newest, or ``key``'s): a yes is recorded as a code fact for that
        very step (rows 1-2 are read again when it runs; a send keeps its countdown); a no drops the question."""
        with self._lock:
            if not self.asks:
                return
            key = key if key in self.asks else next(reversed(self.asks))
            _question, row = self.asks.pop(key)
            self._carried_asks.discard(key)
            if yes:
                self.yes[key] = row

    def ask(self, key: str, question: str, row: int) -> None:
        """The gate asked the user about this step (newest last); asked again here, it is this turn's question."""
        if not key:
            return
        with self._lock:
            self.asks.pop(key, None)
            self.asks[key] = (question, row)
            self._carried_asks.discard(key)

    def use_yes(self, key: str) -> None:
        """A recorded yes authorised its step: used up (``consent.after_ask``: allowed this once)."""
        with self._lock:
            self.yes.pop(key, None)

    def carry(self, previous: "Turn | None") -> "Turn":
        """A new user turn that follows ``previous`` (voice): the questions ``previous`` itself asked and nobody answered
        (the user answers in their next turn; one carried in already expires), and the sends that went in it (the same
        message again is asked first). A yes never carries (it is recorded on the turn that reads the answer and used
        once); tidy and budget state do not carry. Returns self."""
        if previous is not None:
            with previous._lock:
                fresh = {k: v for k, v in previous.asks.items() if k not in previous._carried_asks}
                sent = set(previous.sent)
            with self._lock:
                for k, v in fresh.items():
                    if k not in self.asks:
                        self.asks[k] = v
                        self._carried_asks.add(k)
                self.sent_before |= sent
        return self

    # -- the runner's facts -------------------------------------------------------------------------------------------

    def flag(self, *, own: Sequence[Any] = (), attention: Sequence[Any] = (), before: Any = None, acted: Any = None,
             touched: Any = None, sound_app: str = "") -> None:
        """An act's diff came back flagged (it covered what they watched, took the keyboard, left something open):
        a tidy-up may follow, on these windows only. ``before`` and the scope (``acted``, ``touched``, ``sound_app``):
        that act's own before and whose change was whose, for the turn's tidy debt (``Runner.tidy_debt``)."""
        self.tidy = Tidy(open=True, own={h for h in map(_hwnd, own) if h},
                         attention={h for h in map(_hwnd, attention) if h}, before=before, acted=acted,
                         touched=touched, sound_app=str(sound_app or ""))

    def begin_skill(self, decision: Decision, tool_input: Mapping[str, Any], *, counted: bool = False) -> None:
        """A ``run_skill`` the gate allowed starts on this thread (the runner calls it there, right before the run):
        a join their plain words allowed lets its own steps in its app through row 3, and a send whose countdown ran
        (``counted``) or that they said yes to lets its own send key through once, for exactly its text."""
        key, slots = rule.skill_input(rule.Call("run_skill", dict(tool_input or {})))
        sk = util.skill(key)
        with self._lock:
            self._join = (threading.get_ident(), _app_id(getattr(sk, "app", "")), key) if decision.join else None
            self._send = None
            if decision.countdown and counted and sk is not None:
                tname = rule.text_slot(sk)
                self._send = {"thread": threading.get_ident(), "skill": key,
                              "text": _fold(slots.get(tname)) if tname else "", "used": False}

    def end_skill(self) -> None:
        """The skill run ended: its grants end with it."""
        with self._lock:
            self._join = None
            self._send = None

    def mark_sent(self, tool_input: Mapping[str, Any]) -> None:
        """A send may have gone (its run's own result says so): the same message never goes twice this request (and is
        asked first in the next turn, :meth:`carry`)."""
        key = send_key(tool_input)
        with self._lock:
            self.sent.add(key)

    def _join_granted(self, verb: str, vinput: Mapping[str, Any], facts: Mapping[str, Any]) -> bool:
        grant = self._join
        if not grant or grant[0] != threading.get_ident() or verb == "run_skill":
            return False
        if not facts.get("app") or _app_id(facts.get("app")) != grant[1]:
            return False
        traits = rule.call_traits(verb, dict(vinput or {}), role=str(facts.get("role") or "") or None,
                                  typed_pending=bool(facts.get("typed_pending")))
        return not rule.sends_for_certain(traits)

    def _send_granted(self, waiting: str | None) -> bool:
        """The skill run on this thread holds a counted send grant not used yet, for exactly the text waiting there."""
        with self._lock:
            grant = self._send
            if not grant or grant["thread"] != threading.get_ident() or grant["used"]:
                return False
            if waiting is not None and waiting and grant["text"] and _fold(waiting) != grant["text"]:
                return False
            grant["used"] = True
            return True


def _fold(text: Any) -> str:
    return " ".join(str(text or "").split()).casefold()


def _app_id(app: Any) -> str:
    """An app's identity (``voice.hands._app_id``: casefolded, no ``.exe``)."""
    key = str(app or "").strip().casefold()
    return key[:-4].rstrip() if key.endswith(".exe") else key


def send_key(tool_input: Mapping[str, Any]) -> tuple[str, str]:
    """A ``run_skill`` send's identity in a request: whom (the person slot, folded) and what (the text, folded)."""
    key, slots = rule.skill_input(rule.Call("run_skill", dict(tool_input or {})))
    sk = util.skill(key)
    pname = rule.person_slot(sk) if sk is not None else ""
    tname = rule.text_slot(sk) if sk is not None else ""
    return (_fold(slots.get(pname)) if pname else "", _fold(slots.get(tname)) if tname else "")


def step_identity(verb: str, vinput: Mapping[str, Any], facts: Mapping[str, Any]) -> str:
    """The step's identity for a recorded yes (``check.memo_key``: its window, verb, element, a digest of its input)."""
    call = rule.Call(verb, dict(vinput or {}), hwnd=_hwnd(facts.get("hwnd")), app=str(facts.get("app") or ""),
                     role=str(facts.get("role") or ""), name=str(facts.get("name") or ""))
    return hashlib.sha1(_check.memo_key(call).encode("utf-8", "replace")).hexdigest()[:16]


# ---------------------------------------------------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------------------------------------------------


def gate(tool: str, tool_input: Mapping[str, Any] | None = None, target: Mapping[str, Any] | None = None,
         turn: Turn | None = None, *, inner: bool = False) -> Decision:
    """The decision for one call (module docstring). ``tool``: the agent's own call, one of its ONE tools (any other
    name is unknown, refused); with ``inner``, a skill engine step by its Dispatcher verb name and input (or a code
    step of the runner's, e.g. typing a draft's ``type_after``): not the agent's own call, so not counted on the
    budget's calls or refusals. ``target``: the code facts the verb read (``hwnd``, ``app``: the window's process,
    ``title``, ``role``, ``name`` of the item from the fresh read). Never raises: an error is a refusal (nothing
    done)."""
    turn = turn if turn is not None else Turn()
    started = time.perf_counter()
    name = str(tool or "")
    try:
        decision = _gate(name, dict(tool_input or {}), dict(target or {}), turn, inner)
    except Exception as exc:  # noqa: BLE001 - fail closed
        decision = Decision(REFUSE, "error", line=LINE_ERROR)
        _emit(turn, "one_gate_error", tool=name, error=type(exc).__name__)
    if not inner:
        decision = _counted(decision, turn)
    _emit(turn, "one_gate", tool=name, inner=inner, ms=round((time.perf_counter() - started) * 1000, 1),
          **decision.facts())
    return decision


def _counted(decision: Decision, turn: Turn) -> Decision:
    """The second refusal of a turn settles it (addendum must-fix 6): code's line (a final refusal's own, else rule
    #149's), no model run."""
    if decision.kind != REFUSE or decision.settle:
        return decision
    if turn.budget is not None:
        spend = turn.budget.refused(decision.line if decision.final else "")
        return replace(decision, settle=True, line=spend.line) if spend.settle else decision
    turn.refusals += 1
    if turn.refusals >= 2:
        from yuki.one.budget import LINE_REST_LEFT

        return replace(decision, settle=True, line=decision.line if decision.final else LINE_REST_LEFT)
    return decision


def _gate(name: str, args: dict[str, Any], facts: dict[str, Any], turn: Turn, inner: bool) -> Decision:
    budget = turn.budget
    if budget is not None and budget.closed:
        return Decision(REFUSE, "closed", settle=True, silent=True)
    spec = T.spec_of(name, inner=inner)
    if spec is None:
        return Decision(REFUSE, "unknown_tool", line=LINE_UNKNOWN)
    if turn.channel not in spec.channels:
        return Decision(REFUSE, "channel", line=LINE_CHANNEL.format(tool=name))
    mapped = T.as_verb(name, args, tab_names=facts.get("tabs") if isinstance(facts.get("tabs"), list) else None,
                       inner=inner)
    if mapped is None:
        return Decision(REFUSE, "unknown_tool", line=LINE_UNKNOWN)
    verb, vinput = mapped
    if facts.get("hwnd") is None and vinput.get("hwnd") is not None:
        facts["hwnd"] = vinput.get("hwnd")
    if not inner and verb == "click" and not facts.get("role") and not facts.get("name"):
        # a target with no role and no name may be anything: judged as a committing click (``authorize``'s rule for a
        # forced click whose role is unknown; the runner passes the item's role and name from the fresh read)
        facts["role"] = "Button"
    # row 0: the budget first, so a call it refuses leaves nothing behind (no ask, no typed mark, no tidy step, no check)
    counted = not inner and budget is not None and spec.kind not in (T.CONTROL, T.WRITE)
    if counted:
        spend = budget.before_call(name, args, acts=spec.acts)
        if not spend.ok:
            return _spent(spend, verb)
    decision = _decide(name, spec, verb, vinput, args, facts, turn, inner)
    if counted and decision.allowed and (decision.countdown or decision.join):
        spend = budget.lasting()
        if not spend.ok:
            return _spent(spend, verb)
    if decision.allowed and decision.yes and decision.key:
        turn.use_yes(decision.key)
    return decision


def _spent(spend: Any, verb: str) -> Decision:
    """The budget's refusal as a decision."""
    return Decision(REFUSE, f"budget_{spend.why}", line=spend.line, verb=verb, settle=spend.settle,
                    silent=spend.silent)


def _decide(name: str, spec: T.Spec, verb: str, vinput: dict[str, Any], args: dict[str, Any],
            facts: dict[str, Any], turn: Turn, inner: bool) -> Decision:
    # row 1: the conversation's own controls, reads, memory writes
    if spec.kind == T.CONTROL:
        return Decision(ALLOW, "control", verb=verb)
    if spec.kind == T.WRITE:
        return Decision(ALLOW, "write", verb=verb)
    if spec.kind == T.READ:
        return _read(name, verb, vinput, args, facts, turn)

    board = turn.board_now()
    game = rule.game_now(board)
    in_game = rule.game_holds(rule.Live(game=game))

    # row 2: the media session and the mixer never reach a window: as the hands, fine with a game in front
    if spec.kind == T.SOUND and verb == "media_key":
        return Decision(ALLOW, "sound", verb=verb, game=in_game)
    if verb == "tab" and in_game:
        return Decision(REFUSE, "game", line=LINE_GAME, row=1, verb=verb, final=True, game=True)

    if spec.kind == T.SKILL:
        return _skill(verb, vinput, args, facts, turn, board, in_game, inner)

    # rows 1-2 first (a game in front, the Act switch, "just suggest"): never a commit check, nor an M9 read, past them
    early = rule.screen_gate(_call(verb, vinput, facts), rule.Live(game=game, policy=turn.policy))
    if early is not None:
        refused = _refusal(early.why, early.line, early.row, verb, final=early.final)
        return replace(refused, act_off=_act_off_name(facts, turn)) if early.why == "act_off" else refused

    # row 4 by code: Enter or a click where Yuki's unsent text waits
    hwnd = _hwnd(facts.get("hwnd"))
    waiting = typed_text(hwnd) if hwnd else None
    if waiting is not None:
        facts["typed_pending"] = True
        facts["typed_text"] = waiting
        if _submits(verb, vinput):
            if inner and turn._send_granted(waiting):
                clear_typed(hwnd)
                return Decision(ALLOW, "granted_send", row=4, verb=verb)
            return Decision(REFUSE, "send_by_code", line=LINE_SEND_BY_CODE, row=4, verb=verb)
    # row 4 by code: the agent's own Enter in a chat sends whatever its box holds (addendum zero tolerance), never
    # left to the commit check's reading; the skill engine's own steps (a quick switcher's Enter) are judged as before
    if not inner and verb != "click" and _submits(verb, vinput) \
            and rule.chat_window(_call(verb, vinput, facts), _board_live(board)) is not False:
        return Decision(REFUSE, "send_by_code", line=LINE_CHAT_SEND, row=4, verb=verb)

    # row 6 (shell): shown whole to the commit check, or refused plainly
    if verb == "run_powershell":
        size = len(" ".join(str(vinput.get("command") or "").split()))
        if not size or size > T.SHELL_CHARS:
            return Decision(REFUSE, "shell_long" if size else "shell_empty",
                            line=LINE_SHELL_LONG.format(n=T.SHELL_CHARS), verb=verb)

    # M9: an address carrying a query after a private read is judged as an outbound fetch
    if verb == "open_url" and turn.private_read and _carries(vinput.get("url")):
        return _outbound(verb, {"url": str(vinput.get("url") or "")}, facts, turn, board)

    # row 8: a tidy-up
    tidy = _tidy(verb, vinput, facts, turn)
    if tidy is not None and tidy.kind != ALLOW:
        return tidy

    key = step_identity(verb, vinput, facts)
    said = turn.yes.get(key)
    if said is not None:
        # their yes to this very step (rows 1-2 were read again just above): allowed once, used up by the gate
        decision = Decision(ALLOW, "said_yes", row=said, verb=verb, key=key, yes=True)
    else:
        words, earlier = turn.words_for(verb, vinput)
        verdict = _check.one(verb, vinput, facts, words=words, checker=turn.checker(), earlier=earlier, board=board,
                             policy=turn.policy, log=turn.log)
        decision = _from_verdict(verdict, verb, key, turn)
        if not decision.allowed and decision.row == 3 and inner and turn._join_granted(verb, vinput, facts):
            decision = Decision(ALLOW, "join_words", row=3, verb=verb, join=True)
    if decision.allowed:
        if tidy is not None:   # a tidy-up step counts toward the flagged diff's steps, a yes'd one too
            decision = replace(decision, tidy=True)
            turn.tidy.acts += 1
        if verb == "type_text" and hwnd:
            if vinput.get("press_enter") is True:
                clear_typed(hwnd)
            else:   # Yuki's words wait there unsent (after any that waited already)
                text = " ".join(str(vinput.get("text") or "").split())
                mark_typed(hwnd, f"{waiting} {text}".strip() if waiting else text)
    return decision


def _read(name: str, verb: str, vinput: Mapping[str, Any], args: Mapping[str, Any], facts: dict[str, Any],
          turn: Turn) -> Decision:
    if verb == "fetch_url" and turn.private_read:
        return _outbound(verb, dict(vinput), facts, turn, turn.board_now())
    if name == "quick_memory" and str(args.get("action") or "") == "add_todos":
        return Decision(ALLOW, "write", verb=verb)
    kind = PRIVATE_VERBS.get(verb)
    if kind:
        turn.note_read(kind)
    return Decision(ALLOW, "read", verb=verb)


def _outbound(verb: str, vinput: dict[str, Any], facts: dict[str, Any], turn: Turn,
              board: Mapping[str, Any] | None) -> Decision:
    """M9: after a private read, an address that carries a query is an outbound fetch for the commit check."""
    judged = {"url": str(vinput.get("url") or "")}
    key = step_identity("fetch_url", judged, {})
    if key in turn.yes:
        return Decision(ALLOW, "said_yes", row=5, verb=verb, key=key, yes=True)
    verdict = _check.one("fetch_url", judged, {**facts, "files_read": True}, words=turn.words,
                         checker=turn.checker(), earlier=turn.earlier, board=board, policy=turn.policy, log=turn.log)
    decision = _from_verdict(verdict, verb, key, turn)
    if decision.kind == REFUSE and not decision.final and decision.reason != "error":
        # the check read it as reaching someone (a send) their words didn't ask for: theirs to say, asked plainly
        turn.ask(key, LINE_M9_ASK, 5)
        decision = Decision(ASK, "m9", question=LINE_M9_ASK, row=5, verb=verb, key=key)
    return replace(decision, reason=f"m9_{decision.reason}") if not decision.reason.startswith("m9") else decision


def _skill(verb: str, vinput: dict[str, Any], args: Mapping[str, Any], facts: dict[str, Any], turn: Turn,
           board: Mapping[str, Any] | None, in_game: bool, inner: bool) -> Decision:
    key, slots = rule.skill_input(rule.Call("run_skill", vinput))
    sk = util.skill(key)
    # row 2 for a skill: its own app, and the app its app slots name (the Act switch)
    off = _skill_act_off(sk, slots, turn)
    if off:
        return Decision(REFUSE, "act_off", line=util.app_off_line(off), row=2, verb=verb, final=True, act_off=off,
                        game=in_game)
    declared = _declaration(args.get("consent"))
    sends = sk is not None and rule.skill_sends(sk, slots)
    if in_game and sk is not None and (sends or rule.skill_joins(sk)):
        # nothing reaches a game in front: a send or a join is held now, as the engine would hold it, with no check
        return Decision(REFUSE, "game", line=LINE_GAME, row=1, verb=verb, final=True, game=True)
    if sends and declared is None:
        # addendum must-fix 4: no declaration, no send: the send slot goes to a draft, never a hand-over
        tname = rule.text_slot(sk)
        text = rule.words(slots.get(tname)) if tname else ""
        own = bool(text) and rule.in_words(text, turn.words)
        drafted = rule.draft_slots(sk, slots, keep_text=own)
        if drafted is None:
            return Decision(REFUSE, "no_draft", line=ONE_LINES["no_draft"], row=4, verb=verb, game=in_game)
        rewrite = {k: v for k, v in args.items() if k != "consent"}
        rewrite["slots"] = drafted
        verdict = _check.one(verb, {**vinput, "slots": drafted}, facts, words=turn.words, checker=turn.checker(),
                             earlier=turn.earlier, board=board, policy=turn.policy, log=turn.log)
        decision = _from_verdict(verdict, verb, "", turn)
        if not decision.allowed:
            return replace(decision, game=in_game)
        return Decision(ALLOW, "draft", rewrite=rewrite, type_after="" if own else text, line=rule.LINE_DRAFTED,
                        row=4, verb=verb, game=in_game)
    if sends and declared is not None and send_key(vinput) in turn.sent:
        return Decision(REFUSE, "already_sent", line=LINE_ALREADY_SENT, row=4, verb=verb, game=in_game)
    step = step_identity(verb, vinput, facts)
    said = turn.yes.get(step)
    if said is not None:
        recheck = recheck_send(turn, vinput)
        if recheck is not None:
            return _refusal(recheck[0], recheck[1], 1 if recheck[0] == "game" else 2, verb, final=True)
        return Decision(ALLOW, "said_yes", row=said, countdown=said == 4 and sends, verb=verb, key=step,
                        game=in_game, yes=True)
    verdict = _check.one(verb, vinput, facts, words=turn.words, checker=turn.checker(), earlier=turn.earlier,
                         board=board, policy=turn.policy, log=turn.log, consent=declared if sends else None,
                         recent=turn.recent_for(declared["to"]) if sends and declared else None,
                         people_in=turn.people_in)
    decision = replace(_from_verdict(verdict, verb, step, turn), game=in_game)
    if decision.allowed and decision.countdown and sends and send_key(vinput) in turn.sent_before:
        # the same message went to them in the turn before (memory may not have it yet): asked before it goes again
        who = (declared or {}).get("to") or rule.words(slots.get(rule.person_slot(sk))) or "them"
        question = LINE_SENT_AGAIN.format(who=who)
        turn.ask(step, question, 4)
        return Decision(ASK, "sent_again", question=question, row=4, verb=verb, key=step, game=in_game)
    if decision.allowed and decision.row == 3 and verdict.code == "join_words":
        decision = replace(decision, join=True)
    if decision.allowed and decision.rewrite is not None:
        rewrite = {k: v for k, v in args.items() if k != "consent"}
        rewrite.update({k: v for k, v in decision.rewrite.items() if k in ("skill", "slots")})
        decision = replace(decision, rewrite=rewrite)
    return decision


def _from_verdict(v: OneVerdict, verb: str, key: str, turn: Turn) -> Decision:
    """``check.one``'s verdict as a decision (an ask is remembered by the step's identity)."""
    if v.allowed:
        return Decision(ALLOW, v.code or v.why or "allow", countdown=bool(v.countdown), row=v.row, verb=verb, key=key)
    if v.decision == "draft":
        return Decision(ALLOW, "draft", rewrite=dict(v.draft_input or {}) or None, type_after=v.type_after,
                        line=v.line or rule.LINE_DRAFTED, row=v.row, verb=verb, key=key)
    if v.question:
        turn.ask(key, v.question, v.row)
        return Decision(ASK, v.code or v.why or "ask", question=v.question, row=v.row, verb=verb, key=key)
    if v.act_off:
        return Decision(REFUSE, "act_off", line=util.app_off_line(v.act_off), row=2, verb=verb, final=True,
                        act_off=v.act_off)
    if v.source == "error":
        return Decision(REFUSE, "error", line=LINE_ERROR, verb=verb)
    code = v.code or "refused"
    return _refusal(code, ONE_LINES.get(code) or v.line or v.why, v.row, verb, final=v.final, focus=v.focus)


def _refusal(code: str, line: str, row: int, verb: str, *, final: bool = False, focus: int | None = None) -> Decision:
    if code == "game":
        line, final = LINE_GAME, True
    return Decision(REFUSE, code, line=line, row=row, verb=verb, final=final, focus=focus, game=code == "game")


def _call(verb: str, vinput: Mapping[str, Any], facts: Mapping[str, Any]) -> rule.Call:
    return rule.Call(verb, dict(vinput or {}), index=0, hwnd=_hwnd(facts.get("hwnd")), app=str(facts.get("app") or ""),
                     title=str(facts.get("title") or ""), role=str(facts.get("role") or ""),
                     name=str(facts.get("name") or ""))


def _board_live(board: Mapping[str, Any] | None) -> rule.Live:
    """Consent's code facts for a chat-window read, from the board this decision already holds (each window's page and
    the extraction's kind); no board: ``consent`` reads its own (50 ms at most)."""
    if not isinstance(board, Mapping) or not isinstance(board.get("windows"), list):
        return rule.Live()
    pages: dict[int, str] = {}
    kinds: dict[int, str] = {}
    for w in board["windows"]:
        key = _hwnd(w.get("hwnd")) if isinstance(w, Mapping) else None
        if key is not None:
            pages[key] = str(w.get("url") or "")
            kinds[key] = str(w.get("kind") or "")
    return rule.Live(pages=pages, kinds=kinds)


def _act_off_name(facts: Mapping[str, Any], turn: Turn) -> str:
    try:
        return str((turn.policy or util._shared_reader()).app_off(str(facts.get("app") or "")) or "")
    except Exception:  # noqa: BLE001
        return ""


def _declaration(value: Any) -> dict[str, str] | None:
    """``run_skill``'s ``consent``: ``{to, text, quote}`` when it names whom and what (an empty one is none)."""
    if not isinstance(value, Mapping):
        return None
    out = {k: rule.words(value.get(k)) for k in ("to", "text", "quote")}
    return out if out["to"] or out["text"] or out["quote"] else None


def _skill_act_off(sk: Any, slots: Mapping[str, Any], turn: Turn) -> str:
    """The name Settings shows for the skill's own app, or an app its ``app`` slots name, when its Act switch is off."""
    if sk is None:
        return ""
    apps = [str(getattr(sk, "app", "") or "")]
    for slot in getattr(sk, "slots", ()) or ():
        if str(getattr(slot, "type", "") or "") == "app" and isinstance(slots.get(slot.name), str):
            apps.append(slots[slot.name])
    try:
        reader = turn.policy or util._shared_reader()
        for app in apps:
            off = reader.app_off(app) if app.strip() else None
            if off:
                return str(off)
    except Exception:  # noqa: BLE001 - an unreadable policy never blocks (the old judge's rule)
        return ""
    return ""


def _submits(verb: str, vinput: Mapping[str, Any]) -> bool:
    """A step that sends what waits in its window: any click, Enter pressed (alone or in a combination, or a slider's
    keys), typing with Enter or a line break. Key names and tool facts only."""
    if verb == "click":
        return True
    if verb == "press":
        return _fold(vinput.get("key")) in ENTER_KEYS
    if verb in ("hotkey", "set_slider"):
        keys = vinput.get("keys")
        return isinstance(keys, list) and any(_fold(k) in ENTER_KEYS for k in keys)
    if verb == "type_text":
        text = vinput.get("text")
        return vinput.get("press_enter") is True or (isinstance(text, str) and ("\r" in text or "\n" in text))
    return False


def _carries(url: Any) -> bool:
    """An address with a query or a fragment (a fixed machine format, parsed): what can carry data out (M9)."""
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return True
    return bool(parsed.query or parsed.fragment)


def _tidy(verb: str, vinput: Mapping[str, Any], facts: Mapping[str, Any], turn: Turn) -> Decision | None:
    """Row 8: a window move or focus_back while a flagged act diff is open, on a window the turn's acts brought up
    (move) or one that had their attention or the front before (focus_back): the tidy dial. ``None``: not a tidy-up
    (an undoable act like any other)."""
    if verb != "window_action" or not turn.tidy.open:
        return None
    action = str(vinput.get("action") or "")
    if action not in ("place", "focus_back"):
        return None
    hwnd = _hwnd(facts.get("hwnd"))
    mine = (hwnd in turn.tidy.attention) if action == "focus_back" else (hwnd in turn.tidy.own)
    if not mine:
        return None
    mode = util.tidy_mode(turn.policy)
    if mode == util.TIDY_OFF:
        return Decision(REFUSE, "tidy_off", line=LINE_TIDY_OFF, row=8, verb=verb, tidy=True)
    if turn.tidy.acts >= TIDY_STEPS:
        return Decision(REFUSE, "tidy_steps", line=LINE_TIDY_STEPS, row=8, verb=verb, tidy=True)
    if mode == util.TIDY_ASK and not turn.tidy.asked_yes:
        key = step_identity(verb, vinput, facts)
        if key in turn.yes:
            turn.tidy.asked_yes = True
        else:
            raw = str(facts.get("app") or "").strip()
            app = util.app_name(raw) if raw else "that window"
            question = LINE_TIDY_ASK["place" if action == "place" else "focus_back"].format(app=app)
            turn.ask(key, question, 8)
            return Decision(ASK, "tidy_ask", question=question, row=8, verb=verb, tidy=True, key=key)
    return Decision(ALLOW, "tidy", row=8, verb=verb, tidy=True)


# ---------------------------------------------------------------------------------------------------------------------
# For the runner, the countdown and the skill engine
# ---------------------------------------------------------------------------------------------------------------------


def recheck_send(turn: Turn, tool_input: Mapping[str, Any]) -> tuple[str, str] | None:
    """After a send's countdown (and before a yes'd step runs): a game in front, or the skill's app switched off,
    stops it: ``(why, line)``; ``None``: go on. The countdown host's ``recheck``."""
    try:
        if rule.game_holds(rule.Live(game=rule.game_now(turn.board_now()))):
            return "game", LINE_GAME
        key, slots = rule.skill_input(rule.Call("run_skill", dict(tool_input or {})))
        off = _skill_act_off(util.skill(key), slots, turn)
        if off:
            return "act_off", util.app_off_line(off)
    except Exception:  # noqa: BLE001 - the check could not run: nothing is sent
        return "error", LINE_ERROR
    return None


def skill_grant(turn: Turn, tool_input: Mapping[str, Any], decision: Decision, *, person_id: int | None = None,
                counted: bool = False, step: int = 0) -> dict[str, Any]:
    """The grant the desk hands read for one ``run_skill`` (``brain.executor.Executor._skill_grant``'s shape:
    ``skills.uia_route.DeskHands.grant`` / ``send_grant``): a send the gate allowed with its countdown (``counted``:
    one the user could stop ran out, or they said yes), a join their plain words allowed, the step's own text and
    person slots, a one-step token, this thread and request."""
    key, slots = rule.skill_input(rule.Call("run_skill", dict(tool_input or {})))
    sk = util.skill(key)
    tname = rule.text_slot(sk) if sk is not None else ""
    pname = rule.person_slot(sk) if sk is not None else ""
    send = bool(decision.allowed and decision.countdown and sk is not None
                and rule.skill_sends(sk, slots, partial=False))
    join = bool(decision.allowed and decision.join)
    grant: dict[str, Any] = {"skill": key, "text": rule.words(slots.get(tname)) if tname else "",
                             "person": rule.words(slots.get(pname)) if pname else "", "send": send, "join": join}
    if send:
        grant.update(text_slot=tname, person_slot=pname, step=int(step), request_id=turn.request_id,
                     thread=threading.get_ident(), token=secrets.token_hex(8), person_id=person_id,
                     counted=bool(counted))
    return grant


def authorize(turn: Turn, tool: str, tool_input: Mapping[str, Any], target: Mapping[str, Any] | None = None,
              earlier: Sequence[str] | None = None, *, force: bool = False) -> Any:
    """The hands' ``authorize`` shape for the skill engine (``SkillRunner._held``, ``chat._judged``, rung B) on the ONE
    agent: ``allowed``, ``decision``, ``question``, ``why``, ``source``, ``handover`` (always false: nothing is handed
    over), ``act_off``, ``row``. A caller with no role or name has them read from its element words (``role "name"``);
    a ``force`` d click whose role is unknown is judged as a committing click (the hands' rule)."""
    del earlier   # the turn holds their earlier words
    facts = dict(target or {})
    if not facts.get("role") and not facts.get("name"):
        role, name = _element_parts(facts.get("element") or (tool_input or {}).get("element"))
        if role or name:
            facts.update(role=role, name=name)
    if tool == "click" and force and not facts.get("role"):
        facts["role"] = "Button"
    d = gate(tool, tool_input, facts, turn, inner=True)
    return SimpleNamespace(allowed=d.allowed, decision="allow" if d.allowed else "ask_user", question=d.question,
                           why=d.line or d.reason, source=f"row{d.row}" if d.row else d.reason, handover=False,
                           act_off=d.act_off, row=d.row, countdown=d.countdown)


def _element_parts(element: Any) -> tuple[str, str]:
    """``role "name"`` element words split (``voice.hands._element_parts``); ``("", "")`` when not that shape."""
    text = " ".join(str(element or "").split())
    head, sep, rest = text.partition(' "')
    if not sep or '"' not in rest or " " in head.strip():
        return "", ""
    role = head.strip()
    return ("" if role.casefold() in ("control", "field", "item") else role), rest[:rest.rindex('"')].strip()


#: The yes/no read of the user's answer to the gate's question (``brain.prompt.ANSWER_PROMPT``: only the question and
#: the reply, a small model, fail closed). The driver calls it on their next words when :attr:`Turn.asks` holds one.
def read_answer(client: Any, settings: Any, question: str, reply: str, *, timeout_s: float = 3.0,
                log: Callable[..., None] | None = None) -> bool:
    """Whether ``reply`` agrees to go ahead with exactly what ``question`` asked (``brain.agent.Brain._judge_answer``:
    the check model, forced tool, thinking off). No client, no model, an error, a timeout or an unclear reply: no.
    One model call: the drivers' (never this lane's checks)."""
    from yuki.brain.prompt import ANSWER_PROMPT, ANSWER_TOOL

    model = str(getattr(settings, "check_model", "") or getattr(settings, "judge_model", "") or "")
    if client is None or not model or not str(reply or "").strip():
        return False
    started = time.perf_counter()
    agrees = False
    try:
        from yuki.agent.failover import claude_path

        response = claude_path(client).messages.create(
            model=model, max_tokens=64, system=ANSWER_PROMPT, tools=[ANSWER_TOOL],
            tool_choice={"type": "tool", "name": "answer"}, thinking={"type": "disabled"}, timeout=timeout_s,
            messages=[{"role": "user", "content": f"Yuki asked: {question}\nThe user replied: {reply}"}])
        for block in getattr(response, "content", None) or []:
            if getattr(block, "type", None) == "tool_use" and isinstance(getattr(block, "input", None), Mapping):
                agrees = block.input.get("agrees") is True
        ok = True
    except Exception:  # noqa: BLE001 - fail closed
        ok = False
    if log is not None:
        try:
            log("one_answer", ok=ok, agrees=agrees, ms=round((time.perf_counter() - started) * 1000, 1))
        except Exception:  # noqa: BLE001
            pass
    return agrees


def _emit(turn: Turn, event: str, **fields: Any) -> None:
    if turn.log is None:
        return
    try:
        turn.log(event, **{k: v for k, v in fields.items() if v is not None})
    except Exception:  # noqa: BLE001 - a log never breaks a decision
        pass


__all__ = ["ALLOW", "ASK", "Decision", "ENTER_KEYS", "KINDS", "LINE_ALREADY_SENT", "LINE_CHANNEL", "LINE_CHAT_SEND",
           "LINE_ERROR", "LINE_GAME", "LINE_M9_ASK", "LINE_SEND_BY_CODE", "LINE_SENT_AGAIN", "LINE_SHELL_LONG",
           "LINE_TIDY_ASK", "LINE_TIDY_OFF", "LINE_TIDY_STEPS", "LINE_UNKNOWN", "ONE_LINES", "PRIVATE_VERBS", "REFUSE",
           "TIDY_STEPS", "TYPED_WAIT_S", "Tidy", "Turn",
           "adopt_typed", "authorize", "clear_typed", "gate", "mark_typed", "read_answer", "recheck_send", "send_key",
           "skill_grant", "step_identity", "typed_text"]
