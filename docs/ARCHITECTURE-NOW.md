# Yuki architecture as it is now (10 Oct 2026)

Mapped from the code on `main` (a800748) by three read-only readers. Tags:
- **[r15]**: wave F, installed on the PC;
- **[main]**: wave S, merged but **not released** (held for the skill learner);
- **[old]**: unchanged from before 9 Oct, and installed.

The target shape after the course correction is in section 5.

---

## 1. The processes

```mermaid
flowchart LR
  subgraph TRAY["Tray process: yuki-ui"]
    VOICE["Voice session<br/>(Pipecat: STT, talker, TTS)"]
    BRIDGE["Bridge"]
    RT["Agent runtime<br/>worker lane + reader lane<br/>(the thinker)"]
    SK["Skill engine [main]"]
    UI["Island, corner cards,<br/>ask bar, tray menu"]
    MC["Memory client<br/>(reads/writes the DB directly)"]
  end
  subgraph MEM["Memory service (below-normal priority)"]
    CAP["Capture threads"]
    DIS["Distil jobs<br/>(Haiku/Sonnet)"]
    NUDGE["Card worker (every 15 s)"]
    NOW["Now board"]
    EMB["Embedder"]
  end
  WAKE["Wake child<br/>(local wake word)"]
  WIN["Window (HTML, on demand)"]
  MCP["MCP server<br/>(Claude Code only)"]
  DB[("memory.db<br/>encrypted SQLite")]

  WAKE -->|"heard"| VOICE
  VOICE --> BRIDGE --> RT
  VOICE --> SK
  RT --> SK
  CAP --> DB
  DIS --> DB
  NUDGE --> DB
  MC <--> DB
  NOW -->|"pipe"| RT
  NOW -->|"pipe"| VOICE
  EMB -->|"pipe"| MC
  NUDGE -->|"event"| UI
  WIN <-->|"pipe"| UI
  MCP --> DB
```

---

## 2. The brain: how a request becomes actions

```mermaid
flowchart TD
  MIC["You speak"] --> WW["Wake word [old]"] --> STT["Deepgram STT [old]"]
  STT --> FP{"Fast path, code [old]<br/>'go away' → end<br/>bare name → 'Yeah?'"}
  FP --> ROUTER["LLM 1 Router, Haiku [old]<br/>'needs the thinker?'<br/>may start the thinker early"]
  FP --> TALKER["LLM 2 Talker, Haiku [old]<br/>sees: brief slice, people, rules, desk,<br/>last hour, grounding lookup"]
  TALKER --> T1["Just reply / stay quiet"]
  TALKER --> HANDS["Hands: one-step actions<br/>(media, volume, scroll, window, tab, open, click, type) [old]"]
  TALKER --> RS["run_skill → skill engine [main]"]
  TALKER --> TELL["tell_yuki → thinker<br/>r15: talker's paraphrase<br/>main: your exact words"]
  HANDS --> JH["LLM 3 Judge, Haiku<br/>on risky hands"]
  JH --> GAME1{"Game in front?<br/>refuse [r15]"}
  RS --> ENG
  subgraph ENG["Skill engine [main], code only"]
    RES["Resolver: your words → skill + slots<br/>(prefs → taste → default)"] --> LAD["Route ladder:<br/>A app's own controls → B recorded click path → C thinker"]
    LAD --> DC{"Done-check<br/>saw the result?"}
    DC -->|"yes"| DONE["'Done' (recheck ≤ 6 s)"]
    DC -->|"no / unknown"| CC["'Couldn't confirm'"]
  end
  LAD -->|"rung C"| TH
  TELL --> TH
  TYPED["You type (ask bar / chat)"] --> TH
  subgraph TH["The thinker: LLM 4, Sonnet 5 [old]<br/>voice: low effort, thinking OFF · typed: medium, thinking on<br/>reactive loop: look → pick next tool → repeat (≤ 40 steps)"]
    CTX["Context: memory block, task frame<br/>(people, routes, pitfalls, procedures),<br/>Now board, last 3 requests"]
    STEP["Pick next tool"]
    G1["Gates before each action:<br/>loop breaker · risk check ·<br/>LLM 5 Judge (Haiku) · situation gate ·<br/>LLM 6 Consult (Sonnet) · 3 s send countdown ·<br/>game refusal [r15/main]"]
    ACT["Act: click / type / key / launch / tab ..."]
    VER["Readback: 'did something change?'"]
    COR["After each turn: finish hold · claim check ·<br/>kept-list hold · skill gate [main] · consult again ·<br/>reply rewriter (honest_final)"]
    CTX --> STEP --> G1 --> ACT --> VER --> COR --> STEP
  end
  TH --> NARR["Narrator gate, owed ledger [old]<br/>short/verified said by code,<br/>else re-voiced by the talker"]
  DONE --> NARR
  CC --> NARR
  NARR --> SG["LLM 7 Speech guard, Haiku (shadow) [old]"] --> TTS["TTS (Gemini / Aura) [old]"]
```

Count on the spoken path: **7+ distinct model roles**. Side calls (progress answers, "want the rest?", read-only helpers) add 3 more. Between your words and an action there are **~11 gates**, and about **11 correctors** after each thinker turn. Only requests that match one of the 7 skills take the new engine path [main]. Everything else is the old reactive thinker.

---

## 3. Capture → memory → what actually feeds execution

```mermaid
flowchart LR
  subgraph CAPTURE["Capture (memory service, always on)"]
    W["Screen text: window in front + visible<br/>(UIA, secret filter) [old]"]
    TL["Timeline: app/site, presence,<br/>meetings, media every 15 s [old]"]
    CALLS["Calls: mic + app audio → local Whisper<br/>→ call digests [old]"]
    TR["'Record how I work': clicks, keys,<br/>elements [old]"]
    MOM["Moments: what changed<br/>(call_join, page, media, volume,<br/>message_sent ...) [r15]"]
    SPR["Spotify window reader,<br/>VolumeWatch, Meet tab census [r15]"]
  end
  subgraph DISTIL["Distil (~$9-10/day, ~90% of it describing you)"]
    J["Journal facts + work threads<br/>(Haiku ~$2.9/day)"]
    P["Portrait, brief, people & routes<br/>(Sonnet nightly ~$2.4/day + weekly)"]
    CONV["Rules, commitments, to-dos<br/>from your chats with Yuki"]
    PR["Practice memory: pitfalls, procedures,<br/>habits; trace task pass, families, demos"]
    RT2["Routines (Sonnet) → one line in the brief"]
    PT["Prefs, taste, skill trust ledger [main]"]
  end
  subgraph EXEC["Execution"]
    THK["Thinker context:<br/>brief slice, rules, to-dos,<br/>related memories, task frame"]
    TLK["Talker blocks:<br/>brief slice, people, desk,<br/>last hour [old]; skill menu [main]"]
    RSV["Resolver slots + done-checks [main]"]
    CARDS["Cards"]
  end
  W --> J --> P
  TL --> J
  CALLS --> J
  CONV --> THK
  P --> THK
  P --> TLK
  P --> CARDS
  J --> THK
  PR --> THK
  PT --> RSV
  MOM --> RSV
  SPR --> RSV
  TL --> CARDS
  TR -.->|"barely: 20 tasks, 1 repeated"| PR
  RT2 -.->|"text only, never a trigger"| P
  MOM -.->|"r15: reaches NOTHING at run time"| X(("unused"))
  TR -.->|"'what comes next' query: MCP only"| X
```

What this shows:
- **Capture is rich.** Most of the spend turns it into a *description* of you: the journal, the portrait and the brief. That description reaches the thinker and talker only as background text.
- **"How you do things"** (the click trace, practice memory) reaches execution only as advisory notes. Before wave S they were used about 1% of the time. On `main`, practice memory no longer reaches the talker at all.
- **"What changed"** (moments, wave F) feeds only the new engine's done-checks and resolver [main]. In the installed r15 it reaches nothing yet.
- **The missing arrow:** nothing turns what you repeatedly do into a skill. Seven skills were hand-written instead, which is why release 16 is held for the **skill learner** (phase L).

---

## 4. Proactive cards

```mermaid
flowchart TD
  TICK["Card worker tick, every 15 s [old]"] --> GATE{"Coarse gate: paused, locked,<br/>snoozed, quiet hours 00:30-08:00"}
  GATE --> OBS{"observe(): Yuki acting · meeting ·<br/>game in front [r15] · full screen +120 s [r15] ·<br/>away · settling · meeting audio"}
  OBS --> P1["1 Welcome back (Sonnet)"]
  P1 --> P2["2 Reminders (Sonnet)"]
  P2 --> P3["3 Check-ins (Sonnet + vet)"]
  P3 --> P4["4 Observation cards (Haiku decides, Sonnet writes)"]
  P4 --> P5["5 Questions (Sonnet)"]
  P5 --> P6["6 Coach / break (Haiku + Sonnet)"]
  P1 & P2 & P3 & P4 & P5 & P6 --> ATT{"Attention budgets:<br/>card gap 10 min, per-kind limits"}
  ATT --> CARD["Corner card in the tray"]
  CARD --> OFF["Offer buttons (closed list, run by code):<br/>add to-do · show memory · reopen page · draft · put in ask bar"]
  CARD --> REPLY["Reply → becomes your own request → thinker"]
  RL["Routine lane + 'Your usual' card<br/>(wave R, not on main yet)"] -.->|"runs first, code-written,<br/>Open it → skill engine"| CARD
```

The first producer that writes a card wins the tick. **Nothing ranks the candidates against each other.** Each producer builds its own picture of you from the DB.

---

## 5. Where this is going (the agreed course correction)

```mermaid
flowchart TD
  subgraph NOWSTATE["One 'you right now' state (shared)"]
    S1["Moments: what just changed"]
    S2["Threads and goals: what you're working on"]
    S3["Routines: when you usually do things"]
    S4["Prefs and taste: which app/control, what you like"]
  end
  CAPT["Capture (keep)"] --> NOWSTATE
  CAPT --> LEARN["Skill learner, phase L:<br/>occasions (app visit → state change + the steps)<br/>→ group → find what varies → cheapest route<br/>→ verify → write to the Playbook"]
  LEARN --> PB[("Playbook: skills learned from you,<br/>'seen N times', you can delete")]
  WORDS["Your words (voice/typed), unchanged"] --> BRAIN
  NOWSTATE --> BRAIN
  PB --> BRAIN
  subgraph BRAIN["One brain"]
    B1{"Matches a learned skill?"}
    B1 -->|"yes"| B2["Run it the cheapest way"]
    B1 -->|"no"| B3["One planning step (goal, target, route, done-check)<br/>→ act in a batch"]
    B2 --> B4{"One done-check + one consent rule<br/>(sends/joins need your words; nothing to a game in front)"}
    B3 --> B4
  end
  NOWSTATE --> JUDGE["One card judge: ranks all candidates;<br/>routine offers yes, work offers never"]
  PB --> JUDGE
  B4 -->|"verified runs"| PB
```

What goes away in that shape:
- the judge, consult, situation gate, claim holds and reply rewriter (replaced by the one done-check and one consent rule);
- the 7 separate card producers (replaced by one judge);
- the advisory "pitfalls" text;
- the hand-written habit skills (replaced by learned ones);
- the old trace learners, once the skill learner replaces them.

The aim is well under today's ~243k lines.
