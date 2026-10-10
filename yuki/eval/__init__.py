"""Developer evaluations that run on a copy of the memory database (never the live one, never the desktop).

* :mod:`yuki.eval.c3` - report 78 §10.3 phase C3: "did it my way", plan-only, time-split, three arms (report 105).
* :mod:`yuki.eval.dbcopy` - the copy every eval runs on (sqlite backup API, source opened read-only, key copied;
  the live database and its folder refused; ``remove_copy`` deletes only what ``copy_db`` made).
* :mod:`yuki.eval.habits_replay` - the audit plan's eval (PLAN.md rev 4, Appendix C.4): the suites (``moments`` for
  phase F, ``requests``, ``cost``), the $3 / 55 min caps enforced in code, the scorecard; CLI
  ``scripts/skills_eval.py``.

Nothing here is imported by the app.
"""
