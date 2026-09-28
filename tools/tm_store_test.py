#!/usr/bin/env python3
"""tm_store_test.py -- Tetra Master's state outside the process, on a real
PostgreSQL database and a real Valkey server.

    python tools/tm_store_test.py

Pins:
  * the migrations: services/tm_migrations/ applies next to OpenLobby's own set
    in one schema_migrations table, and a second run of either applies
    nothing;
  * tournament standings (tmeventstate): scoring, the picked deck, the paid
    mark, the member list blob, and that two games finishing at once lose
    neither's steps;
  * the weekly champion: tools/tmrank.py names it, the card shop reads the
    newest week, and earlier weeks stay as history;
  * the board's Discord bookkeeping (polboards): message ids and the bot's
    channels go to tm_board_state when a database is configured, and an
    explicit --<feed>-discord-state path is still a file;
  * live state across processes, on Valkey: a roster one process publishes is
    what another process (the board) reads, and an accept quorum written by
    one process is adopted by the next (the restart it exists for).

It needs a PostgreSQL server (Docker, or POL_TEST_DATABASE_URL) and, for the
cross-process part, Valkey (Docker, or POL_TEST_VALKEY_URL). Without them it
reports SKIP, or FAIL when POL_TEST_REQUIRE_DB=1.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.normpath(os.path.join(HERE, os.pardir, "services"))
sys.path.insert(0, HERE)
import tm_testenv                                                  # noqa: E402

CORE = tm_testenv.setup(need_core=False)
import tmpg                                                        # noqa: E402

TMP = tempfile.mkdtemp(prefix="tm-store-")
os.makedirs(os.path.join(TMP, "resources"))
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_RESOURCE_DIR"] = os.path.join(TMP, "resources")
os.environ["POL_LOG_DIR"] = TMP
os.environ.pop("POL_VALKEY_URL", None)
os.environ.pop("POL_TM_CHAMPION_NAME", None)

FAILS = []


def check(label, ok, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                           ("  --  %r" % (detail,)) if detail and not ok else ""), flush=True)
    if not ok:
        FAILS.append(label)


def migrations(tmstore):
    print("migrations")
    from polcore import db
    applied = tmstore.db.migrate(directory=tmstore.MIGRATIONS_DIR, log=lambda m: None)
    check("HippaulMaster's set applies on an empty database", applied == ["3001_tm_state"],
          applied)
    tables = {r["table_name"] for r in db.query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
    check("...creating its tm_ tables",
          {"tm_event_standing", "tm_champion", "tm_board_state"} <= tables, tables)
    core = db.migrate(log=lambda m: None)
    names = [n for _v, n, _p in db.migration_files()]
    check("OpenLobby's own set still applies afterwards, every file of it",
          core == names and len(names) >= 1, (core, names))
    have = db.applied_migrations()
    check("both sets share schema_migrations without a clash",
          3001 in have and all(v < 1000 for v in have if v != 3001), sorted(have))
    again = (tmstore.db.migrate(directory=tmstore.MIGRATIONS_DIR, log=lambda m: None),
             db.migrate(log=lambda m: None))
    check("a second run of either set applies nothing", again == ([], []), again)
    tmstore.forget_schema()
    tmstore.ensure_schema()
    check("ensure_schema on an up-to-date database is quiet and remembered",
          db.database_url() in tmstore._schema_ready)


def standings(tmeventstate):
    print("tournament standings (tm_event_standing)")
    w = 1789000000
    rows = tmeventstate.record_game(w, [(7, "perfect"), (8, "lose")],
                                    active_missions=[(1, 1)],
                                    extras={7: {"combos": 2}})
    check("a perfect win moves 3 steps and ticks the perfect-win mission",
          rows["7"]["steps"] == 3 and rows["7"]["perfect"] == 1
          and rows["7"]["missions"] == 1 << 1 and rows["7"]["combos"] == 2, rows)
    check("...the loser plays a game and moves nowhere",
          rows["8"]["games"] == 1 and rows["8"]["steps"] == 0, rows)
    tmeventstate.record_game(w, [(7, "win"), (8, "quit")])
    st = tmeventstate.standings(w)
    check("a second game adds to the first (win +2, quit -1 floored at 0)",
          st["7"]["steps"] == 5 and st["7"]["streak"] == 2 and st["8"]["steps"] == 0, st)
    tmeventstate.set_deck(w, 7, [b"1|2|3|4|5|6|7|8"] * 5)
    check("the picked deck comes back as the rows it was given",
          tmeventstate.deck(w, 7) == [b"1|2|3|4|5|6|7|8"] * 5
          and tmeventstate.standings(w)["7"]["steps"] == 5)
    check("a player who picked nothing has no deck", tmeventstate.deck(w, 99) == [])
    tmeventstate.mark_paid(w, 8)
    check("the paid mark sticks", tmeventstate.standings(w)["8"].get("paid") is True)
    check("another window is a fresh board", tmeventstate.standings(w + 7200) == {})
    blob = tmeventstate.member_list_blob(w, lambda m: {"7": "AB12CD56EB0F5932",
                                                       "8": "00000000000000AA"}[m],
                                         lambda m: {"7": "Fox", "8": "Perry"}[m])
    import struct
    check("the member list: two rows, the leader first, their POL-ID and name",
          len(blob) == tmeventstate.MEMBER_LIST_SIZE
          and struct.unpack_from("<I", blob, 4)[0] == 2
          and struct.unpack_from("<Q", blob, 8)[0] == 0xAB12CD56EB0F5932
          and blob[8 + 8:8 + 11] == b"Fox"
          and struct.unpack_from("<i", blob, 8 + 0x1C)[0] == 5)

    # two games finishing together: each read-modify-write holds the lock, so
    # neither overwrites the other's steps
    w2 = w + 14400
    errs = []

    def game(i):
        try:
            tmeventstate.record_game(w2, [(100 + i % 2, "win"), (200, "lose")])
        except Exception as exc:                             # noqa: BLE001
            errs.append(exc)
    threads = [threading.Thread(target=game, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    st = tmeventstate.standings(w2)
    check("twelve games scored at once: every one counted",
          not errs and st["200"]["games"] == 12 and st["100"]["games"] == 6
          and st["101"]["games"] == 6 and st["100"]["steps"] == 12, (errs, st))


def champion(tetramaster, tmstore):
    print("the weekly champion (tm_champion)")
    spec = importlib.util.spec_from_file_location("tmrank_tool", os.path.join(HERE, "tmrank.py"))
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    import tmprize
    shop = tetramaster.shopdoors
    shop._CHAMPION_CACHE.update(mtime=None, data=None)
    check("no champion yet: the shop sends no /SN=", b"/SN=" not in shop._shopinit_body(1))
    real_week = tmprize.week_id
    try:
        tmprize.week_id = lambda *a, **k: 2957
        blob = tool._write_champion([{"member_id": "7", "name": "Fox"}], {"7": {1: 1}})
        tmprize.week_id = lambda *a, **k: 2958
        tool._write_champion([{"member_id": "8", "name": "Perry"}], {"8": {1: 1}})
    finally:
        tmprize.week_id = real_week
    check("the publish names the Top 30's #1", blob == {"member_id": "7", "name": "Fox",
                                                        "week": 2957}, blob)
    rows = tmstore.db.query("SELECT week, member_id, name FROM tm_champion ORDER BY week")
    check("one row per week: last week's champion stays as history",
          [(r["week"], r["name"]) for r in rows] == [(2957, "Fox"), (2958, "Perry")], rows)
    shop._CHAMPION_CACHE.update(mtime=None, data=None)
    body = shop._shopinit_body(1)
    check("the card shop labels the rank_1 Pack after the newest champion",
          b"/SN=Perry" in body and shop._champion()["member_id"] == "8", body)
    os.environ["POL_TM_CHAMPION_NAME"] = "Fox"
    try:
        check("POL_TM_CHAMPION_NAME still overrides the table",
              b"/SN=Fox" in shop._shopinit_body(1))
    finally:
        del os.environ["POL_TM_CHAMPION_NAME"]


def boards(polboards, tmstore):
    print("the board's Discord bookkeeping (tm_board_state)")
    args = polboards.build_parser().parse_args(["--tm-port", "1"])
    os.environ["POL_BOARDS_STATE_DIR"] = os.path.join(TMP, "state")
    path = polboards.state_path(args, "tm_auction")
    check("with a database, a feed's message ids are a tm_board_state row",
          path == "db:tm_auction_discord", path)
    hook = "https://discord.com/api/webhooks/8/SECRET"
    d = polboards.Discord("tm", hook, polboards.state_path(args, "tm"))
    d.msg_id, d.events = "1234", [{"id": "55", "t": 1.0}]
    d._save()
    row = tmstore.db.query_one("SELECT data FROM tm_board_state WHERE name = 'tm_discord'")
    check("...saved there, the webhook as a hash, never the URL",
          row and row["data"]["message_id"] == "1234"
          and "SECRET" not in json.dumps(row["data"]), row)
    again = polboards.Discord("tm", hook, polboards.state_path(args, "tm"))
    check("a restarted board edits the same message", again.msg_id == "1234"
          and again.events == [{"id": "55", "t": 1.0}], (again.msg_id, again.events))
    other = polboards.Discord("tm", hook + "X", polboards.state_path(args, "tm"))
    check("a new webhook starts fresh", other.msg_id is None)
    s = polboards.DiscordSet("tm_auction", hook, path)
    s.msgs = {"21": {"id": "77", "sig": "a", "t": 2.0, "ended": None}}
    s.done = {"20"}
    s._save()
    s2 = polboards.DiscordSet("tm_auction", hook, path)
    check("a message set round-trips (slots and the ones already done)",
          s2.msgs == s.msgs and s2.done == {"20"}, (s2.msgs, s2.done))
    polboards._note_channel("chosen", "tm", "4444", guild="g1")
    polboards._note_channel("posted", "tm", "4445", guild="g1")
    ch = polboards.bot_channels()
    check("the bot's channels are the discord_channels row",
          ch["chosen"] == {"tm": {"g1": "4444"}} and ch["posted"] == {"tm": {"g1": "4445"}}
          and tmstore.db.query_one("SELECT 1 AS x FROM tm_board_state "
                                   "WHERE name = 'discord_channels'") is not None, ch)
    polboards._forget_channel("chosen", "tm", guild="g1")
    check("...and forgetting one removes it", polboards.bot_channels()["chosen"] == {})
    check("nothing was written to the state dir", not os.path.exists(os.path.join(TMP, "state")))
    fpath = os.path.join(TMP, "explicit", "tm_discord.json")
    args.tm_discord_state = fpath
    check("an explicit --tm-discord-state is still a file",
          polboards.state_path(args, "tm") == fpath)
    f = polboards.Discord("tm", hook, fpath)
    f.msg_id = "999"
    f._save()
    with open(fpath, encoding="utf-8") as fh:
        check("...written there", json.load(fh)["message_id"] == "999")
    saved = os.environ.pop("POL_DATABASE_URL")
    try:
        check("with no database, the state dir as before",
              polboards.state_path(args, "tm_live").endswith("tm_live_discord.json"))
    finally:
        os.environ["POL_DATABASE_URL"] = saved
    del os.environ["POL_BOARDS_STATE_DIR"]


CHILD = r"""
import os, sys
sys.path.insert(0, sys.argv[1])
what = sys.argv[2]
if what == "roster":
    import tmroom
    tmroom.note_name(41, "Quina")
    tmroom.note_pol_id(41, "AB12CD56EB0F5932")
elif what == "accept":
    import tetramaster as T
    T._accepts_persist("#TM0R009", 2, {"m41"}, {"m41", "m42"})
print("child done")
"""


def cross_process(url):
    print("live state across processes (Valkey)")
    from polcore import kv
    env = dict(os.environ, POL_VALKEY_URL=url, POL_KV_PREFIX="tmtest:%s:" % os.path.basename(TMP),
               POL_TM_ROSTER_KEY="tm:roster", POL_TM_ACCEPTS_KEY="tm:match-accepts",
               PYTHONPATH=os.pathsep.join(p for p in (SERVICES, CORE or "") if p))
    for what in ("roster", "accept"):
        r = subprocess.run([sys.executable, "-c", CHILD, SERVICES, what], env=env,
                           capture_output=True, text=True, timeout=120)
        check("a separate process publishes the %s" % what,
              r.returncode == 0 and "child done" in r.stdout, (r.stdout[-400:], r.stderr[-800:]))
    os.environ.update(POL_VALKEY_URL=url, POL_KV_PREFIX=env["POL_KV_PREFIX"],
                      POL_TM_ROSTER_KEY="tm:roster", POL_TM_ACCEPTS_KEY="tm:match-accepts")
    kv.reset()
    try:
        check("this process is on Valkey", kv.default().backend == "valkey")
        import boardtm
        check("the board (another process) reads the name the roster published",
              boardtm._roster_section("names").get("41") == "Quina",
              boardtm._roster_section("names"))
        check("...and the POL-ID", boardtm._roster_section("polids").get("41")
              == "AB12CD56EB0F5932")
        import tmroom
        tmroom._OWNER[0] = False
        tmroom._KEY = "tm:roster"
        check("tmroom in a process that has not written reads it too",
              tmroom.name_of(41) == "Quina" and tmroom.pol_id_of(41) == "AB12CD56EB0F5932")
        import tetramaster as T
        got = T._accepts_adopt("#TM0R009", 2, {"m41", "m42"})
        check("an accept one process kept is adopted by the next", got == {"m41"}, got)
        ttl = kv.ttl("tm:match-accepts")
        check("...and the accept store expires on its own", 0 < ttl <= 600, ttl)
        kv.default().flush()
    finally:
        for k in ("POL_VALKEY_URL", "POL_KV_PREFIX", "POL_TM_ROSTER_KEY", "POL_TM_ACCEPTS_KEY"):
            os.environ.pop(k, None)
        kv.reset()


def main():
    if tmpg.fresh_database() is None:
        return tmpg.skip_or_fail("tm_store")
    import tmstore
    print("database: %s" % tmstore.where())
    migrations(tmstore)
    import tmeventstate
    standings(tmeventstate)
    import tetramaster
    champion(tetramaster, tmstore)
    import polboards
    boards(polboards, tmstore)
    url = tmpg.valkey_url()
    if url is None:
        if tmpg.skip_or_fail("tm_store", "Valkey server"):
            FAILS.append("no Valkey")
    else:
        cross_process(url)
    print()
    if FAILS:
        print("FAILED: %d check(s)" % len(FAILS))
        return 1
    print("all store checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
