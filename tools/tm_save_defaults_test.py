#!/usr/bin/env python3
"""A Tetra Master save the player has never stored must still carry the DEFAULTS.

THE BUG THIS PINS, reported live 2026-08-25: "Tetra Master is giving players the
audio off by default instead of the game's own default settings".

`U/g/TM0DataFile` has no shipped template and no `RESOURCE_INIT` entry, so a
member with no stored save was answered with 12332 zeros -- and the client reads
its settings straight out of that header (`apply_options_from_save`, TM.dll rva
0x14D9F0). Every setting whose default is not zero therefore read as the first
entry of its list: **SE Volume and BGM Volume Off**, chat window 0 lines, a
table-settings preset of `au=0/al=0` that refuses every player, and 0 gold.

`tmsave.DEFAULT_HEADER` has held the right values since 2026-08-20 -- the twelve
the client itself sent under its Options `Default` button -- but they were only
applied on the WRITE side. That is too late, and the zeros LAUNDER themselves:
the client echoes its whole option set back in `@Opt=`, so the zeros it just read
from us are stored as though the player had chosen them.

    python tools/tm_save_defaults_test.py        # exit 0 on success
"""
import os
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tm_testenv                                                  # noqa: E402
tm_testenv.setup()          # this tree's services + the OpenLobby core

os.environ.setdefault("POL_LOG_DIR", tempfile.mkdtemp(prefix="tmdef-log-"))
_RES = tempfile.mkdtemp(prefix="tmdef-res-")
os.environ["POL_RESOURCE_DIR"] = _RES
os.environ["POL_DATA_DIR"] = _RES
# One process, one session: the fallback in `_session_get` is only defensible
# with a single live session, and this suite deliberately relies on it.
os.environ["POL_SESSION_SHARE"] = "0"

import responders as R                                          # noqa: E402
import tmsave                                                   # noqa: E402
import tetramaster                                              # noqa: E402

R.RESOURCE_DIR = _RES
PATH = "U/g/TM0DataFile"
LEN = 12328 + 4
MEMBER = "9901"

fails = []
checks = 0


def check(what, got, want):
    global checks
    checks += 1
    if got == want:
        print("  ok    %s" % what)
        return
    fails.append(what)
    print("  FAIL  %s\n          got  %r\n          want %r" % (what, got, want))


print("a member who has never stored a save:")
R._session_put("tmdef-sid", member_id=MEMBER, peer_ip="127.0.0.1")
check("the session binds", R._session_get("member_id"), MEMBER)

blob = R._resource_blob(PATH, LEN)
check("the reply is the declared length", len(blob), LEN)

# THE REPORTED SYMPTOM. 1 is "Low" and 0 is "Off" -- measured, and the reason
# this went unnoticed for so long is that the two members who HAD been through
# the write path carried 1/1, so the volumes were quietly wrong rather than
# obviously absent.
check("SE Volume is Medium, not Off", blob[0x0B3], 2)
check("BGM Volume is High, not Off", blob[0x0B4], 3)
check("the chat window is 5 lines, not 0", blob[0x102], 5)
check("chat transparency is 60%, not 0", blob[0x103], 0x3C)

# The table-settings preset is the SAME header one block over (`0x14DAE3`), and
# an all-zero one is a rank band of 0..0: a table nobody can join.
for off, want in sorted(tmsave.default_header_writes().items()):
    width = want[0]
    got = (blob[off] if width == 1
           else struct.unpack_from("<H" if width == 2 else "<I", blob, off)[0])
    check("factory header +0x%03X" % off, got, want[1])

# Money comes from the SAVE, not from SHOPINIT's `/M=` (measured 2026-08-18), so
# whatever this field says IS the gold on the new player's screen.
#
# WARNING: THIS CHECK USED TO BE `> 0`, AND THAT IS THE BUG IT WAS MEANT TO CATCH,
# INVERTED. It passed only because `POL_TM_START_MONEY` defaulted to an
# unmeasured 10000, so it was asserting that a brand new player is HANDED
# money -- which two of them were, on 2026-08-25, one of them right after
# quitting a COM match. What this path owes the player is their OWN balance,
# whatever it is; a fresh member's is 0, and the FREE Pauper's Pack (PackPrm
# No=0, price 0) is what starts them off. Assert the value, not its sign.
check("the opening balance served IS the member's balance",
      struct.unpack_from("<I", blob, tmsave.MONEY_OFF)[0],
      tetramaster.money_of(MEMBER))
check("a brand new member is handed NOTHING",
      tetramaster.money_of(MEMBER), 0)

print("a STORED save is never healed on the way out:")
# Zero is a LEGAL value here (Off, muted, "Don't skip"). Overwriting a stored
# zero would destroy a real choice -- the trap `iniheal` documents for the shim's
# ini, one game over. Repair is `tools/tmsave.py --heal`, per member, explicitly.
stored = os.path.join(_RES, "%s.%s.bin" % (MEMBER, "U_g_TM0DataFile"))
with open(stored, "wb") as f:
    f.write(b"\x00" * LEN)
kept = R._resource_blob(PATH, LEN)
check("a stored all-zero save is served verbatim", any(kept), False)

print("the switch:")
os.environ["POL_TM_SAVE_DEFAULTS"] = "0"
os.remove(stored)
off_blob = R._resource_blob(PATH, LEN)
check("POL_TM_SAVE_DEFAULTS=0 restores the old zeros", off_blob[0x0B3], 0)
os.environ["POL_TM_SAVE_DEFAULTS"] = "1"

print("\n%s -- %d check(s), %d failure(s)"
      % ("FAIL" if fails else "PASS", checks, len(fails)))
sys.exit(1 if fails else 0)
