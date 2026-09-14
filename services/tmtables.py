#!/usr/bin/env python
"""tmtables -- the table-truth report.

For every room the shared roster knows, print: the published table rows
DECODED (state, id, cap, seat count, block count, occupancy id), the seat
list, the confirm flags -- and an invariant verdict per table from
`tetramaster.audit_room_tables`, which is the ONE definition of a violation
(the checker the Phase 2 reconcile will heal from).

WARNING: RUN THIS INSIDE THE CONTAINER THE CLIENTS TALK TO. `tools/` is not mounted
(the compose mount is `services:/app`), which is why this lives in `services/`:

    docker exec <authsess-container> python /app/tmtables.py
    docker exec <authsess-container> python /app/tmtables.py --json

Three-outcome output, always: per-room CLEAN / N VIOLATION(S), and a final
summary line -- never silence. Exit codes: 0 clean, 1 violations found,
2 the audit itself failed.

WARNING: Standalone limits (this is a fresh process, not the serving band): live
sessions are invisible, so presence-dependent checks degrade to `info` --
see `audit_room_tables`' docstring. The seat list and rows are the SHARED
file's view (`tm-roster.json`), which is exactly what both containers serve
from; what a band's in-process `_SEATED` privately believes is only auditable
from inside that band.
"""
import argparse
import json
import sys

import tmroom
import tetramaster


def room_channels():
    """Every room any shared store mentions, as `#TM0RNNN` names."""
    keys = set()
    for getter in (tmroom._live_tables, tmroom._live_seats,
                   tmroom._live_confirmed):
        try:
            keys.update((getter() or {}).keys())
        except Exception:
            pass
    try:
        for vals in (tmroom._live_records() or {}).values():
            keys.add(tmroom._room_key(vals))
    except Exception:
        pass
    chans = set()
    for k in keys:
        try:
            rid = int(k, 16)
        except (TypeError, ValueError):
            continue
        if rid:
            chans.add("#TM0R%03d" % (rid & 0xFFFFFFFF))
    return sorted(chans)


def room_report(chan):
    """One room's decoded truth + verdicts, as a dict."""
    seats = tmroom.seats(chan)
    rows = []
    for name, row in tmroom.tables(chan):
        blk = row[6] or ""
        cnt = occ = None
        try:
            if len(blk) >= 30:
                cnt = tmroom.unletters(blk[4:6])
                occ = tmroom.unletters(blk[14:30])
        except Exception:
            pass
        try:
            idx = int(name[5:]) if name.startswith("#TM0T") else None
        except ValueError:
            idx = None
        authored = tmroom.fixture_table(idx) if idx is not None else None
        rows.append({
            "name": name,
            "index": idx,
            "id": row[2],
            "state": tmroom._as_int(row[3]),
            "authored_state": (tmroom._as_int(authored[3]) if authored
                               else tmroom.EMPTY_TABLE_STATE),
            "cap": tmroom._as_int(row[4]),
            "row_seated": tmroom._as_int(row[5]),
            "block_count": cnt,
            "occupancy_id": ("0x%X" % occ) if occ else occ,
            "confirmed": bool(tmroom.table_confirmed(chan, idx))
                         if idx is not None else False,
            "seats": [{"member": m, "ident": "0x%X" % i if i else 0}
                      for m, i in (seats.get(idx) or [])] if idx is not None
                     else [],
        })
    return {
        "room": chan,
        "sequence": tmroom.sequence(chan),
        "members": [m for m, _v in tmroom.members(chan)],
        "tables": rows,
        "seats_without_rows": sorted(
            i for i, s in seats.items()
            if s and ("#TM0T%03d" % i) not in dict(tmroom.tables(chan))),
        "violations": tetramaster.audit_room_tables(chan),
    }


def print_report(rep):
    bad = [v for v in rep["violations"] if v["severity"] == "bad"]
    infos = [v for v in rep["violations"] if v["severity"] != "bad"]
    print("%s  seq %s  member record(s): %s"
          % (rep["room"], rep["sequence"],
             ", ".join(str(m) for m in rep["members"]) or "none"))
    if not rep["tables"] and not rep["seats_without_rows"]:
        print("  no live table rows and no seats -- room serves the authored "
              "fixture untouched")
    for t in rep["tables"]:
        print("  %-9s id=%s state=%s(authored %s) cap=%s row-seated=%s "
              "block-count=%s occ-id=%s confirmed=%s seats=%s"
              % (t["name"], t["id"], t["state"], t["authored_state"],
                 t["cap"], t["row_seated"], t["block_count"],
                 t["occupancy_id"], "yes" if t["confirmed"] else "no",
                 t["seats"] or "[]"))
    for i in rep["seats_without_rows"]:
        print("  table %d: seats held with NO published row" % i)
    for v in bad:
        print("  WARNING: VIOLATION[%s] table %s: %s"
              % (v["kind"], v["table"], v["detail"]))
    for v in infos:
        print("  WARNING: info[%s] table %s: %s" % (v["kind"], v["table"], v["detail"]))
    if not bad:
        print("  -> CLEAN (%d advisory note(s))" % len(infos)
              if infos else "  -> CLEAN")
    else:
        print("  -> %d VIOLATION(S)" % len(bad))
    return len(bad)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true",
                    help="machine-readable: one JSON document with every room")
    ap.add_argument("room", nargs="*",
                    help="specific room(s), e.g. '#TM0R001' (default: all)")
    args = ap.parse_args(argv)
    try:
        chans = args.room or room_channels()
        reports = [room_report(c) for c in chans]
    except Exception as exc:
        print("AUDIT ERROR: %r" % (exc,))
        return 2
    if args.json:
        print(json.dumps({"rooms": reports}, indent=2))
        bad = sum(1 for r in reports for v in r["violations"]
                  if v["severity"] == "bad")
    else:
        if not reports:
            print("no rooms in the shared roster -- nothing to audit "
                  "(that is a result, not an error)")
        bad = sum(print_report(r) for r in reports)
        print("SUMMARY: %d room(s), %s"
              % (len(reports),
                 "ALL CLEAN" if not bad else "%d VIOLATION(S)" % bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
