"""The one consent rule (brain-design.md §2.5): one table, in code, applied to every act step before it runs.

**Permission by undoability** (P1.5, 18 Oct): the rows below are three classes, decided by code facts in
:func:`check`'s order:

* **never** (rows 1-2, refused and said plainly): input, focus, a launch or a link with a game in front; an app whose
  Act switch is off; a kind on "just suggest"; and by row 4's own rules, typing over their draft and a send in Yuki's
  own wording without their yes;
* **undoable** (row 6, allowed, then said): a step that cannot commit (:func:`may_commit` false): place, focus, focus
  back, put it back, open, switch, tab show, minimise / maximise / restore, volume, media, scroll, typing without
  Enter, reads; place and focus back are journaled for "put it back";
* **lasting** (rows 3-5, their plain words or one question; a send keeps its countdown): a send, a join, anything that
  destroys or reaches (delete, close, buy, a setting, a shell, a committing click).

A tidy-up inside a request (the executor's after-look) may take only undoable window steps.

``check(step, plan, user_words, live, check_verdict) -> allow | ask | refuse | draft``, row by row, the first row that
applies decides:

1. **Game in front** (the Now board's ``game_front``, else the local foreground read, D9; unknown from both, which
   happens only when the local read raised, is read once more at the step, :func:`game_holds`, and counts as a game
   only when that read fails too) and the step takes focus, sends input, restores, launches or opens a link:
   **refuse** ("after the round?").
   The D17 backstop in ``yuki/actions`` still guards underneath. A ``run_skill`` is not refused here: the skill engine
   applies each rung's own game rule (a no-focus rung, the mixer or the media session, may run mid-game).
2. The step's app has its "Act" switch off, or one of its kinds is "just suggest" (``util.PolicyReader``,
   ``util.acts_in_app``, ``util.app_off_line``, ``util.suggest_only``): **refuse**.
3. **Join** (H3; morning21 A4: the step is judged, never the window). A step is a join when (i) its target is a
   declared join control by (role, name) identity: a click on it, or Enter/Space (or typing that submits) with the
   focus read on it (catalog data: chat apps' ``[chat] labels.*.join_voice`` anywhere; calling sites'
   ``[call.labels.*] join`` and ``join_in_page``, ``yuki/connect/sites``, only on that site's page shown now: names
   ordinary pages draw too, such as "Join now" or Meet's "Present"; identity, never keywords); (ii) the commit check
   says ``effect = join``; or (iii) it
   runs a capability that joins: **refuse** as final (bring the window forward, "you click Join"). The one exception:
   a ``run_skill`` of a capability whose commit word (phase L's D24 table, ``Skill.commit``) is ``words``, on the
   user's plain words (the commit check's ``asked``; decision 7). (iv) Fail-closed only: in a window whose calling
   site's tab is the page it shows now (the Now board's ``calling_tabs`` entry, its tab's RuntimeId read again at the
   step), a step whose target can't be identified (a click with no control named by the read, Enter/Space or a
   submit with no focus read) or whose commit check failed: **refuse**, not final: the planner gets
   :data:`LINE_JOIN_UNIDENTIFIED` (act on the control the read names; a toggle takes ``toggle_to``). Every other step
   in that window goes on to rows 4-6 (the 21 Oct pre-join page's "Turn off microphone" was refused as a "join" for
   the window alone, and the user heard "you click Join"). A calling tab open behind another tab is not the page
   shown.
4. **Send**: the step sends for certain (:func:`sends_now`: a click on a control that is not a button, menu item or
   split button while Yuki's typed text waits in that window; typing with Enter or a line break where Enter sends a
   message: a chat's window, :func:`chat_window`, by its program among a chat app's connection data (``[chat]
   images`` / ``processes``), by the page it shows (the Now board's address) on one of a chat app's web ``services``
   hosts, by the Now board's read of the window being a ``conversation`` (the extraction's kind, its generic chat
   detector's too), or by memory's capture profiles calling the program or page a ``conversation``, a window whose
   program is unknown counting as a chat; or a window where Yuki's typed text waits; and a line break anywhere else
   but a browser loading one address, since the commit check is never shown that Enter: in an editor too, whose
   terminal or chat panel runs or sends it, review r2 L4), is a
   ``run_skill`` whose skill sends with these slots, or the commit check says ``effect = send`` (a button, menu item
   or split button click, a key, or typing with ``press_enter`` anywhere else, a search box, a command field, is
   may-commit and goes to the check, told when Yuki's text waits, so "type a search, press Enter" is row 5's;
   designer's call, review r2 L1, failure fix-plan item 11).

   One web address typed with Enter into a browser window known not to be a chat (:func:`navigates`: its address bar)
   opens that page, as ``open_url`` would: judged as typing (rows 1-2), never a send, whatever word the check gives
   it. Enter typed or pressed in a window known not to be a chat, which the check calls a send, is **ask**ed: the
   check's question when their words didn't ask for it, else what is typed and where (a search box it misread, or a
   chat no data knows: their call, never a dead end; review r1 L1).

   A send: **refuse** ("undeclared send") unless the plan declared ``consent.commit = send`` and the step is a sending
   capability. A plan that declares a send while its capability step leaves a draft (its send slot on a non-sending
   option) is refused too, so the planner makes the two agree. Then (HANDBOOK rule 8 and the lead's send rule of 18 Oct:
   "send X to Y", and "message / tell / ask / reply to Y" with X in their words, mean send after the countdown; naming
   only the person, or asking for a draft, means a draft; the commit check reads which):

   * code facts first, each a **refuse** the planner can fix: ``consent.to`` is given; the capability's person slot
     names the same person as ``to`` (the same words, or memory's people index, ``live.people_in``, finds one person
     who goes by both); the step's text slot is ``consent.text`` (the countdown shows ``consent.text``);
   * the commit check does not say this very send was asked (``effect = send`` and ``asked``), or failed: **draft**
     (the capability runs with its send slot on a non-sending option; for Yuki's own wording the chat opens with no
     text and the wording is typed without Enter);
   * ``quote`` (their words asking for it) stands in the user's words of this exchange, whole words in order, where a
     name the recipient goes by may stand for one to :data:`NAME_GAP` of the words they said there (speech heard "hey
     meant" or "हेमंत" for Hemanth), and those words name the recipient or no one, never another person
     (:func:`_quote_place`, review r2 M1 b); else **refuse**;
   * the text is their own words (a span of them), or Yuki's wording of what they said, both measured in the turns
     the quote stands in (:func:`quote_turns`), never every turn of the exchange: :func:`said_share`, the share of
     the text's words (the recipient's names aside) they said close together there. When that falls short of
     :data:`SAID_SHARE` but another turn of theirs names the person and says something of the message, **refuse**
     (the planner quotes that turn). None of them: nothing of the message came from the user, they named only the
     person: **draft**. When those turns name someone else too, the share that sends without asking is measured in
     the words the quote stands for alone;
   * every ``sends_when`` slot on a sending value, else **refuse** (a slot left out would be filled with its default,
     a draft: never counted down or reported as sent);
   * a near-duplicate of the user's recent messages to that person (``util.similar``, last 3 hours): **ask** "send it
     again?"; Yuki's wording with less than :data:`SAID_SHARE` of it from their words, or a recipient the words the
     quote stands for don't name (through the people index: exactly one person who goes by ``to``; literally when it
     knows no one by that name; review r2 M1 a): **ask** "Send "<text>" to <to>?" (a yes is their plain
     instruction);
   * otherwise **allow** with the countdown first (showing the text, the user may stop or change it).
5. **Lasting**: any other may-commit step (its traits destroy or reach: a committing-role click, a key, a shell, a
   close, a non-web open, a fetch after a file, clipboard or memory read, M9), and a step of a kind the user's policy
   holds below "do it and tell me": the commit check decides (``effect = none`` allow; lasting and asked allow; lasting
   and not asked **ask** its question; the check failed or timed out **ask**, fail closed).
6. Anything else, the undoable class (focus, place, focus back, put it back, a web page, an address typed into a
   browser with Enter, scroll, typing without Enter, a list item, tab or link click, minimise / maximise / restore,
   volume, media, reads): **allow**.

Rows 1-4's facts are code facts: tool metadata (``yuki.agent.tools.call_traits``), the Now board, capability data,
memory's people index, string spans and counted shares of words. Whether the words *ask* for the effect is the commit
check's call (``yuki.brain.check``), an independent model that sees only the user's words and the steps, never the
planner's own judgement (H1, H2). No regex, no keyword list, no app name: strings are only compared with strings.

A verdict's ``why`` is a code word for the logs and ``line`` of a refusal that is not final is for the planner: neither
is ever said to the user (:func:`plain_why` gives the plain words for a code word, or nothing). Final refusals' lines,
questions and the draft line are what the user hears.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

from yuki.agent.tools import WEB_SCHEMES, Traits, call_traits, sends_for_certain, tool_traits
from yuki.brain import util

ALLOW, ASK, REFUSE, DRAFT = "allow", "ask", "refuse", "draft"
RESULTS: tuple[str, ...] = (ALLOW, ASK, REFUSE, DRAFT)
#: The plan's declared commit (``consent.commit``, §2.2).
COMMITS: tuple[str, ...] = ("none", "send", "join", "lasting")
#: The commit check's effects (§2.6); every one but ``none`` is lasting. ``later`` (A2, Z7): the step leaves something
#: acting on its own after it returns (a hidden process, a watcher, a job, a scheduled task), refused as final whatever
#: ``asked`` says.
EFFECTS: tuple[str, ...] = ("none", "send", "join", "delete", "close", "buy", "setting", "later", "other")

#: What the user is told (code lines; voice-shaped).
LINE_GAME = "You're in a game: after the round?"
LINE_JOIN = "{app} is in front: you click Join."
LINE_JOIN_PLAIN = "You click Join."
#: morning21 (A5): row 3's fail-closed refusal (iv), for the planner (not final, never said): the step's target in a
#: meeting page in front can't be told from its Join.
LINE_JOIN_UNIDENTIFIED = ("on this window's {service} page this step's target can't be identified (no control the "
                          "read names, or a key with no focus read), so it could be its Join, which is the user's own "
                          "click: act on the control the read names (its role and name); a toggle (a microphone, a "
                          "camera) takes toggle_to on or off; a key only with the focus read on a named control")
#: integ29 (S3, A7's second rung in code): a calling site's own device key on its page shown now, with no state of
#: that device measured by the hand just before (planner-facing, not final).
LINE_DEVICE_KEY = ("not pressed: on this window's {service} page this key flips the {device} whatever its state "
                   "(against its state it turns the {device} on; outside the page it bookmarks the page): act on the "
                   "page's own {device} toggle by the control the read names, with toggle_to (read first, clicked "
                   "only if not there already); the key only with the {device}'s state read by the hand")
LINE_UNDECLARED_SEND = ("that step would send something the plan did not declare as a send: send only through the "
                        "message capability with commit send, or leave it as a draft")
#: The same refusal for typing with Enter (``submit`` or a line break) in a chat app's window or where Yuki's typed text
#: waits (planner-facing). Elsewhere (an address bar, a search box) typing with Enter goes to the commit check (row 5).
LINE_TYPED_ENTER = ("typing with Enter here sends what is typed (a chat app's window or page, or Yuki's unsent text "
                    "waits there), and the plan declared no send: send a message through the message capability with "
                    "commit send, type it without submit and press Enter as its own step (that step is checked), or "
                    "leave it as a draft; to open a web address, use open_url")
#: The same for a line break in typed text outside a chat app's window: it presses Enter, and the commit check is shown
#: the text without it, so it can't judge that Enter (planner-facing).
LINE_LINE_BREAK = ("a line break in the typed text presses Enter, which may send or run it, and the commit check can't "
                   "see it: type the text without the line break and set submit (that Enter is checked), or press "
                   "Enter as its own step")
#: A2 (Z7): a step that would leave something acting on its own after it returns (the commit check's ``later`` effect).
#: Refused as final in both brains, whatever their words asked for: Yuki acts only while it works on the request.
LINE_LATER = "I can't wait for that and do it later; want it now?"
LINE_NOT_CAPABILITY = "send through the message capability"
LINE_RECIPIENT = "the step sends to someone other than the plan's recipient"
#: Planner-facing refusals of a declared send (code facts the planner can fix).
LINE_NO_RECIPIENT = "a send names whom it goes to in consent.to"
LINE_DECLARED_DRAFT = ("the plan declares a send (consent.commit send) but this step leaves a draft (its send slot "
                       "{slots} on a non-sending option): put the send slot on its sending value with the text in the "
                       "text slot, or declare consent.commit none for a draft")
LINE_TEXT_MISMATCH = ("the capability's text slot and consent.text differ: write the same text in both, the message in "
                      "their own words and language (the countdown shows consent.text)")
LINE_QUOTE = ("consent.quote is not the user's words as heard: copy their words asking for the send exactly as they "
              "stand in their words (a name they said may be written as that person's name)")
#: The quote stands in a turn that says nothing of the message, while another turn of theirs names the person and says
#: it (10 Oct: "message hey meant ... ask him when he will come", then "Do it, bro"): the planner quotes that turn.
LINE_QUOTE_TURN = ("consent.quote stands in a turn of their words that says nothing of the message, and another turn "
                   "names the person and says what to send: quote their words from that turn")
LINE_DRAFTED = "It's typed in; send it when you're ready."
LINE_DUPLICATE = "You sent that to {to} at {at}. Send it again?"
LINE_DUPLICATE_PLAIN = "You sent that recently. Send it again?"
#: A send the user is asked about, showing what goes to whom (Yuki's own wording of what they said, or a recipient their
#: words didn't plainly name: speech heard the name another way). A yes is their plain instruction (rule 8).
LINE_ASK_SEND = "Send \"{text}\" to {to}?"
LINE_ASK_SEND_PLAIN = "Send that message?"
#: The most characters of the text a question shows.
ASK_TEXT_CHARS = 160
#: Enter typed or pressed in a window known not to be a chat, which the commit check calls a send: the question when
#: the check gave none (it gives none when it found the send asked for). What is typed, where; voice-shaped.
LINE_MAYBE_SEND_TYPED = "Should I type \"{text}\" and press Enter{where}? That may send it."
LINE_MAYBE_SEND_KEY = "Should I press {key}{where}? That may send what's typed there."
#: The same when the executor passed the text Yuki typed there and has not sent (review r2 L6).
LINE_MAYBE_SEND_KEY_TEXT = "Should I press {key}{where}? That may send \"{text}\", typed there."
LINE_CHECK_FAILED = "Should I go ahead with that? I couldn't check it was what you asked for."
#: The same question naming the step (``describe_step``'s running words for it: "Closing Slack").
LINE_CHECK_FAILED_STEP = "Should I go ahead with {step}? I couldn't check it was what you asked for."
LINE_SEND_SLOT = ("the send leaves its send slot ({slots}) out: fill it with its sending value, or leave it as a "
                  "draft")

#: A near-duplicate send counts within this many seconds (the user's messages to that person, §2.3).
DUPLICATE_WINDOW_S = 3 * 3600.0
#: Yuki's own wording of a message goes with the countdown alone when at least this share of its words (the
#: recipient's names aside, each word once) the user said close together in the turns their quote stands in
#: (:func:`said_share`): a rewording keeps their words and changes one in four, the person ("ask him when he will
#: come" -> "When will you come?": 3 of 4). Less is asked first, showing the text; none at all is a draft (they named
#: only the person, rule 8). A counted share of words, never a reading of them; measured on the 10 Oct requests
#: (failure fix-plan item 11, review r1 M1), where the commit check alone called "Message Hemanth." an asked send, and
#: half (the old bar, counted over every turn) let words that only frame a request ("Can you message Hemanth for me?"
#: -> "Hey, can you hop on for me?": 4 of 7) send Yuki's own message.
SAID_SHARE = 0.75
#: The stretch of the user's words the share is counted in: at most this many times the message's own words (a
#: rewording of what they said to say keeps its words together; common words scattered over a long turn don't add up).
SAID_SPAN = 2
#: The extraction kind (``yuki.memory.extract.model.Extraction.kind``, carried on the Now board's window entries) of a
#: chat: Enter there sends a message, whatever app it is (the generic chat detector's too).
CHAT_KIND = "conversation"

#: P1.5: row 6's code word: the undoable class (module docstring).
UNDOABLE = "undoable"

#: The plain words said for a refusal's code word (``Verdict.why``), for a line the user hears; a code word itself is
#: never said. Missing or empty: nothing worth saying (the planner's own fixes).
PLAIN_WHY: dict[str, str] = {
    "game": "a game is in front",
    "act_off": "Act is switched off for that app",
    "suggest": "your settings say only suggest for that",
    "join_control": "joining is your click",
    "join_effect": "joining is your click",
    "join_skill": "joining is your click",
    "join_unidentified": "I couldn't tell that control from the meeting's Join button",
    "device_key": "that key flips it either way, and I couldn't read whether it was on",
    # sBrainFix (F1d): true whether or not their words asked for the send (the refusal is about the way, never about
    # what they asked: on 10 Oct and in the 16 Oct simulation this was said right after they said yes to the send)
    "undeclared_send": "that step could have sent it outside the chat app's own way",
    "not_capability": "I only send messages through the chat apps' own way",
    "recipient": "I couldn't be sure who it was for",
    "recipient_slot": "I couldn't be sure who it was for",
    "no_draft": "that app has no way to leave a draft",
    "check_failed": "I couldn't check it was what you asked for",
    "error": "I couldn't check it was what you asked for",
}

#: The steps whose input takes the screen even with no focus trait of their own (row 1): an app opened, a link
#: opened, a shell (it may start a window), a window shown or restored. Fixed tool names and operations.
_TAKES_SCREEN_TOOLS = frozenset({"launch_app", "open_url", "run_powershell", "open_yuki"})
_TAKES_SCREEN_WINDOW_OPS = frozenset({"restore", "focus", "maximize", "fullscreen", "place", "focus_back", "undo"})
#: The steps that submit with Enter (typed with it, or pressed as its own step: the way :data:`LINE_TYPED_ENTER`
#: offers). Outside a chat app's window the check's ``send`` their words didn't ask for is a question, never a refusal.
_ENTER_TOOLS = frozenset({"type_text", "press"})
#: A web address typed with Enter in a window known not to be a chat (:func:`navigates`): typing in the app (its Act
#: switch and the typing policy apply) that opens a page (the kind ``open_url`` has); it neither sends nor destroys.
ADDRESS_TRAITS = Traits(("click_type", "open_apps"), "input")


# ---------------------------------------------------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class Call:
    """One act step as it will run: the Dispatcher tool (the plan's verb) and its input, and the code facts of its
    target read just before (``role`` and ``name``: the element as the window's read shows it, never the plan's word
    for it; ``app``: the target window's process). ``facts`` (integ29 S3): what a hand measured for a key on a calling
    site's device (:func:`device_key`): ``toggle_state`` (``on`` / ``off``, that device's in-page control read just
    before), ``toggle_to`` (the state wanted) and ``focus_in_page`` (True: the focus read shows the keyboard inside the
    page document); none of them from the model."""

    tool: str
    input: dict[str, Any]
    index: int = -1
    hwnd: int | None = None
    app: str = ""
    title: str = ""
    role: str = ""
    name: str = ""
    facts: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class Live:
    """Code facts at the moment a step runs.

    ``game``: the game in front (``""`` none, its name, ``None`` unknown: the local read raised; row 1 reads the game
    gate once more, :func:`game_holds`). ``calling``: the windows the Now board lists in ``calling_tabs``. ``typed_pending``:
    windows holding text Yuki typed and has not sent (a set of hwnds, or the executor's own map of hwnd to the text
    waiting there, which a question then shows).
    ``reads``: what this request has read so far (``file``, ``clipboard``, ``memory``: M9). ``join_controls``: the
    (role, name) pairs capabilities declare as their join control, casefolded. ``recent``: the user's own last messages
    to the person the send names (``{"text", "at"}``). ``people_in(text)``: memory's people index, the names of each
    person named in ``text`` (``None``: memory can't say). ``policy``: the autonomy policy reader. ``pages``: the
    address each window shows (the Now board's ``windows[].url`` by hwnd), for a caller that read the board already;
    ``None``: :func:`window_page` reads the board itself when a step needs it (typing with Enter outside a chat app's
    program). ``kinds``: the same for the extraction's kind of each window (``windows[].kind``: ``conversation`` for
    a chat), read by :func:`window_kind`. ``own_undo`` (final-G2 R13, A6; gb's contract K-U): the hand measured this
    key step as the run's own close of what its own shortcut opened, while that very holder (the field it opened, or
    the switcher's own element that takes no text in its place: fix-r33) holds the keyboard (``Hands.own_undo``):
    undoable, row 6 after rows 1-2, never sent to the commit check as a possible send.
    ``calling_tabs`` (morning21 A4): the Now board's ``calling_tabs`` entries by hwnd, for a caller that read the board
    already; ``None``: row 3 reads the board itself when a step needs it (:func:`calling_tab_now`)."""

    game: str | None = ""
    calling: frozenset[int] = frozenset()
    typed_pending: frozenset[int] | Mapping[int, str] = frozenset()
    reads: frozenset[str] = frozenset()
    join_controls: frozenset[tuple[str, str]] = frozenset()
    recent: tuple[Mapping[str, Any], ...] = ()
    people_in: Callable[[str], list[list[str]] | None] | None = None
    policy: Any = None
    now: float = field(default_factory=time.time)
    pages: Mapping[int, str] | None = None
    kinds: Mapping[int, str] | None = None
    own_undo: bool = False
    calling_tabs: Mapping[int, Mapping[str, Any]] | None = None


#: A6 (K-U): the steps a run's own undo can be (a key: the app's own close key for what the run's shortcut opened).
_OWN_UNDO_TOOLS = frozenset({"press", "hotkey"})


def own_undo(call: Call, live: Live) -> bool:
    """A6 (final-G2 R13; gb's contract K-U): this key step is the run's own undo, measured by the hand that runs it
    (:attr:`Live.own_undo`). Identity facts only."""
    return bool(live.own_undo) and call.tool in _OWN_UNDO_TOOLS


@dataclass(frozen=True)
class Verdict:
    """The rule's answer for one step. ``why``: a code word for the logs (content-free), never said to the user
    (:func:`plain_why`). ``line``: what a refusal says (to the user when ``final``, else to the planner, never
    spoken); ``question``: what an ask asks; ``final``: a refusal that ends the run with its line (a game in front, an
    app switched off, a join) instead of a replan; ``countdown``: an allowed send runs the countdown first;
    ``draft_input``: a draft's capability input (its send slot on a non-sending option, and for Yuki's own wording no
    text); ``type_after``: Yuki's own wording, typed without Enter once the chat is open; ``focus``: a refused join's
    window, brought forward."""

    result: str
    row: int
    why: str
    line: str = ""
    question: str = ""
    final: bool = False
    countdown: bool = False
    draft_input: dict[str, Any] | None = None
    type_after: str = ""
    focus: int | None = None

    @property
    def allowed(self) -> bool:
        return self.result == ALLOW


# ---------------------------------------------------------------------------------------------------------------------
# Plain readers (a plan may be a dict, bCore's dataclass, or a strict-tool JSON object)
# ---------------------------------------------------------------------------------------------------------------------


def field_of(obj: Any, key: str, default: Any = None) -> Any:
    """``obj[key]`` for a mapping, else ``obj.key``; ``default`` when missing or ``None``."""
    if obj is None:
        return default
    value = obj.get(key, default) if isinstance(obj, Mapping) else getattr(obj, key, default)
    return default if value is None else value


def words(value: Any) -> str:
    return " ".join(str(value or "").split())


def plan_consent(plan: Any) -> dict[str, Any]:
    """The plan's ``consent`` as a plain dict (``commit`` defaulting to ``none``)."""
    c = field_of(plan, "consent", {})
    commit = str(field_of(c, "commit", "none") or "none")
    return {"commit": commit if commit in COMMITS else "none", "quote": words(field_of(c, "quote", "")),
            "to": words(field_of(c, "to", "")), "text": words(field_of(c, "text", ""))}


def in_words(text: str, user_words: Sequence[str]) -> bool:
    """Whether ``text`` stands, whole words in order, in the user's own words: one entry, or all of this request's
    entries read as one (speech finals split a sentence). ``contacts.span_in``: case and punctuation aside."""
    from yuki.memory.contacts import span_in

    text = words(text)
    entries = [words(w) for w in user_words if words(w)]
    if not text or not entries:
        return False
    return any(span_in(text, w) for w in entries) or span_in(text, " ".join(entries))


def plain_why(why: str) -> str:
    """The plain words for a refusal's code word (:data:`PLAIN_WHY`), for a line the user hears; ``""`` when there is
    nothing worth saying. A code word is never said as it is."""
    return PLAIN_WHY.get(str(why or ""), "")


# ---------------------------------------------------------------------------------------------------------------------
# Whom a send goes to, and what of it the user said (row 4; memory's people index through ``live.people_in``)
# ---------------------------------------------------------------------------------------------------------------------


class _People:
    """Memory's people index for one check (``live.people_in``: each person named in a text, with every name they go
    by; ``None``: memory can't say), each text looked up once."""

    def __init__(self, live: Live) -> None:
        self.live = live
        self.memo: dict[str, list[list[str]] | None] = {}

    def __call__(self, text: str) -> list[list[str]] | None:
        key = words(text)
        if not key:
            return []
        if key not in self.memo:
            try:
                got = self.live.people_in(key) if self.live.people_in is not None else None
            except Exception:  # noqa: BLE001 - memory can't say
                got = None
            self.memo[key] = [[str(n) for n in names] for names in got if names] if isinstance(got, list) else None
        return self.memo[key]


def _fits(people: Sequence[Sequence[str]] | None, name: str) -> list[Sequence[str]]:
    """The people (each a list of their names) one of whose names is ``name``'s words."""
    from yuki.memory.contacts import tokens

    want = tokens(name)
    if not want:
        return []
    return [names for names in people or [] if any(tokens(n) == want for n in names)]


def _joined(user_words: Sequence[str]) -> str:
    return " ".join(words(w) for w in user_words if words(w))


def _recipient_names(to: str, who: str, look: _People, user_words: Sequence[str]) -> list[str]:
    """Every name the recipient goes by: ``to``, the person slot's words, and the names memory's people index keeps for
    the one person ``to`` names (looked up by ``to`` itself and in the user's words)."""
    names = [n for n in (to, who) if words(n)]
    for text in (to, _joined(user_words)):
        fits = _fits(look(text), to)
        if len(fits) == 1:
            names += [str(n) for n in fits[0]]
    return list(dict.fromkeys(words(n) for n in names if words(n)))


def _named_by_user(to: str, who: str, user_words: Sequence[str], look: _People) -> bool:
    """Some of the user's words name the recipient: ``to`` or the person slot's words stand in them, or memory's people
    index finds exactly one person in them who goes by ``to`` (a misheard or transliterated name the index knows:
    ``contacts`` matches heard forms). Only for whether another turn of theirs speaks of the message
    (:func:`_said_elsewhere`); whether the send's own quote names the recipient is :func:`_names_recipient`'s."""
    if any(n and in_words(n, user_words) for n in (to, who)):
        return True
    return len(_fits(look(_joined(user_words)), to)) == 1


@dataclass(frozen=True)
class _Who:
    """Whom a send goes to, for one check: ``to``, the person slot's words (``slot``), every name they go by
    (:func:`_recipient_names`), and memory's people index (``look``)."""

    to: str
    slot: str = ""
    names: tuple[str, ...] = ()
    look: Any = None

    def people(self, text: str) -> list[list[str]] | None:
        return self.look(text) if self.look is not None else None


def _goes_by(person: Sequence[str], name: str) -> bool:
    """One of this person's names (memory's people index) is ``name``'s words."""
    from yuki.memory.contacts import tokens

    want = tokens(name)
    return bool(want) and any(tokens(n) == want for n in person)


def _others(text: str, rec: _Who, body: str = "") -> list[list[str]]:
    """The people memory's index finds in ``text`` other than the recipient (none of their names is ``to``'s words),
    leaving out anyone the message itself names ("Tell Hemanth that Rahul is coming": Rahul is what it says, not whom
    it goes to). ``[]`` too when memory can't say."""
    from yuki.memory.contacts import span_in

    return [p for p in rec.people(text) or []
            if not _goes_by(p, rec.to) and not any(span_in(n, body) for n in p)]


def _cuts(text: str, names: Sequence[str]) -> tuple[list[list[str]], bool, bool, bool]:
    """``text``'s words with every run of words that is one of ``names`` cut out (the longest first): the pieces left
    between them, whether any name was cut, and whether one was cut before the first piece and after the last (a cut
    always lies between two pieces)."""
    from yuki.memory.contacts import tokens

    runs = sorted({tuple(tokens(n)) for n in names if tokens(n)}, key=len, reverse=True)
    ws = tokens(text)
    pieces: list[list[str]] = []
    cur: list[str] = []
    cut = lead = trail = False
    i = 0
    while i < len(ws):
        hit = next((r for r in runs if tuple(ws[i:i + len(r)]) == r), None)
        if hit is None:
            cur.append(ws[i])
            trail = False
            i += 1
            continue
        if cur:
            pieces.append(cur)
            cur = []
        elif not pieces:
            lead = True
        i += len(hit)
        cut = trail = True
    if cur:
        pieces.append(cur)
    return pieces, cut, lead, trail


def _cut_names(text: str, names: Sequence[str]) -> tuple[list[list[str]], bool]:
    """``text``'s words with every name the recipient goes by cut out: the pieces left, and whether any was cut."""
    pieces, cut, _lead, _trail = _cuts(text, names)
    return pieces, cut


#: The most of the user's words a name the plan's quote writes as the recipient's may stand for (review r2 M1 b): a
#: first and last name, or a name speech heard in pieces ("hey meant" for Hemanth; "हेमंत" is two of the people
#: index's words).
NAME_GAP = 4


def _token_spans(text: str) -> list[tuple[str, int, int]]:
    """``contacts.tokens``' words of ``text`` (casefolded runs of letters and digits, everything else separating),
    each with where it stands in ``text`` (start, end), so a run of them is given back as the user said it: a script's
    vowel signs, which split its words there ("हेमंत" is two), stay in what memory's people index is shown."""
    out: list[tuple[str, int, int]] = []
    cur: list[str] = []
    start = end = 0
    for i, ch in enumerate(text):
        for c in ch.casefold():
            if c.isalnum():
                if not cur:
                    start = i
                cur.append(c)
                end = i + 1
            elif cur:
                out.append(("".join(cur), start, end))
                cur = []
    if cur:
        out.append(("".join(cur), start, end))
    return out


def _gap_kind(said: str, rec: _Who) -> str:
    """What the user's own words where a quote writes the recipient's name (``said``, as they said them) say of
    people: ``recipient`` (one of the names the recipient goes by, or memory's index finds only people who go by
    ``to``), ``nobody`` (it finds no one, or can't say), or ``other`` (it finds someone else: the planner's name took
    another person's place)."""
    from yuki.memory.contacts import tokens

    gap = tokens(said)
    if any(tokens(n) == gap for n in rec.names):
        return "recipient"
    found = rec.people(said)
    if not found:
        return "nobody"
    return "recipient" if all(_goes_by(p, rec.to) for p in found) else "other"


def _edge(runs: Sequence[str], rec: _Who) -> tuple[str, int] | None:
    """A name cut at the quote's edge stands for the user's words next to it, one to :data:`NAME_GAP` of them
    (``runs``, shortest first, as they said them): ``("recipient", n)`` when the first ``n`` name the recipient alone,
    ``("nobody", 1)`` when no run names anyone; ``None`` when a run names someone else and none the recipient alone,
    or no words are there (the quote's name stands for nothing they said)."""
    kinds = [_gap_kind(r, rec) for r in runs if r]
    if not kinds:
        return None
    if "recipient" in kinds:
        return "recipient", kinds.index("recipient") + 1
    return ("nobody", 1) if all(k == "nobody" for k in kinds) else None


def _align(h: Sequence[str], pieces: Sequence[Sequence[str]], lead: bool, trail: bool, rec: _Who,
           said: Callable[[int, int], str]) -> tuple[int, int, bool] | None:
    """Where in the user's words ``h`` (one turn, or neighbouring turns, as words; ``said(a, b)``: words ``a`` to
    ``b`` as they said them) the quote's pieces stand in order, each name cut out of the quote standing for one to
    :data:`NAME_GAP` of their words that name the recipient or no one (:func:`_gap_kind`; review r2 M1 b): ``(start,
    end, a cut's words name the recipient)``, a placement naming the recipient preferred; ``None``: nowhere."""
    h = list(h)
    found: list[tuple[int, int, bool]] = []
    if not pieces:
        return None

    def place(k: int, prev_end: int, start: int, named: bool) -> bool:
        """Places piece ``k`` (the first anywhere, a later one one to NAME_GAP words after the one before it); True
        once a placement naming the recipient is found (the search stops)."""
        p = list(pieces[k])
        lo, hi = (1 if lead else 0, len(h)) if k == 0 else (prev_end + 1, prev_end + NAME_GAP)
        for i in range(lo, min(hi, len(h) - len(p)) + 1):
            if h[i:i + len(p)] != p:
                continue
            s, nm = start, named
            if k == 0:
                s = i
                if lead:
                    edge = _edge([said(i - n, i) for n in range(1, min(NAME_GAP, i) + 1)], rec)
                    if edge is None:
                        continue
                    s, nm = i - edge[1], edge[0] == "recipient"
            else:
                kind = _gap_kind(said(prev_end, i), rec)
                if kind == "other":
                    continue
                nm = nm or kind == "recipient"
            end = i + len(p)
            if k + 1 < len(pieces):
                if place(k + 1, end, s, nm):
                    return True
                continue
            stop = end
            if trail:
                edge = _edge([said(end, end + n) for n in range(1, min(NAME_GAP, len(h) - end) + 1)], rec)
                if edge is None:
                    continue
                stop, nm = end + edge[1], nm or edge[0] == "recipient"
            found.append((s, stop, nm))
            if nm:
                return True
        return False

    place(0, 0, 0, False)
    return next((f for f in found if f[2]), found[0] if found else None)


@dataclass(frozen=True)
class _Placed:
    """Where the plan's quote stands in the user's words: the turns (non-empty, in order), the indices of those it
    stands in, the user's own words it stands for (``span``, as they said them: the names they said where the quote
    writes the recipient's), and whether those words name the recipient where the quote's name stands
    (``gap_named``)."""

    entries: list[str]
    held: frozenset[int]
    span: str
    gap_named: bool


def _quote_place(quote: str, user_words: Sequence[str], rec: _Who) -> _Placed | None:
    """Row 4: where the plan's quote stands in the user's words (one turn, or the fewest neighbouring turns: speech
    finals split a sentence), whole words in order: as it is, or with each name the recipient goes by (``rec.names``)
    standing for the user's own words there (speech writes a name the way it heard it, "hey meant" or "हेमंत"; the
    planner writes the person's name), which must name the recipient or no one, never someone else (:func:`_align`).
    A quote that is only a name does not stand. ``None``: it stands nowhere."""
    from yuki.memory.contacts import tokens

    entries = [words(w) for w in user_words if words(w)]
    q = words(quote)
    if not q or not entries:
        return None
    if in_words(q, entries):
        pieces, lead, trail = [tokens(q)], False, False
    else:
        pieces, cut, lead, trail = _cuts(q, rec.names)
        if not cut or not pieces:
            return None
    for size in range(1, len(entries) + 1):
        hits: list[tuple[int, tuple[int, int, bool], str]] = []
        for i in range(len(entries) - size + 1):
            text = " ".join(entries[i:i + size])
            spans = _token_spans(text)

            def said(a: int, b: int, text: str = text, spans: list = spans) -> str:
                return text[spans[a][1]:spans[b - 1][2]] if 0 <= a < b <= len(spans) else ""

            at = _align([t for t, _s, _e in spans], pieces, lead, trail, rec, said)
            if at is not None:
                hits.append((i, at, said(at[0], at[1])))
        if hits:
            held = frozenset(j for i, _at, _span in hits for j in range(i, i + size))
            _i, (_s, _e, named), span = next((x for x in hits if x[1][2]), hits[0])
            return _Placed(entries, held, span, named)
    return None


def _names_recipient(placed: _Placed, rec: _Who) -> bool:
    """Row 4 (review r2 M1 a): the user's words the quote stands for name the recipient: memory's people index finds
    exactly one person there who goes by ``to``; or, the index finding no one who does, ``to`` or the person slot's
    words stand there (a group or channel by its name), or the words where the quote writes the name name them
    (:func:`_gap_kind`). Two people who go by that name: not named (asked, never guessed). A name said only outside
    the quote is not named either: asked. Another person named in the quote too does not unname the recipient: the
    index takes words this user says to Yuki all the time for people ("bro", 10 Oct), so that would ask about their
    ordinary requests; where the message's words came from is measured in the quote alone then (:func:`_send`)."""
    from yuki.memory.contacts import span_in

    span = placed.span
    if not span:
        return False
    fits = _fits(rec.people(span), rec.to)
    if fits:
        return len(fits) == 1
    return placed.gap_named or any(n and span_in(n, span) for n in (rec.to, rec.slot))


def _named_in_turn(placed: _Placed, rec: _Who) -> bool:
    """jBrain (fMsg B2; 18 Oct live A, msg_hemanth_misheard: "message hey meant on this card, ask him when he's gonna
    join the VC" was asked with ``recipient_unsaid``: the planner's quote stood after the name): the rest of the very
    turn the quote stands in (the held turns, the quote's own words cut out) names the recipient, by the same rule as
    :func:`_names_recipient`: memory's people index finds exactly one person there who goes by ``to``, or, finding no
    one who does, ``to`` or the person slot's words stand there as whole words. The index matches names and the heard
    forms the user confirmed, never a guess by sound, so a sound-alike still never names anyone for a send. A name said
    only in another turn is still not named (asked). Code facts only."""
    from yuki.memory.contacts import span_in

    span = placed.span
    if not span:
        return False
    rest = ""
    for run in _runs_of(placed.entries, placed.held):
        at = run.find(span)
        if at >= 0:
            rest = f"{run[:at]} {run[at + len(span):]}"
            break
    rest = words(rest)
    if not rest:
        return False
    fits = _fits(rec.people(rest), rec.to)
    if fits:
        return len(fits) == 1
    return any(n and span_in(n, rest) for n in (rec.to, rec.slot))


def quote_in_words(quote: str, user_words: Sequence[str], names: Sequence[str] = (), *, to: str = "",
                   people_in: Callable[[str], list[list[str]] | None] | None = None) -> bool:
    """Row 4: the plan's quote stands in the user's words, as it is or with each name the recipient goes by
    (``names``, for ``to``) standing for their own words there, which name the recipient or no one
    (:func:`_quote_place`; ``people_in``: memory's people index)."""
    look = _People(Live(people_in=people_in))
    return _quote_place(quote, user_words, _Who(to=words(to), names=tuple(names), look=look)) is not None


def _quote_turn_indices(quote: str, user_words: Sequence[str], names: Sequence[str] = (),
                        rec: _Who | None = None) -> tuple[list[str], set[int]]:
    """The user's turns (non-empty, in order) and the indices of those the quote stands in (:func:`_quote_place`)."""
    rec = rec if rec is not None else _Who(to="", names=tuple(names))
    placed = _quote_place(quote, user_words, rec)
    if placed is None:
        return [words(w) for w in user_words if words(w)], set()
    return placed.entries, set(placed.held)


def _runs_of(entries: Sequence[str], held: Sequence[int] | frozenset[int]) -> list[str]:
    """The held turns, each run of neighbouring turns joined."""
    runs: list[str] = []
    cur: list[str] = []
    last = None
    for i in sorted(held):
        if cur and last is not None and i != last + 1:
            runs.append(" ".join(cur))
            cur = []
        cur.append(entries[i])
        last = i
    if cur:
        runs.append(" ".join(cur))
    return runs


def quote_turns(quote: str, user_words: Sequence[str], names: Sequence[str] = ()) -> list[str]:
    """The turns of the user's words the plan's quote stands in, each run of neighbouring turns joined: where row 4
    measures what of a message came from them (:func:`said_share`), never every turn of the exchange (words they said
    to Yuki for something else, "Yuki, are you there?", are not what to send). ``[]``: it stands nowhere."""
    entries, held = _quote_turn_indices(quote, user_words, names)
    return _runs_of(entries, held)


def said_share(text: str, heard: str | Sequence[str], names: Sequence[str] = ()) -> float:
    """The share (0-1) of a message's words (the recipient's names aside, each word once) that the user said close
    together: the most of them found within one stretch of their words at most :data:`SAID_SPAN` times the message's
    own words long, inside one run of ``heard`` (the turns their quote stands in, :func:`quote_turns`) and between the
    recipient's names. A rewording of what they said to say keeps its words together ("ask him when he will come" ->
    "When will you come?": 3 of 4), while words that only frame the request to Yuki on either side of the name ("Can
    you message Hemanth for me?") or common words scattered over a long turn don't add up. ``0`` when the text has no
    word of its own. A count of words, never a reading of them."""
    from yuki.memory.contacts import tokens

    name_words = {t for n in names for t in tokens(n)}
    own = list(dict.fromkeys(t for t in tokens(text) if t not in name_words))
    if not own:
        return 0.0
    want = set(own)
    span = max(1, SAID_SPAN * len(own))
    best = 0
    for run in ([heard] if isinstance(heard, str) else list(heard or [])):
        pieces, _cut = _cut_names(str(run or ""), names)
        for piece in pieces:
            for i in range(max(1, len(piece) - span + 1)):
                best = max(best, len(want.intersection(piece[i:i + span])))
    return best / len(own)


def spoken_name(to: str, people: Callable[[str], list[list[str]] | None] | None) -> str:
    """pBrain (B35, bRound2: 'Send "..." to हेमंत?' and "Sending to हेमंत" when the planner wrote ``consent.to`` in
    Devanagari): the name a line says or shows for the person ``to`` names: memory's own name for them (the first of
    the names its people index keeps, ``contacts.Contact.names``) when the index finds exactly one person in ``to`` and
    ``to`` is that person's name (one of their names, a learned heard form, or a transliteration no name of theirs
    stands beside), else ``to`` as given (nobody, two people, more words than their name, memory can't say). Only what
    is said: whom a message goes to is never changed here. Memory's data, never a word list."""
    from yuki.memory.contacts import span_in, tokens

    given = words(to)
    if not given or people is None:
        return given
    try:
        found = people(given)
    except Exception:  # noqa: BLE001 - memory can't say: the name as given
        return given
    if not isinstance(found, list) or len(found) != 1 or not found[0]:
        return given
    names = [words(str(n)) for n in found[0] if words(str(n))]
    if not names:
        return given
    if any(tokens(n) == tokens(given) for n in names) or not any(span_in(n, given) for n in names):
        return names[0]
    return given


def ask_send_question(text: str, to: str) -> str:
    """The question for a send the user is asked about: what goes to whom (the text cut to :data:`ASK_TEXT_CHARS`)."""
    body = words(text)
    if len(body) > ASK_TEXT_CHARS:
        body = body[:ASK_TEXT_CHARS - 1].rstrip() + "…"
    return LINE_ASK_SEND.format(text=body, to=words(to)) if body and words(to) else LINE_ASK_SEND_PLAIN


def _clip_text(text: Any) -> str:
    body = words(text)
    return body[:ASK_TEXT_CHARS - 1].rstrip() + "…" if len(body) > ASK_TEXT_CHARS else body


def waiting_text(hwnd: int | None, live: Live | None) -> str:
    """The text Yuki typed in that window and has not sent, when the caller passed the executor's map
    (``live.typed_pending`` as hwnd to text); ``""`` when none waits or only the window is known."""
    pending = live.typed_pending if live is not None else None
    key = _hwnd_key(hwnd)
    if key is None or not isinstance(pending, Mapping):
        return ""
    try:
        return words(pending.get(key) or "")
    except Exception:  # noqa: BLE001
        return ""


def maybe_send_question(step: Call, live: Live | None = None) -> str:
    """The question for Enter typed or pressed in a window known not to be a chat, which the commit check calls a send
    (row 5): what is typed (cut to :data:`ASK_TEXT_CHARS`) and in which app, or the key and the text of Yuki's waiting
    there that it may send (:func:`waiting_text`; review r2 L6). Code words and Yuki's own typing only (the tool's
    input, the window's program, what Yuki typed), so a yes is a yes to that very step."""
    app = util.app_name(step.app) if words(step.app) else ""
    where = f" in {app}" if app else ""
    inp = step.input or {}
    if step.tool == "type_text":
        body = _clip_text(inp.get("text"))
        if body:
            return LINE_MAYBE_SEND_TYPED.format(text=body, where=where)
    key = words(inp.get("key")) if step.tool == "press" else ""
    key = (key[:1].upper() + key[1:]) if key else "Enter"
    waiting = _clip_text(waiting_text(step.hwnd, live))
    if waiting:
        return LINE_MAYBE_SEND_KEY_TEXT.format(key=key, where=where, text=waiting)
    return LINE_MAYBE_SEND_KEY.format(key=key, where=where)


# ---------------------------------------------------------------------------------------------------------------------
# Skills (catalog data through util's shim)
# ---------------------------------------------------------------------------------------------------------------------


def skill_input(call: Call, plan: Any = None) -> tuple[str, dict[str, Any]]:
    """``(skill key, slots)`` of a ``run_skill`` call: its own ``skill`` and ``slots``, the key falling back to the
    plan's ``route = "skill:<id>"``."""
    key = words(call.input.get("skill"))
    if not key:
        route = words(field_of(plan, "route", ""))
        key = route.split(":", 1)[1].strip() if route.startswith("skill:") else ""
    slots = call.input.get("slots") if isinstance(call.input.get("slots"), Mapping) else {}
    return key, dict(slots)


def _templated_slot(skill: Any, arg: str) -> str:
    """The slot a skill's checks template into a probe argument (``text = "{text}"``, ``person = "{person}"``): the
    capability's own data names which slot carries what the message says and whom it goes to. ``""``: none."""
    from yuki.skills.connection import template_names

    for route in getattr(skill, "routes", ()) or ():
        for check in (getattr(route, "done", None), getattr(route, "recheck", None)):
            value = (getattr(check, "args", None) or {}).get(arg) if check is not None else None
            names = template_names(value) if isinstance(value, str) else []
            if len(names) == 1 and value.strip() == "{" + names[0] + "}":
                return names[0].split(".", 1)[0]
    return ""


def text_slot(skill: Any) -> str:
    """The slot of a sending capability that holds the text it sends (its checks' ``text`` argument). A sending
    capability whose check reads its own run instead (a driver probe with no arguments: ``app.way``, the user's own
    ways, kWays merge note 2) types its slot named ``text`` (its data's own slot name): that one, so the countdown's
    text, the send grant (``uia_route.route_way`` matches it) and a draft keep the same words."""
    named = _templated_slot(skill, "text")
    if named or not getattr(skill, "sends_when", ()):
        return named
    slot = skill.slot("text") if callable(getattr(skill, "slot", None)) else None
    return "text" if slot is not None and str(getattr(slot, "type", "") or "") == "text" else ""


def person_slot(skill: Any) -> str:
    """The slot of a sending capability that names whom it goes to (its checks' ``person`` argument)."""
    return _templated_slot(skill, "person")


def skill_sends(skill: Any, slots: Mapping[str, Any], *, partial: bool = True) -> bool:
    """Whether running ``skill`` with these slots sends to people (its own ``sends`` / ``sends_when`` data). With
    ``partial`` (the default: is this step a send?) a slot left out counts as sending, the resolver may fill it so;
    without it (does this step send for sure?) every ``sends_when`` slot must hold a sending value. Never raises:
    unreadable data counts as a send when ``partial``, as not sending for sure otherwise."""
    if skill is None:
        return False
    try:
        from yuki.skills.connection import sends_with

        return bool(sends_with(skill, slots, partial=partial))
    except Exception:  # noqa: BLE001 - fail closed both ways
        return bool(getattr(skill, "sends", False)) if partial else False


def send_slots(skill: Any) -> list[str]:
    """The slots a capability's ``sends_when`` data names (``send``): code words for a mismatch line."""
    return [str(c.get("slot")) for c in getattr(skill, "sends_when", ()) or () if isinstance(c, Mapping)
            and c.get("slot")]


def skill_joins(skill: Any) -> bool:
    """Whether a skill's end is joining a call (its declared ``end``, D1's moment vocabulary ``call_join``)."""
    return str(getattr(skill, "end", "") or "") == "call_join"


def skill_commit(skill: Any) -> str:
    """The skill's commit word (phase L's D24 ``commit``: ``none`` < ``words`` < ``user``); ``""`` before L ships it."""
    return str(getattr(skill, "commit", "") or "")


def draft_slots(skill: Any, slots: Mapping[str, Any], *, keep_text: bool) -> dict[str, Any] | None:
    """The capability's slots with every ``sends_when`` slot set to one of its options that does not send (its own
    data: ``send = "draft"``), and without the text when ``keep_text`` is false (Yuki's own wording: the chat opens
    empty). ``None`` when the data offers no non-sending option (nothing to draft with)."""
    out = dict(slots)
    for cond in getattr(skill, "sends_when", ()) or ():
        name = str(cond.get("slot") or "")
        if not name or "in" not in cond:
            return None
        sending = {str(x) for x in cond.get("in") or []}
        slot = skill.slot(name) if callable(getattr(skill, "slot", None)) else None
        options = [str(o) for o in (getattr(slot, "options", ()) or ()) if str(o) not in sending]
        if not options:
            return None
        default = getattr(slot, "default", None)
        out[name] = str(default) if default is not None and str(default) in options else options[0]
    if not getattr(skill, "sends_when", ()):
        return None   # a skill that always sends has no draft form
    if not keep_text:
        name = text_slot(skill)
        if not name:
            return None
        out.pop(name, None)
    return out if not skill_sends(skill, out) else None


# ---------------------------------------------------------------------------------------------------------------------
# Code facts of one step
# ---------------------------------------------------------------------------------------------------------------------


def traits_of(call: Call, live: Live) -> Traits:
    """The step's traits (tool metadata with its target's facts: the clicked role, Yuki's typed text waiting in that
    window, a web fetch after a read in this request, M9). ``call.tool`` is the Dispatcher name that runs
    (``fetch_url`` or ``read_file`` for a ``fetch`` step)."""
    return call_traits(call.tool, call.input, role=call.role or None,
                       typed_pending=bool(call.hwnd) and int(call.hwnd) in live.typed_pending,
                       files_read=bool(live.reads))


def _chat_data() -> tuple[frozenset[str], frozenset[str]] | None:
    """What chat apps' connection data says identifies them (``[chat]`` of each, read through ``yuki.skills.chat``, as
    :func:`join_controls` reads its labels): their programs (``images`` / ``processes``, by memory's app key) and the
    web hosts they serve chats on (``services`` that are hosts: ``discord.com``, ``web.whatsapp.com``). ``None``: the
    data can't be read or lists no program. Never raises."""
    try:
        from yuki.core.appnames import app_key
        from yuki.skills.chat import app_data, chat_apps

        programs: set[str] = set()
        hosts: set[str] = set()
        for app in chat_apps():
            data = app_data(app)
            programs.update(app_key(str(p)) for p in [*(data.get("images") or []), *(data.get("processes") or [])])
            hosts.update(str(s).strip().casefold() for s in data.get("services") or [] if "." in str(s))
    except Exception:  # noqa: BLE001 - no data: unknown (counted as a chat by the caller)
        return None
    programs.discard("")
    hosts.discard("")
    return (frozenset(programs), frozenset(hosts)) if programs else None


#: A board read for :func:`window_page` and :func:`window_kind` serves the rule's other questions about the same step
#: for this long (one check asks several times: the step's traits, may-commit, the send row).
PAGE_TTL_S = 1.0
_board_read: dict[int, tuple[float, str, str]] = {}


def _hwnd_key(hwnd: Any) -> int | None:
    if not hwnd or isinstance(hwnd, bool):
        return None
    try:
        return int(hwnd)
    except (TypeError, ValueError):
        return None


def _board_window(key: int) -> tuple[str, str]:
    """The Now board's entry for one window, read now (50 ms at most, kept :data:`PAGE_TTL_S`): its address
    (``windows[].url``) and the extraction's kind (``windows[].kind``). ``("", "")``: not known. Never raises."""
    now = time.monotonic()
    hit = _board_read.get(key)
    if hit is not None and now - hit[0] <= PAGE_TTL_S:
        return hit[1], hit[2]
    url = kind = ""
    try:
        from yuki.memory.now import shared_client

        client = shared_client()
        board = client.now(wait_s=0.05) if client is not None else None
        for w in (board or {}).get("windows") or []:
            if isinstance(w, Mapping) and str(w.get("hwnd")) == str(key):
                url, kind = words(w.get("url") or ""), words(w.get("kind") or "")
                break
    except Exception:  # noqa: BLE001 - no board: not known
        url = kind = ""
    if len(_board_read) > 64:
        _board_read.clear()
    _board_read[key] = (now, url, kind)
    return url, kind


def window_page(hwnd: int | None, live: Live | None = None) -> str:
    """The address the window shows: ``live.pages`` when the caller read the board already, else the Now board's entry
    for that window (:func:`_board_window`). ``""``: not known (no board, a window with no page, a page privacy keeps
    out). Never raises."""
    key = _hwnd_key(hwnd)
    if key is None:
        return ""
    if live is not None and live.pages is not None:
        try:
            return words(live.pages.get(key) or "")
        except Exception:  # noqa: BLE001
            return ""
    return _board_window(key)[0]


def window_kind(hwnd: int | None, live: Live | None = None) -> str:
    """The kind memory's extraction gave the window's last read (``conversation``, ``page``, ``document``, ...):
    ``live.kinds`` when the caller read the board already, else the Now board's entry (:func:`_board_window`). ``""``:
    not known. Never raises."""
    key = _hwnd_key(hwnd)
    if key is None:
        return ""
    if live is not None and live.kinds is not None:
        try:
            return words(live.kinds.get(key) or "")
        except Exception:  # noqa: BLE001
            return ""
    return _board_window(key)[1]


def _host(url: str) -> str:
    """An address's host, casefolded (a fixed machine format, parsed); ``""`` when it has none."""
    from urllib.parse import urlparse

    try:
        return (urlparse(words(url)).hostname or "").casefold().rstrip(".")
    except ValueError:
        return ""


def _capture_kind(program: str, url: str) -> str:
    """The kind memory's capture profiles give this window (``yuki.memory.extract.profiles``: data, by the window's
    program or its page's host and path; ``conversation`` for a chat such as Teams, Telegram or Messenger), ``""``
    when none fits or the data can't be read. Never raises."""
    try:
        from yuki.memory.extract import profiles

        prof = profiles.current().for_window(process=program.casefold(), url=url or None)
        return str(getattr(prof, "kind", "") or "") if prof is not None else ""
    except Exception:  # noqa: BLE001 - no profile data: not known
        return ""


def chat_window(call: Call, live: Live | None = None) -> bool | None:
    """Whether the step's window is a chat's, where Enter sends a message. Data only, never names: its program
    (``call.app``, the window's process) among the programs a chat app's connection data lists; the page it shows
    (:func:`window_page`) on one of a chat app's web hosts or below one (Discord or WhatsApp in a browser); the Now
    board's read of that window a ``conversation`` (:func:`window_kind`: the extraction's own kind, its generic chat
    detector's too, so a chat no data names, Signal or Element, still counts); or memory's capture profiles calling
    that program or page a ``conversation`` (Teams, Telegram, Messenger, LinkedIn or Instagram messages: chats with no
    message capability). ``None``: the program is unknown, or the chat apps' data can't be read (the caller counts
    that as a chat, fail closed)."""
    import ntpath

    from yuki.core.appnames import app_key

    data = _chat_data()
    if data is None:
        return None
    programs, hosts = data
    program = ntpath.basename(words(call.app))
    key = app_key(program)
    if key and key in programs:
        return True
    url = window_page(call.hwnd, live) if call.hwnd else ""
    host = _host(url)
    if host and any(host == h or host.endswith("." + h) for h in hosts):
        return True
    if call.hwnd and window_kind(call.hwnd, live) == CHAT_KIND:
        return True
    if _capture_kind(program, url) == CHAT_KIND:
        return True
    return False if key else None


def sends_now(call: Call, live: Live, traits: Traits | None = None) -> bool:
    """Row 4's code fact: the step sends something to someone for certain. A click while Yuki's typed text waits in
    that window (``tools.sends_for_certain``) does. Typing with Enter or a line break does only where Enter sends a
    message: in a chat app's own window or page (:func:`chat_window`; a program that can't be told counts as a chat,
    fail closed), or in a window where Yuki's typed text waits (Enter sends that text too, and the commit check is not
    told of it for typing). Anywhere else (an address bar, a search box, a command field) typing with ``press_enter``
    is a may-commit step like a key: the commit check, shown "and presses Enter", decides it (row 5), and its ``send``
    (a chat the data doesn't know) is asked (row 5) or comes back to row 4. A line break in the text is an Enter the
    check is never shown (it sees the text as one line), so it sends for certain outside a chat's window too, unless
    the step loads one web address in a browser (:func:`navigates`; review r1 M2). A document is no exception: an
    editor's read is a ``document`` while its integrated terminal or chat panel, where a line break runs or sends, has
    the focus (review r2 L4); the planner types each line and presses Enter as its own step, which is checked.
    ``tools.sends_for_certain`` itself is left as it is: the old loop shares it."""
    traits = traits if traits is not None else traits_of(call, live)
    if not sends_for_certain(traits):
        return False
    if call.tool != "type_text":
        return True
    if call.hwnd and int(call.hwnd) in live.typed_pending:
        return True
    if chat_window(call, live) is not False:
        return True
    if _line_break_submit(call):
        return not navigates(call, live)
    return False


def _line_break_submit(call: Call) -> bool:
    """A ``type_text`` whose text holds a line break (with ``press_enter`` or without): the commit check's step line
    shows the text as one line, so it says nothing of that Enter (only of ``press_enter``'s own, at the end)."""
    text = (call.input or {}).get("text")
    return call.tool == "type_text" and isinstance(text, str) and ("\r" in text or "\n" in text)


def typed_address(call: Call) -> str:
    """The one web address a ``type_text`` step types: an http(s) address with a host and nothing else (a trailing line
    break aside), else ``""``. A fixed machine format, parsed as ``tools`` parses ``open_url``'s address, never
    words."""
    if call.tool != "type_text":
        return ""
    text = str((call.input or {}).get("text") or "").strip()
    if not text or any(ch.isspace() for ch in text):
        return ""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(text)
    except ValueError:
        return ""
    return text if parsed.scheme.casefold() in WEB_SCHEMES and parsed.netloc else ""


def navigates(call: Call, live: Live) -> bool:
    """Typing one web address with Enter (:func:`typed_address`) where Enter can only load it: a window showing a web
    page (the Now board holds an http(s) address for it: a browser, never a terminal where Enter runs what was typed)
    that is known not to be a chat (:func:`chat_window` false: its program is known and no chat's, and its page is no
    chat's), with no text of Yuki's waiting there and no call in it (``live.calling``: loading over a call's tab would
    leave the call). That opens the address through the browser's own address bar, as ``open_url`` would (the path that
    worked in Arc on 5 Oct: a new tab, the command bar, Enter): typing that opens a page (:data:`ADDRESS_TRAITS`), not a
    send (row 4), and not the commit check's to turn into one (10 Oct: it called that very step a send once in two
    runs)."""
    if not typed_address(call) or not sends_for_certain(traits_of(call, live)) or not call.hwnd:
        return False
    if int(call.hwnd) in live.typed_pending or int(call.hwnd) in live.calling:
        return False
    try:
        from urllib.parse import urlparse

        shows_page = urlparse(window_page(call.hwnd, live)).scheme.casefold() in WEB_SCHEMES
    except ValueError:
        shows_page = False
    # a window showing a web page (the Now board's address for it): a browser, never a terminal or an editor, where
    # Enter would run or keep what was typed
    return shows_page and chat_window(call, live) is False


def step_traits(call: Call, live: Live) -> Traits:
    """The traits the rule judges a step by: :func:`traits_of`, but a navigating step's are :data:`ADDRESS_TRAITS`
    (typing in the app, so its Act switch and the typing policy still apply, that opens a page: no send, nothing that
    destroys)."""
    return ADDRESS_TRAITS if navigates(call, live) else traits_of(call, live)


def is_join_control(call: Call, live: Live) -> bool:
    """A click on a capability's declared join control, by (role, name) identity."""
    if call.tool != "click" or not call.name:
        return False
    return (call.role.casefold(), words(call.name).casefold()) in live.join_controls


#: morning21 (A4): the keys that press whatever control holds the keyboard (a focused Join button joins on either),
#: pyautogui's names and its aliases for them. Machine key names, never the user's words.
ACTIVATION_KEYS: frozenset[str] = frozenset({"enter", "return", "space", "spacebar"})


#: integ29 (S7): a key name the input layer can't name (:func:`_key_names`); counted as one that presses (fail closed).
UNKNOWN_KEY = "?"


def _fold_key(key: Any) -> str:
    """integ29 (S7): one key name as the press will send it: the input layer's own fold
    (``yuki.actions.input.normalize_key``: case, ``_``, ``-`` and spaces, its aliases such as ``spacebar``), casefolded;
    :data:`UNKNOWN_KEY` when it names no key."""
    if str(key) == " ":
        return "space"
    try:
        from yuki.actions.input import normalize_key

        got = normalize_key(str(key))
    except Exception:  # noqa: BLE001 - no input layer here: the old fold, with _ and - too
        got = "".join(str(key).replace("_", "").replace("-", "").split()) or None
    return str(got).casefold() if got else UNKNOWN_KEY


def _key_names(call: Call) -> list[str]:
    inp = call.input or {}
    if call.tool == "press":
        raw = [inp.get("key")]
    elif call.tool in ("hotkey", "set_slider"):
        raw = list(inp.get("keys") or []) if isinstance(inp.get("keys"), list) else []
    else:
        return []
    return [_fold_key(k) for k in raw if k is not None]


def activates(call: Call) -> bool:
    """morning21 (A4): the step presses the control that holds the keyboard: Enter or Space (alone or in a combination),
    or typing that submits (``press_enter``, a line break). Tool metadata and key names only (integ29 S7: folded as the
    press folds them; a name no key answers to counts as pressing, fail closed)."""
    if call.tool == "type_text":
        return _line_break_submit(call) or (call.input or {}).get("press_enter") is True
    return any(k in ACTIVATION_KEYS or k == UNKNOWN_KEY for k in _key_names(call))


def join_target(call: Call, live: Live) -> bool:
    """morning21 (A4 i): the step's target is a declared join control (:attr:`Live.join_controls`, by (role, name)
    identity; a name read with no role matches a control declared by that name): a click on it, or a key or a submit
    that presses it while it holds the keyboard (the focus read the step carries). Review M2, integ29 S5: a calling
    site's join controls ("Join now", "Rejoin", Meet's "Present": names ordinary pages draw too) only on that site's
    page shown now (:func:`calling_tab_now`; unknown counts as shown), that site's own when the board knows its
    service (:func:`page_join_controls`)."""
    name = words(call.name).casefold()
    if not name or not (call.tool == "click" or activates(call)):
        return False
    role = words(call.role).casefold()

    def declared(controls: frozenset[tuple[str, str]]) -> bool:
        return (role, name) in controls if role else any(n == name for _r, n in controls)

    if declared(live.join_controls):
        return True
    if not declared(page_join_controls()) or not calling_tab_now(call.hwnd, live):
        return False
    return declared(page_join_controls(calling_service(call.hwnd, live)))


#: review M2: every calling site's join controls, read once per process (data files ship with the build); integ29 S5:
#: and each site's own, by its service (casefolded).
_page_joins: frozenset[tuple[str, str]] | None = None
_page_joins_of: dict[str, frozenset[tuple[str, str]]] = {}


def _site_joins(site: Mapping[str, Any]) -> frozenset[tuple[str, str]]:
    """One calling site's ``[call.labels.<language>]`` ``join`` and ``join_in_page`` pairs, casefolded."""
    out: set[tuple[str, str]] = set()
    for table in (site.get("labels") or {}).values():
        for name in ("join", "join_in_page"):
            for pair in (table.get(name) if isinstance(table, Mapping) else None) or []:
                if isinstance(pair, (list, tuple)) and len(pair) >= 2 and words(pair[1]):
                    out.add((words(pair[0]).casefold(), words(pair[1]).casefold()))
    return frozenset(out)


def page_join_controls(service: Any = "") -> frozenset[tuple[str, str]]:
    """Review M2, integ29 S5: the calling sites' join controls (``[call.labels.<language>] join`` and ``join_in_page``,
    :func:`yuki.memory.now.site_join_controls`), casefolded: a join only on that site's page shown now
    (:func:`join_target`), never on an ordinary page that draws "Join now". ``service``: the window's calling service
    (the board's): that site's own; unknown, or a site declaring none: every calling site's (fail closed). Data, read
    once; never raises."""
    global _page_joins
    want = words(service).casefold()
    if want:
        got = _page_joins_of.get(want)
        if got is None:
            try:
                from yuki.memory.now import calling_site

                got = _site_joins(calling_site(want))
            except Exception:  # noqa: BLE001
                got = frozenset()
            if len(_page_joins_of) > 32:
                _page_joins_of.clear()
            _page_joins_of[want] = got
        if got:
            return got
    if _page_joins is None:
        try:
            from yuki.memory.now import site_join_controls

            _page_joins = frozenset(site_join_controls()) | frozenset(site_join_controls(in_page=True))
        except Exception:  # noqa: BLE001 - no data: the other row-3 tests still hold
            return frozenset()
    return _page_joins


def device_key(call: Call, live: Live) -> str:
    """integ29 (S3): the device a key step flips by a calling site's own declared keys (``[call.keys]``, each
    list-valued entry: Meet's ``microphone = ["ctrl", "d"]``), folded as the press folds them, in a window with a
    calling tab (:attr:`Live.calling`): the data's name for it (``microphone``), else ``""``. The window's service's
    keys when the board knows it, else every calling site's (fail closed). Site data and key names only, never the
    user's words."""
    if call.tool not in ("hotkey", "press") or not call.hwnd:
        return ""
    key = _hwnd_key(call.hwnd)
    if key is None or key not in live.calling:
        return ""
    names = sorted(_key_names(call))
    if not names:
        return ""
    try:
        from yuki.memory.now import calling_site, calling_sites

        site = calling_site(calling_service(call.hwnd, live))
        tables = [site.get("keys")] if site.get("keys") else [s.get("keys") for s in calling_sites()]
    except Exception:  # noqa: BLE001 - no data: rows 3-6 as before
        return ""
    for data in tables:
        for what, combo in (data.items() if isinstance(data, Mapping) else ()):
            if isinstance(combo, (list, tuple)) and combo and sorted(_fold_key(k) for k in combo) == names:
                return str(what)
    return ""


def device_state_read(call: Call) -> bool:
    """integ29 (S3): the hand measured what a device key needs (:attr:`Call.facts`): that device's in-page state just
    before, a wanted state it is not in already, and the keyboard inside the page document."""
    facts = call.facts or {}
    state = str(facts.get("toggle_state") or "").casefold()
    want = str(facts.get("toggle_to") or (call.input or {}).get("toggle_to") or "").casefold()
    return state in ("on", "off") and want in ("on", "off") and state != want and facts.get("focus_in_page") is True


def unidentified_target(call: Call) -> bool:
    """morning21 (A4 iv): what the step acts on can't be told from a Join: a click on no control the read names (a
    point), or a key or a submit that presses whatever holds the keyboard with no focus read naming it."""
    if call.tool == "click":
        return not words(call.name)
    return activates(call) and not words(call.name)


#: The Now board's calling tabs a row-3 check read itself: ``{hwnd: (monotonic, entry or None)}``, kept
#: :data:`PAGE_TTL_S`.
_calling_read: dict[int, tuple[float, Mapping[str, Any] | None]] = {}


def _calling_entry(key: int, live: Live) -> Mapping[str, Any] | None:
    """The Now board's ``calling_tabs`` entry for one window: the caller's (:attr:`Live.calling_tabs`), else read now
    (50 ms at most, kept briefly). ``None``: not known."""
    if live.calling_tabs is not None:
        try:
            got = live.calling_tabs.get(key) or live.calling_tabs.get(str(key))  # type: ignore[call-overload]
            return got if isinstance(got, Mapping) else None
        except Exception:  # noqa: BLE001
            return None
    now = time.monotonic()
    hit = _calling_read.get(key)
    if hit is not None and now - hit[0] <= PAGE_TTL_S:
        return hit[1]
    entry: Mapping[str, Any] | None = None
    try:
        from yuki.memory.now import shared_client

        client = shared_client()
        board = client.now(wait_s=0.05) if client is not None else None
        got = ((board or {}).get("calling_tabs") or {}).get(str(key))
        entry = got if isinstance(got, Mapping) else None
    except Exception:  # noqa: BLE001 - no board: not known
        entry = None
    if len(_calling_read) > 64:
        _calling_read.clear()
    _calling_read[key] = (now, entry)
    return entry


def _shown_tabs(hwnd: int) -> list[str] | None:
    """The RuntimeIds of the tabs ``hwnd``'s strip shows selected now (every group; read live, bounded), ``None``: the
    strip couldn't be read."""
    try:
        from yuki.skills.web_route import _selected_tabs

        return _selected_tabs(int(hwnd))
    except Exception:  # noqa: BLE001
        return None


def calling_tab_now(hwnd: Any, live: Live) -> bool:
    """morning21 (A4 iv, A6): the window ``hwnd`` shows its calling site's page now: it is in :attr:`Live.calling`
    and either the window's strip, read again now, shows the board's calling tab (by RuntimeId) selected, or the
    board's last sighting of the window says it is showing (``selected`` not False). The strip read only confirms
    "showing", never overrides the board: the tab id is refreshed at most every TAB_READ_GAP_S, so a newer meeting
    tab in the same window would read as "not this tab" (integ29 S1/F1). Unknown is shown (fail closed). Never
    raises."""
    key = _hwnd_key(hwnd)
    if key is None or key not in live.calling:
        return False
    try:
        entry = _calling_entry(key, live)
        if entry is None:
            return True
        tab = words(entry.get("tab_id"))
        if tab:
            shown = _shown_tabs(key)
            if shown and tab in shown:
                return True
        return entry.get("selected") is not False
    except Exception:  # noqa: BLE001
        return True


def calling_service(hwnd: Any, live: Live) -> str:
    """The calling site's service in window ``hwnd`` (the board's entry), ``""`` when not known."""
    key = _hwnd_key(hwnd)
    if key is None:
        return ""
    try:
        return words((_calling_entry(key, live) or {}).get("service"))
    except Exception:  # noqa: BLE001
        return ""


def policy_holds(call: Call, live: Live, traits: Traits | None = None) -> bool:
    """One of the step's kinds (``traits``, default :func:`traits_of`) is held below "do it and tell me" by the user's
    autonomy settings (``ask`` or ``show_plan``): the commit check must find their words asked for it, as the judge did
    for those settings."""
    try:
        levels = (live.policy or util._shared_reader()).levels()
    except Exception:  # noqa: BLE001 - an unreadable policy: the defaults
        levels = dict(util.POLICY_DEFAULTS)
    kinds = (traits if traits is not None else traits_of(call, live)).kinds
    return any(levels.get(kind, "do_and_tell") in ("ask", "show_plan") for kind in kinds)


def may_commit(call: Call, live: Live, plan: Any = None) -> bool:
    """Whether the step could send, delete, buy, close, join or otherwise last (§2.5 rows 3-5): its traits destroy or
    reach, it clicks a declared join control, it is a ``run_skill`` that sends or joins, or the user's policy holds one
    of its kinds. These are the steps the commit check sees. A run's own undo (:func:`own_undo`) is not one."""
    if call.tool in ("look", "memory", "wait"):
        return False
    if own_undo(call, live):
        return False
    if call.tool == "run_skill":
        key, slots = skill_input(call, plan)
        sk = util.skill(key)
        return skill_sends(sk, slots) or skill_joins(sk)
    if navigates(call, live):
        # an address typed with Enter outside any chat: it opens a page (only a policy holding its kinds checks it)
        return policy_holds(call, live, ADDRESS_TRAITS)
    t = traits_of(call, live)
    # morning21 (A4 i): a declared join control by identity, a name read with no role included; integ29 (S3): a calling
    # site's own device key is never undoable (it flips a device in a meeting)
    return t.destroys or t.reaches or join_target(call, live) or policy_holds(call, live) \
        or bool(device_key(call, live))


def takes_screen(call: Call) -> bool:
    """Row 1: the step takes focus, sends input, restores a window, launches an app or opens a link (tool metadata and
    fixed operations). ``run_skill`` is gated rung by rung inside the engine instead."""
    if call.tool in ("run_skill", "look", "memory", "fetch", "fetch_url", "wait"):
        return False
    if call.tool in _TAKES_SCREEN_TOOLS:
        return True
    if call.tool == "window_action" and str(call.input.get("action") or "") in _TAKES_SCREEN_WINDOW_OPS:
        return True
    return tool_traits(call.tool, call.input).needs in ("input", "focus")


def game_now(board: Mapping[str, Any] | None = None) -> str | None:
    """D9: the game in front: the Now board's ``game_front`` when fresh (its name, or ``"game"``), else the local read
    of the foreground program (``gamegate.game_in_front``). ``""``: none; ``None``: only when that local read raised
    (row 1 then reads the game gate once more at the step, :func:`game_holds`, and counts it as a game only if that
    fails too). Never raises."""
    try:
        from yuki.memory.now import game_in_front as board_game

        front = board_game(board) if board else None
        if front is not None:
            return str((board or {}).get("game_front_name") or "game") if front else ""
    except Exception:  # noqa: BLE001 - the local read decides
        pass
    try:
        from yuki.memory.gamegate import game_in_front

        return str(game_in_front() or "")
    except Exception:  # noqa: BLE001 - unknown: counted as a game
        return None


def calling_windows(board: Mapping[str, Any] | None) -> frozenset[int]:
    """The windows the Now board lists in ``calling_tabs`` (a calling site's tab in them)."""
    out: set[int] = set()
    for key in ((board or {}).get("calling_tabs") or {}):
        try:
            out.add(int(key))
        except (TypeError, ValueError):
            continue
    return frozenset(out)


def join_controls() -> frozenset[tuple[str, str]]:
    """The join controls capabilities declare (``[chat] labels.<language>.join_voice = [role, name]`` in each chat
    app's connection file), casefolded: a join in any window. Calling sites' (``[call.labels.<language>] join``, names
    ordinary pages draw too: "Join now", "Rejoin") count only on that site's page shown now (integ29 S5:
    :func:`page_join_controls`, :func:`join_target`). Data; never raises."""
    out: set[tuple[str, str]] = set()
    try:
        from yuki.skills.chat import app_data, chat_apps

        for app in chat_apps():
            for table in ((app_data(app).get("labels") or {}).values()):
                pair = table.get("join_voice") if isinstance(table, Mapping) else None
                if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                    out.add((str(pair[0]).casefold(), words(pair[1]).casefold()))
    except Exception:  # noqa: BLE001 - no data: rows 3 (ii)-(iv) still hold
        pass
    return frozenset(out)


def _clock(at: float) -> str:
    try:
        return time.strftime("%I:%M %p", time.localtime(float(at))).lstrip("0")
    except (TypeError, ValueError, OverflowError):
        return ""


def near_duplicate(text: str, live: Live) -> Mapping[str, Any] | None:
    """The user's recent message to this person that the text nearly repeats (``util.similar`` at ``util.SIMILAR`` or
    more, within :data:`DUPLICATE_WINDOW_S`), else ``None``. A number, never a reading of the words."""
    for row in live.recent:
        try:
            at = float(row.get("at") or 0.0)
        except (TypeError, ValueError):
            continue
        if live.now - at > DUPLICATE_WINDOW_S:
            continue
        if util.similar(text, str(row.get("text") or "")) >= util.SIMILAR:
            return row
    return None


# ---------------------------------------------------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------------------------------------------------


def check(step: Call, plan: Any, user_words: Sequence[str], live: Live, check_verdict: Any) -> Verdict:
    """The table, row by row (module docstring). ``check_verdict``: the commit check's verdict for this very step
    (``yuki.brain.check.CheckVerdict``: ``effect``, ``asked``, ``question``, ``failed``), or ``None`` when the step is not
    may-commit (or none was had: a may-commit step without one fails closed). ``plan``: the plan (``None``: a talker
    hand, a one-step plan with nothing declared). Never raises: an error inside is an ask."""
    try:
        return _check(step, plan, list(user_words or []), live, check_verdict)
    except Exception:  # noqa: BLE001 - fail closed
        return Verdict(ASK, 0, "error", question=failed_question(step))


def failed_question(step: Call) -> str:
    """The fail-closed question, naming the step in code words (``describe_step``'s running line: the verb, the
    control's role and name, the app), so a yes is a yes to that step. Never raises."""
    try:
        from yuki.ui.step_words import describe_step

        about = {k: v for k, v in (("element", step.name[:80]), ("role", step.role), ("app", step.app)) if v}
        line = words(describe_step(step.tool, {**(step.input or {}), "_about": about}, done=False))
    except Exception:  # noqa: BLE001 - the plain question
        line = ""
    if not line:
        return LINE_CHECK_FAILED
    return LINE_CHECK_FAILED_STEP.format(step=line[:1].lower() + line[1:])


def screen_gate(step: Call, live: Live) -> Verdict | None:
    """Rows 1-2 alone (a game in front; the app's Act switch off or a kind on "just suggest"): the refusal, or
    ``None``. Re-run after an ask was answered and after the send countdown, since the screen may have changed while
    the user was asked or the countdown ran."""
    return _rows_1_2(step, live, step_traits(step, live))


def game_holds(live: Live) -> bool:
    """Row 1's game state for this step. ``live.game`` as read when the step's facts were gathered (:func:`game_now`:
    the Now board when fresh, else the local foreground read). ``game_now`` is ``None`` only when that local read
    (``gamegate.game_in_front``) raised; then it is read once more here, so a read that failed a moment ago and works
    now decides. Only when this read fails too is it a game (fail closed). Hardening only: this is not what gave the
    17 Oct live test's "after the round?" with no game (that line came from a key refusal tagged ``game`` below this
    rule: ``actions/input.py`` ``_game_stopped`` -> ``executor`` ``LINE_GAME``; see the lBrain report). Never
    raises."""
    if live.game is not None:
        return bool(live.game)
    try:
        from yuki.memory.gamegate import game_in_front

        return bool(game_in_front())
    except Exception:  # noqa: BLE001 - neither could tell: counted as a game
        return True


def _rows_1_2(step: Call, live: Live, traits: Traits) -> Verdict | None:
    # row 1: a game in front (unknown: read once more, fail closed: :func:`game_holds`)
    if takes_screen(step) and game_holds(live):
        return Verdict(REFUSE, 1, "game", line=LINE_GAME, final=True)

    # row 2: the app's Act switch off, or a kind set to "just suggest"
    if step.tool != "run_skill":
        reader = live.policy or util._shared_reader()
        try:
            off = reader.app_off(step.app) if util.acts_in_app(step.tool, step.input, traits) else None
        except Exception:  # noqa: BLE001 - an unreadable policy never blocks (the old judge's rule)
            off = None
        if off:
            return Verdict(REFUSE, 2, "act_off", line=util.app_off_line(off), final=True)
        try:
            only = util.suggest_only(traits, reader.levels())
        except Exception:  # noqa: BLE001
            only = None
        if only is not None:
            kind = util.KIND_WORDS.get(only, only)
            return Verdict(REFUSE, 2, "suggest", final=True,
                           line=f"Your settings say only suggest for {kind.lower()}, so I'll leave that to you.")
    return None


def _check(step: Call, plan: Any, user_words: list[str], live: Live, cv: Any) -> Verdict:
    consent = plan_consent(plan)
    nav = navigates(step, live)
    traits = ADDRESS_TRAITS if nav else traits_of(step, live)
    committing = may_commit(step, live, plan)
    effect = str(getattr(cv, "effect", "") or "") if cv is not None else ""
    if nav and effect == "send":
        effect = ""          # an address loaded in a browser is no message, whatever word a check gave it
    failed = cv is None or bool(getattr(cv, "failed", False))
    asked = bool(getattr(cv, "asked", False)) and not failed

    # never (rows 1-2): a game in front; the app's Act switch off or a kind on "just suggest"
    gate = _rows_1_2(step, live, traits)
    if gate is not None:
        return gate

    # A6 (final-G2 R13; K-U): the run's own close of what its own shortcut opened, measured while that very field
    # holds the keyboard (the hand sets it only then): its own undo, row 6, after the game gate and the Act switch.
    # Never a send by code nor by the commit check: Yuki's unsent text there is the run's own, and it stays unsent.
    if own_undo(step, live):
        return Verdict(ALLOW, 6, "own_undo")

    # integ29 (S3, A7's second rung in code): a calling site's own device key (Meet's Ctrl+D) flips the device whatever
    # its state, so on that site's page shown now it is pressed only with the device's state measured by the hand just
    # before (and a wanted state it is not in), the keyboard inside the page; else refused, not final: the planner acts
    # on the page's own toggle with toggle_to. Never row 6's undoable class.
    device = device_key(step, live)
    if device and calling_tab_now(step.hwnd, live) and not device_state_read(step):
        service = calling_service(step.hwnd, live) or "meeting"
        return Verdict(REFUSE, 3, "device_key", line=LINE_DEVICE_KEY.format(service=service, device=device))

    # A2 (Z7): a step that leaves something acting on its own after it returns (a hidden process, a watcher, a job, a
    # scheduled task: the check's `later` effect, judged from the whole step) is refused as final, whatever `asked`
    # says. The user's words ask for a result, never for an unattended mechanism; code offers to do it now instead.
    if effect == "later" and not failed:
        return Verdict(REFUSE, 5, "later", line=LINE_LATER, final=True)

    sk = None
    if step.tool == "run_skill":
        key, slots = skill_input(step, plan)
        sk = util.skill(key)
    slots = skill_input(step, plan)[1] if sk is not None else {}
    if sk is not None and consent["commit"] == "send" and getattr(sk, "sends", False) \
            and not skill_sends(sk, slots):
        # a declared send whose capability step leaves a draft (its send slot on a non-sending option, 10 Oct job 7):
        # it could only open the chat and end unconfirmed; the planner makes the plan and the step agree
        return Verdict(REFUSE, 4, "declared_draft",
                       line=LINE_DECLARED_DRAFT.format(slots=", ".join(send_slots(sk)) or "send"))

    # undoable (row 6): nothing it does lasts, so it is allowed and said after (P1.5); morning21 (A4 iv): unless it
    # acts on a point or a key with nothing named, on a meeting page in front (a point can't be told from its Join)
    if not committing:
        if not nav and unidentified_target(step) and calling_tab_now(step.hwnd, live):
            return _join_unidentified(step, live)
        return Verdict(ALLOW, 6, UNDOABLE)

    # lasting (rows 3-5). Row 3: joins are the user's click, judged by the step, never the window (morning21 A4):
    # (i) a declared join control, (ii) the commit check's join, (iii) a capability that joins
    why = ("join_control" if join_target(step, live) else "join_effect" if effect == "join"
           else "join_skill" if sk is not None and skill_joins(sk) else "")
    if why:
        if sk is not None and skill_commit(sk) == "words" and asked and effect in ("join", "none", ""):
            return Verdict(ALLOW, 3, "join_words")
        app = util.app_name(step.app) if step.app else ""
        # A5: the user's own click, said as such only for a real join (i-iii)
        return Verdict(REFUSE, 3, why, line=LINE_JOIN.format(app=app) if step.app else LINE_JOIN_PLAIN, final=True,
                       focus=int(step.hwnd) if step.hwnd else None)
    # (iv) fail-closed only: on a meeting page in front, a target that can't be identified, or a step the commit check
    # couldn't judge (a join control under a name no data holds); everything else goes on to rows 4-6
    if not nav and (unidentified_target(step) or failed) and calling_tab_now(step.hwnd, live):
        return _join_unidentified(step, live)

    # row 4: sends (typing with Enter outside a chat goes to the commit check: row 5, or back here on its "send")
    by_code = sends_now(step, live, traits) or (sk is not None and skill_sends(sk, slots))
    if not by_code and effect == "send" and step.tool in _ENTER_TOOLS and chat_window(step, live) is False:
        # Enter typed or pressed where no data says Enter sends (a search box, a command field; the check called
        # "lofi hip hop" + Enter in Arc's command bar a send on 10 Oct, and "Japanese" + Enter in Spotify's search
        # might be), or a chat no data knows: theirs to say, asked plainly, never refused into a dead end. Their words
        # asking for it (the check's ``asked``) don't skip the question: nothing in the data says this is a chat whose
        # message capability could send it. The check's own question when it gave one (only when not asked), else
        # what is typed and where (review r1 L1).
        question = (words(getattr(cv, "question", "")) if not asked else "") or maybe_send_question(step, live)
        return Verdict(ASK, 5, "maybe_send", question=question)
    if by_code or effect == "send":
        return _send(step, plan, consent, user_words, live, sk, asked, failed, effect)

    # row 5: anything else that could last
    if failed:
        return Verdict(ASK, 5, "check_failed", question=failed_question(step))
    if effect in ("none", "") and cv is not None:
        return Verdict(ALLOW, 5, "none")
    if asked:
        return Verdict(ALLOW, 5, "asked")
    question = words(getattr(cv, "question", "")) or failed_question(step)
    return Verdict(ASK, 5, "lasting", question=question)


def _join_unidentified(step: Call, live: Live) -> Verdict:
    """morning21 (A4 iv, A5): row 3's fail-closed refusal: not final (the planner routes around it by the control the
    read names), never "you click Join", nothing brought forward."""
    service = calling_service(step.hwnd, live) or "meeting"
    return Verdict(REFUSE, 3, "join_unidentified", line=LINE_JOIN_UNIDENTIFIED.format(service=service))


def _send(step: Call, plan: Any, consent: dict[str, Any], user_words: list[str], live: Live, sk: Any, asked: bool,
          failed: bool, effect: str = "") -> Verdict:
    """Row 4 for a step that sends (module docstring, row 4; HANDBOOK rule 8 and the send rule: "send X to Y" and
    "message / tell / ask / reply to Y" with X in their words mean send, naming only the person means a draft). For one
    of the user's own ways (``app.way``: no person slot) ``to`` is the place they named. The order matters: an ask's
    yes skips everything after it (``after_ask``), so every refusal and the send slots come before the asks."""
    if consent["commit"] != "send":
        line = LINE_UNDECLARED_SEND
        if step.tool == "type_text" and sends_now(step, live):
            # a line break outside a chat's window (no Yuki text waiting there) sends for certain only because the
            # check can't see it: the planner types without it
            by_break = _line_break_submit(step) and chat_window(step, live) is False \
                and not (step.hwnd and int(step.hwnd) in live.typed_pending)
            line = LINE_LINE_BREAK if by_break else LINE_TYPED_ENTER
        return Verdict(REFUSE, 4, "undeclared_send", line=line)
    if step.tool != "run_skill" or sk is None or not getattr(sk, "sends", False):
        return Verdict(REFUSE, 4, "not_capability", line=LINE_NOT_CAPABILITY)
    _key, slots = skill_input(step, plan)
    quote, to, text = consent["quote"], consent["to"], consent["text"]
    look = _People(live)
    # code facts of the plan itself (refused: the planner fixes them)
    if not to:
        return Verdict(REFUSE, 4, "recipient", line=LINE_NO_RECIPIENT)
    pname = person_slot(sk)
    who = words(slots.get(pname)) if pname else ""
    if who and not _same_person(who, to, live, look):
        return Verdict(REFUSE, 4, "recipient_slot", line=LINE_RECIPIENT)
    tname = text_slot(sk)
    step_text = words(slots.get(tname)) if tname else ""
    if tname and step_text != text:
        # one refusal with every fix the step needs (a declared send whose step drafts with no text: both at once)
        fixes = [LINE_TEXT_MISMATCH]
        if not skill_sends(sk, slots, partial=False):
            fixes.append(LINE_SEND_SLOT.format(slots=", ".join(send_slots(sk)) or "send"))
        return Verdict(REFUSE, 4, "text_mismatch", line="; ".join(fixes))
    body = step_text or text
    own = bool(step_text) and in_words(step_text, user_words)
    # the commit check: their words ask for this very send (to that person, with that text), else a draft
    if failed or not asked or effect != "send":
        return _draft(step, sk, slots, body, own, "not_asked")
    # the quote is their words as heard (a name they said may be written as the person's name, never in place of
    # another person's: review r2 M1 b)
    names = _recipient_names(to, who, look, user_words)
    rec = _Who(to=to, slot=who, names=tuple(names), look=look)
    placed = _quote_place(quote, user_words, rec) if quote else None
    if placed is None:
        return Verdict(REFUSE, 4, "quote", line=LINE_QUOTE)
    # what the message says came from them, in the turns the quote stands in (never every turn of the exchange: what
    # they said to Yuki for something else is not what to send): their own words, or Yuki's wording of what they said
    # there, its words close together (said_share); nothing of it from them is naming only the person: a draft (rule
    # 8). Review r1 M1.
    turns = _runs_of(placed.entries, placed.held)
    said = bool(step_text) and in_words(step_text, turns)
    share = 1.0 if said else said_share(body, turns, names)
    if share < SAID_SHARE and _said_elsewhere(body, step_text, to, who, placed, names, look):
        # the quote is a turn that says nothing of the message ("Do it, bro") while another turn of theirs names the
        # person and says something of it: the planner quotes that turn (a refusal it can fix, before any draft or
        # ask), and the share there decides
        return Verdict(REFUSE, 4, "quote_turn", line=LINE_QUOTE_TURN)
    if share <= 0.0:
        return _draft(step, sk, slots, body, own, "not_said")
    if not skill_sends(sk, slots, partial=False):
        # a send slot left out: the resolver would fill its default (a draft), so this step would not send for sure;
        # it is never counted down or reported as sent (the planner fills the slot, or the plan drafts)
        return Verdict(REFUSE, 4, "send_slot", line=LINE_SEND_SLOT.format(slots=", ".join(send_slots(sk)) or "send"))
    seen = near_duplicate(body, live)
    if seen is not None:
        at = _clock(float(seen.get("at") or 0.0))
        question = LINE_DUPLICATE.format(to=spoken_name(to, look), at=at) if to and at else LINE_DUPLICATE_PLAIN
        return Verdict(ASK, 4, "duplicate", question=question)
    # whom it goes to: the user's words the quote stands for name the recipient (review r2 M1 a: a name said only
    # outside the quote is asked, never sent unasked)
    named = _names_recipient(placed, rec) or _named_in_turn(placed, rec)
    if _others(" ".join(turns), rec, body):
        # the turns name someone else too: what of the message goes to the recipient without asking is measured in
        # the words the quote stands for alone (the rest of the turn may be what to tell the other person)
        said = bool(step_text) and in_words(step_text, [placed.span])
        share = 1.0 if said else said_share(body, [placed.span], names)
    if not named or share < SAID_SHARE:
        # asked first, showing what goes to whom: a recipient the quote doesn't plainly name (speech heard the name
        # another way, two people go by it, the name was said only outside the quote), or Yuki's wording with little
        # of their words in it; a yes is their plain instruction
        return Verdict(ASK, 4, "worded" if named else "recipient_unsaid",
                       question=ask_send_question(body, spoken_name(to, look)))
    return Verdict(ALLOW, 4, "send" if said else "send_worded", countdown=True)


def _said_elsewhere(body: str, step_text: str, to: str, who: str, placed: _Placed, names: Sequence[str],
                    look: _People) -> bool:
    """Whether a turn of the user's words the quote does not stand in names the recipient (:func:`_named_by_user`)
    and says something of the message: the text is a span of it, or some of the text's words stand close together in
    it (:func:`said_share` above 0; review r2 L2: "he'll" is two words, so "ask him when he'll come" holds only 2 of
    "When will you come?"'s 4). The planner then quotes that turn, and the share there decides ask or allow."""
    for i, turn in enumerate(placed.entries):
        if i in placed.held:
            continue
        says = (bool(step_text) and in_words(step_text, [turn])) or said_share(body, [turn], names) > 0.0
        if says and _named_by_user(to, who, [turn], look):
            return True
    return False


def _draft(step: Call, sk: Any, slots: Mapping[str, Any], body: str, own: bool, why: str) -> Verdict:
    """Row 4's draft: the capability with its send slot on a non-sending option; the user's own words stay in its text
    slot, Yuki's wording is typed without Enter once the chat is open (``type_after``)."""
    drafted = draft_slots(sk, slots, keep_text=own)
    if drafted is None:
        return Verdict(REFUSE, 4, "no_draft", line="this capability has no way to leave a draft")
    return Verdict(DRAFT, 4, why, line=LINE_DRAFTED, draft_input={**step.input, "slots": drafted},
                   type_after="" if own else body)


def _same_person(slot_value: str, to: str, live: Live, look: _People | None = None) -> bool:
    """The capability's person slot names the plan's recipient: the same words; or memory's people index finds one
    person ``to`` names who also goes by the slot's words (a first name for the full name); or it finds exactly one
    person in the slot's words (as the user said them: a heard form, a script of its own), who goes by ``to``. A part
    of a name alone ("Iyer" for "Hemanth Iyer") is not enough, and with memory unable to say only the same words
    pass."""
    from yuki.memory.contacts import tokens

    want, slot = tokens(to), tokens(slot_value)
    if not want or not slot:
        return False
    if slot == want:
        return True
    look = look or _People(live)
    fits = _fits(look(to), to)
    if len(fits) == 1 and any(tokens(n) == slot for n in fits[0]):
        return True
    found = look(slot_value) or []
    return len(found) == 1 and any(tokens(n) == want for n in found[0])


def after_ask(verdict: Verdict) -> Verdict:
    """An ask the user said yes to: allowed this once (a send keeps its countdown)."""
    return replace(verdict, result=ALLOW, countdown=verdict.row == 4)


__all__ = ["UNDOABLE",
           "ALLOW", "ASK", "CHAT_KIND", "COMMITS", "DRAFT", "EFFECTS", "LINE_ASK_SEND", "LINE_DRAFTED", "LINE_GAME",
           "LINE_JOIN", "LINE_JOIN_UNIDENTIFIED", "LINE_LATER", "ACTIVATION_KEYS", "activates", "calling_service",
           "LINE_DEVICE_KEY", "device_key", "device_state_read",
           "calling_tab_now", "join_target", "unidentified_target",
           "NAME_GAP", "PLAIN_WHY", "REFUSE", "RESULTS", "SAID_SHARE", "SAID_SPAN", "Call", "Live", "Verdict",
           "after_ask", "ask_send_question", "spoken_name", "calling_windows", "chat_window", "check", "draft_slots",
           "failed_question", "field_of", "game_holds", "game_now", "in_words", "is_join_control", "join_controls",
           "page_join_controls",
           "may_commit", "maybe_send_question", "near_duplicate", "own_undo", "person_slot", "plain_why",
           "plan_consent", "policy_holds",
           "quote_in_words", "quote_turns", "said_share", "screen_gate", "send_slots", "sends_now", "skill_commit",
           "skill_input", "skill_joins", "skill_sends", "step_traits", "takes_screen", "text_slot", "traits_of",
           "typed_address", "navigates", "waiting_text", "window_kind", "window_page"]
