"""Stable message identities.

A message must get the same fingerprint every time it is read, whatever the
scroll position, hover state or day it is read on, so the pipeline can store
fingerprints and treat only unseen ones as new.  What changes between two
reads of the same message, and so is kept out of the hash:

* the time label's wording: "Today at 6:12 PM" becomes "Yesterday at 6:12 PM",
  then "18/09/2026 18:12" - only the time of day (``HH:MM``) goes in;
* the sender: a grouped message's sender is only known while the group's
  first message is on screen, so it is not hashed (collisions need the same
  text in the same thread at the same minute);
* "(edited)" markers, reactions, hover toolbars and link previews - removed by
  the extractor before the text gets here (reaction/toolbar subtrees are never
  body text) and by :func:`normalise`;
* whitespace, case, zero-width characters and trailing ellipses of clipped
  previews.

When the app exposes its own message id (Slack's and Discord's row ids carry
the message timestamp), that id is the fingerprint, scoped to the app.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\ufffc"))
_SPACE = re.compile(r"\s+")


def normalise(text: str, noise: tuple[str, ...] = ()) -> str:
    """Canonical form of a message text for hashing (not for display)."""
    value = unicodedata.normalize("NFKC", text or "").translate(_INVISIBLE)
    lowered = value.casefold()
    for marker in noise:
        if marker:
            lowered = lowered.replace(marker.casefold(), " ")
    lowered = _SPACE.sub(" ", lowered).strip()
    return lowered.rstrip(".…").strip()


def _digest(*parts: str) -> str:
    joined = "\x1f".join(parts)
    return hashlib.sha256(joined.encode("utf-8", "surrogatepass")).hexdigest()[:24]


def message_fingerprint(scope: str, time_of_day: str, text: str, noise: tuple[str, ...] = ()) -> str:
    return _digest("m1", scope.casefold().strip(), time_of_day, normalise(text, noise))


def content_key(scope: str, text: str, noise: tuple[str, ...] = ()) -> str:
    return _digest("c1", scope.casefold().strip(), normalise(text, noise))


def app_id_fingerprint(profile: str, app_id: str) -> str:
    return _digest("id1", profile, app_id)
