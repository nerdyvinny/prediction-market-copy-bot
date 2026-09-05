"""One disk-backed quote cache, shared by every research script.

`ExactCopyBacktester.build_book` does the fetching; this only remembers what it
fetched. The scripts mostly sweep the same roster over the same month, so the
second one to run pays almost nothing.

Why every script needs this at all: `simulate` prices each fill off the token's
own book, and treats "no quote" as "no trade" — the same rule
`PaperExecutor` follows. A script that skips this step still runs, but it fills
at the leader's own price, which the live bot never does, and its numbers come
out several points too good. See `simulate`'s docstring.
"""

from __future__ import annotations

import json
import os

CACHE = os.environ.get(
    "PMBOT_BOOK_CACHE",
    os.path.join(os.environ.get("TEMP", "/tmp"), "pmb_book.json"),
)


def shared_book(bt, tapes, *, attach: bool = True, quiet: bool = False, **kw):
    """Build (or reload) the quote book for `tapes` and attach it to `bt`.

    With no window given, discovery covers the WHOLE span of the tapes. That is
    deliberate and it is the safe default: `build_book` quotes only what its
    discovery pass sees, an unquoted trade is a skipped trade, and a
    walk-forward that discovered on one fold would silently zero the others.
    Over-fetching costs one cached request per extra token; under-fetching
    changes the answer. Pass `now`/`lookback_days` only to narrow it on purpose.
    """
    stamps = [t.timestamp for tape in tapes.values() for t in tape]
    if stamps:
        kw.setdefault("now", max(stamps))
        span = (max(stamps) - min(stamps)).total_seconds() / 86_400.0
        kw.setdefault("lookback_days", int(span) + 2)
    try:
        with open(CACHE) as fh:
            cache = json.load(fh)
    except (OSError, ValueError):
        cache = {}
    before = len(cache)

    def progress(done: int, total: int) -> None:
        if not quiet:
            print(f"  book {done}/{total}...", flush=True)

    book = bt.build_book(tapes, book_cache=cache, progress=progress, **kw)
    if len(cache) != before:
        tmp = CACHE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(cache, fh)
        os.replace(tmp, CACHE)
    if not quiet:
        quoted = sum(1 for v in book.values() if v)
        print(f"book: {len(book)} tokens, {quoted} quoted "
              f"({len(cache) - before} newly fetched) -> {CACHE}", flush=True)
    if attach:
        bt.attach_book(book)
    return book
