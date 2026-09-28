"""tmstore.py -- where HippaulMaster reaches OpenLobby's storage layer.

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
range: HippaulMaster numbers its files 3001..3999, and every table it creates
starts with `tm_`. A version number that appears in two sets would be taken as
already applied by whichever set ran second.

Every live key starts with `tm:` (below polcore.kv's own POL_KV_PREFIX).

    python tmstore.py migrate     apply what is pending (uses POL_DATABASE_URL)
    python tmstore.py status      list HippaulMaster's migrations and their state
    python tmstore.py import event_state FILE [--merge] [--dry-run]
    python tmstore.py import champion FILE [--merge] [--dry-run]
    python tmstore.py import board_state FILE|DIR [--merge] [--dry-run]

`import` moves what an earlier release kept in files into the tables:
`event_state` reads tm-event-state.json into tm_event_standing (one row per
player per tournament window), `champion` reads tm-champion.json into
tm_champion (the row of the week it names), and `board_state` reads the web
board's tm_*_discord.json and discord_channels.json (a file, or every such
file in a directory; the other boards' files in a shared state directory
are skipped) into tm_board_state, one row per file named as the board names
it. The file is only read. The import runs in one transaction
and refuses a table that already holds rows (exit 2) unless --merge is
given, which adds only the keys the table lacks. A second run finds nothing
to add. --dry-run prints the same report and writes nothing. Entries it
cannot map are listed and skipped.
"""
import datetime
import json
import os
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))

#: HippaulMaster's migration files. Versions 3001..3999 are this repository's.
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
    """Apply HippaulMaster's pending migrations, once per process and database.

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


# --------------------------------------------------------------------------- #
# importing the files an earlier release kept
# --------------------------------------------------------------------------- #
class SourceError(RuntimeError):
    """The source cannot be read as the file it should be."""


class _Rollback(Exception):
    pass


class Plan:
    """One target table of an import: the rows read, and what became of them."""

    def __init__(self, table, key, cols, rows, jsonb=(), compare_skip=()):
        self.table, self.key, self.cols = table, tuple(key), tuple(cols)
        self.rows, self.jsonb = rows, set(jsonb)
        self.compare_skip = set(compare_skip)
        self.target = 0
        self.exists = False
        self.new, self.same, self.differs, self.dups = [], 0, [], 0
        self.inserted = 0

    def keyof(self, row):
        return tuple(row[c] for c in self.key)


def _mtime(path):
    return datetime.datetime.fromtimestamp(os.path.getmtime(path),
                                           datetime.timezone.utc)


def _read_json(path, want=dict):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise SourceError("cannot read %s: %s" % (path, exc)) from None
    except ValueError as exc:
        raise SourceError("%s is not JSON: %s" % (path, exc)) from None
    if not isinstance(data, want):
        raise SourceError("%s holds a %s, not the JSON object the game wrote"
                          % (path, type(data).__name__))
    return data


def _json_ok(value):
    """The value survives a JSONB round trip (no NUL, which jsonb refuses)."""
    return "\\u0000" not in json.dumps(value)


def read_old_event_state(path):
    """tmeventstate.py's {window: {member: row}} as one tm_event_standing row
    per player per window, keyed as tmeventstate keys them: the window's start
    as a number and the member id as text. The row is kept as the file held
    it. The file only ever held the current window; every window it names is
    imported."""
    state = _read_json(path)
    rows, skipped = [], []
    stamp = _mtime(path)
    for window, board in sorted(state.items()):
        try:
            win = int(str(window))
        except ValueError:
            skipped.append(("window %r" % window, "not a window start (unix seconds)"))
            continue
        if not isinstance(board, dict):
            skipped.append(("window %r" % window, "holds a %s, not the players' "
                            "rows" % type(board).__name__))
            continue
        for member, row in sorted(board.items()):
            what = "window %s, member %r" % (win, member)
            if not isinstance(row, dict):
                skipped.append((what, "holds a %s, not the player's row"
                                % type(row).__name__))
            elif not _json_ok(row):
                skipped.append((what, "holds a NUL character"))
            else:
                rows.append({"event_window": win, "member": str(member),
                             "data": row, "updated_at": stamp})
    return [Plan("tm_event_standing", ("event_window", "member"),
                 ("event_window", "member", "data", "updated_at"), rows,
                 jsonb=("data",), compare_skip=("updated_at",))], skipped


def read_old_champion(path):
    """tools/tmrank.py's {member_id, name, week} as the tm_champion row of
    that week. The file kept no time of its own, so named_at is the file's
    mtime, the moment the publish wrote it."""
    rec = _read_json(path)
    rows, skipped = [], []
    try:
        week = rec.get("week")
        if isinstance(week, bool) or not isinstance(week, int):
            raise ValueError("the week is %r, not a week number" % (week,))
        mid = rec.get("member_id")
        if mid is None or isinstance(mid, (dict, list, bool)) or not str(mid).strip():
            raise ValueError("the member id is %r" % (mid,))
        name = rec.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("the name is %r, not the champion's display name"
                             % (name,))
        if "\x00" in name or "\x00" in str(mid):
            raise ValueError("the record holds a NUL character")
        rows.append({"week": week, "member_id": str(mid), "name": name,
                     "named_at": os.path.getmtime(path)})
    except ValueError as exc:
        skipped.append(("the champion record", str(exc)))
    return [Plan("tm_champion", ("week",), ("week", "member_id", "name", "named_at"),
                 rows, compare_skip=("named_at",))], skipped


#: The start of every Discord state file the Tetra Master board wrote.
#: polboards names a feed's file <feed key>_discord.json and the bot's <feed
#: key>_bot_<guild>_discord.json, and board "tm"'s feed keys are "tm" and
#: "tm_<feed>", so each of its files starts with "tm_": tm_discord.json,
#: tm_auction_discord.json, tm_live_discord.json, and the bot's
#: tm_bot_<guild>_discord.json, tm_auction_bot_<guild>_discord.json and
#: tm_live_bot_<guild>_discord.json. The old state directory was shared by
#: every board, so a directory import takes only these and
#: discord_channels.json, and lists the other boards' files as skipped.
BOARD_PREFIX = "tm_"


def read_old_board_state(path):
    """The web board's <name>_discord.json and discord_channels.json: one
    file, or every such file directly in a directory. Each is one row of
    tm_board_state named for the file without its extension, which is the
    name polboards gives it."""
    if os.path.isdir(path):
        files = [os.path.join(path, n) for n in sorted(os.listdir(path))]
    elif os.path.isfile(path):
        files = [path]
    else:
        raise SourceError("%s does not exist" % path)
    rows, skipped = [], []
    for f in files:
        n = os.path.basename(f)
        if not os.path.isfile(f):
            continue
        if not (n.endswith("_discord.json") or n == "discord_channels.json"):
            skipped.append((n, "not a board state file (<name>_discord.json, "
                               "discord_channels.json)"))
            continue
        if n != "discord_channels.json" and not n.startswith(BOARD_PREFIX):
            skipped.append((n, "another board's state (this board's files "
                               "start with %s)" % BOARD_PREFIX))
            continue
        try:
            data = _read_json(f)
            if not _json_ok(data):
                raise SourceError("holds a NUL character")
        except SourceError as exc:
            skipped.append((n, str(exc)))
            continue
        rows.append({"name": n[:-len(".json")], "data": data, "updated_at": _mtime(f)})
    return [Plan("tm_board_state", ("name",), ("name", "data", "updated_at"), rows,
                 jsonb=("data",), compare_skip=("updated_at",))], skipped


IMPORTS = {"event_state": read_old_event_state, "champion": read_old_champion,
           "board_state": read_old_board_state}


def import_source(store, path, merge=False, dry_run=False, out=print):
    """Import one old source. Returns the exit status: 0 done, nothing to do
    or dry run; 1 the source or the database failed; 2 refused, the table
    already holds rows and the source has rows it lacks (without --merge)."""
    try:
        plans, skipped = IMPORTS[store](path)
    except SourceError as exc:
        out("error: %s" % exc)
        return 1
    out("import %s: %s" % (store, path))
    for what, why in skipped:
        out("  skipped %s: %s" % (what, why))
    if not dry_run:
        ensure_schema(log=lambda msg: out("  " + msg))
    status = None
    try:
        with db.transaction(lock="hippaulmaster.import") as conn:
            for p in plans:
                p.exists = conn.execute("SELECT to_regclass(%s) IS NOT NULL AS ok",
                                        (p.table,)).fetchone()["ok"]
                if not p.exists and not dry_run:
                    raise RuntimeError("%s does not exist after the migrations" % p.table)
                have = {}
                if p.exists:
                    have = {p.keyof(r): r for r in conn.execute(
                        "SELECT %s FROM %s" % (", ".join(p.cols), p.table))}
                p.target = len(have)
                seen = set()
                for r in p.rows:
                    k = p.keyof(r)
                    if k in seen:
                        p.dups += 1
                        continue
                    seen.add(k)
                    if k not in have:
                        p.new.append(r)
                    elif all(have[k][c] == r[c] for c in p.cols if c not in p.compare_skip):
                        p.same += 1
                    else:
                        p.differs.append(k)
            for p in plans:
                out("  %s: %d read, %d in the table%s, %d already there, %d to insert"
                    % (p.table, len(p.rows), p.target,
                       "" if p.exists else " (not created yet)",
                       p.same + len(p.differs), len(p.new)))
                for k in p.differs:
                    out("    kept the table's row, the file's differs: %s" % (k,))
            if any(p.new and p.target for p in plans) and not merge:
                status = "refused"
                raise _Rollback()
            if dry_run:
                status = "dry-run"
                raise _Rollback()
            for p in plans:
                sql = "INSERT INTO %s (%s) VALUES (%s) ON CONFLICT DO NOTHING" % (
                    p.table, ", ".join(p.cols),
                    ", ".join("%s::jsonb" if c in p.jsonb else "%s" for c in p.cols))
                for r in p.new:
                    p.inserted += conn.execute(sql, [
                        jsonb(r[c]) if c in p.jsonb else r[c] for c in p.cols]).rowcount
            status = "done" if any(p.inserted for p in plans) else "nothing"
    except _Rollback:
        pass
    except errors() + (RuntimeError,) as exc:
        out("FAILED, rolled back: %s" % exc)
        return 1
    if status == "refused":
        out("REFUSED: %s already holds rows. Nothing was written. Run again "
            "with --merge to add only the keys it lacks."
            % ", ".join(p.table for p in plans if p.new and p.target))
        return 2
    if status == "dry-run":
        out("Dry run: nothing was written.")
    elif status == "nothing":
        out("Nothing to import: the table already holds every entry. "
            "Nothing was changed.")
    else:
        out("Done: %d row(s) written." % sum(p.inserted for p in plans))
    return 0


def _import_main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="python tmstore.py import",
                                 description="Import what an earlier release "
                                 "kept in files (uses POL_DATABASE_URL).")
    ap.add_argument("store", choices=sorted(IMPORTS))
    ap.add_argument("source")
    ap.add_argument("--merge", action="store_true",
                    help="add only the keys the table lacks")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be imported; write nothing")
    args = ap.parse_args(argv)
    try:
        return import_source(args.store, args.source, merge=args.merge,
                             dry_run=args.dry_run)
    except errors() as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    finally:
        db.close()


def _main(argv):
    if argv and argv[0] == "import":
        return _import_main(argv[1:])
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
