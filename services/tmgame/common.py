"""The flushed log line (_say) and the small helpers every module leans on: integer knobs, reply
delays.
"""
import os
import sys
import time


def _say(*args, **kwargs):
    """`print()`, but FLUSHED -- and that word is the whole point of this.

    WARNING: EVERY DIAGNOSTIC THIS MODULE EMITS WAS INVISIBLE UNTIL 2026-08-20. The
    `authsess` container does NOT set `PYTHONUNBUFFERED` (the `jan` service does,
    which is why nobody noticed), so a bare `print()` from here sat in a block
    buffer and effectively never reached `docker logs` at all. `log("authserv")`
    lines were fine, so the log looked healthy while this module was mute.

    It cost three wrong diagnoses in one night: "the @Opt= writer never fires"
    (it fires -- nothing had moved), "member_id is None" (it is not), and
    "@Save= recorded nothing" (unknown -- the recorder's own line was buffered).
    Every one of those was reasoning from an absence of output that was never
    evidence of anything.

    Fixed HERE rather than in the compose env on purpose: `pol-git-sync` ignores
    docker-compose files, so an env fix lands on disk and recreates nothing,
    while a Python change deploys itself.
    """
    kwargs.setdefault("flush", True)
    # WARNING: STDOUT HAS NOW EATEN THIS MODULE'S DIAGNOSTICS TWICE. First the
    # unbuffered-print trap this banner describes; then, 2026-08-20T22:02, the
    # FLUSHED print -- verified on `jan`, which sets PYTHONUNBUFFERED -- fed a
    # stdout that reaches `docker logs` on no other container, so a reservation
    # failed with zero observable output while authserv.log looked healthy
    # (1,220 lines, not one of them ours). Absence of output was read as
    # evidence three times in one night on exactly this path.
    #
    # So: the same file every other diagnostic in this process uses, through
    # the core's log handed over by `titles.core` (no import of the core: a
    # title never imports responders). Outside the core (the selftests) the
    # handle's default prints, flushed.
    try:
        import titles
        titles.core.log("authserv",
                        " ".join(a if isinstance(a, str) else repr(a)
                                 for a in args))
        return
    except Exception:
        pass
    try:
        print(*args, **kwargs)      # the real builtin; _say is its only caller
    except UnicodeEncodeError:
        # WARNING: A LOG LINE MUST NEVER TAKE DOWN THE REQUEST THAT WROTE IT --
        # `responders.log` carries that rule verbatim and this function was
        # missing it. Measured 2026-08-20: a single emoji in a MATCH READY line
        # raised UnicodeEncodeError out of `_seat_at_table`, INSIDE a reply path,
        # on any stdout that is not UTF-8 (cp1252 on the Windows dev box; a
        # container that ever starts without a UTF-8 locale would do the same).
        # Half of what this module logs is client-supplied bytes, so the guard
        # is load-bearing, not decoration.
        enc = (getattr(sys.stdout, "encoding", None) or "ascii")
        safe = [a.encode(enc, "backslashreplace").decode(enc)
                if isinstance(a, str) else a for a in args]
        print(*safe, **kwargs)


def _reply_delay(name, default_ms):
    """Hold a reply back before sending it -- see the banner at `@GameM=`."""
    ms = _env_int(name, default_ms)
    if ms > 0:
        time.sleep(min(ms, 30000) / 1000.0)


def _env_int(name, default):
    """An integer knob, tolerant of junk -- a bad env must not kill a reply."""
    try:
        return int(os.environ.get(name, str(default)), 0)
    except ValueError:
        return default
