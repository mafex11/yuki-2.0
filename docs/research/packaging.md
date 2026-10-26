# Yuki — packaging, distribution and RAM research (2026-09-27)

Read-only research for shipping Yuki as a downloadable Windows app to other users.
Grounded in `docs/HANDBOOK.md`, `docs/ARCHITECTURE.md`, `docs/MEMORY.md` ("Footprint"),
`pyproject.toml`/`uv.lock`, `yuki/__init__.py`, `yuki/config.py`, `yuki/memory/embed.py`,
`yuki/voice/keys.py`, `yuki/memory/service.py`, `yuki/ui/boot.py`, `yuki/ui/window/__main__.py`,
plus current (Sept 2026) web research on PyInstaller/Nuitka, Azure Artifact Signing (formerly
Trusted Signing), MSIX, and Velopack. No code was changed.

**Bottom line recommendation:** PyInstaller (**onedir**, not onefile) for both frozen
executables → Inno Setup (per-user, no admin) → Azure Artifact Signing (individual tier,
$9.99/mo) from day one → Velopack for updates once the onedir build is proven → GitHub
Actions on `windows-latest` doing all of the above. Defer Nuitka and MSIX; they solve
problems Yuki doesn't have yet (AV false positives, Store distribution) at a cost
(long C-compile CI, Store/MSIX process-model friction) Yuki doesn't need to pay yet.

---

## 0. What's actually running today (baseline)

Two long-lived processes plus two short-lived helpers, confirmed from code:

- **`yuki-ui` (tray)** — started at logon via `pythonw -m yuki.ui.boot` (HKCU Run key,
  per-user, no admin). Spawns the memory service (`yuki/ui/startup.py`/`yuki/ui/app.py`,
  `ensure_service`) and watches it with a 2-minute watchdog that matches processes by
  **`CommandLine`** substring (`yuki.memory.service`, `yuki.ui.boot`) — this is a packaging
  landmine, see Phase 1 below.
- **`yuki-memory` (service)** — `yuki/memory/service.py`: watcher, journal, timeline,
  portrait, coach, calls, the shared fastembed embedder, an MCP server. Deliberately its own
  process ("a crash here must never take Yuki down").
- **Deferred call transcriber** — `yuki/memory/transcribe.py`, its own short-lived process,
  loads ctranslate2 + faster-whisper on CUDA only when the GPU is idle, exits after the batch.
  The service **never** imports a speech model or touches the GPU itself.
- **Window** — `yuki/ui/window/__main__.py` (`pythonw -m yuki.ui.window`), its own process,
  opened from the tray, 0 MB when closed. Already the right shape for packaging.

Model access today is **entirely environment-variable based, no files**: `AWS_BEARER_TOKEN_BEDROCK`
for `anthropic.AnthropicBedrock` (constructed in `yuki/agent/loop.py`, `yuki/agent/failover.py`,
`yuki/voice/pipeline.py`), `DEEPGRAM_API_KEY`/`ELEVENLABS_API_KEY` read by `yuki/voice/keys.py`
from `os.environ` then `HKCU\Environment` as a fallback. This is convenient for the owner (who
already has AWS/Bedrock access) and a hard onboarding blocker for anyone else — see §3.

Measured footprint (`docs/MEMORY.md` "Footprint", 2026-09-24/25, read-only measurements on this
PC, Ryzen 2700/16 GB):

| State | Private bytes |
|---|---|
| Service, idle, embedder unloaded | **51–100 MB** |
| Service, active, embedder loaded (journal runs every few minutes) | **643–648 MB** |
| Tray, cold (imports only, offscreen) | **112 MB** |
| Tray, live before recent cuts (full agent import graph at boot) | 587 MB |
| Deferred transcriber, large-v3 on CUDA (its own process, exits after batch) | ~2.6–3.7 GB private, ~1.9 GB VRAM |

The user's "650–850 MB running" figure lines up with **service-active (≈645 MB) + tray
(≈112–170 MB)**. The single largest resident chunk by far is the fastembed embedder
(~520–570 MB: a ~260 MB tokenizer plus the ONNX session), already the target of §4.
`numpy`'s OpenBLAS threads were already cut (`OPENBLAS_NUM_THREADS=1` in `yuki/__init__.py`,
−490 MB/process) and the embedder already unloads after 5 idle minutes
(`yuki/memory/embed.py`, `IDLE_UNLOAD_S=300`). No `torch` dependency exists anywhere in
`uv.lock` — CUDA use is confined to `ctranslate2`/`faster-whisper` in the transcriber process.

---

## 1. Packaging options

| Tool | Fit for this stack | Build size/startup | AV | Verdict |
|---|---|---|---|---|
| **PyInstaller (onedir)** | Mature first-party hooks for PySide6, onnxruntime, numpy, pywin32/comtypes; broad community hook zoo for exactly these kinds of ML/native packages | Onedir cold start ~1–3 s typical; folder size dominated by Qt + onnxruntime + (if bundled) CUDA DLLs, same order regardless of tool | Onedir avoids the self-extraction heuristic that flags onefile builds; still needs code signing to fully avoid SmartScreen friction | **Recommended for v1** |
| **Nuitka (standalone, not onefile)** | Qt itself recommends Nuitka or PyInstaller for PySide6 (`--enable-plugin=pyside6`); compiles to C, so no bootstrap-unpack step | Standalone folder similar size to PyInstaller; **much longer CI build times** (real C compilation of a project this size with PySide6+onnxruntime can be tens of minutes) | Real-world reports show far fewer AV engine flags than PyInstaller (one dev: 11/61 → 1/60 engines after switching) — genuinely compiled code, not a bundled interpreter | Strong v2 candidate once the frozen dependency graph is proven under PyInstaller; free edition is sufficient (Commercial adds IP-protection/obfuscation features Yuki doesn't need) |
| **Briefcase (BeeWare)** | Windows support is real but young for this shape of app (MSI or zip output; April 2026 added ARM64 + MSI banner customization); Briefcase's own docs frame PyInstaller/Nuitka/cx_Freeze as the established desktop-target competition | — | — | Not recommended; BeeWare's own primary UI target is Toga, not PySide6 |
| **cx_Freeze** | Works, but has a narrower, less actively updated hook set than PyInstaller for exotic native packages (sherpa-onnx, livekit-rtc) | — | — | Not recommended over PyInstaller |
| **Embeddable Python + launcher** | Ships `python.exe` itself (PSF-signed, well-known — sidesteps freezing bugs entirely: no import-graph analysis to get wrong for a dependency tree this size) | Larger on disk (ships the full interpreter + site-packages verbatim) but the most robust for big ML dependency trees; several "portable AI app" projects use exactly this shape today | No bootstrap-unpack heuristic at all (it's literally CPython) | Worth prototyping in parallel as a fallback if PyInstaller's import scanning breaks on sherpa-onnx/livekit/winrt in ways hooks can't fix quickly; needs manual `pywin32_postinstall.py` handling since the embeddable distribution doesn't run wheel post-install scripts |

**Specific compatibility notes found:**

- **onnxruntime, PySide6, pywin32/comtypes** — all have first-class PyInstaller hooks; lowest risk.
- **sherpa-onnx** ships its wake-word model files as package data; PyInstaller's default
  onnxruntime/generic hooks will **not** know to collect sherpa-onnx's own data files —
  budget for an explicit `--collect-data sherpa_onnx` / `datas=` spec entry and test the
  frozen build actually finds the wake-word model.
- **livekit / livekit-rtc** ships native (Rust) extension wheels; these are binary-as-is
  packages PyInstaller normally copies whole, but this project has less documented PyInstaller
  precedent than PySide6/onnxruntime — budget real testing time here.
- **ctranslate2 + CUDA is the hardest problem, and it is independent of the packaging tool.**
  Confirmed via current issue reports: pip-installed `nvidia-cublas-cu12`/`nvidia-cudnn-cu12`
  drop their DLLs under `site-packages/nvidia/<pkg>/bin`, which Windows never adds to the DLL
  search path; `os.add_dll_directory()` alone is not always enough because CTranslate2's native
  loader falls back to plain `PATH`. This will surface as a **silent CPU fallback or a late
  "cublas64_12.dll not found" error at first inference**, not at startup — whichever tool is
  used, `yuki/memory/transcribe.py`'s worker entry point needs an explicit, tested fix (add the
  `nvidia-*-cu12` and `ctranslate2` folders to the DLL search path before the first
  `ctranslate2`/`faster_whisper` import) and this must be re-verified once the layout changes
  under a frozen build (PyInstaller flattens `site-packages` differently). ctranslate2 4.5+
  also requires CUDA ≥ 12.3/cuDNN 9 — confirm the target machines' driver is new enough, and
  keep the existing CPU `small`-model fallback as the default when it isn't (already
  implemented per `docs/MEMORY.md`'s own comparison table).
- `pyproject.toml`'s `[tool.uv] override-dependencies = ["onnxruntime>=1.30.0"]` forces one
  onnxruntime version across pipecat's own pin and the memory service's Silero/fastembed use —
  this resolves at `uv sync` time today; a frozen build only ever sees the one resolved version,
  so this is not an added packaging risk, just worth keeping in mind when re-running `uv sync`
  for a release build.

---

## 2. Installer and updates

**Installer: Inno Setup, per-user, no admin.** Yuki already lives entirely in
`%LOCALAPPDATA%\Yuki` and registers itself in **`HKCU\...\Run`** (per-user, no elevation) —
this is exactly Inno Setup's `PrivilegesRequired=lowest` mode: install to
`%LOCALAPPDATA%\Programs\Yuki`, write the same HKCU Run key the app already writes today
(confirm `yuki/ui/startup.py` targets HKCU, not HKLM), uninstall cleans both. Free, mature,
enormous community precedent, no admin prompt for the user — the friction-minimal choice for
a single-user desktop assistant. **WiX/MSI** is more powerful (Group Policy, per-machine,
enterprise deployment) but needs the WiX v4/v5 toolchain and buys nothing Yuki needs today —
skip it. **MSIX** is attractive long-term (Store discoverability, clean per-user model by
design) but has two real frictions for this specific app: (a) sideloaded MSIX (i.e. not
through the Store) can only be installed via `Add-AppxPackage`/PowerShell with admin, or
requires the sideloading toggle enabled first — worse first-run friction than double-clicking
an Inno Setup `.exe`; (b) MSIX's per-app virtualized filesystem/registry views are a real risk
against an app that does its own DPAPI encryption, hand-rolled process management (watchdog
matching sibling processes by `CommandLine`), `SetWinEventHook`, and documents exact
`%LOCALAPPDATA%\Yuki\...` paths for debugging (HANDBOOK §3) — this would all need re-validation
under MSIX's virtualization. Revisit MSIX only if/when Store listing becomes a goal.

**Auto-update: Velopack.** Has a first-party Python SDK and documented PyInstaller support,
with one hard constraint that matches the recommendation above: **it requires onedir output,
not onefile** ("Velopack packages and updates a directory of files... the single self-extracting
executable produced by `--onefile` is not compatible"). It generates the installer, delta
updates and a self-updating portable package from one command; the `vpk` CLI needs the .NET SDK
at *build* time only, not at runtime on the user's machine. This is a stronger, more
current-in-2026 fit than a hand-rolled WinSparkle-style updater, but the combination with
Yuki's exotic native deps (CUDA DLLs, sherpa-onnx data files, livekit) is untested — treat as
a Phase 3 spike, not a day-one dependency.

**User data location:** `%LOCALAPPDATA%\Yuki` is already correct and should not change —
matches the per-user install, needs no admin, and is what the uninstaller should offer to
keep or delete.

---

## 3. First-run and onboarding

**API keys — this is the biggest real blocker to "other users," bigger than any packaging
choice.** Today the only model path is `AnthropicBedrock` reading `AWS_BEARER_TOKEN_BEDROCK`
(`yuki/agent/loop.py`, `yuki/agent/failover.py`, `yuki/voice/pipeline.py`). Requiring a new
user to have an AWS account, request Bedrock model access, and mint a bearer token is not a
realistic "download and run" flow. **Recommendation:** add a `model_backend` setting in
`yuki/config.py` (`bedrock` | `anthropic_api`) and branch the three client-construction sites
to build a plain `anthropic.Anthropic(api_key=...)` client when `ANTHROPIC_API_KEY` is set and
Bedrock isn't configured — a single pasted key is a realistic first-run step; keep Bedrock as
the owner's own opt-in advanced configuration. This is real engineering work (three call sites,
plus whatever retry/failover logic in `yuki/agent/failover.py` assumes Bedrock error shapes)
and should be scoped and tested like any other agent-layer change, not treated as a packaging
detail.

Deepgram and ElevenLabs keys have the same shape (env var / `HKCU\Environment`,
`yuki/voice/keys.py`) — for a first shippable release, have new users **bring their own free-tier
keys** via a first-run wizard (three key fields + "why" + links to each vendor's key page +
"skip voice for now," since voice is already an optional, feature-gated layer and the owner's
own ElevenLabs free quota is currently exhausted per HANDBOOK). Running a shared backend/proxy
so users don't need their own keys is a legitimate future option but is a separate product
(ongoing hosting cost, billing, abuse handling) — don't conflate it with packaging.

**Model downloads on first run.** Don't bundle either model in the installer:
- The fastembed embedder (~260 MB tokenizer + ONNX weights, `snapshot_download` into
  `%LOCALAPPDATA%\Yuki\memory\models`, per `yuki/memory/embed.py`) is needed for memory search
  from day one — download it with a visible, resumable progress bar on first run (`huggingface_hub`
  already supports resume).
- large-v3 (~3 GB, only used by the deferred call transcriber) should be **opt-in and lazy**:
  detect a CUDA-capable GPU (a `nvidia-smi` subprocess check is sufficient — no need for a
  torch/pynvml dependency just to detect a GPU), and only offer/download large-v3 if one is
  found and the user opts into call transcription; otherwise default silently to the existing
  CPU `small`-model fallback path (already implemented and already measured as noticeably worse
  on non-English but adequate as a safety net).

**Consent and privacy.** The existing per-call consent card (`yuki/ui/calls.py`,
Yes/No/Always/Never) is a good pattern to generalize into a **first-run ambient-capture consent
screen** before `SetWinEventHook` ever runs for a new install — today's denylists (banking,
password managers, adult sites) are good defaults but a new non-technical user should see and
be able to extend them (the app/site blocklist, and the `paused` flag as a visible manual
off-switch in the tray) before first use, not discover `privacy.toml` by reading docs. Expose
that file's settings as a real Settings page (the window UI already has a Settings destination
per `yuki/ui/window/pages`) rather than requiring hand-editing TOML.

---

## 4. RAM reduction plan

Ordered by measured leverage (biggest number first), all figures from `docs/MEMORY.md`.

1. **A smaller/quantized embedder — the single biggest remaining lever (~520–570 MB today).**
   `paraphrase-multilingual-MiniLM-L12-v2` was chosen specifically because it was the only one
   of three tested that handled English/Japanese cross-lingual queries (`bge-small-en-v1.5`
   failed, 1/5; `potion-multilingual-128M` worked at 4/5 but cost 547 MB — no smaller model has
   passed this test yet). Two current-generation candidates worth benchmarking against the
   *exact same 5-fact × 5-query eval already documented in `yuki/memory/embed.py`* before
   picking one:
   - **`intfloat/multilingual-e5-small`**, int8 ONNX: same parameter count/architecture family
     (117.7 M params, 384-dim) as the current model — int8 quantization typically halves-to-quarters
     weight memory, but the dominant cost measured here was the **tokenizer (~260 MB of the
     ~550 MB)**, which weight quantization does not shrink. Expect a moderate win (maybe
     550 MB → ~350–450 MB), not a dramatic one, unless a smaller-vocab tokenizer is also used.
   - **EmbeddingGemma-300M**, ONNX int8/uint8: Google states it can run in **under 200 MB RAM**
     quantized — the strongest lead found — but it defaults to 768-dim (Matryoshka-truncatable
     to 128), trained on 100+ languages, and its accuracy on Yuki's specific English/Japanese
     fact-vs-query matching is **unverified** — this needs the same 5×5 eval, not a swap on
     Google's benchmark numbers alone.
   - **Whichever wins, switching embed models is not a config flip.** Every stored vector
     (journal facts, episodes, conversation facts, know-how, portrait) is tagged with the
     `embed_model` that produced it (seen throughout `journal.py`, `episodes.py`,
     `conversations.py`, `review.py`, `questions.py`) specifically so a model change is a
     supported migration, not silent corruption — but it is a real batch re-embed job (a
     maintenance command in the same family as the existing `rejournal`/`audit`/`scrub_secrets`
     pipeline commands), and recall quality should be re-verified after, not assumed.
2. **Loading only on demand — already done.** The embedder lazy-loads on first call and
   unloads after 5 idle minutes (`IDLE_UNLOAD_S=300`); reload costs ~1–2 s / ~1.3 s CPU. This
   is explicitly framed in the docs as a CPU-for-RAM dial the owner can turn — expose it as a
   user-facing "low RAM mode" (shorter idle timeout) rather than a fixed constant, for
   users with less headroom than this PC's 16 GB.
3. **Single process vs two — keep two.** The service/tray split is a deliberate crash-isolation
   decision ("a crash here must never take Yuki down") worth far more than the modest
   Python/Qt import-overhead duplication (tens of MB) it costs. Not a real RAM target.
4. **Lazy imports — partially done, worth auditing further.** `yuki/__init__.py` already
   defers/forces `OPENBLAS_NUM_THREADS=1` before numpy import (−490 MB/process, already
   captured). The tray's "live before cuts" figure (587 MB private) vs. its cold-import figure
   (112 MB) shows the **agent's full import graph (perception/actions/Bedrock client) was being
   pulled in at tray boot** — audit `yuki/ui/app.py` for constructing the Bedrock client and
   importing `yuki.agent`/`yuki.perception`/`yuki.actions` eagerly at startup rather than on
   first overlay use, same pattern as the embedder's existing lazy `_ensure_loaded()`. Also
   worth deferring: WinRT media-session imports (~20 MB, only needed for media-key handling).
5. **Qt footprint — already small (~20 MB private measured alone).** Not a real target; the
   587 MB "live" tray number above was the agent import graph riding along with Qt, not Qt
   itself.
6. **Python's own interpreter overhead — negligible (~10–15 MB).** The real cost attributed to
   "Python" in the measured table is import-time cost of numpy/onnxruntime/comtypes, already
   covered by points 1 and 4 above; there is no separate "shrink CPython itself" lever here.
7. **Native shell rewrite (Rust/Tauri/C#) — not worth it yet.** Every dominant cost measured
   (embedder ~550 MB, numpy BLAS threads, the transcriber's CUDA context) lives in the
   ML/native-library layer, not in Python or Qt. A Tauri rewrite would save perhaps
   100–150 MB of Qt/Python overhead while keeping onnxruntime, ctranslate2, sherpa-onnx,
   livekit and UIA/comtypes exactly as heavy as today — for the cost of re-implementing weeks
   of contract-documented perception/actions/voice/memory logic (`ARCHITECTURE.md`,
   `MEMORY.md`, `VOICE.md`) in another language and losing direct access to the Python bindings
   several of those native libraries are built around. Revisit only if a future profiling pass
   shows PySide6/Python overhead itself — not the ML libraries — is the dominant remaining cost
   after items 1 and 4 land.

**Realistic targets:**
- **Idle** (tray + service, embedder unloaded, no window, no call): **~150–250 MB total** —
  essentially what's already measured today (51–100 MB service + 112–170 MB tray).
- **Active** (journal running, embedder loaded, no call transcription): **~500–650 MB** is
  achievable if the embedder swap in point 1 lands and saves ~150–250 MB; today's ~750–820 MB
  is mostly the embedder and won't move much further without it.
- **During deferred call transcription**: +2.6–3.7 GB private / +1.9 GB VRAM for the batch's
  duration is already correctly isolated to its own short-lived process and, by design, should
  not count against an "always-on" RAM target.

---

## 5. Code signing

- **OV vs EV:** as of August 2024, Microsoft no longer distinguishes OV from EV certificates
  for SmartScreen purposes — EV's traditional "instant trust" advantage is gone; both now
  build reputation the same way, over time, from consistently-signed releases under one
  publisher identity.
- **Azure Artifact Signing** (formerly Azure Trusted Signing) — **$9.99/mo Basic** (up to 5,000
  signatures/month), $99.99/mo Premium; keys in FIPS 140-2 Level 3 HSMs; reached General
  Availability in 2026 and **individual/self-employed applicants are now accepted** (the old
  "3 years of business history" requirement from public preview is gone). This is the cheapest,
  most current, most individual-developer-friendly option and the clear recommendation over
  buying a traditional standalone cert — set it up in Phase 2, not as an afterthought, since
  SmartScreen reputation only starts accruing once real signed releases exist.
- Sign **every** artifact in the chain (each frozen `.exe`, the Inno Setup installer, and later
  each Velopack delta) under the same publisher identity from the first release, so reputation
  starts building immediately rather than after a "v2 decision to finally sign."

---

## 6. CI build (GitHub Actions, `windows-latest`)

Proposed pipeline, matching Yuki's existing `uv`-based workflow (`HANDBOOK.md` §3: "Never
`uv run` while yuki-ui runs" — irrelevant on a CI runner, nothing is running there):

1. Checkout → `astral-sh/setup-uv` → `uv sync` (resolves the same locked deps as local dev,
   including the `onnxruntime>=1.30.0` override in `pyproject.toml`).
2. `uv run pyinstaller packaging/yuki.spec` — one spec producing the onedir build with **two
   entry-point executables** sharing one lib folder (tray `yuki.ui.boot` and service
   `yuki.memory.service`; see Phase 1 below for why this needs source changes, not just spec
   changes) plus the window process.
3. Smoke-test step: import onnxruntime/ctranslate2 on CPU paths from the frozen build (GH's
   `windows-latest` runners have no GPU, so a real CUDA-path test needs a self-hosted GPU
   runner — out of scope for basic packaging CI; don't attempt to install a CUDA toolkit on
   the hosted runner, it isn't needed to *bundle* the redistributable DLLs, only to *execute*
   them).
4. Sign every produced `.exe`/`.dll` with Azure Artifact Signing (`azure/trusted-signing-action`),
   using repo secrets for the signing credentials (matches Yuki's own "keys only in environment
   variables, never in files or commits" rule).
5. Build the installer with Inno Setup (`iscc`, e.g. `Minionguyjpro/Inno-Setup-Action`), sign
   the installer `.exe` itself.
6. Upload to a GitHub Release (`softprops/action-gh-release`); later, a Velopack `vpk` step
   publishing to the same release as an update feed.
7. Cache `uv`'s download cache and `.venv` between runs — this dependency tree (onnxruntime,
   ctranslate2, sherpa-onnx, livekit wheels) is large enough that cold installs will noticeably
   slow CI without it.
8. Watch for the historical `pywin32_postinstall.py` gotcha: confirm the COM-registration hook
   still fires correctly under `uv sync` on a fresh CI runner (some frozen/alternate-venv setups
   have been known to skip it).

---

## 7. Phased plan, file by file

**Phase 0 — prep (low risk).**
- New `packaging/` folder: `yuki.spec` (PyInstaller), `installer.iss` (Inno Setup).
- `yuki/config.py`: add `model_backend: Literal["bedrock", "anthropic_api"]`.
- `yuki/agent/loop.py`, `yuki/agent/failover.py`, `yuki/voice/pipeline.py`: branch client
  construction on `model_backend`; check whatever error-shape assumptions `failover.py` makes
  about Bedrock still make sense for a plain Anthropic client.
- New onboarding surface (extend `yuki/ui/window/pages` or a new `yuki/ui/onboarding.py`):
  key entry (Anthropic/Bedrock toggle, Deepgram, ElevenLabs, all optional/skippable), ambient-
  capture consent screen, link into the Settings page for `privacy.toml`'s denylists.
  *Risk:* new UI surface must match the existing Liquid-Glass styling (`docs/ui-mockups`,
  `docs/UI-window.md`) — moderate design + review effort, and per the standing rules only the
  owner does live UI testing, builders test offline only.

**Phase 1 — packaging (highest risk phase).**
- **The watchdog/spawn problem is the single riskiest item.** Today `yuki/ui/app.py` /
  `yuki/ui/startup.py` spawn `pythonw -m yuki.memory.service` and the tray's watchdog matches
  sibling processes **by `CommandLine` substring** (per HANDBOOK's own restart procedure). A
  frozen build's `CommandLine` won't contain `yuki.memory.service` or `yuki.ui.boot` as literal
  text anymore — this matching logic must be changed to match the frozen executable's name/path
  instead, and the spawn call itself must launch the sibling frozen `.exe` rather than
  `pythonw -m ...`. Get this working and tested (offline, by a builder, per the standing rules)
  before anything else in this phase.
- PyInstaller onedir spec covering all three entry points (tray, service, window) as one
  shared onedir folder with three executables.
- Fix the ctranslate2/CUDA DLL search-path problem explicitly in `yuki/memory/transcribe.py`'s
  worker entry (add `nvidia-*-cu12` and `ctranslate2` folders via `os.add_dll_directory` before
  first import) and **re-verify under the frozen layout** — PyInstaller reorganizes
  `site-packages` into a different folder shape than the dev venv.
- Add explicit data-file collection for sherpa-onnx's wake-word model files
  (`--collect-data sherpa_onnx` or spec `datas=`) — the default hooks will likely miss them.
- Budget real testing time for livekit-rtc and winrt bindings under a frozen build; these have
  less PyInstaller precedent than PySide6/onnxruntime.
- Inno Setup script: `PrivilegesRequired=lowest`, install to `%LOCALAPPDATA%\Programs\Yuki`,
  write the same HKCU Run key `yuki/ui/startup.py` already writes, uninstaller offers to keep
  or delete `%LOCALAPPDATA%\Yuki`.

**Phase 2 — signing + CI (low-medium risk).**
- Apply for Azure Artifact Signing as an individual/self-employed applicant — start this early,
  the application/eligibility process and reputation-building both take real calendar time.
- Wire the GitHub Actions pipeline from §6.

**Phase 3 — auto-update (medium risk, untested combination).**
- Spike Velopack against the Phase 1 onedir build with a minimal test release before committing
  the real release process to it; if the exotic native deps (CUDA DLLs, sherpa-onnx data files)
  don't survive Velopack's packaging step cleanly, fall back to a simple custom updater
  (version-check endpoint + download-and-run-the-new-installer), which the app can build itself
  given it already runs a background service capable of polling.

**Phase 4 — RAM cut (independent of packaging, can run in parallel).**
- Benchmark EmbeddingGemma-300M-int8 and multilingual-e5-small-int8 against the 5×5
  English/Japanese eval documented in `yuki/memory/embed.py` before choosing one.
- If a smaller model wins, write a re-embed maintenance command (same family as
  `rejournal`/`audit`/`scrub_secrets`) that walks every table carrying `embed_model` and
  refreshes vectors; re-verify recall quality, don't just trust the new model's published
  benchmark numbers.
- Audit `yuki/ui/app.py` startup imports for the same lazy-load treatment the embedder already
  has (defer Bedrock client construction and WinRT media-session imports until first use).
- Re-measure everything after Phase 1's freezing changes — frozen builds sometimes force
  earlier import of things that were lazy in source (e.g. PyInstaller's import scanner
  sometimes needs a `import X` at module scope where the source had a deferred `from X import Y`
  inside a function, to make static analysis find it).

**Phase 5 — onboarding polish.**
- Model/whisper download UI with progress + resume, GPU/CPU auto-detect via a plain
  `nvidia-smi` subprocess check, the consent screen and Settings-page privacy controls from
  Phase 0, and documentation for testers on the Bedrock-vs-direct-API and bring-your-own-key
  choices.
