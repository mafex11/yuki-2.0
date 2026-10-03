"""Recognition of the names memory holds, with and without keyterms (docs/VOICE.md §9, §27). Files only.

Each clip of ``voice_clips.py --set names`` is streamed to Deepgram Flux (the app's model and settings)
twice: without keyterms and with the keyterms a conversation would get (``yuki.voice.keyterms`` over a
COPY of the memory database; the real one is never opened). A clip counts as right when every name it
says is in the transcript. Nothing is played or recorded.

Usage::

    uv run python scripts/voice_keyterms.py --clips <dir with the names clips> --db <copy of memory.db>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import wave
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from yuki.config import Settings  # noqa: E402
from yuki.voice.keys import DEEPGRAM, get_key  # noqa: E402

#: The names each clip says (test data: what the clip's text was written with).
TARGETS = {"m_tarnesh": ["Tarnesh"], "m_rohit": ["Rohit"], "m_cli": ["CLI"], "m_kavya": ["Kavya"],
           "m_bhaskar": ["Bhaskar", "Malini"], "m_corvana": ["Corvana"], "m_claude": ["Claude"],
           "m_jatin": ["Jatin"], "m_tarnesh2": ["Tarnesh"], "m_ezra": ["Ezra"]}


def _pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes())


async def transcribe(pcm: bytes, settings: Settings, keyterms: list[str]) -> str:
    import websockets

    params = [f"model={settings.voice_stt_model}", "sample_rate=16000", "encoding=linear16",
              f"eot_threshold={settings.voice_eot_threshold}"]
    if settings.voice_stt_model.endswith("multi"):
        params += [urlencode({"language_hint": code}) for code in settings.voice_stt_languages]
    params += [urlencode({"keyterm": term}) for term in keyterms]
    url = "wss://api.deepgram.com/v2/listen?" + "&".join(params)
    texts: list[str] = []
    async with websockets.connect(url, additional_headers={"Authorization": f"Token {get_key(DEEPGRAM)}"}) as ws:
        async def reader() -> None:
            async for message in ws:
                if isinstance(message, str):
                    data = json.loads(message)
                    if data.get("event") == "EndOfTurn" and data.get("transcript"):
                        texts.append(data["transcript"])

        task = asyncio.create_task(reader())
        chunk = 2560  # 80 ms
        for i in range(0, len(pcm), chunk):
            await ws.send(pcm[i:i + chunk])
            await asyncio.sleep(0.04)  # twice real time
        for _ in range(25):  # 2 s of silence for the end of turn
            await ws.send(b"\x00" * chunk)
            await asyncio.sleep(0.08)
        await ws.send(json.dumps({"type": "CloseStream"}))
        try:
            await asyncio.wait_for(task, timeout=5)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001 - the socket closing ends the reader
            task.cancel()
    return " ".join(texts)


def right(name: str, text: str) -> bool:
    words = "".join(ch.casefold() if ch.isalnum() else " " for ch in text).split()
    return all(t.casefold() in words for t in TARGETS[name.split("__")[0]])


async def main_async(args: argparse.Namespace) -> dict:
    from yuki.memory.api import MemoryClient
    from yuki.voice.keyterms import terms_from_facts
    from yuki.voice.pipeline import keyterm_list

    settings = Settings(**({"voice_stt_model": args.model} if args.model else {}))
    with MemoryClient.open(args.db) as client:  # the copy
        store = client.store
        memory_terms = terms_from_facts(store.portrait_facts(("person", "work", "interest"),
                                                             include_corrections=False), store.open_loops(), 50)
    with_terms = keyterm_list(["Yuki", *settings.user_names], memory_terms, settings.voice_keyterms_max)
    before = ["Yuki", *settings.user_names]  # what v3.4 passed
    clips = [c for c in json.loads((args.clips / "manifest.json").read_text(encoding="utf-8")) if c["set"] == "names"]
    sem = asyncio.Semaphore(6)
    rows: list[dict] = []

    async def one(clip: dict, terms: list[str], label: str) -> None:
        async with sem:
            text = await transcribe(_pcm(Path(clip["file"])), settings, terms)
        rows.append({"clip": clip["name"], "variant": label, "text": text, "right": right(clip["name"], text)})

    await asyncio.gather(*[one(c, t, label) for c in clips for t, label in ((before, "before"), (with_terms, "after"))])
    rows.sort(key=lambda r: (r["clip"], r["variant"]))
    summary = {label: sum(r["right"] for r in rows if r["variant"] == label) for label in ("before", "after")}
    return {"keyterms": with_terms, "clips": len(clips), "right": summary, "rows": rows,
            "audio_s_sent": round(2 * sum(c.get("seconds", 0) + 2 for c in clips), 1)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips", type=Path, required=True)
    ap.add_argument("--db", type=Path, required=True, help="a COPY of memory.db (with its memory.key beside it)")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--model", default="", help="another Flux model to compare (default: the app's)")
    args = ap.parse_args(argv)
    report = asyncio.run(main_async(args))
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, ensure_ascii=False))
    for r in report["rows"]:
        print(f"{r['clip']:22s} {r['variant']:6s} {'OK ' if r['right'] else 'BAD'} {r['text']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
