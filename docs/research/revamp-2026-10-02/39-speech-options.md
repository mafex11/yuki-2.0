# 39 - Speech options for Yuki, October 2026 (TTS, STT, how local to go)

Date: 3 Oct 2026. Research only: no code, config or other file was changed. Sources are listed at the end (section 9).

Tags used on numbers: **[Y]** measured by Yuki on this PC (docs or the local voice logs), **[I]** independent third-party benchmark, **[V]** vendor claim (marketing, usually server-side, often best case), **[E]** my estimate, not measured.

## 0. Bottom line

1. **The slow part of a spoken reply is the voice, not the ears.** End of speech to first audio is about 3.0-3.2 s today [Y]. Gemini 3.8 Flash-Lite TTS needs p50 1.05-1.14 s, p90 1.12-1.36 s for its first audio (187 replies, 28 Sep to 3 Oct, my read of `tts_reply` rows in the local voice logs) [Y]. Flux adds only ~0.02 s at p50 with eager end of turn [Y, 07-voice-realtime.md]. Deepgram Aura (0.31 s TTS) gave 2.25-2.34 s end to end [Y, VOICE.md v3.8].
2. **A faster TTS is a clear win: about 0.8-0.9 s sooner [E], to roughly 2.1-2.4 s.** Independent first-audio medians (Coval, 2 Oct 2026): Inworld TTS-2 150 ms, ElevenLabs Flash v2.5 185 ms, Eleven v4 Turbo 185 ms, Deepgram Flux TTS 198 ms, Aura-2 257 ms, Cartesia Sonic 3.6 302 ms [I]. Gemini is not in that table and Google publishes no latency.
3. **Quality does not have to be given up.** On the Artificial Analysis arena (Sept 2026) Cartesia Sonic 3.6 (1279) and Eleven v4 (1319) are above Gemini 3.8 Flash-Lite (1239); Inworld TTS-2 (1246) is level with it [I]. In Hindi (Voice Arena) Eleven v4 1117, Sonic 3.6 1097, Gemini 3.8 Flash 1097, Flash-Lite 1070 [I]. Gemini is great at Hindi, but not uniquely so.
4. **Best single swap: Inworld Realtime TTS-2** (GA 31 Aug 2026). It is the closest functional match to what Yuki uses Gemini for: natural-language style steering (`[say excitedly]`), `[laugh] [sigh] [breathe]` tags, Hindi as a production language, Pipecat service with websocket and word timestamps. **Quality pick to bake off against it: Cartesia Sonic 3.6** (beta, Hinglish code-switching demoed, #2 in Hindi). **Zero-code option today: ElevenLabs Flash v2.5** (already a provider in Yuki; no tags).
5. **STT: keep Flux for speed, but name accuracy is the open problem and it is a data question.** Yuki's own numbers: names right 11 -> 13 of 30 with keyterms on `flux-general-multi`, 13 -> 23 of 30 on `flux-general-en` [Y]. Run a small name A/B on AssemblyAI Universal-3.6 Pro (Hindi + Hinglish code-switching + keyterms, same price as Flux), Soniox stt-rt-v5 (4x cheaper) and ElevenLabs Scribe v2 Realtime before changing anything. None of them will make replies faster than Flux with eager end of turn.
6. **Local TTS on this PC is not faster than the cloud and is less lifelike.** Kokoro-82M on a CPU takes ~1.5-2.3 s of compute for one short sentence on newer CPUs than this one [I/E]; its arena score is ~175-215 Elo below the leaders [I]. The one local model worth a one-hour trial is **Supertonic 3** (Hindi, tags, ONNX). Use local TTS as the "never silent / offline" fallback, not as the main voice.
7. **Local STT on CPU is not viable for Hindi/Hinglish or names.** The user's own test: faster-whisper small on CPU, 22 s for a 13.5 s English clip under load, "poor" on Hindi [Y, MEMORY.md]. Parakeet, Nemotron, Voxtral and Kyutai need an NVIDIA GPU, which the no-GPU-during-games rule forbids.
8. **The local things that are worth doing:** local VAD plus a local turn detector (Silero + Smart Turn v3, Hindi included, 12-95 ms per call depending on CPU [V]). That makes the STT vendor swappable and fixes the "Flux reports start of turn 0.5-1 s late" barge-in lag (VOICE.md 6.4). It gives up Flux's eager end of turn.
9. **Cost is not a deciding factor.** Yuki spoke 1,100-4,600 characters a day over the last week (`turn` rows, `tts_chars`) [Y]. At $0.05 per 1k characters that is $0.05-0.25 a day on any vendor listed here.
10. **Dated risks to know:** Gemini TTS prices double on 1 Jan 2027 (Flash-Lite $6 -> $12 per 1M audio tokens, still ~$0.02 per 1k characters). Eleven v4 Turbo has a 72% launch discount only until 12 Oct. Cartesia Sonic 3.6 and Microsoft's new MAI voices are beta/days old. PlayHT is gone (shut 31 Dec 2025).

## 1. Where the time goes today

From VOICE.md v3.8, v3.17 and `07-voice-realtime.md`, plus the live logs (end of speech to first audio, user turns):

| Stage | Time | Note |
|---|---|---|
| Flux end of turn (eager 0.4) | ~0.02 s p50 over the transcript [Y] | Flux reports "start of turn" 0.5-1 s late (VOICE.md 6.4) |
| Talker (Haiku 4.5, Bedrock ap-south-1) first token | 1.17-1.25 s [Y, 26-build-voice-speed.md] | prompt cache, 15.4k tokens |
| First clause to TTS | ~0.19 s [Y] | `clause_aggregator` |
| **TTS first audio (Gemini Flash-Lite)** | **1.05-1.14 s p50, p90 1.12-1.36 s** [Y] | 3 s timeout, hedged with Aura at 1.8 s since v3.16 |
| Total | 3.0-3.2 s [Y] | Aura gave 2.25-2.34 s |

What a swap can and cannot buy:
- Replacing the 1.1 s TTS with a ~0.2-0.3 s one (add ~40-60 ms for India to US servers: Aura measured 313 ms here against 257 ms in Coval's table, ElevenLabs Flash 221 ms against 185 ms [Y vs I]) saves ~0.8-0.9 s. Expected first audio: **~2.1-2.4 s**.
- The talker's 1.2 s first token is now the largest block. No speech vendor changes that; it is a separate lever (not in scope here).
- With `speech_channel = "guard"` the Haiku spoken-line check (~0.9 s) becomes the floor with any fast voice (07-voice-realtime.md); with the v3.16 marker channel it only runs in shadow. Keep the marker channel.

## 2. TTS, cloud options

Latency columns: Coval = independent median/p95 TTFA, 2 Oct 2026, rolling 7 days, probably from US servers [I]. Elo = Artificial Analysis, Sept 2026 [I]. Prices per 1M characters.

| Option | First audio (streams?) | Realism | Hindi / Hinglish | Price | Pipecat | Verdict |
|---|---|---|---|---|---|---|
| **Inworld Realtime TTS-2** (GA 31 Aug 2026) | Coval 150 / 193 ms; TTS-2 Flash 61 / 93 ms; Vapi bench 288 ms. Websocket, streams | Elo 1246 (level with Gemini Flash-Lite 1239). Steering text, `[laugh] [sigh] [breathe] [cough] [yawn]` | Hindi is a production language; mid-utterance language switching claimed [V]. Not on Voice Arena Hindi table, so Hindi quality is unverified | $25 (Flash $15) | Yes, `InworldTTSService`, default model `inworld-tts-2`, word timestamps, interruption fallback | **Best match for Gemini's role** |
| **Cartesia Sonic 3.6** (beta since 17 Aug; `sonic-3.6` is Pipecat's default id) | Coval 302 / 397 ms (3.5: 286 / 365); Voice Arena 341 ms; vendor "sub-90 ms" | **#1 English Elo 1279-1283**, #2 Hindi (1097); `[laughter]`, 60+ emotions, speed | 42-44 languages; Hinglish code-switching shown in launch demos [V] | $49 (docs list Pro $5/mo for 100k credits) | Yes, websocket, word timestamps | **Quality pick**, beta, hosted only |
| **ElevenLabs Flash v2.5** | Coval 185 / 225 ms; **Yuki measured 221 ms p50** [Y]. Websocket | Vapi humanness 68 (Flash v2: 76); no tags | Hindi yes (32 languages); Yuki reads Devanagari with Latin English words | list $40-50 ($0.04-0.05/1k, sources differ) | Yes, current provider, `--tts elevenlabs` works today | Fastest zero-code A/B; free quota gone until ~26 Oct |
| **ElevenLabs Eleven v4 Turbo** (GA 28 Sep 2026) | Coval 185 / 232 ms; vendor ~150 ms. Via "Text to Dialogue WebSocket" | Eleven v4 Elo **1319** (top), Hindi **#1** (1117); inline audio tags | 90+ languages | list $0.04/1k, 72% launch discount to 12 Oct | **Pipecat's ElevenLabs docs list only flash/turbo v2.5/v3 models and the usual websocket; v4 Turbo uses a different endpoint, so expect a custom service. Verify in 1.11** | Best quality; wiring risk |
| **Deepgram Flux TTS** (GA 13 Aug 2026, `flux-haley-en`) | Coval 198 / 256 ms; vendor 80 ms. `/v2/speak` websocket, token streaming | Not on the arena tables I found; cross-turn prosody; speed and expressivity knobs only, no tags | **English only** | $45 ($0.045/1k) | Yes, `DeepgramFluxTTSService` | One-vendor path (same key as STT); loses Hindi speech and sounds |
| Deepgram Aura-2 (current fallback) | Coval 257 / **519** ms; Yuki 313 ms [Y] | Mid; no tags | English only | $30 | Yes | Fine as fallback; Flux TTS is better on p95 |
| **Gemini 3.8 Flash-Lite TTS** (current) | **1.05-1.14 s p50** [Y]; Google publishes none; OpenRouter shows 3.47 s for Flash | Elo 1239 (Flash 1265); style prompts and sound tags | **Top-tier Hindi** (Flash 1097, Flash-Lite 1070, #5); 100+ languages, 2,000+ voices | $6 per 1M audio tokens (~$10/1M chars), **doubles 1 Jan 2027** | Pipecat has `GeminiTTSService`; Yuki uses its own ~400-line client | Best Hindi/expression per dollar, slowest by 5x |
| Rime Mist v3 / Arcana v3 | Coval 274 / 358 ms; vendor 96 ms | Not on the arena tables | Hindi listed for both | Mist $30 / Arcana $40 (Starter) | Yes | Fine, nothing special here |
| Hume Octave 2 | vendor ~100-200 ms; no independent number found | Emotion-first | Hindi "in preview" | ~$7.6 at scale; overage $0.05-0.15/1k | Yes | Unverified latency; skip for now |
| xAI Grok TTS | Coval 359 / 451 ms; Vapi 460 ms (streaming 285 ms) | Vapi humanness 93 (streaming 87) | Hindi among 20 languages; `[laugh] [whisper]` tags | $15 | Yes | Dark horse worth a listen |
| Sarvam Bulbul v3 (v4 announced 30 Jul, no API doc yet) | vendor <200-250 ms | Voice Arena Hindi rank #13 [I]; blind study says it beats ElevenLabs Flash at 8 kHz telephony [V] | Indian languages + Indian English, Hinglish in a single pass [V] | Rs 3 per 1k characters (~$35) | `SarvamTTSService` (no word timestamps or interruption listed, so heard-words trimming on barge-in would be lost) | Only for Hindi-heavy use |
| Azure HD / MAI-Voice-2.1-Flash (1 Oct 2026) | HD <300 ms [V]; MAI Flash "150 ms end to end" [V] | not measured | MAI Flash lists Hindi | HD $22, MAI Flash $15 | Azure yes; MAI not confirmed | Watch, too new |
| Mistral Voxtral TTS | vendor ~90 ms | Elo 1078 | Hindi yes (9 languages) | $16; open weights CC BY-NC | Yes (Mistral) | Skip |
| Speechify Simba 3.2 | Coval 108 / 161 ms | Elo 1239 | **English only** | $6-10 | Yes; HTTP chunks, no websocket | Skip (English only) |
| OpenAI gpt-4o-mini-tts | **Coval 2,981 / 5,484 ms** | decent | multilingual | $12 per 1M audio tokens | Yes | Out. No TTS successor found; `gpt-realtime-*` are speech-to-speech, excluded by the user's rule |
| PlayHT | gone | n/a | n/a | n/a | n/a | Shut down 31 Dec 2025 after Meta took the team |

Notes:
- Vendor latencies exclude network and often application time; Coval says so itself and warns against vendor numbers. Use the independent column, then add India's RTT, then measure from this PC (section 7).
- Yuki's expression layer (`expressive.py`: `[laugh] [sigh] [gasp] [phew] [short pause]`, `[style: ...]`) keeps working best on Inworld (steering text plus tags) and Cartesia (laughter tag, emotions). ElevenLabs Flash v2.5 and Aura get the marks stripped (already how the code treats them).
- Gemini's Hindi rank stays high, so if Hindi replies matter more than speed, Gemini should stay selectable (`voice.json` `tts_provider`).

## 3. TTS, local options

All numbers below are published on other hardware. This PC (Ryzen 7 2700, Zen+, AVX2 only, ~35-40% lower per-core speed than a 2022 desktop part, cores 8-15 for Yuki after the Process Lasso split) will be **slower than the published figures [E]**.

| Option | Published speed | Realism | Hindi | CPU / RAM cost here | Pipecat | Verdict |
|---|---|---|---|---|---|---|
| **Kokoro-82M** (Apache-2.0) | RTF 0.64 (ONNX) / 0.67 (PyTorch) on a 4-core Xeon; 2.0-2.3 s for a short text; Ryzen 7 8745HS 1.5-1.8 s for a sentence [I] | UTMOS 4.44 (natural); arena Elo 1063 vs 1239-1319 for the leaders [I] | 4 voices, overall grade C, 1-10 h of training data; one port (Kokoro-ANE) reads the Hindi voices as English for lack of a Hindi text frontend [I]. No Hinglish | ~4 threads busy while synthesising, ~0.5-1 s of work per second of speech [E]; ~0.2-0.4 GB [E] | Yes, `KokoroTTSService` | Offline fallback only: **not faster than Gemini's 1.1 s** |
| **Supertonic 3** (99M, ONNX, OpenRAIL-M, 2026) | vendor RTF 0.20 on 16 threads; 4-core Xeon: RTF 0.24 / 0.96 s short text at 5 steps (UTMOS 4.32), RTF 0.12 at 2 steps but robotic (1.53) [I] | usable at 5 steps; `<laugh> <breath> <sigh>` tags, 263 preset voices, cloning | **31 languages incl. Hindi** | 8 threads for ~1 s per sentence [E] | Community service only | **The one local model to trial** |
| Pocket TTS (Kyutai, 100M, MIT) | ~200 ms first chunk on 2 cores [V]; RTF 0.71 on the Xeon [I]; real streaming | UTMOS 4.10 | English + 5 European languages, **no Hindi** | 2 cores | Yes | English-only fallback candidate |
| Piper | RTF 0.15 on an i7-1255U | Dated, robotic | **No usable Hindi voice**; GPL-3.0 now | light | Yes | Emergency voice only |
| Chatterbox Turbo / Multilingual v3 / Nano | Turbo 75-150 ms on a consumer **GPU**; Nano (110M) 3x real time on 8 CPU cores [V] | good | Multilingual v3: 23 languages incl. Hindi | GPU = forbidden in games and calls | Not in maintained list | No |
| Orpheus 3B, Sesame CSM-1B, F5-TTS | GPU-class models (general knowledge, not re-checked); Orpheus scores humanness 89 in Vapi's index [I] | high | mostly English | GPU | Not maintained | No (GPU rule, 6 GB card) |
| Qwen3-TTS 0.6B / 1.7B | 97-113 ms first packet on GPU [V] | good | Hindi not confirmed | GPU | via hosted services only | No |
| Mistral Voxtral TTS 4B | ~90 ms [V] | Elo 1078 | Hindi yes | 4B params, GPU; CC BY-NC | Cloud only | No |

Why local TTS cannot be the main voice here: first audio is bounded by the first clause's compute time (~0.5-2 s on this CPU [E]), no better than Gemini, with a visible quality gap and a CPU burst during every reply.

## 4. STT, cloud options

Latency = time from end of speech to final transcript (TTFS) unless stated. The Pipecat benchmark uses 1,000 clean, single-speaker **English** utterances, so it says nothing about Hindi, accents or names [I]. Prices are streaming list prices.

| Option | Streaming latency / end of turn | Names, Hinglish | Price | Pipecat | Notes |
|---|---|---|---|---|---|
| **Deepgram Flux multi** (current, `flux-general-multi`, since 29 Apr 2026) | EOT median < 300 ms (~260 ms) [V]; +22 ms over transcript with eager EOT [Y] | Hindi 0% WER in Devanagari; Hinglish comes back mixed script [Y]. Keyterms barely help: 11 -> 13 of 30 name clips [Y]. Independent warning: dialects and code-switch boundaries are untested | $0.0078/min ($0.47/h) | Yes, the app's own turn strategy and eager EOT race fix | Owns turns; no per-word local control |
| Flux English (`flux-general-en`) | same | **Keyterms work: 13 -> 23 of 30** [Y]; no Hindi | $0.0065-0.0078/min (Coval's range for Flux) | Yes | Switching is the user's call (VOICE.md 9) |
| Deepgram Nova-3 | 247 ms median, 298 p95, sWER 1.62% [I] | Multilingual keyterm prompting (100 terms); GitHub discussion #1208: `multi` "often fails Indian names" | $0.0077/min | Yes | No built-in EOT |
| **AssemblyAI Universal-3.6 Pro** (29 Sep 2026; 3.5 since 23 Jun) | 3.5: 282 ms median, sWER 1.22%, p95 354 ms; 3.6: 307 ms median [V citing Pipecat's bench]; top of Pipecat's list at 0.80% pooled WER, 88.7% perfect [I]. Own acoustic EOT ~300 ms | **32 languages incl. Hindi, native mid-sentence code-switching ("Hinglish included")** [V]; `keyterms_prompt` up to 100 terms and context prompts (vendor: name errors -48.6% with detailed prompts; entity error rate 15.3%) | $0.45/h ($0.0075/min); keyterms/prompt about +$0.05/h on its page | **Yes**, `universal-3-5-pro` and `universal-3-6-pro` documented; own turn mode or Pipecat's | **Best candidate for names + Hinglish**; no eager-EOT equivalent, Hinglish script output undocumented |
| **Soniox stt-rt-v5** | 260 ms median, 305 p95, sWER 1.27% (Pipecat) [I]; its own benchmark ranks it second to Azure on WER | Hindi + Hinglish code-switching claimed, no published Hindi WER; context object (terms, 8k tokens) | **$0.002/min ($0.12/h)** | Yes, official | Endpoint detection built in; also sells TTS (Coval 256 / 260 ms) |
| ElevenLabs Scribe v2 Realtime | mean 128 ms to final, WER 5.1% (Coval) [I]; vendor ~150 ms | 90+ languages, keyterm prompting | $0.28-0.39/h | Yes | No semantic EOT noted |
| Speechmatics (linden-1 / Ursa 2) | 369 ms median, sWER 1.05% (Pipecat) [I] | Strongest code-switching claims [V] | $0.24/h real time; Flow agent $0.0537/min | Yes | Slowest of the front group |
| Gladia Solaria-1 | 103 ms best partial, 270 ms avg, 698 ms finals [V] | Native code-switching, 100+ languages incl. Hindi | $0.55-0.75/h | Yes | Slow finals |
| Sarvam Saaras v3 | <250 ms websocket, 150 ms first token in fast mode [V] | Hindi WER <8%, code-mixed <12% on IndicVoices [V]; English names unknown | Rs 30-90/h | Yes (`sarvam` STT) | Best Indic specialist; unverified on Indian-English names |
| Microsoft MAI-Transcribe-2-Streaming (1 Oct 2026) | final 0.13 s after end of speech; 2.5% WER on AA [V/I] | 60 languages incl. Hindi, auto language detect | $0.54/h intro through 2026 | Azure service exists; MAI not confirmed | Days old; Azure's older model: best WER (1.18%) but slowest (1,016 ms) in Soniox's bench |
| Google Chirp 3 | 300-600 ms streaming [V] | 85+ languages, phrase adaptation | $0.016/min | Yes | Slower and 2x price |
| OpenAI `gpt-realtime-whisper` (7 May 2026) | vendor <150 ms to first token | 99 languages | $0.017/min | Yes | Transcription only (allowed under the rule), but same product family as the speech-to-speech API; no semantic EOT |
| Mistral Voxtral Realtime | delay 80 ms-2.4 s configurable; 480 ms keeps WER within 1-2% [V] | 13 languages incl. Hindi; Apache-2.0 weights | $0.006/min API | Yes (Mistral) | 4B params; local = GPU |
| Cartesia Ink-2 | 100 ms [V] | English only per what I found | ~$0.39/h | Yes | No Hindi |

## 5. STT, local options (this PC)

| Option | Streaming? | Accuracy | CPU / GPU | Verdict |
|---|---|---|---|---|
| faster-whisper / whisper.cpp small, int8 | No (chunked) | Yuki's test: English exact but 22 s for a 13.5 s clip under 99% CPU load, **Hindi and Hinglish "poor"** [Y]; the same code on large-v3 GPU is fine [Y] | ~7.8x real time on an i7-12700K, 8 threads [I]; here 8 threads for seconds per utterance [E] | No; the deferred call transcriber already uses GPU only when idle |
| Moonshine v2 Tiny/Small/Medium (34/123/245M) | Yes, sliding window, 80 ms lookahead | WER 12.0 / 7.8 / 6.65% (Open ASR average) [I]; names untested | M3: 50 / 148 / 258 ms, 8 / 18 / 29% compute [I]; no x86 figure | English only. Possible English fallback; Pipecat has `Moonshine` |
| sherpa-onnx streaming zipformer / NeMo streaming CTC | Yes | English models only; Hindi is limited to offline Dolphin CTC [I] | Same runtime already used by the wake word, no new dependency | English fallback candidate; hotword biasing for names is a feature of transducers (not re-checked) |
| NVIDIA Parakeet / Nemotron 3.5 ASR 0.6B | Yes, 80 ms-1.12 s chunks | Hindi 6.81% WER at 1.12 s chunks [V] | **NVIDIA GPU**; CPU "not recommended". An int4 CPU build exists for English only (8.2% WER, 0.56 s delay on a 64-core EPYC) | Forbidden in games/calls; not evaluated on a Ryzen 2700 |
| Kyutai STT 1B en/fr, 2.6B en | Yes, 0.5 s / 2.5 s delay, semantic VAD | no Hindi | GPU | No |
| Voxtral Mini 4B Realtime | Yes | 13 languages incl. Hindi | 4B, GPU | No locally; cloud API is the usable form |
| Vosk | Yes | old, much worse than the above | light | Not re-checked this round |

**Local turn detection (worth having):** Pipecat's Smart Turn v3 (8 MB int8 ONNX, **23 languages incl. Hindi, 93.4% accuracy on 1,295 Hindi samples**, 12.6 ms on a fast cloud CPU, 34-95 ms on slow ones) plus Silero VAD [V]. It runs only at pauses. On this PC expect ~30-60 ms [E]. It already ran in Yuki once and was dropped because it imported onnxruntime in every conversation (VOICE.md 9); in standby it can be loaded once. The "two onnxruntime builds in one process" caveat (VOICE.md 755) applies.

## 6. Recommended configurations

### (a) Best quality and speed, staying cloud

- **TTS:** Inworld Realtime TTS-2 as the main voice, with Cartesia Sonic 3.6 as the alternate to bake off by ear. Keep Gemini (Leda) as a selectable provider for Hindi-heavy days and Aura (or Flux TTS) as the breaker fallback.
- **STT:** Flux stays, plus the name A/B of section 7. Move to AssemblyAI Universal-3.6 Pro only if it clearly beats flux-en + keyterms on names without losing Hinglish.
- **Expected first audio:** **~2.1-2.4 s** (from 3.0-3.2 s) [E]. STT changes alone move nothing.
- **What changes for the user:** replies start about 0.8-0.9 s sooner; the voice is no longer Leda (pick by ear; Inworld and Cartesia both offer voice libraries and cloning); sighs, laughs and mood still work through the tags; Hindi quality needs a listen; the Gemini tail goes away (first byte over 2 s in 4 of 61 calls, max 15.6 s, in the 07 audit; the 3 s timeout that made "open chrome" take 6 s).
- **Effort / risk:** about 15-40 lines in the provider branch of `pipeline.py`, `PROVIDERS`, the `--tts` choices, `DEFAULT_VOICES`, a key in the user environment, and a tag mapping in `expressive.py`. Risks: Cartesia 3.6 is beta; Inworld Hindi is unmeasured; one more vendor (net count unchanged if Gemini goes unused, because the fallback stays Deepgram).
- **Vendor count:** Deepgram (STT, fallback), the new TTS vendor, Bedrock. Google drops out as a hard dependency, and its 1 Jan 2027 price doubling stops mattering.

### (b) Hybrid: local ears and local safety net, cloud voice

- **b1 (recommended, small):** Silero VAD + Smart Turn v3 locally; cloud STT receives only voiced audio, so any vendor (AssemblyAI, Soniox, Scribe, Sarvam) can be swapped in; barge-in follows local VAD instead of Flux's late "start of turn". Costs about 1% of a core for VAD and ~30-60 ms per pause for Smart Turn on cores 8-15 [E for both; Silero's cost was not sourced]. Loses Flux's eager end of turn (worth up to ~0.4 s on some turns, VOICE.md 9), so first audio is **about 2.2-2.6 s [E]** with the cloud TTS from (a), not better than (a).
- **b2 (optional):** Supertonic 3 (Hindi, tags) and Kokoro (English) as the "no internet / Google or Deepgram down" voice instead of Aura, so Yuki is never silent (VOICE.md 717 lists a local fallback as not built). As the main voice it would give **~2.9-3.3 s [E]**, no gain over today, and a less lifelike voice, with 8 threads busy for ~1 s per sentence and 0.2-0.5 GB more RAM [E] on a PC at 80% RAM.
- **What changes for the user:** nothing audible for b1 except a faster cut-in when they interrupt; b2 only matters when the network fails.

### (c) Most local, with honest trade-offs

- Wake word (sherpa, done) + Silero + Smart Turn + local streaming STT (Moonshine v2 Small/Medium or sherpa zipformer, English only) + local TTS (Supertonic 3 or Kokoro). The **talker and thinker stay on Bedrock**; nothing local can replace them on a 6 GB GPU that must stay free.
- **Expected first audio:** ~2.5-3.4 s [E]: local STT needs the pause plus Smart Turn (0.3-0.5 s) instead of Flux's eager end of turn, and local TTS needs ~0.5-1 s for its first clause.
- **What it costs the user:** Hindi/Hinglish recognition is effectively gone (no good local streaming Hindi; Whisper small was "poor" in the user's own test), names get worse (Flux keyterms are the best tool they have found), continuous 20-30% of one core while a conversation is live plus TTS bursts [E], 0.5-1 GB of RAM [E], and a flatter voice. The privacy and no-vendor gain is real; the speed gain is zero.
- **Verdict:** not worth building unless offline use or privacy is the goal. The honest local pieces are the ones in (b).

## 7. Cheapest things to try first (all offline, no live mic or speaker)

1. **Today, zero code: ElevenLabs Flash v2.5 against Gemini against Aura** with `scripts/voice_scenarios.py parallel927 --tts elevenlabs|gemini|deepgram` (the same A/B that produced the v3.8 table: p50 end of speech to first audio, 16-33 turns, 2 runs). Needs a paid ElevenLabs plan (free quota exhausted, resets ~26 Oct). It shows how much of the 3.0 s a fast voice gets back before touching any code. Real Bedrock calls are in `parallel927` (~$0.6 in v3.8); `voice_session_offline.py latency --one-breath --prewarm` uses a stand-in brain and is cheaper but has no `--tts` flag.
2. **About an hour: add `inworld` and `cartesia` to the provider branch** (`pipeline.py` around the `if provider == "gemini" / "deepgram" / else` chain, `PROVIDERS` in `session.py`, `--tts` choices in `voice_scenarios.py`, tag mapping in `expressive.py`). The `_clause_tts(...)` wrapper, the TTS cache and the word-timestamp handling are service-agnostic (Deepgram and ElevenLabs already go through them). Run the same script and read `first_audio_ms` in `tts_reply` rows, plus listen to the `%TEMP%\yuki-tts\agent\index.html` style samples (8 moods) re-rendered per provider. Cost: cents; the 1,100-4,600 characters a day shows how little a test costs.
3. **STT name A/B with the existing eval:** extend `scripts/voice_keyterms.py` (a plain websocket function) with AssemblyAI Universal-3.6 Pro (`keyterms_prompt` plus a context prompt), Soniox stt-rt-v5 (context terms) and Scribe v2 Realtime (keyterms), on the same 30 name clips (10 sentences x 3 voices) and the Hindi/Hinglish clips of `voice_clips.py`. Baselines to beat: flux-multi 13/30, flux-en 23/30. About 2.5 minutes of audio per variant, well under $0.10 each. Caveat: the clips are synthetic voices, not the user's accent; if the user records ~10 real name sentences it will be a fairer test.
4. **Local turn detector cost check:** load Smart Turn v3 (int8 ONNX) on 2 threads pinned to CPUs 8-15, time 50 inferences while the offline session runs. Decides (b1) in minutes; free.
5. **Direct TTS first-byte probe from this PC** (a 40-line throwaway script: the same 12 reply lines to each provider, 30 runs each, p50/p90). Vendors measure inside their data centres; Coval measures from the US; this PC is in India. Only this PC's numbers decide. Under $0.05.

Not worth trying first: any GPU model, Kokoro as the main voice, Eleven v4 Turbo through Pipecat (custom endpoint) until a probe shows it is reachable, MAI voices (days old).

## 8. Caveats and unknowns

- Hinglish and names have **no independent benchmark** in this research. The only hard data is Yuki's own (flux-multi/en name clips, Hindi WER in Devanagari). Everything vendor-side about Hinglish (Soniox, AssemblyAI, Cartesia, Sarvam, Gladia, Speechmatics) is a claim.
- Coval's TTS table does not include Gemini, Hume, Sarvam, Azure, Kokoro or Mistral. Gemini's 1.1 s is Yuki's own measurement; Google publishes none.
- Voice Arena Hindi scores and Artificial Analysis scores come from different arenas (provider-voice vs controlled-voice) and are not interchangeable.
- ElevenLabs v4 Turbo on Pipecat, Pipecat 1.11.0's exact model-id list for AssemblyAI (`universal-3-6-pro` is on the live docs; a Pipecat PR to make it the default exists), Inworld's Hindi quality and Cartesia's Hinglish handling were not verified against the installed code or by listening.
- Prices are list prices from late Sept / early Oct 2026; Google's and ElevenLabs' change soon (see section 0 item 10).
- CPU numbers for this Ryzen 2700 are estimates scaled from other CPUs; nothing was run on the PC (research-only task).
- The first-audio estimates assume the talker's first token stays ~1.2 s.

## 9. Sources

Yuki internal: `docs/VOICE.md` (v3.8, v3.17, sections 6.4, 9, 10, 22), `docs\research\revamp-2026-10-02\07-voice-realtime.md` and `26-build-voice-speed.md`, `docs\MEMORY.md` (Whisper comparison 2026-09-24), local `%LOCALAPPDATA%\Yuki\logs\voice\voice-2026*.jsonl` (`tts_reply` first audio, `turn` `tts_chars`), `scripts\voice_keyterms.py`, `voice_scenarios.py`, `voice_session_offline.py`, `voice_ttft.py`, `yuki\voice\pipeline.py`.

TTS, independent and leaderboards
- Coval TTS latency table (2 Oct 2026): https://openbenchmarks.com/text-to-speech-benchmark-by-coval and https://benchmarks.coval.ai/tts
- Best TTS models, Sept 2026 (Elo, prices): https://www.digitalapplied.com/blog/best-text-to-speech-models-september-2026-ranked-priced
- Voice Arena Hindi leaderboard: https://voicearena.com/tts-leaderboard/hindi
- Artificial Analysis TTS: https://artificialanalysis.ai/text-to-speech
- Vapi Humanness Index: https://humannessindex.vapi.ai, https://humannessindex.vapi.ai/models/cartesia-sonic-3-5, https://humannessindex.vapi.ai/models/grok-tts, https://humannessindex.vapi.ai/models/speechify-simba-3-2
- Coval on vendor benchmarks: https://www.coval.ai/blog/best-text-to-speech-providers-in-2026-how-to-choose-(and-why-vendor-benchmarks-lie)/

TTS vendors
- Cartesia Sonic 3.6: https://www.marktechpost.com/2026/08/18/cartesia-ships-sonic-3-6-a-streaming-tts-model-that-now-leads-both-artificial-analysis-speech-arenas/, https://explainx.ai/blog/cartesia-sonic-3-6-tts-artificial-analysis-august-2026, https://docs.cartesia.ai/build-with-cartesia/tts-models/sonic-3-5, https://invideo.io/blog/cartesia-sonic-ai-voice/
- Inworld TTS-2: https://inworld.ai/blog/realtime-tts-2, https://replicate.com/inworld/realtime-tts-2, https://docs.inworld.ai/tts/capabilities/multilingual, https://docs.pipecat.ai/server/services/tts/inworld
- ElevenLabs v4: https://elevenlabs.io/blog/eleven-v4, https://elevenlabs.io/docs/changelog/2026/9/28, https://www.digitalapplied.com/blog/eleven-v4-turbo-voice-agents-latency-pricing, https://docs.pipecat.ai/api-reference/server/services/tts/elevenlabs
- Deepgram Flux TTS and Aura-2: https://audioxpress.com/news/deepgram-launches-flux-tts-conversation-aware-text-to-speech-model-for-voice-agents, https://docs.pipecat.ai/api-reference/server/services/tts/deepgram, https://deepgram.com/learn/best-text-to-speech-apis-2026
- Gemini 3.8 Flash TTS pricing: https://neomanex.com/news/gemini-3-8-flash-tts-flash-lite-tts-api-pricing, https://openrouter.ai/google/gemini-3.8-flash-tts
- Rime: https://www.rime.ai/pricing, https://www.rime.ai/blog/introducing-mist-v2/; Hume: https://dev.hume.ai/docs/text-to-speech-tts/overview, https://futureagi.com/blog/best-text-to-speech-providers-2026/
- Sarvam: https://www.sarvam.ai/text-to-speech, https://docs.pipecat.ai/server/services/tts/sarvam, https://evolutionaihub.com/sarvam-ai-bulbul-v3-beats-top-voice/, https://explainx.ai/blog/sarvam-bulbul-v4-tts-emotion-voice-july-2026
- Azure and MAI: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/high-definition-voices, https://www.marktechpost.com/2026/10/02/microsoft-ai-releases-mai-transcribe-2-streaming-1-real-time-speech-to-text-model-on-artificial-analysis/
- Mistral Voxtral TTS: https://abhs.in/blog/mistral-voxtral-tts-open-weight-beats-elevenlabs-90ms-latency-2026
- Speechify: https://speechify.ai/realtime-tts; xAI Grok TTS: https://openrouter.ai/x-ai/grok-voice-tts-1.0
- OpenAI gpt-4o-mini-tts: https://developers.openai.com/api/docs/models/gpt-4o-mini-tts
- PlayHT shutdown: https://inworld.ai/resources/migrate-from-playht, https://aitoolgraveyard.com/what-happened-to-play-ht
- Pipecat supported services: https://docs.pipecat.ai/server/services/supported-services

Local TTS
- Kokoro / Supertonic / Pocket TTS CPU benchmark: https://heyneo.com/blog/kokoro-supertonic-inflect-nano-pocket-tts-cpu-benchmark
- Supertonic 3: https://huggingface.co/Supertone/supertonic-3, https://supertonic3.github.io/
- Kokoro on CPU and Hindi voices: https://blog.nemesisnet.co.za/self-hosted-tts-with-kokoro-onnx-what-cpu-only-inference-actually-gets-you/, https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md, https://github.com/FluidInference/FluidAudio/issues/920
- Pocket TTS: https://kyutai-labs.github.io/pocket-tts/; Piper: https://www.cekura.ai/discover/piper-tts, https://quick-tts.com/blog/piper-voices-ranked.html; Chatterbox: https://www.resemble.ai/learn/models/chatterbox-turbo; Qwen3-TTS: https://qwen.ai/blog?id=qwen3tts-0115

STT
- Pipecat STT benchmark (English, Sept 2026): https://github.com/pipecat-ai/stt-benchmark, https://hackernoon.com/pipecat-benchmarked-23-real-time-stt-models-for-voice-agents-there-isnt-one-winner
- Soniox benchmark: https://soniox.com/benchmarks; Hindi: https://soniox.com/platform/hindi; Pipecat: https://docs.pipecat.ai/server/services/stt/soniox
- AssemblyAI: https://www.assemblyai.com/blog/universal-3-5-pro-realtime, https://www.assemblyai.com/docs/streaming/multilingual-transcription, https://docs.pipecat.ai/api-reference/server/services/stt/assemblyai, https://www.assemblyai.com/blog/universal-3-5-pro-independent-stt-benchmarks
- Deepgram Flux multilingual: https://www.implicator.ai/deepgram-launches-flux-multilingual-speech-model-with-10-language-mid-call-switching/, https://developers.deepgram.com/docs/flux/quickstart, https://www.softwareseni.com/deepgram-flux-and-the-multilingual-voice-agent-deployment-challenge/, Nova-3 Indian names: https://github.com/orgs/deepgram/discussions/1208
- Coval STT overview: https://www.coval.ai/blog/best-speech-to-text-providers-in-2026-independent-benchmarks-and-how-to-choose/
- ElevenLabs Scribe v2 Realtime: https://elevenlabs.io/realtime-speech-to-text, https://benchmarks.coval.ai/models/scribe_v2_realtime
- Sarvam Saaras v3: https://www.sarvam.ai/blogs/asr; Gladia: https://www.gladia.io/blog/best-real-time-stt-models-for-meeting-assistants-2026; Speechmatics: https://www.speechmatics.com/pricing; Google Chirp 3: https://docs.cloud.google.com/speech-to-text/docs/models/chirp-3; OpenAI pricing: https://developers.openai.com/api/docs/pricing; Cartesia Ink-2: https://www.cartesia.ai/blog/ink-2
- Mistral Voxtral Realtime: https://mistral.ai/news/voxtral-transcribe-2/
- Moonshine v2: https://arxiv.org/html/2602.12241v1; Nemotron 3.5 ASR: https://huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b; Nemotron on CPU: https://arxiv.org/html/2604.14493v2; Kyutai STT: https://kyutai.org/stt/; sherpa-onnx Indic: https://github.com/k2-fsa/sherpa-onnx/discussions/3199; Whisper CPU speed: https://www.promptquorum.com/power-local-llm/local-whisper-stt-comparison-2026
- Smart Turn v3: https://www.daily.co/blog/announcing-smart-turn-v3-with-cpu-inference-in-just-12ms/
