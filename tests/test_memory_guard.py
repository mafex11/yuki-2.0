"""The test-run guard (tests/conftest.py): default memory locations point at a temp folder.

No test may write into the user's real memory (docs/research/coach-and-answers-review.md 3.2 H).
"""

from __future__ import annotations

import os
from pathlib import Path


def _real_memory_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Yuki" / "memory"


def test_default_locations_are_under_the_temp_dir(_temp_memory_dir) -> None:
    from yuki.memory import acting
    from yuki.memory.extract.profiles import user_path
    from yuki.memory.privacy import user_config_path
    from yuki.memory.store import default_db_path, default_memory_dir

    real = str(_real_memory_dir()).lower()
    for path in (default_memory_dir(), default_db_path(), acting.acting_path(None), user_config_path(), user_path()):
        assert str(path).startswith(str(_temp_memory_dir)), path
        assert not str(path).lower().startswith(real), path


def test_marker_without_a_path_lands_in_the_temp_dir(_temp_memory_dir) -> None:
    """acting.begin(None, ...) - what an agent built without a memory path does - never touches the real marker."""
    from yuki.memory import acting

    assert acting.begin(None, "test-token", "a test request", lane="test")
    try:
        assert (Path(_temp_memory_dir) / acting.ACTING_FILE).exists()
        current = acting.current(None)
        assert current is not None and current.token == "test-token"
    finally:
        assert acting.end(None, "test-token")
    assert not (Path(_temp_memory_dir) / acting.ACTING_FILE).exists()


def test_embedding_models_stay_shared(_temp_memory_dir) -> None:
    """The model cache is public data, not memory: it is not redirected (no re-download under the guard)."""
    from yuki.memory.embed import Embedder

    assert Embedder().models_dir == _real_memory_dir() / "models"
