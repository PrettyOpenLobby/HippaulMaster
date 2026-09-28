"""tmstore.py -- where CrystalMaster reaches OpenLobby's storage layer.

Tetra Master keeps two kinds of state outside the process that made it:

  * durable game data (tournament standings, the weekly champion, the web
    board's Discord bookkeeping) in the PostgreSQL database the whole stack
    shares, through OpenLobby's `polcore.db` (POL_DATABASE_URL);
  * live state that login, authsess, the ranking job and the web board all
    read (the room roster, the matches being watched, a pending accept
    quorum, the tournament ticker's latest moment) in Valkey, through
    `polcore.kv` (POL_VALKEY_URL). Nothing durable goes there: losing Valkey
    loses who is seated and what is being played right now, and nothing else.

With POL_VALKEY_URL empty, `polcore.kv` keeps live state in this process's
memory. That is right for the self-tests and wrong for a stack, where login and
authsess are two containers and must see the same roster.

Finding polcore:

  * in the image, which is built FROM the OpenLobby image, it is /app/polcore
    and imports directly;
  * in a checkout, OPENLOBBY_SERVICES names OpenLobby's `services/`, and
    failing that the checkout beside this repository (../openlobby/services)
    is used, the same rule tools/tm_testenv.py follows.

Migrations are services/tm_migrations/NNNN_name.sql, applied with
`polcore.db.migrate(directory=...)`. They share OpenLobby's schema_migrations
table, which is keyed by the version number alone, so each repository owns a
range: CrystalMaster numbers its files 3001..3999, and every table it creates
starts with `tm_`. A version number that appears in two sets would be taken as
already applied by whichever set ran second.

Every live key starts with `tm:` (below polcore.kv's own POL_KV_PREFIX).

    python tmstore.py migrate     apply what is pending (uses POL_DATABASE_URL)
    python tmstore.py status      list CrystalMaster's migrations and their state
"""
import json
import os
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))

#: CrystalMaster's migration files. Versions 3001..3999 are this repository's.
MIGRATIONS_DIR = os.path.join(_HERE, "tm_migrations")

#: The prefix of every live key this repository writes.
KEY_PREFIX = "tm:"


def roster_key():
    """The room roster's key (tmroom publishes it; the board and the ranking
    job read it). Was <POL_DATA_DIR>/tm-roster.json."""
    return os.environ.get("POL_TM_ROSTER_KEY", "tm:roster").strip() or "tm:roster"


def watch_key():
    """The matches being played, as the web board draws them
    (tmgame/webwatch.py writes, boardtm.py reads), or None when
    POL_TM_WATCH_KEY is 0/off. Was <POL_DATA_DIR>/tm-tables-live.json."""
    name = os.environ.get("POL_TM_WATCH_KEY", "tm:tables-live").strip()
    return None if name in ("", "0", "off") else name


def _openlobby_services():
    """Candidate OpenLobby `services/` directories, most specific first."""
    out = []
    env = os.environ.get("OPENLOBBY_SERVICES", "").strip()
    if env:
        out.append(env)
    out.append(os.path.normpath(os.path.join(_HERE, os.pardir, os.pardir,
                                             "openlobby", "services")))
    out.append("/app")
    return out


try:
    from polcore import db, kv  # noqa: E402
except ImportError:
    for _cand in _openlobby_services():
        if os.path.isdir(os.path.join(_cand, "polcore")):
            if _cand not in sys.path:
                sys.path.append(_cand)
            break
    from polcore import db, kv  # noqa: E402,F811

_schema_lock = threading.Lock()
_schema_ready = set()
_core_ready = set()


def errors():
    """The exceptions that mean the database could not be reached or read,
    as a tuple for `except`."""
    errs = [db.DatabaseNotConfigured, db.MigrationError]
    if db.psycopg is not None:
        errs.append(db.psycopg.Error)
        try:
            from psycopg_pool import PoolTimeout
            errs.append(PoolTimeout)
        except ImportError:                                  # pragma: no cover
            pass
    return tuple(errs)


def ensure_schema(log=None):
    """Apply CrystalMaster's pending migrations, once per process and database.

    Cheap after the first call. Raises what `polcore.db.migrate` raises when
    the database cannot be reached, and remembers nothing then, so the next
    call tries again.
    """
    key = db.database_url()
    if key in _schema_ready:
        return
    with _schema_lock:
        if key in _schema_ready:
            return
        db.migrate(directory=MIGRATIONS_DIR,
                   log=log or (lambda msg: print("[tmstore] %s" % msg, flush=True)))
        _schema_ready.add(key)


def ensure_core_schema(log=None):
    """Apply the core's own pending migrations, once per process and database.

    Tetra Master's collections, saves, prizes, auction records and rank
    lists are rows of the core's `blob` table (polcore.blobs), which the
    core's services create when they start. A process that can run first
    (the ranking job, the board or a test) applies them itself; the advisory
    lock in `polcore.db.migrate` keeps two from doing it at once. Raises like
    `ensure_schema`.
    """
    key = db.database_url()
    if key in _core_ready:
        return
    with _schema_lock:
        if key in _core_ready:
            return
        db.migrate(log=log or (lambda msg: print("[tmstore] %s" % msg, flush=True)))
        _core_ready.add(key)


def forget_schema():
    """Drop the once-per-process memo (tests that switch databases)."""
    with _schema_lock:
        _schema_ready.clear()
        _core_ready.clear()


def migrate_at_start(who):
    """A service's start-up call: apply the migrations now, and say so in the
    log if the database is not there yet (the first use tries again)."""
    try:
        ensure_schema(log=lambda msg: print("[%s] %s" % (who, msg), flush=True))
        return True
    except errors() as exc:
        print("[%s] database not ready (%s); Tetra Master's tables are "
              "created on first use" % (who, exc), flush=True)
        return False


def jsonb(value):
    """`value` as JSON text for a `%s::jsonb` placeholder."""
    return json.dumps(value, separators=(",", ":"))


class Snapshot:
    """A JSON document that one process publishes and the others read.

    This is what the `<POL_DATA_DIR>/*-live.json` files were: one writer
    replaces the whole document, any process reads it. The document lives
    under `key` and a counter under `key:gen` moves on every write, so a
    reader re-parses only when something changed (the files were re-read on
    an mtime change for the same reason).

    `key` may be reassigned (the self-tests point a store at a scratch key);
    the cache follows it.
    """

    def __init__(self, key, ttl=None):
        self.key = key
        self.ttl = ttl
        self._lock = threading.Lock()
        self._cache = (None, None, {})          # (key, gen, data)

    def read(self):
        """The published document, or {} when there is none (or it cannot be
        read: a reader never breaks on live state it cannot see)."""
        key = self.key
        try:
            gen = kv.get(key + ":gen")
            ck, cgen, cdata = self._cache
            if ck == key and gen is not None and gen == cgen:
                return cdata
            raw = kv.get(key)
            data = json.loads(raw) if raw else {}
            if not isinstance(data, dict):
                data = {}
        except (ValueError, OSError):
            return self._cache[2] if self._cache[0] == key else {}
        except Exception as exc:                             # noqa: BLE001
            # a Valkey outage: the last good view stands, as a torn file did
            _whine("read %s" % key, exc)
            return self._cache[2] if self._cache[0] == key else {}
        with self._lock:
            self._cache = (key, gen, data)
        return data

    def write(self, data, ttl=None):
        """Publish `data` (a dict) in place of the current document."""
        ttl = self.ttl if ttl is None else ttl
        key = self.key
        kv.set(key, json.dumps(data, separators=(",", ":")), ttl=ttl)
        kv.incr(key + ":gen")
        if ttl is not None:
            kv.expire(key + ":gen", ttl)
        with self._lock:
            self._cache = (None, None, {})

    def forget(self):
        """Drop the reader's cache (the next read goes to the store)."""
        with self._lock:
            self._cache = (None, None, {})

    def clear(self):
        kv.delete(self.key, self.key + ":gen")
        self.forget()


_WHINED = set()


def _whine(what, exc):
    if what not in _WHINED:
        _WHINED.add(what)
        print("[tmstore] WARNING: %s failed (%r); said once" % (what, exc),
              flush=True)


def where():
    """The database this process uses, for a log line (never the password)."""
    try:
        url = db.database_url()
    except db.DatabaseNotConfigured:
        return "(POL_DATABASE_URL is not set)"
    try:
        from urllib.parse import urlsplit
        u = urlsplit(url)
        return "postgresql://%s%s%s" % (u.hostname or "", ":%d" % u.port
                                        if u.port else "", u.path or "")
    except ValueError:
        return "(an unparsable POL_DATABASE_URL)"


def _main(argv):
    if not argv or argv[0] not in ("migrate", "status"):
        print(__doc__)
        return 2
    try:
        if argv[0] == "migrate":
            names = db.migrate(directory=MIGRATIONS_DIR)
            print("applied: " + ", ".join(names) if names else "up to date")
        else:
            have = db.applied_migrations()
            for version, name, _path in db.migration_files(MIGRATIONS_DIR):
                row = have.get(version)
                print("%-32s %s" % (name, "applied %s" % row["applied_at"]
                                    if row else "pending"))
        return 0
    except errors() as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
