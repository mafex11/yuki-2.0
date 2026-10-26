"""Zero-setup connections to the user's apps (the design notes D0, D4).

Nothing in this package needs an account, an API key, a sign-in, a token or a per-app install. It holds:

* :mod:`yuki.connect.templates`: every site URL template, URI template and app UI label, as data in ``sites/*.toml``;
* :mod:`yuki.connect.spotify_desktop`: Spotify's own desktop window read through UI Automation (read-only), its media
  session matched to its window's process, and the memory side's watcher of it (warm-up, play-start and leave-front
  reads, the daily state probe, learned labels and links).

Logs written from here are content-free: ids, code words, counts and times; never a name, a link, an id or a title
(a digest, if one is ever logged, goes through ``yuki.log.events.log_digest``, PLAN.md D20).
"""

from __future__ import annotations

__all__: list[str] = []
