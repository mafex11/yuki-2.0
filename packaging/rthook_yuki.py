"""PyInstaller runtime hook for Yuki: runs before any other runtime hook and before yuki.frozen.

* ``OPENBLAS_NUM_THREADS=1`` before anything can load numpy (yuki/__init__.py sets it too, but in
  the frozen app a runtime hook could import numpy first: measured 2026-09-27, the first process
  then committed 15 x 32 MB of OpenBLAS thread buffers, ~480 MB private).
* ``NLTK_DATA``: the bundled nltk data, so pipecat's lazy sentence tokenizer finds it without the
  stock nltk runtime hook (removed in the spec: it imported nltk, +29 MB and ~1 s, in every process).
"""
import os
import sys

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
_data = os.path.join(getattr(sys, "_MEIPASS", ""), "nltk_data")
if os.path.isdir(_data):
    os.environ.setdefault("NLTK_DATA", _data)
