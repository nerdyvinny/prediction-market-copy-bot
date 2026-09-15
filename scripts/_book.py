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
import time

CACHE = os.environ.get(
    "PMBOT_BOOK_CACHE",
    os.path.join(os.environ.get("TEMP", "/tmp"), "pmb_book.json"),
)


def tmp_path(dest: str) -> str:
    """A scratch name only this process will use.

    Every script in here shares one cache path, and they are meant to be run
    together -- a census scoring in one terminal while a sweep reads the same
    quotes in another. With a single fixed `<cache>.tmp`, the second writer
    replaces the first writer's scratch file and the first then dies on a
    vanished path. That is not a rare race: it killed a census 24 minutes into
    a retry, purely because another script ran alongside it.

    Per-process temp names remove the collision. The final `os.replace` is
    still last-writer-wins, which is correct for a cache -- both copies are
    supersets of what they started from, so the loser's new quotes are simply
    refetched next time.
    """
    return f"{dest}.{os.getpid()}.tmp"


def atomic_replace(tmp: str, dest: str) -> bool:
    """Persist the cache, tolerating Windows' non-atomic reality.

    `os.replace` onto an existing path raises WinError 32 whenever anything
    else has the target open even for reading -- an indexer or a virus scanner
    walking TEMP is enough, and this file is tens of megabytes by the end of a
    census, so the scan window is wide. It killed a 90-day census four hours
    in, on chunk five of forty-five, having already fetched every tape.

    A cache write is not worth losing a run over: retry a few times, and if the
    file still will not move, keep the quotes in memory and carry on. The next
    chunk re-fetches only what this one could not save.

    FileNotFoundError means the temp file is already gone -- another process
    replaced it out from under us (see `tmp_path`). Same conclusion: this
    write is lost, the cache still holds someone's good copy, carry on.
    """
    for attempt in range(6):
        try:
            os.replace(tmp, dest)
            return True
        except FileNotFoundError:
            return False
        except PermissionError:
            time.sleep(0.5 * 2 ** attempt)      # 0.5s .. 16s, ~31s in total
    print(f"  cache: could not replace {dest} after 6 tries; "
          f"continuing with the in-memory copy", flush=True)
    try:
        os.unlink(tmp)
    except OSError:
        pass
    return False


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
        tmp = tmp_path(CACHE)
        with open(tmp, "w") as fh:
            json.dump(cache, fh)
        atomic_replace(tmp, CACHE)
    if not quiet:
        quoted = sum(1 for v in book.values() if v)
        print(f"book: {len(book)} tokens, {quoted} quoted "
              f"({len(cache) - before} newly fetched) -> {CACHE}", flush=True)
    if attach:
        bt.attach_book(book)
    return book
