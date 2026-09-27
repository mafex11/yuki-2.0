"""``python -m yuki.onboarding``: the first-run setup dialog (exit code 0 done, 1 cancelled)."""

from __future__ import annotations

import sys

from yuki.onboarding import run

if __name__ == "__main__":
    sys.exit(run())
