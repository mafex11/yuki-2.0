"""Test-run guard: nothing a test does can write into the user's real memory.

Every default memory location (the database, its key, the "Yuki is acting" marker,
the privacy and extract-profile files) follows ``YUKI_MEMORY_DIR``
(:data:`yuki.memory.store.MEMORY_DIR_ENV`) when it is set. Here it points at a temp
folder for the whole session, so a harness that forgets to pass a path (an ``Agent``
built without ``memory=``, ``MemoryClient.open()``, ``acting.begin(None, ...)``) writes
there instead of ``%LOCALAPPDATA%\\Yuki\\memory``. On 2026-09-25 19:40 a harness
without a path (``scripts/voice_scenarios.py hands3`` with no ``--db``) wrote three
exchanges and the acting marker into the real memory; they later showed up in a real
request as "Last 3 exchanges" (docs/research/coach-and-answers-review.md 2.1, 3.2 H).

``LOCALAPPDATA`` itself is left alone: other files live there (the embedding model
cache, speech models, the voice settings), and tests only read those.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _temp_memory_dir(tmp_path_factory: pytest.TempPathFactory):
    """Point memory's default folder at a temp dir for the whole run (restored after)."""
    folder = tmp_path_factory.mktemp("yuki-memory")
    old = os.environ.get("YUKI_MEMORY_DIR")
    os.environ["YUKI_MEMORY_DIR"] = str(folder)
    try:
        yield folder
    finally:
        if old is None:
            os.environ.pop("YUKI_MEMORY_DIR", None)
        else:
            os.environ["YUKI_MEMORY_DIR"] = old
