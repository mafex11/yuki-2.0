# Yuki audit — external research (9 Oct 2026)

Scope: what the 2025–2026 state of the art says about (1) agent harnesses, (2) desktop/computer-use agents, (3) learning workflows from observation, (4) proactive assistants, (5) agent decision-making, and (6) what transfers to Yuki.

Evidence labels used throughout:
- **[Measured]**: a peer-reviewed or arXiv paper with a stated eval, or a vendor engineering post with a stated internal eval.
- **[Vendor claim]**: marketing, a launch post, or a self-reported number nobody else has checked.
- **[Aggregator]**: leaderboard aggregator sites. These often list model names I can't confirm against primary sources, so treat their numbers as indicative only.
- **[Older]**: published before 2025.

Yuki baseline these findings are compared against:
- A common GUI task takes 69 s, 8 model calls and costs $0.54.
- Learned know-how is rarely used.
- 67% of proactive cards are ignored.
- The codebase is 222k lines, built up from patches.

---

## 1. Agent harnesses: what good looks like in 2025–26

### 1.1 Anthropic: "Building effective agents" (Dec 2024) [Older, still the reference]
URL: https://www.anthropic.com/research/building-effective-agents
- The post separates two things. **Workflows** run LLMs and tools along predefined code paths. **Agents** let the LLM direct its own process and tool use.
- It names the patterns: prompt chaining, routing, parallelization, orchestrator-workers, evaluator-optimizer, and autonomous agent.
- Core advice: "use the simplest solution that works; add complexity only when it measurably improves outcomes". Call APIs directly. Frameworks hide prompts and make debugging harder.
- **Agent-computer interface (ACI).** Spend as much effort on tool interfaces as on a human UI. Write tool descriptions with examples, edge cases and the boundaries between similar tools. Make tools mistake-proof ("poka-yoke"). Example: forcing absolute file paths removed a whole class of errors.
- **Lesson for Yuki.** Most of what Yuki does day to day (recall, routing a voice command, building a card) is a *workflow*, not an agent. Reserve the open-ended agent loop for things that really are open-ended.

### 1.2 Anthropic: "Effective context engineering for AI agents" (29 Sep 2025) [Measured / practice]
URL: https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- **Context rot and the attention budget.** Recall gets worse as context grows. Aim for the "smallest possible set of high-signal tokens".
- **System prompts at the "right altitude".** Avoid both brittle hard-coded if/else rules and vague guidance. Start minimal, then add instructions only in response to observed failures.
- **Tools.** Keep the set small and non-overlapping: "If a human can't say which tool to use, neither can an agent."
- **Examples.** Use a few diverse, canonical examples instead of a long list of edge-case rules.
- **Just-in-time retrieval.** Keep lightweight references (paths, queries) and load the data with tools when needed. Claude Code mixes both: CLAUDE.md is loaded up front, files are fetched with grep/glob on demand.
- **Long-horizon techniques.**
  - *Compaction:* keep decisions and open bugs, drop redundant tool output. Tune the compaction prompt for recall first, then precision.
  - *Structured note-taking:* NOTES.md, to-do files, the memory tool.
  - *Sub-agents:* they do deep work in a clean context and return condensed 1–2k-token summaries.
- **Lesson for Yuki.** A rich memory is only an asset if very little of it reaches the prompt at any moment. Inject pointers, and let the model pull the rest.

### 1.3 Anthropic: "Building agents with the Claude Agent SDK" (Sep 2025) and Claude Code best practices (current)
URLs: https://www.anthropic.com/engineering/building-agents-with-the-claude-agent-sdk · https://code.claude.com/docs/en/best-practices
- **Loop shape: gather context, take action, verify work, repeat.** One loop, a small set of strong general tools (bash, file read/write, search), plus MCP or tools for specific domains.
- **Verification is the centre of the harness.** In the docs' words: "Claude stops when the work looks done. Without a check it can run, 'looks done' is the only signal available, and you become the verification loop."
  - The check can be a test, an exit code, or a screenshot diff.
  - It can be enforced deterministically with a Stop hook, or by a separate evaluator (`/goal`), or by a fresh-context reviewer subagent.
- **Hooks are deterministic and CLAUDE.md is advisory.** "Use hooks for actions that must happen every time with zero exceptions."
- **Skills give progressive disclosure.** Only name and description sit in the prompt. The body loads on demand. Use `disable-model-invocation` for side-effecting workflows.
- **Subagents isolate read-heavy investigation.** They keep the main context clean.
- **Failure patterns the docs call out:**
  - The "kitchen sink session".
  - Correcting the same thing over and over. After two failed corrections, clear the context and restart with a better prompt.
  - The over-specified CLAUDE.md: "Bloated CLAUDE.md files cause Claude to ignore your actual instructions."
  - The trust-then-verify gap.
- **Lesson for Yuki.** Every action Yuki takes needs a machine-checkable "done" condition (file saved, message sent, window title changed). That is what removes the user from the verification loop. Rules that must always hold belong in code (hooks), not in prompts.

### 1.4 Anthropic: "Effective harnesses for long-running agents" (late 2025)
URL: https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents (details partly via secondary summaries)
- **Initializer agent.** On the first run it writes `init.sh`, a progress log, a `feature_list.json` with a pass/fail flag per item, and an initial git commit.
- **Each later session** reads the progress log and git history, checks the app still works, implements one failing item, verifies it end to end (Puppeteer), and commits.
- **Failure modes it targets:** declaring victory too early, leaving the state undocumented, marking items done without testing them.
- **Lesson for Yuki.** Long-running "threads of work" need an external, structured state file with explicit done flags. A narrative memory is not enough.

### 1.5 Anthropic: multi-agent research system (Jun 2025) [Measured, internal eval]
URL: https://www.anthropic.com/engineering/multi-agent-research-system
- An Opus 4 lead with Sonnet 4 subagents beat a single Opus 4 agent by **90.2%** on Anthropic's internal research eval. The gain was on breadth-first queries.
- On BrowseComp, **token usage alone explained 80% of the performance variance**.
- Cost: agents use about 4× the tokens of a chat; multi-agent systems use about **15×**. It only pays off when the task is valuable enough.
- **Lesson for Yuki.** Multi-agent designs help with parallel *reading* and research, not with GUI execution.

### 1.6 Cognition: "Don't build multi-agents" (Jun 2025) and "Multi-agents: what's actually working" (22 Apr 2026)
URLs: https://cognition.ai/blog/dont-build-multi-agents · https://cognition.com/blog/multi-agents-working
- **2025 post.** Share the full context, because actions carry implicit decisions. The default should be a single-threaded linear agent.
- **2026 update.** The rule is "writes stay single-threaded". Extra agents should add *intelligence*, not *actions*.
  - **What works:** a clean-context reviewer (about 2 bugs caught per PR, about 58% of them severe); a "smart friend" escalation to a stronger model; manager–child map-reduce.
  - **What doesn't:** parallel-writer swarms; escalation decided by a weak primary model (the SWE-1.5 fast model didn't know when to escalate).
- **Lesson for Yuki.** The talker/thinker split is fine *only if* the talker never acts on the desktop by itself and the thinker owns every write. Watch for a weak fast model failing to know when to escalate. Cognition found exactly that.

### 1.7 Manus: "Context engineering for AI agents" (18 Jul 2025) [Practice; Manus is now part of Meta]
URL: https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus
- **KV-cache hit rate is "the most important metric".**
  - The input:output token ratio is about 100:1.
  - Cached Sonnet input costs $0.30/MTok against $3 uncached, a 10× difference.
  - Keep the prompt prefix stable (no timestamps at the top), keep the context append-only, and serialize deterministically.
- **Mask tools, don't remove them.** Adding or removing tools mid-task breaks the cache and confuses the model. Use logit masking or prefixed tool groups (`browser_`, `shell_`).
- **Use the file system as context.** Compression should be restorable: drop the page content but keep its URL.
- **Recite the plan.** A `todo.md` is rewritten at the end of the context. A typical task takes about 50 tool calls, which is enough to cause goal drift. Caveat: early on, about one third of actions were spent updating the to-do list, so planning was later moved to a planner sub-agent.
- **Keep the wrong stuff in.** Errors left in context shift the model away from repeating them.
- **Don't get few-shotted.** Repetitive action/observation pairs push the model into a rut, so add controlled variation.
- The framework was rebuilt four times ("Stochastic Graduate Descent").
- **Lesson for Yuki.** At $0.54 for 8 calls, prompt caching is probably the cheapest win available. Check whether the screen-tree snapshots invalidate the cached prefix on every call. They should be appended late in the prompt and diffed, not re-rendered at the top.

### 1.8 HumanLayer: "12-Factor Agents" (2025)
URL: https://github.com/humanlayer/12-factor-agents · https://www.humanlayer.dev/12-factor-agents
- This is a design guide, not a framework. Its main points:
  - Own your prompts, your context window and your control flow.
  - Treat tools as structured outputs.
  - Unify execution state with business state.
  - Make agents launchable, pausable and resumable through an API.
  - Contact humans through tool calls.
  - Compact errors into the context.
  - Build **small, focused agents**; treat the agent as a stateless reducer.
- The thesis: successful products are mostly deterministic code with LLM decision points placed deliberately. They are not "a loop with 40 tools".

### 1.9 OpenAI Agents SDK and the Codex harness (2025–26)
URLs: https://openai.github.io/openai-agents-python/ · https://openai.com/index/unrolling-the-codex-agent-loop/ (Jan 2026)
- **Agents SDK primitives:** Agent, Handoff, Guardrail, Session, and built-in Tracing (on by default). A handoff appears to the model as a `transfer_to_<agent>` tool. "Agents-as-tools" keeps the manager in control.
- **Codex harness:** one agent loop shared by the CLI, the IDE and the cloud product, with sandbox and approval boundaries and thread/turn/event streams.
- **Lesson for Yuki.** Tracing every model call, tool call and handoff should be on by default. This is how teams find where the 8 calls and 69 s actually go.

### 1.10 Harness takeaways
- **Single agent, small toolset.** One loop, roughly 10–20 tools, everything else loaded progressively as skills.
- **Separate deterministic code from LLM decisions.** Hooks and state machines enforce invariants; the model chooses the next step.
- **Verification-first.** Every action has a checkable postcondition. Use reviewer subagents (clean context) for anything high-stakes.
- **Context is the scarce resource.** Use a stable cached prefix, just-in-time retrieval, restorable compression, and an external to-do/progress file.
- **Keep the harness small through subtraction.** Every rule added to the prompt in response to a failure should be re-tested and pruned. Cognition, Manus and Anthropic all report rewriting or pruning, not accreting.

---

## 2. Computer-use and desktop agents

### 2.1 Benchmarks: where the field is
- **OSWorld / OSWorld-Verified.**
  - XLANG's Jul 2025 post [Measured]: CoAct-1 led at 60.76%; Agent S2.5 w/ o3 56.0%; Claude 4 Sonnet 43.9%. Human baseline about 72%. Step budget mattered a lot (o3 ranged 9.1%→23.0%). https://xlang.ai/blog/osworld-verified
  - Agent S3 w/ bBoN (N=10): 69.9% [Simular, self-reported]. Sonnet 4.6 was reported at 72.5% (Feb 2026, launch coverage).
  - By mid/late 2026, aggregators list the top systems at **83–86%** [Aggregator; vendor-reported, custom harnesses, likely near saturation].
  - Sources: https://benchlm.ai/benchmarks/osworld-verified · https://leaderboard.steel.dev/leaderboards/osworld/ · https://epoch.ai/blog/what-does-osworld-tell-us-about-ais-ability-to-use-computers
- **OSWorld 2.0 (Jun 2026)** [Measured]. 108 long-horizon tasks with a human median of about 1.6 h. An average task takes about 318 tool calls, against about 30 in OSWorld 1.0. The best model completes only **about 20.6%** of tasks (54.8% partial credit) at 500 steps. https://arxiv.org/abs/2606.29537 · https://osworld-v2.xlang.ai/
  - Takeaway: short GUI tasks are close to solved on benchmarks. Long, realistic, multi-source work is not.
- **WindowsAgentArena** [Measured, 2024–25]. The original Navi agent scored 19.5% against 74.5% for humans. UFO2 reached 30.5% and Agent S2 about 29.8% (repo-reported). CoAct-1 reports 52.5% on WAA in its ICLR 2026 version. https://arxiv.org/abs/2409.08264
- **OSWorld-Human (efficiency; MLSys 2026)** [Measured]. https://arxiv.org/abs/2506.16042
  - Agents take **1.4–2.7× more steps than the human-minimal trajectory** (2.7–4.3× in the MLSys version).
  - **Planning and reflection calls make up most of the latency** (planning alone >50%, up to about 75%).
  - Later steps take about 3× longer than early ones, because the context grows.
  - The best agent: 42.5% success, but only 17.4% on the strict efficiency metric.
  - Grounding errors (about 23%) can add up to 30 extra steps.
  - **This is Yuki's 69 s / 8 calls problem in benchmark form.**

### 2.2 Microsoft UFO2, "The Desktop AgentOS" (Apr 2025) and UFO3 Galaxy (Nov 2025)
URLs: https://arxiv.org/abs/2504.14603 · https://github.com/microsoft/UFO

UFO2 is the closest published analogue to Yuki: Windows, UI Automation, per-app agents.
- **Architecture.**
  - A HostAgent decomposes the task and routes it.
  - A per-application AppAgent has app-specific APIs (COM/Office object model), a hybrid GUI+API action interface, and app knowledge.
  - A "Picture-in-Picture" nested desktop lets the agent run without taking over the user's session.
- **Results (Table 1)** [Measured]: UFO2 full scored 27.9–30.5% on WAA and 28.6–32.7% on OSWorld-W, against Operator's 20.8% and 14.3%.
- **GUI-only vs GUI+API (Office tasks).**
  - Success: +6–8 points.
  - **Steps with o1: 16.0 → 6.6 (about 58% fewer).**
- **Speculative multi-action.** The model predicts several actions in one call; each is validated with cheap UIA control-state checks before it runs.
  - **LLM calls: 44–51% fewer on OSWorld-W** and about 11–12% fewer on WAA.
  - Success was unchanged or slightly better.
- **Knowledge substrate.** RAG over help documents plus the agent's own past execution traces gave +2–4 points. Self-experience *hurt* o1 on WAA (25.3% → 20.8%), so learned experience isn't automatically positive.
- **Control detection (relevant to Yuki's no-vision choice).**
  - On WAA, **more than 62% of UFO2-base failures were control-detection failures**, mostly in apps that follow accessibility standards poorly (LibreOffice).
  - Adding a vision parser (OmniParser) to UIA recovered 9.9–25% of those cases. UIA-only was still a strong base.
  - The trade-off for Yuki: UIA-only is cheap and private, but some apps will stay partly invisible. The most effective mitigation that keeps the no-vision rule is the *API route*: COM, CLI, app plugins, browser DOM/CDP.
- **UFO3 Galaxy** adds DAG-based orchestration across devices. It is still in flux; UFO2 is the stable layer.

### 2.3 CoAct-1, "coding as actions" (Salesforce, Aug 2025; ICLR 2026) [Measured]
URL: https://arxiv.org/abs/2508.03923
- An orchestrator delegates each subtask either to a GUI Operator or to a **Programmer** agent that writes and runs Python or Bash.
- Results:
  - **60.76% on OSWorld**, which was SOTA when published.
  - **10.15 steps on average, against about 15** for GUI-only agents.
  - The biggest gains were in Calc, VS Code and multi-app tasks.
  - The ICLR version reports 52.5% on WAA.
- Related [Measured]: "How Do AI Agents Do Human Work?" (CMU/Stanford, Oct 2025) found that agents naturally go **programmatic** even for visual tasks. They were 88% faster and 90–96% cheaper than humans, but produced lower quality and sometimes fabricated results. https://arxiv.org/abs/2510.22780
- **Lesson for Yuki.** "GUI last" is the single biggest speed lever. File operations, Office (COM / python-docx / openpyxl), browser work (CDP/Playwright), and most "send a message" flows with a CLI or API should not be clicks.

### 2.4 Agent S2 / S3 (Simular)
URLs: https://arxiv.org/abs/2504.00906 · https://www.simular.ai/articles/agent-s3
- **Agent S2.** A generalist planner with specialist grounding experts ("Mixture of Grounding": visual, textual, structural) and proactive hierarchical *re*-planning after each subtask. The abstract reports a 52.8% relative gain on WAA.
- **Agent S3.** A simpler flat loop plus a native coding agent. Behavior Best-of-N runs N rollouts and uses a judge to pick the best: **62.6% → 69.9%** on OSWorld [self-reported]. This buys accuracy with N times the compute, so it is the wrong trade for an interactive personal assistant, but it can suit background jobs.

### 2.5 Products
- **OpenAI Operator/CUA (Jan 2025) → ChatGPT agent (17 Jul 2025) → Atlas agent mode.** CUA: 38.1% OSWorld, 58.1% WebArena, 87% WebVoyager [Vendor claim, at launch]. ChatGPT agent merged Operator with Deep Research and runs in a cloud VM with user takeover and confirmation for sensitive steps.
- **Claude computer use.** Sonnet 4.5 (Sep 2025) scored 61.4% on OSWorld. Sonnet 4.6 (Feb 2026) was reported at about 72.5% [Vendor claim]. Claude for Chrome is Anthropic's browser agent.
- **Google Project Mariner** (2024–25). Introduced "**teach and repeat**": show a task once, then repeat it from a short instruction. It ran up to about 10 parallel tasks in cloud VMs. **Mariner was shut down as a standalone product on 4 May 2026** and folded into Gemini Agent, Chrome auto-browse and the Gemini 2.5 Computer Use API. I couldn't confirm that teach-and-repeat survived. https://www.aiwiki.ai/wiki/project_mariner
- **Microsoft Copilot Actions / Windows "Agent Workspace"** (preview, late 2025). Each agent runs in a separate Windows session under its **own non-admin account** with limited folder access. Admins must opt in. https://support.microsoft.com/en-us/windows/ai/ai-features/experimental-agentic-features
- **Perplexity Comet.** Brave showed in Aug 2025 that hidden page text (Reddit spoiler tags) could hijack the agent into leaking an email address and an OTP. Brave says the fix was incomplete. https://www.theregister.com/2025/08/20/perplexity_comet_browser_prompt_injection/
  - Lesson: anything Yuki reads from the screen (mail, chat, web) is untrusted input. It must never be treated as instructions to act on.
- **Simular Pro / Agent S** is the most open desktop-agent stack.

### 2.6 What drives reliability and speed (synthesis)
1. **API/CLI/code first, GUI last.**
   - UFO2: about 58% fewer steps with o1 on Office.
   - CoAct-1: about 33% fewer steps and higher success.
   - Anthropic's "code execution with MCP" (Nov 2025): one example dropped from about 150k to 2k tokens. Independent tests show about 37–78% savings, and the 98.7% figure is a single cherry-picked case.
2. **Batch actions.** Predict several actions per call and validate each with a cheap local check: 44–51% fewer LLM calls (UFO2).
3. **Cache plans and skills.** Agentic Plan Caching (NeurIPS 2025): **−50% cost, −27% latency, 96.6% of accuracy kept** [Measured]. https://arxiv.org/abs/2506.14852
4. **Planning dominates latency.** Use a cheap model or a cached plan for routine steps and the big model only at decision points (OSWorld-Human).
5. **Grounding quality.** Most WAA failures are perception failures. Hybrid perception helps, and so does the app-API route.
6. **Verify after each action** with cheap state checks, not model calls.
7. **App-specific knowledge** (UFO2 AppAgents, Agent S2 specialists) beats one generalist that has to rediscover every app.

---

## 3. Learning from demonstration and workflow induction

### 3.1 Research systems
| System | What it does | Result | Note |
|---|---|---|---|
| **Agent Workflow Memory (AWM)**, ICML 2025, https://arxiv.org/abs/2409.07429 | Induces reusable natural-language workflows (with variable slots) from successful trajectories. Retrieves them as in-context guidance, offline or online. | WebArena **+51.1% relative** success over the best autonomous baseline. Beat human-written workflows by 7.9%. Mind2Web +24.6% relative step success. Fewer steps. | Relative gains on a low base. Workflows are *text*, so the model still re-executes every step. |
| **ASI, Agent Skill Induction** (CMU, COLM 2025), https://arxiv.org/abs/2504.06821 | Turns successful trajectories into **executable programs** (e.g. `search_product(name)`). A skill is kept only if re-running it solves the task, it calls the new skill, and every call changes the state. | WebArena **+23.5% over the static baseline, +11.3% over text skills; 10.7–15.3% fewer steps**. | **Programmatic verification** is what made the difference. |
| **SkillWeaver** (OSU/CMU, Apr 2025), https://arxiv.org/abs/2504.07079 | Proposes skills, practises them, synthesizes Playwright functions, then "hones" them with tests. | WebArena +31.8% relative; real sites +39.8%. Skills from strong agents lifted weak agents by up to 54.3%. | Self-practice without a user. |
| **ICAL** (CMU/DeepMind, NeurIPS 2024) [Older], https://arxiv.org/abs/2406.14596 | A VLM rewrites noisy demonstrations into cleaned "programs of thought" (causal links, state changes, subgoals), with human feedback on failure. | VisualWebArena 14.3% → 22.7% (GPT-4V). Needs less feedback as the library grows. | Clean the demo before storing it. |
| **AgentRR, Record & Replay** (SJTU, May 2025), https://arxiv.org/abs/2505.17716 | Records traces and summarizes them into **multi-level experience**: low-level exact replay for near-identical contexts, high-level procedure for changed contexts. Each experience gets a **check function** that bounds replay. | Systems paper; little benchmark data. | The right *shape* for Yuki: replay without the model when the context matches, fall back to the model when it doesn't. |
| **ReasoningBank** (Google, Sep 2025), https://arxiv.org/abs/2509.25140 | Distils strategies from self-judged successes *and failures* ("don't click load-more when nothing new appears"). | WebArena +4.6 to +8.3 points; about 1.6–2.1 fewer steps. | Modest. Failure lessons are cheap and valuable. |
| **Task Model Induction** (Stanford/CMU, Aug 2026), https://arxiv.org/abs/2608.20319 | Takes **passive, interleaved** screen and mouse/keyboard traces, separates out the latent tasks, and builds a goal hierarchy plus a procedure model per task. | 0.974 agreement with ground-truth task grouping; reconstructs 74.9% of steps; skills derived from it give **+30% held-out accuracy** over the best baseline. | The closest match to "Record how I work". Evaluated on controlled traces. |
| **"How should agents read demonstrations?"** (Jun 2026), https://arxiv.org/abs/2606.20978 | Same action sequence, presented as a flat log vs as hierarchical subgoals. | On vague tasks: **76.7% → 90.7% with hierarchical demos**. Flat demos gave no significant gain. On precise tasks no format helped. **Pre/postconditions and parameter annotations: no measurable benefit.** | Store demonstrations as labelled subgoals. Their value is in disambiguating *vague* requests. |
| **ShowUI-Aloha** (Jan 2026), https://arxiv.org/abs/2601.07181; **UI-Mate** (Aug 2026), https://arxiv.org/html/2608.15930; **ALLOY** (Oct 2025); **AppAgent-Claw** (Apr 2026), https://arxiv.org/abs/2606.05171 | Record a desktop demo, parse it into intent/subtask workflows, execute with re-planning. UI-Mate lets the live screen override the recording. AppAgent-Claw replays *without model inference* and checks each on-screen effect. | Mostly system papers with few numbers in the abstracts. | Confirms the pattern: a record–parse–abstract–verify pipeline. |
| **Voyager** (2023) [Older], https://arxiv.org/abs/2305.16291 | Skill library of verified code with an automatic curriculum (Minecraft). | 3.3× more unique items; tech-tree milestones up to 15.3× faster. | The original "skills as verified code" result. |

### 3.2 Why learned skills don't get used: the 2026 evidence
- **SkillsBench (Feb 2026, v1→v4)** [Measured]. https://arxiv.org/abs/2602.12670
  - Curated skills raise the pass rate from **33.9% to 50.5%**.
  - **"Self-generated Skills provide no benefit on average"** (v1 abstract).
  - Focused skills with at most 3 modules beat comprehensive bundles.
  - 16 of 84 tasks got *worse* with skills.
- **"How well do agentic skills work in the wild"** (Apr 2026) [Measured]. https://arxiv.org/abs/2604.04323
  - **Agents often don't load curated skills even when they are directly available.** The gap widens with distractors and when retrieval is needed.
  - In realistic settings, skill gains approach the no-skill baseline.
  - Query-specific refinement recovers part of the loss (Terminal-Bench 2.0: 57.7% → 65.5%).
- A further 2026 preprint (hyper.ai listing 2608.14036, not checked against the primary source): skill-use precision falls **from 29.6% to 3.3% as the pool grows from 5 to 100 skills**.
- Practitioner guidance: descriptions should state the *triggering condition* ("Use when …"), not the topic.
- **Lesson for Yuki.** This matches "know-how rarely used" closely. A large library of auto-written text know-how gives about zero benefit on average and is mostly not retrieved. What works:
  - few skills;
  - verified, executable skills;
  - descriptions written as trigger conditions;
  - deterministic matching where possible: if the app and goal pattern match, the harness injects or runs the skill, rather than hoping the model picks it.

### 3.3 Products and enterprise process/task mining
- **UiPath Task Mining** [Vendor docs].
  - Records clicks, keys and screenshots on consented machines and needs **20k–70k actions** before analysis.
  - ML clusters traces into task groups and merges variants into a graph with decision points.
  - Tasks are ranked by **repeatability** (how many users, how often), **complexity** (steps, time) and **effort** (total time spent).
  - It says outright that it does not discover unknown tasks automatically; that is a separate mode with its own limits.
  - https://docs.uipath.com/task-mining/automation-suite/2024.10/user-guide/best-practices-utm
- **Power Automate task/process mining** [Vendor docs]. A recorder captures the process. Analytics then generates a draft desktop flow **when the activity uses an app that has native Power Automate actions** (Outlook, Excel). In other words, it maps observed GUI steps onto API actions. https://learn.microsoft.com/en-us/power-automate/task-mining-tutorial
- **Windows Recall** (GA Apr 2025). Opt-in, local, encrypted, Windows Hello-gated. Screenshots plus OCR of the active window. Click to Do offers actions on what is on screen. It is a memory and search product, not an automation one. https://www.techrepublic.com/article/news-microsoft-recall-expands-rollout/
- **Rewind / Limitless.** Acquired by Meta on 5 Dec 2025. Screen and audio capture was disabled on 19 Dec 2025. Whole-screen "remember everything" never became a business on its own. https://9to5mac.com/2025/12/05/rewind-limitless-meta-acquisition/
- **Screenpipe** (YC S26). Event-driven capture: it saves a frame on app switch, click or typing pause, pairs it with the **accessibility tree** and falls back to OCR. "Pipes" are a markdown prompt plus a schedule run by a coding agent (e.g. Claude Code) over the captured data. Per-pipe permissions. About 5–10% CPU [Vendor claim]. Architecturally the nearest open project to Yuki.
- **Anthropic Agent Skills** (Oct 2025; open standard Dec 2025). A folder with SKILL.md: name and description always loaded, body on demand, scripts executable. https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills

### 3.4 How observed actions become reusable, parameterised skills (the common pipeline)
1. **Segment** the raw event stream into tasks, using app switches, idle gaps, screen changes and goal shifts (TMI, the workflow-induction toolkit).
2. **Abstract** each task into a goal hierarchy of subgoals with natural-language labels. Generalize literals into parameters (AWM, ICAL, hierarchical demos).
3. **Cluster** repeated instances. A skill is only a candidate once it has been seen N times with a stable path (task mining's repeatability and effort scores).
4. **Compile** to the cheapest executable form: an API/CLI/COM call, then a parameterised UIA script, then text guidance for the model (ASI, SkillWeaver, Power Automate).
5. **Verify** by re-execution and a postcondition check before admitting the skill (ASI, SkillWeaver honing, AgentRR check functions).
6. **Decide reuse**:
   - Deterministic match on context (app, window, goal type) → exact replay with checks (AgentRR low-level).
   - Partial match → give the model the high-level procedure.
   - No match → plan from scratch.
   - Retire skills whose checks fail repeatedly.

---

## 4. Proactive agents

### 4.1 Foundational HCI
- **Horvitz, "Principles of Mixed-Initiative User Interfaces" (CHI 1999)** [Older, still foundational]. https://erichorvitz.com/uiact.htm
  - Note: the PDF is a scan. The list below is reproduced from the well-known paper, not machine-extracted.
  - The 12 factors:
    1. Develop significant value-added automation.
    2. Consider uncertainty about the user's goals.
    3. Consider the status of the user's attention when timing services.
    4. Infer the ideal action in light of costs, benefits and uncertainties.
    5. Use dialog to resolve key uncertainties.
    6. Allow efficient direct invocation and termination.
    7. Minimize the cost of poor guesses about action and timing.
    8. Scope the precision of the service to match uncertainty.
    9. Provide mechanisms for efficient agent–user collaboration to refine results.
    10. Use socially appropriate behaviours.
    11. Maintain working memory of recent interactions.
    12. Continue to learn by observing.
  - Lookout used **expected-utility thresholds**: act automatically when P(goal) > P\*action, ask (dialog) when it falls between P\*dialog and P\*action, do nothing below. It timed its offers to the user's attention, waiting longer for longer messages.
- **Iqbal & Bailey (CHI 2005/06/08)** [Older, Measured].
  - Interruptions at **task breakpoints**, especially low-workload ones, consistently reduce resumption lag and annoyance.
  - Holding notifications until a detected breakpoint reduced frustration and reaction time in authentic tasks.
  - https://www.interruptions.net/literature/Iqbal-CHI08.pdf

### 4.2 Proactive-agent research (2024–2026)
- **Proactive Agent / ProactiveBench** (ICLR 2025) [Measured]. https://arxiv.org/abs/2410.12361
  - Collected 6,790 real activity events and trained a reward model that agrees with humans at **91.8% F1**.
  - Their fine-tuned agent reached **66.5% F1** at proposing appropriate tasks; Claude 3.5 Sonnet scored 62%.
  - All models do well at spotting a need but **badly on false alarms**: they propose help when none is wanted.
- **ProAgentBench** (Feb 2026) [Measured]. https://arxiv.org/abs/2602.04482
  - 28k events from 500+ hours of real desktop sessions.
  - Best **"when to assist" accuracy is 64.4%**; best **"how to assist" intent accuracy is 37.1%**.
  - Long-term memory and history help significantly, and real data beats synthetic.
  - Predicting *what* the user needs is much harder than predicting *that* they need something.
- **PIRA-Bench** (Mar 2026). https://arxiv.org/abs/2603.08013. Includes pure-noise trajectories to test **restraint**: the agent should stay idle when the user has no intent.
- **PROBE** (Oct 2025; ICLR 2026). https://arxiv.org/abs/2510.19771
  - Splits proactivity into finding an unspecified issue → identifying the bottleneck → resolving it.
  - Best end-to-end score is **40%** (GPT-5, Claude Opus 4.1).
- **Pare / Pare-Bench** (Apr 2026) [Measured, simulated users]. https://arxiv.org/abs/2604.00842
  - Claude 4.5 Sonnet: **highest acceptance (78.2%) and lowest proposal rate (12.8%)**.
  - GPT-5 proposed most often (28.1%) and had lower acceptance (70.2%), i.e. overconfident goal inference.
  - Lessons: gather context (read actions) *before* proposing, propose only when confident, and make proposals specific. Premature proposals send users off exploring on their own.
- **ProPerSim** (Sep 2025; ICLR 2026). https://arxiv.org/abs/2509.21730
  - Simulated users with 32 personas over 14 days. The assistant learns from each user's ratings of its suggestions, and satisfaction rises steadily.
  - The follow-up **EOPA** (Aug 2026) learns each user's *timing* preference (temporal anchors plus activity prototypes) online, without retraining: **timing F1 +19.8 points**; daily adaptation 11.4 s → 0.39 s. https://arxiv.org/abs/2608.04416
- **Codellaborator, "Assistance or Disruption?"** (CHI 2025) [Measured, n=18]. https://arxiv.org/abs/2502.18658
  - Proactive help made users more efficient than prompt-only, but disrupted their flow.
  - **Showing the agent's presence and the context behind its help reduced the disruption.**
  - Proactivity should adapt to the stage of the work.
- **Sensible Agent** (Google, UIST 2025) [Measured, n=10, preliminary]. https://research.google/blog/sensible-agent-a-framework-for-unobtrusive-interaction-with-proactive-ar-agents/
  - Decides both *what* to offer and *how*: the modality and a low-effort confirmation such as a nod.
  - Lower perceived effort and intrusiveness than a voice-prompted (Astra-like) baseline.
- **Field data on acceptance** [Measured, cited secondhand]. Delaying LLM code suggestions until a feedback moment raised acceptance from **4.9% to 18.6%** over two months (Al Awad et al. 2025, cited in arXiv 2605.06717). Even good proactive systems in the wild see acceptance in the teens to the low tens of percent.

### 4.3 Products
- **ChatGPT Pulse** (Sep 2025). https://www.tomsguide.com/ai/chatgpt-pulse-is-here-now-ai-starts-the-chat-and-curates-your-feed
  - Runs **overnight asynchronous research** over chat history, memory, feedback and optional Gmail/Calendar.
  - Delivers a **morning batch of 5–10 cards** that expire daily unless saved.
  - Feedback is thumbs up/down per card plus a "Curate" box ("show me X tomorrow").
  - Its design avoids interruption altogether: one batched, predictable moment the user chooses to open.
- **Littlebird** (Mac, launched Mar 2026, $11M seed). Reads on-screen text (no screenshots) plus meeting audio. Its proactive output is mainly **scheduled "Routines"** such as a Monday briefing. There is little evidence of real-time anticipatory interruptions [Vendor claim]. https://littlebird.ai/blog/littlebird-raises-11m-to-build-the-first-full-context-ai
- **Microsoft Copilot on Windows.** The late-2025 push of Copilot entry points and "AI Suggestions" drew visible user backlash over intrusiveness and lack of opt-out. Microsoft began removing Copilot buttons from built-in apps and reworking Recall. https://www.pcworld.com/article/2995972/no-one-asked-for-this-microsofts-copilot-ai-push-sparks-social-media-backlash.html
- **Google Gemini / Project Astra.** Astra is described as able to "start conversations" proactively, but it is still a prototype for testers. I found no published mechanism or acceptance data [Vendor claim]. https://deepmind.google/project-astra
- **Rewind/Limitless.** Shut down after the Meta acquisition (above).
- **Highlight AI, Dia (Browser Company → Atlassian, 2025).** Context-aware assistants invoked on demand. I found no published proactive acceptance data.
- **LangChain "ambient agents" and the Agent Inbox** (2025). https://blog.langchain.com/introducing-ambient-agents
  - Agents listen to event streams, not prompts. They surface three kinds of item: **notify** (FYI), **question** (needs input), **review** (approve or edit a drafted action).
  - "Ambient does not mean fully autonomous."

### 4.4 What produces offers users actually accept (synthesis)
1. **Restraint.** The best systems propose rarely: Pare's best had a 12.8% proposal rate and 78% acceptance. Benchmarks now explicitly score false alarms (ProactiveBench, PIRA-Bench).
2. **Ground offers in the user's *goals*, not the current screen.** "When" is easier than "what" (64% vs 37% in ProAgentBench), and memory and history are what lift "what".
3. **Timing at breakpoints**: task switches, after a send or save, at session start, during idle (Iqbal & Bailey). Learn each user's timing preference online (EOPA).
4. **Do the work first, then offer the result.** "I drafted the reply to X — send?" (a review item) beats "Want me to draft a reply?" (a question). A finished artifact costs the user less to judge (Horvitz #7, #9; the agent-inbox "review" type).
5. **Batch low-urgency items into a predictable digest** (Pulse, Littlebird Routines). Interrupt in real time only for time-critical items.
6. **Show presence and the "why"** (Codellaborator): one line of evidence on each card ("you asked Priya for this on Tuesday").
7. **Close the loop.** Track accept, dismiss and ignore per card *type*. Lower the frequency automatically or suppress types with low acceptance. Offer an explicit "curate" control (Pulse, ProPerSim).
8. **Scope precision to confidence** (Horvitz #8). If confidence is low, either ask one question or stay silent.

---

## 5. Decision-making and reasoning

- **ReAct vs plan-then-act.** Plan-and-Act (ICML 2025) uses a separate Planner that writes a high-level plan and **replans after each action**, with an Executor that grounds it. It scored 57.58% on WebArena-Lite (SOTA at the time). https://arxiv.org/abs/2503.09572
  - Agent S2's "proactive hierarchical planning" and Claude Code's explore → plan → implement → verify follow the same shape.
  - Plans are what make caching possible (Agentic Plan Caching) and what make "steer mid-task" possible: the user edits the plan, not the clicks.
- **Belief state / world model.**
  - WebDreamer (TMLR 2025) and WMA agents (ICLR 2025) simulate an action's outcome before committing, which helps with irreversible actions. Gains are modest; follow-up work (WAC, 2026) reports +1–2 points. https://arxiv.org/abs/2411.06559 · https://arxiv.org/abs/2410.13232
  - For a personal assistant, the more useful "world model" is a **structured model of the user's open goals**: threads, commitments, deadlines, who is waiting on whom. That is what Horvitz's goal inference and ProAgentBench's memory gains point to.
- **Reflection and post-mortems.**
  - Leave failures in context (Manus).
  - Distil success and failure lessons into strategy memory (ReasoningBank, +4.6–8.3 points).
  - Prefer fixing the harness or tools over adding prompt rules (Anthropic: "add instructions based on observed failures", then prune).
- **Uncertainty and clarifying questions.** CollabLLM (ICML 2025 Outstanding Paper) trained models to optimise multi-turn outcomes, so they ask clarifying questions when it pays off. Results [Measured]: +18.5% task performance, **+17.6% user satisfaction, −10.4% user time** in a 201-person study. https://arxiv.org/abs/2502.00640
  - Hierarchical demonstrations help mainly on *vague* requests, so learned procedures can stand in for a clarifying question.
- **Assistant vs bot.**
  - A *bot* executes the command in front of it.
  - An *assistant* holds the user's goals, works on them in the background, and returns reviewable results. Examples:
    - Cursor background agents and Codex cloud: an isolated sandbox per task, asynchronous hand-off, a PR or diff as the review surface; suited to "tasks with clear acceptance checks".
    - Devin: a manager–child setup with a clean-context reviewer.
    - Anthropic's long-running harness: a feature list plus progress file.
    - LangChain ambient agents: event-driven, with an inbox of notify, question and review items.
  - Common architecture: an **event → triage (cheap) → queue → background worker (expensive, sandboxed) → verifiable artifact → inbox** pipeline. The user's attention is spent only on reviewing.

---

## 6. Synthesis for Yuki

### 6.1 Most transferable principles
1. **One loop, small tools, workflows where possible.**
   - Default to deterministic workflows: recall, card building, routing.
   - Run a single agent loop for open-ended work, with roughly 10–20 well-designed tools. Load everything else as skills.
   - Extra agents should only *read* or *review*; all desktop writes go through one thread (Anthropic, Cognition 2026, 12-factor).
2. **API/CLI/COM first, GUI last.** For each frequent app, build or find an action layer: Office COM, Outlook/Graph, browser via CDP, Explorer and the file system via PowerShell, chat apps via their APIs or URL schemes. Click-by-click UIA is the fallback. Expected effect from the literature: about 30–60% fewer steps (UFO2, CoAct-1).
3. **Batch actions and verify locally.** Let the model emit a short action sequence per call. The harness validates each step with UIA state checks (no model call) and only goes back to the model on a mismatch. UFO2 saw 44–51% fewer calls.
4. **Compile what's learned into verified, executable skills.** Pipeline: observed task → hierarchical subgoals with parameters → compiled script (API or UIA) → verified by re-run and postcondition → stored with a *trigger condition*. Then:
   - Replay deterministically when the context matches; give the model the procedure on a partial match (AgentRR, ASI).
   - Admit few skills, and only verified ones. Self-written text know-how shows about zero average benefit (SkillsBench).
5. **Retrieve skills deterministically, not by hoping the model picks one.** Match on app, window and goal type in code and inject or run the skill. Retrieval precision collapses as libraries grow (29.6% → 3.3% from 5 to 100 skills). Track hit rate, success rate and last use per skill, and retire dead skills.
6. **Make goals the core memory object.** Open threads, commitments and who is waiting form a structured belief state about the user's goals. Timeline, people and portrait are supporting evidence. Proactivity and planning both read from it (Horvitz #2, ProAgentBench).
7. **Proactivity: be rare, timely, evidence-backed and done-first.**
   - Propose only above a confidence threshold, at breakpoints, with a one-line "why".
   - Prefer a *finished draft to approve* over a question.
   - Batch non-urgent items into a digest at a moment the user chooses (Pulse).
   - Learn timing per user, and auto-suppress card types with low acceptance (Pare, EOPA, Iqbal & Bailey, Codellaborator).
   - At a 67% ignore rate, the first job is to cut volume and measure acceptance per card type.
8. **Use background workers for the "assistant" behaviour.** Detected goals become queued jobs with explicit done-checks. They run off-screen (Windows Agent Workspace or a second desktop, or headless APIs) and return reviewable artifacts to an inbox (notify, question, review). The voice front end becomes a way to steer the queue, not the main way work gets done.
9. **Verification closes every loop.** Each action and skill has a machine-checkable postcondition. The user should never be the verification loop (Claude Code docs).
10. **Context engineering for cost and latency.**
    - Stable cached prefix, with screen-tree observations appended late and diffed.
    - Just-in-time memory retrieval, restorable compression, and a recited to-do list for long tasks.
    - Planning calls dominate latency (OSWorld-Human), so cache plans (−50% cost, −27% latency) and use the small model for routine steps.
11. **Trace everything and evaluate small.** Per-task traces with calls, tokens, latency, success and skill hits. Keep a small fixed regression suite of real tasks (about 20–40) re-run on every change, with tool-level metrics (Anthropic "Writing tools for agents"). This replaces the patch-per-incident loop.
12. **Treat screen content as untrusted.** Text read from mail, chat or web must never become an instruction to act (the Comet prompt injection). Side-effecting actions need confirmation or an allowlist.

### 6.2 Anti-patterns behind "bag of patches" agents
- **Prompt rules as patches.** Every failure adds an instruction. The prompt grows until rules contradict each other and get ignored (Claude Code: "bloated CLAUDE.md files cause Claude to ignore your actual instructions"). Fix with code, tools or hooks, then prune.
- **Heuristic special cases in code** for one app or one incident, instead of a general action layer. Anthropic warns against "brittle hard-coded if-else logic" at the prompt level, and the same applies in code.
- **Tool sprawl** with overlapping tools, and adding or removing tools mid-task (breaks the KV cache, confuses tool choice).
- **Memory as a dump.** Everything captured is pushed into context. The result is context rot, high cost and no improvement in decisions.
- **Learned know-how as unverified prose** in a large library the model has to choose from. It doesn't get picked, and it doesn't help when it is.
- **Parallel writers.** Multiple agents (talker, thinker, workers) acting on the desktop without one owner, so decisions conflict.
- **No verification signal.** The agent "thinks it's done" and the user notices the failures.
- **Proactivity by volume.** Frequent, low-confidence, screen-triggered nudges. Users learn to ignore them (Microsoft Copilot backlash; ProactiveBench's false-alarm failures).
- **Benchmarking by anecdote.** Without a fixed eval set, every fix risks a regression elsewhere, which pushes development toward more patches.
- **Over-orchestration.** Multi-agent graphs and frameworks for what could be a workflow plus one loop. Cost goes up (about 15× tokens for multi-agent) and failures are harder to locate.

### 6.3 Trade-off note on Yuki's "no screenshots / no vision" rule
UIA-only is cheap, private and structured. UFO2's best mode still starts from UIA. Its cost is measured, though: on WAA, more than 62% of UIA-agent failures were control-detection failures in apps with weak accessibility, and a vision fallback recovered 10–25% of them. Two routes keep the rule intact:
- **Native app APIs** (COM, Graph, CDP DOM), which avoid perception entirely and are also faster.
- **App-specific skills** that encode where things are in apps with poor accessibility.

If those apps matter to the user, the trade-off is worth revisiting deliberately, rather than patching around it one app at a time.

---

## Source list (main)
- Anthropic: https://www.anthropic.com/research/building-effective-agents · https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents · https://www.anthropic.com/engineering/building-agents-with-the-claude-agent-sdk · https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents · https://www.anthropic.com/engineering/multi-agent-research-system · https://www.anthropic.com/engineering/writing-tools-for-agents · https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills · https://code.claude.com/docs/en/best-practices
- Cognition: https://cognition.ai/blog/dont-build-multi-agents · https://cognition.com/blog/multi-agents-working
- Manus: https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus
- 12-factor agents: https://github.com/humanlayer/12-factor-agents
- OpenAI: https://openai.github.io/openai-agents-python/ · https://openai.com/index/unrolling-the-codex-agent-loop/
- Benchmarks: https://xlang.ai/blog/osworld-verified · https://arxiv.org/abs/2606.29537 (OSWorld 2.0) · https://arxiv.org/abs/2506.16042 (OSWorld-Human) · https://arxiv.org/abs/2409.08264 (WAA) · https://benchlm.ai/benchmarks/osworld-verified · https://epoch.ai/blog/what-does-osworld-tell-us-about-ais-ability-to-use-computers
- Desktop agents: https://arxiv.org/abs/2504.14603 (UFO2) · https://github.com/microsoft/UFO · https://arxiv.org/abs/2508.03923 (CoAct-1) · https://arxiv.org/abs/2504.00906 (Agent S2) · https://www.simular.ai/articles/agent-s3 · https://arxiv.org/abs/2510.22780 · https://arxiv.org/abs/2506.14852 (plan caching) · https://support.microsoft.com/en-us/windows/ai/ai-features/experimental-agentic-features · https://www.theregister.com/2025/08/20/perplexity_comet_browser_prompt_injection/
- Learning: https://arxiv.org/abs/2409.07429 (AWM) · https://arxiv.org/abs/2504.06821 (ASI) · https://arxiv.org/abs/2504.07079 (SkillWeaver) · https://arxiv.org/abs/2406.14596 (ICAL) · https://arxiv.org/abs/2505.17716 (AgentRR) · https://arxiv.org/abs/2509.25140 (ReasoningBank) · https://arxiv.org/abs/2608.20319 (TMI) · https://arxiv.org/abs/2606.20978 (hierarchical demos) · https://arxiv.org/abs/2601.07181 (ShowUI-Aloha) · https://arxiv.org/abs/2606.05171 (AppAgent-Claw) · https://arxiv.org/abs/2602.12670 (SkillsBench) · https://arxiv.org/abs/2604.04323 (skills in the wild) · https://docs.uipath.com/task-mining/ · https://learn.microsoft.com/en-us/power-automate/task-mining-tutorial
- Proactive: https://erichorvitz.com/uiact.htm · https://www.interruptions.net/literature/Iqbal-CHI08.pdf · https://arxiv.org/abs/2410.12361 · https://arxiv.org/abs/2602.04482 · https://arxiv.org/abs/2603.08013 · https://arxiv.org/abs/2510.19771 · https://arxiv.org/abs/2604.00842 · https://arxiv.org/abs/2509.21730 · https://arxiv.org/abs/2608.04416 · https://arxiv.org/abs/2502.18658 · https://research.google/blog/sensible-agent-a-framework-for-unobtrusive-interaction-with-proactive-ar-agents/ · https://blog.langchain.com/introducing-ambient-agents · https://littlebird.ai/blog/littlebird-raises-11m-to-build-the-first-full-context-ai · https://9to5mac.com/2025/12/05/rewind-limitless-meta-acquisition/
- Decision-making: https://arxiv.org/abs/2503.09572 (Plan-and-Act) · https://arxiv.org/abs/2502.00640 (CollabLLM) · https://arxiv.org/abs/2411.06559 (WebDreamer) · https://arxiv.org/abs/2410.13232 (WMA)
