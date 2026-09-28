#!/usr/bin/env python3
"""tm_import_test.py -- `python tmstore.py import event_state|champion|
board_state`: Tetra Master's old files into PostgreSQL.

    python tools/tm_import_test.py

The files are written by the code that wrote them, taken from git
(OLD_COMMIT, the last commit that kept them as files, extracted with
`git archive` into a temporary directory and run from there):
tmeventstate.record_game(), set_deck() and mark_paid() write
tm-event-state.json, tools/tmrank.py's _write_champion() writes
tm-champion.json, and the web board's Discord and DiscordSet classes and
_note_channel() write tm_discord.json, tm_auction_discord.json and
discord_channels.json. The old tmeventstate also builds the tournament's
member list from its file, for comparison. Entries no version could map are
added by hand.

Checked on a fresh database: the rows and their values; the server's own
readers (tmeventstate.standings(), deck() and member_list_blob(), the card
shop's champion, polboards) reading them back; the same actions run through
the new code producing the same rows; a second run that changes nothing;
--dry-run; the refusal on a table that already holds rows; --merge; bad
sources; and sources that are byte for byte what they were.

Needs the OpenLobby core beside this tree and a PostgreSQL server (Docker,
or POL_TEST_DATABASE_URL); SKIPs without one, or FAILs under
POL_TEST_REQUIRE_DB=1.
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, os.pardir))
SERVICES = os.path.join(ROOT, "services")
sys.path.insert(0, HERE)
import tm_testenv                                                  # noqa: E402

CORE = tm_testenv.setup(need_core=False)
import tmpg                                                        # noqa: E402

#: The last commit whose tournament standings, champion and board state were
#: files (the `game-split` branch).
OLD_COMMIT = "699a1b3e412e4dc5107bece6b040a016b9931b31"

#: The tournament window the fixture plays in, and the one the new code
#: replays the same games in for comparison.
WINDOW = 1789000000
WINDOW_NEW = WINDOW + 604800
WEEK, WEEK_NEW = 2957, 2958
HOOK = "https://discord.com/api/webhooks/1/x"
DECK = [b"01020304", b"05060708", b"090a0b0c", b"0d0e0f10", b"11121314"]
POL_IDS = {"7": "AB12CD56EB0F5932", "8": "00000000000000A8"}
NAMES = {"7": "Lex", "8": "Quinn"}

FAILS = []


def check(label, ok, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                           ("  --  %s" % (str(detail)[:600],)) if detail and not ok else ""),
          flush=True)
    if not ok:
        FAILS.append(label)


#: The games of the fixture, played by the old code here and by the new code
#: in the test (games(), below, is the same sequence).
FIXTURE = r'''
import importlib.util, json, os, sys
old, data, state, window, week = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
sys.path.insert(0, os.path.join(old, "services"))
os.environ["POL_DATA_DIR"] = data
os.environ["POL_RESOURCE_DIR"] = os.path.join(data, "resources")
os.environ["POL_BOARDS_STATE_DIR"] = state
DECK = [bytes.fromhex(h) for h in sys.argv[6].split(",")]
import tmeventstate as E
E.record_game(window, [(7, "perfect"), (8, "lose")], active_missions=[(1, 1), (4, 1)])
E.record_game(window, [(8, "win"), (7, "tie")], active_missions=[(1, 1), (4, 1)],
              extras={8: {"combos": 2}})
E.set_deck(window, 7, DECK)
E.mark_paid(window, 8)
ids = json.loads(sys.argv[7])
names = json.loads(sys.argv[8])
blob = E.member_list_blob(window, lambda m: ids.get(str(m)), lambda m: names.get(str(m)))
with open(os.path.join(os.path.dirname(data), "member_list.hex"), "w") as fh:
    fh.write(blob.hex())
import tmprize
tmprize.week_id = lambda *a, **k: week
spec = importlib.util.spec_from_file_location("tmrank_tool", os.path.join(old, "tools", "tmrank.py"))
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)
got = tool._write_champion([{"member_id": "7", "name": "  Lex of the Lindblum Guard "}], {"7": {1: 1}})
assert got and got["week"] == week, got
import polboards as B
class Net:
    def __call__(self, req, timeout=None):
        class R:
            status = 200
            def read(self, *a): return b'{"id": "555"}'
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R()
hook = sys.argv[9]
B.Discord("tm", hook, os.path.join(state, "tm_discord.json"),
          opener=Net()).tick("s1", lambda: ({"content": "board"}, []), now=1000.0)
s = B.DiscordSet("tm_auction", hook, os.path.join(state, "tm_auction_discord.json"))
s.msgs = {"21": {"id": "77", "sig": "a", "t": 2.0, "ended": None}}
s.done = {"20"}
s._save()
B._note_channel("chosen", "tm", "4242", guild="99")
B._note_channel("posted", "tm_auction", "4243", guild="99")
'''


def old_files(base):
    probe = subprocess.run(["git", "cat-file", "-e", OLD_COMMIT + "^{commit}"],
                           cwd=ROOT, capture_output=True)
    if probe.returncode != 0:
        print("FAIL: this suite needs the file-based code at %s (a shallow clone "
              "lacks it: git fetch --unshallow)" % OLD_COMMIT)
        sys.exit(1)
    code = os.path.join(base, "old")
    os.makedirs(code)
    tar = subprocess.run(["git", "archive", "--format=tar", OLD_COMMIT, "services", "tools"],
                         cwd=ROOT, capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
        tf.extractall(code)
    with open(os.path.join(base, "fixture.py"), "w", encoding="utf-8") as fh:
        fh.write(FIXTURE)
    data = os.path.join(base, "data")
    state = os.path.join(base, "state")
    os.makedirs(os.path.join(data, "resources"))
    os.makedirs(state)
    env = {k: v for k, v in os.environ.items()
           if k not in ("POL_DATABASE_URL", "POL_VALKEY_URL", "PYTHONPATH")}
    p = subprocess.run([sys.executable, os.path.join(base, "fixture.py"), code, data,
                        state, str(WINDOW), str(WEEK), ",".join(d.hex() for d in DECK),
                        json.dumps(POL_IDS), json.dumps(NAMES), HOOK],
                       cwd=base, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        print(p.stdout + p.stderr)
        print("FAIL: the old code could not write its files")
        sys.exit(1)
    shutil.rmtree(code)
    os.remove(os.path.join(base, "fixture.py"))
    with open(os.path.join(base, "member_list.hex")) as fh:
        member_list = bytes.fromhex(fh.read())
    os.remove(os.path.join(base, "member_list.hex"))
    event = os.path.join(data, "tm-event-state.json")
    champion = os.path.join(data, "tm-champion.json")
    with open(event, encoding="utf-8") as fh:
        old_event = json.load(fh)
    with open(champion, encoding="utf-8") as fh:
        old_champion = json.load(fh)
    old_state = {}
    for n in sorted(os.listdir(state)):
        with open(os.path.join(state, n), encoding="utf-8") as fh:
            old_state[n[:-len(".json")]] = json.load(fh)
    # what no version could map, beside what the old code wrote
    d = dict(old_event)
    d.update({"later": {"9": {"steps": 1}}, "1788000000": {"9": [1, 2]}})
    with open(event, "w", encoding="utf-8") as fh:
        json.dump(d, fh)
    with open(os.path.join(state, "README.txt"), "w") as fh:
        fh.write("not state")
    with open(os.path.join(state, "jan_discord.json"), "w") as fh:
        fh.write("not json")
    return (event, champion, state), (old_event, old_champion, old_state), member_list


def digest(root):
    out = {}
    for d, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = (hashlib.sha256(fh.read()).hexdigest(),
                                                 os.stat(p).st_mtime_ns)
    return out


def run(*args):
    p = subprocess.run([sys.executable, os.path.join(SERVICES, "tmstore.py"), "import"]
                       + list(args), cwd=SERVICES, env=dict(os.environ),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, p.stdout + p.stderr


def fingerprint(db):
    out = {}
    for t in ("tm_event_standing", "tm_champion", "tm_board_state"):
        if db.query_one("SELECT to_regclass(%s) IS NOT NULL AS ok", (t,))["ok"]:
            out[t] = sorted(repr(sorted((k, str(v)) for k, v in r.items()))
                            for r in db.query("SELECT * FROM %s" % t))
    return out


def main():
    if tmpg.fresh_database() is None:
        return tmpg.skip_or_fail("tm_import")
    base = tempfile.mkdtemp(prefix="tm-import-")
    try:
        _main(base)
    finally:
        try:
            from polcore import db
            db.close()
        except Exception:                                    # noqa: BLE001
            pass
        shutil.rmtree(base, ignore_errors=True)
    print()
    print("FAIL: %d check(s)" % len(FAILS) if FAILS else "ALL PASS")
    return 1 if FAILS else 0


def _without_at(board):
    return {m: {k: v for k, v in row.items() if k != "at"} for m, row in board.items()}


def _main(base):
    os.environ.pop("POL_VALKEY_URL", None)
    os.environ.pop("POL_TM_CHAMPION_NAME", None)
    os.environ["POL_DATA_DIR"] = os.path.join(base, "newdata")
    os.environ["POL_RESOURCE_DIR"] = os.path.join(base, "newdata", "resources")
    from polcore import db
    (event, champion, state), (old_event, old_champion, old_state), member_list = \
        old_files(base)
    before = digest(base)
    board = old_event[str(WINDOW)]

    print("the old code's files")
    check("one window, two players, the deck and the paid mark",
          list(old_event) == [str(WINDOW)] and sorted(board) == ["7", "8"]
          and len(board["7"]["deck"]) == 5 and board["8"].get("paid") is True, old_event)
    check("the champion of week %d" % WEEK, old_champion
          == {"member_id": "7", "name": "Lex of the Lindb", "week": WEEK}, old_champion)
    check("three board state files", sorted(old_state)
          == ["discord_channels", "tm_auction_discord", "tm_discord"], sorted(old_state))

    print("--dry-run: a report and nothing written, not even the tables")
    code, out = run("event_state", event, "--dry-run")
    check("exit 0", code == 0, out)
    check("says so", "Dry run: nothing was written." in out, out)
    check("plans the two players", "2 to insert" in out, out)
    check("names the table as not created yet", "(not created yet)" in out, out)
    check("no table was created", fingerprint(db) == {}, fingerprint(db))
    for args in (("champion", champion), ("board_state", state)):
        code, out = run(*(args + ("--dry-run",)))
        check("%s --dry-run: exit 0, nothing written" % args[0],
              code == 0 and "Dry run" in out and fingerprint(db) == {}, out)

    print("import event_state")
    code, out = run("event_state", event)
    check("exit 0", code == 0 and "Done: 2 row(s) written." in out, out)
    rows = db.query("SELECT event_window, member, data FROM tm_event_standing"
                    " ORDER BY member")
    check("a row per player, keyed by the window and the member id as text",
          [(r["event_window"], r["member"]) for r in rows]
          == [(WINDOW, "7"), (WINDOW, "8")], rows)
    check("each row is the player's row as the file held it",
          {r["member"]: r["data"] for r in rows} == board, rows)
    check("a window that is not a number, and a row that is not an object, are skipped",
          "skipped window 'later'" in out
          and "skipped window 1788000000, member '9'" in out, out)
    import tmeventstate
    check("tmeventstate.standings() reads them", tmeventstate.standings(WINDOW) == board)
    check("tmeventstate.deck() returns the picked cards", tmeventstate.deck(WINDOW, 7) == DECK,
          tmeventstate.deck(WINDOW, 7))
    blob = tmeventstate.member_list_blob(WINDOW, lambda m: POL_IDS.get(str(m)),
                                         lambda m: NAMES.get(str(m)))
    check("the member list is byte for byte the old code's", blob == member_list)

    print("import champion")
    code, out = run("champion", champion)
    check("exit 0", code == 0 and "Done: 1 row(s) written." in out, out)
    row = db.query_one("SELECT week, member_id, name, named_at FROM tm_champion")
    check("the row of the week it names", row and (row["week"], row["member_id"], row["name"])
          == (WEEK, "7", "Lex of the Lindb"), row)
    check("named_at is the file's mtime", row and abs(row["named_at"]
                                                      - os.path.getmtime(champion)) < 1e-3, row)
    import tetramaster
    shop = tetramaster.shopdoors
    shop._CHAMPION_CACHE.update(mtime=None, data=None)
    check("the card shop reads it", shop._champion() == old_champion, shop._champion())
    check("and labels the rank_1 Pack", b"/SN=Lex of the Lindb" in shop._shopinit_body(1))

    print("import board_state")
    code, out = run("board_state", state)
    check("exit 0", code == 0 and "Done: 3 row(s) written." in out, out)
    rows = {r["name"]: r["data"] for r in db.query("SELECT name, data FROM tm_board_state")}
    check("a row per state file, named as the board names it, holding the file",
          rows == old_state, sorted(rows))
    check("a file that is not board state, and one that is not JSON, are skipped",
          "skipped README.txt" in out and "skipped jan_discord.json" in out, out)
    os.environ.pop("POL_BOARDS_STATE_DIR", None)
    import polboards
    args = polboards.build_parser().parse_args(["--tm-port", "1"])
    d = polboards.Discord("tm", HOOK, polboards.state_path(args, "tm"))
    check("the board reads the imported message id", d.msg_id == "555", d.msg_id)
    s = polboards.DiscordSet("tm_auction", HOOK, polboards.state_path(args, "tm_auction"))
    check("and the imported message set", s.msgs == {"21": {"id": "77", "sig": "a", "t": 2.0,
                                                           "ended": None}}
          and s.done == {"20"}, (s.msgs, s.done))
    check("and the imported channels, per guild", polboards.bot_channels()
          == {"chosen": {"tm": {"99": "4242"}}, "posted": {"tm_auction": {"99": "4243"}}},
          polboards.bot_channels())
    code, out = run("board_state", os.path.join(state, "discord_channels.json"))
    check("one file at a time: already there", code == 0 and "Nothing to import" in out, out)

    print("a second run changes nothing")
    fp = fingerprint(db)
    for a in (("event_state", event), ("champion", champion), ("board_state", state)):
        code, out = run(*a)
        check("%s: exit 0, nothing to import" % a[0],
              code == 0 and "Nothing to import" in out, out)
        code, out = run(*(a + ("--merge",)))
        check("%s --merge: nothing either" % a[0],
              code == 0 and "Nothing to import" in out, out)
    check("every row as it was", fingerprint(db) == fp)

    print("a table that already holds rows: refused, then --merge")
    ev2 = os.path.join(base, "ev2.json")
    with open(ev2, "w") as fh:
        json.dump({str(WINDOW): {"7": {"steps": 99}, "12": {"steps": 4, "games": 1}}}, fh)
    code, out = run("event_state", ev2)
    check("refused: exit 2", code == 2, out)
    check("says why", "REFUSED: tm_event_standing" in out and "--merge" in out, out)
    check("nothing written", fingerprint(db) == fp)
    code, out = run("event_state", ev2, "--merge", "--dry-run")
    check("--merge --dry-run writes nothing", code == 0 and fingerprint(db) == fp, out)
    code, out = run("event_state", ev2, "--merge")
    check("--merge: exit 0, one row", code == 0 and "Done: 1 row(s) written." in out, out)
    st = tmeventstate.standings(WINDOW)
    check("the new player is in, the one in both keeps the table's row",
          st.get("12") == {"steps": 4, "games": 1} and st.get("7") == board["7"], st)
    check("and the report names it",
          "kept the table's row, the file's differs: (%d, '7')" % WINDOW in out, out)
    ch2 = os.path.join(base, "ch2.json")
    with open(ch2, "w") as fh:
        json.dump({"member_id": "8", "name": "Quinn", "week": WEEK}, fh)
    code, out = run("champion", ch2)
    check("another champion for the same week: nothing written, the difference named",
          code == 0 and "the file's differs: (%d,)" % WEEK in out
          and db.query_one("SELECT member_id FROM tm_champion")["member_id"] == "7", out)
    with open(ch2, "w") as fh:
        json.dump({"member_id": "8", "name": "Quinn", "week": WEEK + 5}, fh)
    code, out = run("champion", ch2)
    check("a champion of another week into a table with rows: refused",
          code == 2 and "REFUSED: tm_champion" in out, out)
    with open(ch2, "w") as fh:
        json.dump({"member_id": "8", "name": "", "week": WEEK + 5}, fh)
    code, out = run("champion", ch2)
    check("a champion with no name is skipped and named",
          code == 0 and "skipped the champion record" in out, out)

    print("the new code writes the same rows")
    games(tmeventstate)
    new = tmeventstate.standings(WINDOW_NEW)
    check("the same games in the new code: the same rows but for the time",
          _without_at(new) == _without_at(board), (new, board))
    check("and the time is a number in both", all(isinstance(r.get("at"), float)
                                                  for b in (new, board) for r in b.values()))
    import importlib.util
    import tmprize
    spec = importlib.util.spec_from_file_location("tmrank_tool", os.path.join(HERE, "tmrank.py"))
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    real_week = tmprize.week_id
    try:
        tmprize.week_id = lambda *a, **k: WEEK_NEW
        tool._write_champion([{"member_id": "7", "name": "  Lex of the Lindblum Guard "}],
                             {"7": {1: 1}})
    finally:
        tmprize.week_id = real_week
    got = {r["week"]: r for r in db.query("SELECT * FROM tm_champion")}
    check("the same champion named by the new publish: the same row but for the week",
          WEEK_NEW in got and {k: v for k, v in got[WEEK_NEW].items()
                               if k not in ("week", "named_at")}
          == {k: v for k, v in got[WEEK].items() if k not in ("week", "named_at")}
          and type(got[WEEK_NEW]["named_at"]) is type(got[WEEK]["named_at"]), got)
    for name, feed, fn in (("tm", "chosen", "4242"), ("tm_auction", "posted", "4243")):
        polboards._note_channel(feed, name, fn, guild="99")
    d2 = polboards.Discord("tm_new", HOOK, "db:tm_new_discord")

    class Net:
        def __call__(self, req, timeout=None):
            class R:
                status = 200

                def read(self, *a):
                    return b'{"id": "555"}'

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False
            return R()
    d2.open = Net()
    d2.tick("s1", lambda: ({"content": "board"}, []), now=1000.0)
    rows = {r["name"]: r["data"] for r in db.query("SELECT name, data FROM tm_board_state")}
    check("the same board post from the new code: the same document",
          rows.get("tm_new_discord") == old_state["tm_discord"],
          (rows.get("tm_new_discord"), old_state["tm_discord"]))
    check("the same channels noted again by the new code change nothing",
          rows.get("discord_channels") == old_state["discord_channels"],
          rows.get("discord_channels"))

    print("bad sources")
    code, out = run("event_state", os.path.join(base, "missing.json"))
    check("a missing file: exit 1", code == 1 and "cannot read" in out, out)
    code, out = run("board_state", os.path.join(base, "missing"))
    check("a missing directory: exit 1", code == 1 and "does not exist" in out, out)
    lst = os.path.join(base, "list.json")
    with open(lst, "w") as fh:
        fh.write("[1]")
    code, out = run("champion", lst)
    check("a file that is not an object: exit 1", code == 1, out)
    bad = os.path.join(base, "bad.json")
    with open(bad, "w") as fh:
        fh.write("{not json")
    code, out = run("event_state", bad)
    check("a file that is not JSON: exit 1", code == 1 and "is not JSON" in out, out)
    for p in (ev2, ch2, lst, bad):
        os.remove(p)
    shutil.rmtree(os.path.join(base, "newdata"), ignore_errors=True)
    check("the sources are byte for byte what they were", digest(base) == before,
          sorted(set(digest(base).items()) ^ set(before.items())))


def games(tmeventstate):
    """The fixture's games, played by the new code in WINDOW_NEW."""
    tmeventstate.record_game(WINDOW_NEW, [(7, "perfect"), (8, "lose")],
                             active_missions=[(1, 1), (4, 1)])
    tmeventstate.record_game(WINDOW_NEW, [(8, "win"), (7, "tie")],
                             active_missions=[(1, 1), (4, 1)], extras={8: {"combos": 2}})
    tmeventstate.set_deck(WINDOW_NEW, 7, DECK)
    tmeventstate.mark_paid(WINDOW_NEW, 8)


if __name__ == "__main__":
    sys.exit(main())
