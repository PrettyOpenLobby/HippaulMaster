"""THE SERVED EVENT DATA LIST MUST NAME A CHOCOBO TRACK THE CLIENT HAS.

2026-10-03, first full cup: every PC on the tournament screen died (AV in
TM.dll RVA 0x72FC6) the moment the first game paid out steps. b/g/TM0EventDataList
said count 1 / +0x20 1, and the scene's init only fills its track pointer for
count 2 (+0x20 4..8), 3 (1..5) or 4 (1..4); any other count leaves it holding
heap garbage. See TRACK_LAYOUTS in tmevent.py.

    python tools/tm_event_track_test.py
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, os.pardir, "services"))

import tmevent                                                     # noqa: E402
import tmfixtures                                                  # noqa: E402

FAILS = []


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def track(blob):
    count = struct.unpack_from("<i", blob, tmevent.DATA_COUNT_OFF)[0]
    sub = struct.unpack_from("<i", blob, tmevent.DATA_REC_OFF + 0x20)[0]
    return count, sub


def main():
    blob = tmfixtures.event_blob("b/g/TM0EventDataList")
    count, sub = track(blob)
    check(sub in tmevent.TRACK_LAYOUTS.get(count, ()),
          "served data list names a real track", f"count={count} +0x20={sub}")
    check(len(blob) == tmevent.DATA_SIZE, "data list is the client's 0x1308 bytes", str(len(blob)))

    # The twin: the 10-03 file must be refused, or this check cannot fail.
    for bad in ([("EVENT", 1)], [("A", 1), ("B", 1)], [("A", 4)] * 5, []):
        try:
            tmevent.build_data(bad)
            check(False, f"build_data refuses {bad!r}")
        except ValueError:
            check(True, f"build_data refuses {bad[:2]!r}{'...' if len(bad) > 2 else ''}")
    for good in ([("A", 8), ("B", 8)], [("A", 1)] * 3, [("A", 4)] * 4):
        try:
            tmevent.build_data(good)
            check(True, f"build_data accepts {len(good)} x +0x20={good[0][1]}")
        except ValueError as exc:
            check(False, f"build_data accepts {good!r}", str(exc))

    print("FAIL" if FAILS else "OK", f"({len(FAILS)} failing)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
