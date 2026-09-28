#!/usr/bin/env python3
"""tm_faces_test.py -- other players' PlayOnline portraits BY WHO THEY ARE, on
the public TM board (services/boardtm.py), for a phone client:

    GET /face.png?nn=<16-hex TM POL-ID>   GET /face.png?name=<handle>
    GET /faces.json?nn=a,b,c              GET /faces.json?name=a,b

    python tools/tm_faces_test.py

Pins, against a temporary accounts.db and room roster: a POL-ID (tmroom's
published `polids`) and a name (the TM roster's, then a handle's, either case)
reach the member's PRIMARY handle's field 19; unknown is 0 / 404; bad input is
a 400 (hex length, printable ASCII <= 15, one kind, <= 50 keys); a quote in a
name is data, not SQL; the database is opened read-only and left
byte-identical; the lookups are CORS-open with a short cache; and `?id=`
answers exactly what origin/main's boardtm answered.

The portraits are the ones YOU baked (tools/tm_boardart_bake.py --faces); with
no baked sheets in services/boardart/faces/ the test SKIPs.
"""
import hashlib
import importlib.util
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.join(HERE, os.pardir, "services")
sys.path.insert(0, SERVICES)

CHECKS = []
ELENA = "AB12CE0000000003"          # member 3: primary handle Elena, face 2439
LEX = "AB12CD0000000001"            # member 1: roster name Lex, no portrait
GHOST = "0123456789ABCDEF"          # nobody


def check(label, cond, detail=""):
    CHECKS.append(label)
    print("  %-72s %s%s" % (label, "PASS" if cond else "FAIL",
                            ("  " + str(detail)) if detail and not cond else ""), flush=True)
    if not cond:
        raise AssertionError(label)


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def setup(tmp):
    os.environ["POL_DATA_DIR"] = tmp
    os.environ["POL_RESOURCE_DIR"] = os.path.join(tmp, "resources")
    os.makedirs(os.environ["POL_RESOURCE_DIR"])
    os.environ["POL_TM_ROSTER_KEY"] = "tm:test:%s:roster" % os.path.basename(tmp)
    db = os.path.join(tmp, "accounts.db")
    os.environ["POL_ACCOUNTS_DB"] = db
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE handle (id INTEGER PRIMARY KEY, member_id INTEGER, "
              "handle_name TEXT, is_primary INTEGER)")
    c.executemany("INSERT INTO handle (member_id, handle_name, is_primary) VALUES (?,?,?)",
                  [(3, "Elena", 1), (3, "OldMaria", 0), (1, "NotLex", 1), (5, "Zidane", 1)])
    # field 19 = z_ficon: Elena's PRIMARY handle 1 is hnf304 tile 7; her other
    # handle (2) has another portrait that must NOT win; Zidane (handle 4) 16
    c.execute("CREATE TABLE handle_profile (handle_id INTEGER, field_id INTEGER, "
              "val_int INTEGER, val_text TEXT, updated_at REAL)")
    c.executemany("INSERT INTO handle_profile (handle_id, field_id, val_int) VALUES (?,?,?)",
                  [(1, 19, 2439), (2, 19, 16), (1, 5, 77), (4, 19, 16)])
    c.commit()
    c.close()
    import tmstore
    # tmroom stores POL-IDs as the client's own 16 upper-case hex digits
    tmstore.Snapshot(tmstore.roster_key()).write(
        {"names": {"1": "Lex", "3": "Elena"},
         "polids": {"3": ELENA, "1": LEX, "9": "garbage"}})
    return db


def origin_boardtm(tmp):
    """origin/main's boardtm, loaded side by side, for the ?id= comparison."""
    r = subprocess.run(["git", "-C", SERVICES, "show", "origin/main:services/boardtm.py"],
                       capture_output=True)
    if r.returncode != 0:
        return None
    src = r.stdout
    path = os.path.join(tmp, "boardtm_origin.py")
    with open(path, "wb") as fh:
        fh.write(src)
    spec = importlib.util.spec_from_file_location("boardtm_origin", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.FACES_DIR = os.path.join(SERVICES, "boardart", "faces")
    return mod


def main():
    faces = os.path.join(SERVICES, "boardart", "faces")
    if not all(os.path.isfile(os.path.join(faces, "hnf%03d.png" % n)) for n in (2, 304)):
        print("[SKIP] tm_faces_test: no baked portrait sheets in services/boardart/faces "
              "(tools/tm_boardart_bake.py --faces)")
        return
    tmp = tempfile.mkdtemp(prefix="tmfaces-")
    db = setup(tmp)
    before = sha(db)
    import boardtm
    import polboards
    from PIL import Image
    args = None
    R = boardtm.route

    print("imports")
    check("the board still pulls in no accounts module, tmroom or responders",
          not {"responders", "tetramaster", "accounts", "tmroom"} & set(sys.modules))

    print("by POL-ID")
    code, png, ctype, cache, hdrs = R("/face.png", {"nn": [ELENA]}, args)
    check("/face.png?nn= Elena's POL-ID is her primary handle's portrait",
          code == 200 and ctype == "image/png" and png == boardtm.face_png(2439)
          and Image.open(io.BytesIO(png)).size == (64, 96))
    check("...CORS-open, cached five minutes",
          hdrs == {"Access-Control-Allow-Origin": "*"} and cache == "public, max-age=300")
    check("...lower case and a 0x prefix are the same id",
          R("/face.png", {"nn": [ELENA.lower()]}, args)[1] == png
          and R("/face.png", {"nn": ["0x" + ELENA]}, args)[1] == png)
    r = R("/face.png", {"nn": [LEX]}, args)
    check("a known player with no portrait is a 404 (CORS-open)",
          r[0] == 404 and r[4] == boardtm.FACE_CORS)
    check("an unknown POL-ID is a 404", R("/face.png", {"nn": [GHOST]}, args)[0] == 404)

    print("by name")
    code, png2 = R("/face.png", {"name": ["elena"]}, args)[:2]
    check("/face.png?name=elena (any case) is Elena's portrait", code == 200 and png2 == png)
    check("...a member's NON-primary handle still draws the PRIMARY one's portrait",
          R("/face.png", {"name": ["OLDMARIA"]}, args)[1] == png)
    check("...a handle only accounts.db knows (not in the TM roster) resolves",
          R("/face.png", {"name": ["zidane"]}, args)[1] == boardtm.face_png(16))
    check("an unknown name, and a roster name with no portrait, are 404s",
          R("/face.png", {"name": ["Nobody"]}, args)[0] == 404
          and R("/face.png", {"name": ["Lex"]}, args)[0] == 404)
    inj = "x' OR '1'='1"
    check("a quote in a name is DATA: no row, no portrait",
          R("/face.png", {"name": [inj]}, args)[0] == 404
          and json.loads(R("/faces.json", {"name": [inj]}, args)[1]) == {inj: 0})

    print("the batch")
    code, body, ctype, cache, hdrs = R(
        "/faces.json", {"nn": ["%s,%s,%s" % (ELENA, LEX, GHOST.lower())]}, args)
    check("/faces.json?nn=a,b,c -> {POL-ID: face id, 0 = none}",
          code == 200 and ctype.startswith("application/json")
          and json.loads(body) == {ELENA: 2439, LEX: 0, GHOST: 0}, body)
    check("...CORS-open, cached five minutes",
          hdrs == boardtm.FACE_CORS and cache == "public, max-age=300")
    body = R("/faces.json", {"name": ["Elena,zidane,Nobody"]}, args)[1]
    check("/faces.json?name=a,b,c -> {name: face id}",
          json.loads(body) == {"Elena": 2439, "zidane": 16, "Nobody": 0}, body)
    body = R("/faces.json", {"nn": [ELENA, LEX]}, args)[1]
    check("...repeated nn= parameters work too", json.loads(body) == {ELENA: 2439, LEX: 0})
    fifty = ",".join("%016X" % (i + 1) for i in range(boardtm.FACE_BATCH_MAX))
    check("50 keys are served", R("/faces.json", {"nn": [fifty]}, args)[0] == 200)

    print("bad input is refused")
    bad = [("/face.png", {"nn": [ELENA[:15]]}), ("/face.png", {"nn": [ELENA + "0"]}),
           ("/face.png", {"nn": ["G" * 16]}), ("/face.png", {"nn": ["0" * 16]}),
           ("/face.png", {"nn": [" " + ELENA[1:]]}),
           ("/face.png", {"name": ["A" * 16]}), ("/face.png", {"name": ["Mariä"]}),
           ("/face.png", {"name": ["Mar\nia"]}), ("/face.png", {"name": ["   "]}),
           ("/face.png", {"name": [""]}),
           ("/face.png", {"nn": [ELENA], "name": ["Elena"]}),
           ("/face.png", {"nn": [ELENA, LEX]}),
           ("/faces.json", {}), ("/faces.json", {"nn": [""]}),
           ("/faces.json", {"nn": [fifty + ",%016X" % 99]}),
           ("/faces.json", {"nn": [ELENA + ",nope"]}),
           ("/faces.json", {"nn": [ELENA], "name": ["Elena"]})]
    got = [(p, q, R(p, q, args)[0]) for p, q in bad]
    check("hex != 16 digits, all-zero, non-ASCII / >15 / blank names, two kinds,"
          " >50 keys: all 400", all(c == 400 for _p, _q, c in got),
          [g for g in got if g[2] != 400])

    print("?id= is unchanged")
    old = origin_boardtm(tmp)
    for q in [] if old is None else ({"id": ["2439"]}, {"id": ["16"]}, {"id": ["0"]}, {"id": ["65535"]},
              {"id": ["-1"]}, {"id": ["x"]}, {}, {"id": ["2439"], "a": ["1"]}):
        check("/face.png %-40s = origin/main's answer" % json.dumps(q),
              R("/face.png", q, args) == old.route("/face.png", q, args))

    print("read-only")
    check("accounts.db is byte-identical and has no journal left behind",
          sha(db) == before and not os.path.exists(db + "-journal")
          and not os.path.exists(db + "-wal"))

    print("the service")
    x = socket.socket()
    x.bind(("127.0.0.1", 0))
    port = x.getsockname()[1]
    x.close()
    srv = polboards.serve(boardtm, polboards.build_parser().parse_args(
        ["--tm-port", str(port)]), port)
    base = "http://127.0.0.1:%d" % port

    def get(p):
        try:
            with urllib.request.urlopen(base + p, timeout=20) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()
    try:
        st, h, body = get("/faces.json?nn=%s,%s" % (ELENA, GHOST))
        check("over HTTP: /faces.json with Access-Control-Allow-Origin: *",
              st == 200 and h["Access-Control-Allow-Origin"] == "*"
              and h["Cache-Control"] == "public, max-age=300"
              and json.loads(body) == {ELENA: 2439, GHOST: 0})
        st, h, body = get("/face.png?name=Elena")
        check("over HTTP: /face.png?name= is the PNG, CORS-open",
              st == 200 and h["Content-Type"] == "image/png" and body == png
              and h["Access-Control-Allow-Origin"] == "*")
        st, h, _b = get("/face.png?nn=%s" % GHOST)
        check("over HTTP: an unknown POL-ID is a 404, CORS-open",
              st == 404 and h["Access-Control-Allow-Origin"] == "*")
        st, h, _b = get("/face.png?name=%27%3B%20DROP%20TABLE%20x")
        check("over HTTP: an injection attempt is a plain miss", st == 404)
        st, h, body = get("/face.png?id=2439")
        check("over HTTP: ?id= as before (day cache, no CORS header added)",
              st == 200 and body == png and h["Cache-Control"] == "public, max-age=86400"
              and h["Access-Control-Allow-Origin"] is None)
    finally:
        srv.shutdown()
    check("accounts.db is still byte-identical", sha(db) == before)
    print("[tm_faces_test] OK -- %d checks" % len(CHECKS))


if __name__ == "__main__":
    main()
