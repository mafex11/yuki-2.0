"""The ONE agent (phase 4, ``brain = "one"``): one Sonnet 5.5 agent owns the whole turn on voice and typed.

Built BESIDE ``all`` (the rollback; nothing under ``all`` changes): the deletions wait for the final test.

* :mod:`yuki.one.modes` - the one brain-mode helper (an unknown value reads as ``all``).
* :mod:`yuki.one.prompt` - the core prompt and the channel tails (ceiling 12,000 at import).
* :mod:`yuki.one.tools` - the tool schemas and the verb table (voice block ceiling 15,000 at import).
* :mod:`yuki.one.policy` - the single gate every act passes.
* :mod:`yuki.one.budget` - the per-turn runaway guard.
* :mod:`yuki.one.countdown` - the one send countdown host for voice and typed.

Only :mod:`yuki.one.modes` is imported here (it imports nothing of Yuki), so ``import yuki.one`` stays light for
``config.py`` and the mode checks; import the other modules by name.
"""

from yuki.one.modes import BRAIN_MODES, brain_mode, is_one, typed_brain, voice_brain

__all__ = ["BRAIN_MODES", "brain_mode", "is_one", "typed_brain", "voice_brain"]
