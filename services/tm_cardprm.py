"""Tetra Master card parameters, read from YOUR client's `data/CardPrm.BIN`
(decoded into services/tmdata/ by tools/tmdata_build.py; nothing of the
table ships with this repository).

WHY THIS EXISTS. `@Card=` hands the client a 16-byte card record per acquired
card, and TM.dll `0xB9410` renders it by combining THAT record with the CardPrm
row for the same card id -- roughly `((2*v - base) / base + K1) * K2` per stat.
So the record is the card's ROLLED stats and CardPrm is the baseline they are
measured against; serving placeholder 1s against a baseline of 6..90 produces a
degenerate card, which on screen is a flat "Acquired" flash with no grade
animation.

The table: a 16-byte header (`u32 string-pool offset, u32 record count, u32
platform, u32 hash`), then 12-byte records indexed by card id:

    [0] attack      <- card record /D= idx 1   ([rec+0x00])
    [1] type 0..3   <- card record /D= idx 2   ([rec+0x01], the `< 4` gate)
    [2] phys def    <- card record /D= idx 3   ([rec+0x02])
    [3] magic def   <- card record /D= idx 4   ([rec+0x03])
    [4] [5] [6] [7] the rest of the row: [5] is the LEVEL (the sell-price
                    ladder index), [6] the group, [7] the category
    [8..11]         a string slot naming the card in the pool

`row(id)` returns the first 8 bytes. Sanity check against the game: Goblin
6/0/7/5, Garnet 60/1/35/75, Al Bhed Primer 30/3/90/90 -- weak monsters low,
characters high.

The names: `card_names_en.txt` (one per line, the ordinal IS the card id),
which tmdata_build.py writes from an English install's own pool; without it
the table's pool is used, whatever language it is in.
"""
import os
import struct

HERE = os.path.dirname(os.path.abspath(__file__))
TABLE = os.path.join(HERE, "tmdata", "CardPrm.BIN")
NAMES_FILE = os.path.join(HERE, "tmdata", "card_names_en.txt")
HEADER, REC = 16, 12

#: The card-number ceiling the client enforces (a record index at or above
#: this is discarded at 0xB90F6), whether or not the table has been built.
CARD_COUNT = 250

_ROWS = b""
NAMES = []
_WARNED = [False]


def _load():
    global _ROWS, NAMES
    try:
        with open(TABLE, "rb") as f:
            blob = f.read()
        pool, count = struct.unpack_from("<2I", blob, 0)
        n = min(count, CARD_COUNT)
        _ROWS = b"".join(blob[HEADER + i * REC:HEADER + i * REC + 8] for i in range(n))
        names = []
        try:
            with open(NAMES_FILE, encoding="utf-8") as f:
                names = [ln.rstrip("\n") for ln in f]
        except OSError:
            if 0 < pool < len(blob):
                names = [s.decode("cp932", "replace") for s in blob[pool:].split(b"\x00")]
        NAMES = names[:CARD_COUNT]
    except OSError:
        if not _WARNED[0]:
            _WARNED[0] = True
            print("tm_cardprm: %s is missing -- run tools/tmdata_build.py --client "
                  "<your Tetra Master install>; cards have no baseline stats until "
                  "then" % TABLE, flush=True)


_load()


def loaded():
    return bool(_ROWS)


def row(card_id):
    """The 8 CardPrm bytes for `card_id`, or None if out of range or unbuilt."""
    if not 0 <= card_id < len(_ROWS) // 8:
        return None
    return _ROWS[card_id * 8:card_id * 8 + 8]


def name(card_id):
    return NAMES[card_id] if 0 <= card_id < len(NAMES) else "?"
