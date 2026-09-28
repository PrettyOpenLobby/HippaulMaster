"""The service manifests the client loads first (TM0SML, TM0CVML, TM0IML, TM0CQL) and fresh blob
defaults.
"""
import os
import struct


#: Tetra Master `b/g/TM0SML` (and its alternate TM0SML2) default-data blob.
#: Parser reversed in TMaster.pex (validator at 0x00420560, accessors 0x00420a10/
#: 0x00420a30). Two gates, and all-zeros fails the first:
#:   * u32 at +0x00 = record COUNT; must be > 0 (else state 300).
#:   * 48-byte records start at +0x08; the client scans record[i] for one whose
#:     flag u32 (at buffer +0x14 + i*48) == 1 (else state 400). On a match it
#:     succeeds (state 3) and reads that record's u64 at +0x08 (0x003ab840).
#: So the minimal valid blob is: count=1, and record[0].flag=1 at +0x14. The rest
#: is zero-padded to the declared length. This is a FIRST cut -- the record body
#: (u64 at +0x08, and whatever the 48 bytes really encode: a card/rule/deck entry)
#: is still zero, which may surface a later gate; iterate against a savestate.
#: The peer every Tetra Master service entry is addressed to. It is the value we
#: already hand the client as `Shm=000000384EA5822C` in `@TeachDVAns`, the
#: subject it fetches `b/g/TM0IML` and `b/g/ZL` under, and the same id the PC
#: side's card-service entry uses (`tools/tmptl.py`'s type-`'F'` row).
_TM_SERVICE_PEER = 0x384EA5822C

#: `b/g/TM0SML` / `TM0SML2` -- the SERVICE list, read exactly like the TM0IML
#: manifest and laid out from TMaster.pex 2026-09-08 (load base 0x280000):
#:
#:     +0x00  u32   count            getter 0x00420a30 (lw [0x005c6190])
#:     +0x08  entry[16], STRIDE 48   -> 0x08 + 16*48 = 776, the exact payload
#:              +0x00  u64  id       getter 0x003ab840 (buf +0x08 + i*48)
#:              +0x0c  u32  flag     getter 0x00420a10 (buf +0x14 + i*48)
#:
#: The scan at 0x00420590 is the manifest's twin: count 0 -> state 300, no
#: flag-1 entry -> state 400, and on success it reads that entry's **u64 id**
#: (0x0042061c) and passes it to 0x003a9660 -- which builds an `sqMgJoinChannel`
#: request. So the id is a DESTINATION, and a zero there is "join nothing".
#:
#: KEY: That is why the card shop answered `0-37085` ("Timed out while connecting
#: to the server") with NOTHING on the wire: the flag was right, so the client
#: selected the entry and then tried to join channel 0. The shape was correct
#: from the start; only the address was missing.
_TM0SML_INIT = (struct.pack("<I", 1)                    # +0x00 count = 1
                + struct.pack("<I", 0)                  # +0x04 (reader ignores)
                + struct.pack("<Q", _TM_SERVICE_PEER)   # +0x08 entry[0].id
                + struct.pack("<I", 0)                  # +0x10 entry[0] pad
                + struct.pack("<I", 1))                 # +0x14 entry[0].flag = 1

#: KEY: `b/g/TM0CVML` IS THE PRIZE CENTER'S MANIFEST, AND IT IS TM0SML'S TWIN.
#: Laid out from TMaster.pex 2026-09-09, off the savestate taken when the
#: console could open every ranking screen EXCEPT this one.
#:
#: The tell was that Prize Center logged NO error and made NO request: the
#: client's own log ring in EE RAM ends on a SUCCESS (`sqMg:ReadSuccess[0]`,
#: `CVML(+$0)->$005C6CF0($188)`), and our lobby log shows why -- we answered
#: `b/g/TM0CVML` with 396B of zeros, so the count is 0 and the scene stops
#: before it ever asks us for anything. A screen that opens onto nothing is
#: this family's failure mode, not an error code.
#:
#:     0x00427fa0  lw v0,0x6cf0(at)      getter: count, buffer +0x00
#:     0x00427b2c  jal 0x00427fa0        s3 = that count
#:     0x00427b38  bne s3, zero, ...     non-zero -> run the scan
#:     0x00427b40  addiu v1, zero, 300   ZERO -> state 300, and it stops
#:     0x00427b98  addiu v1, zero, 400   no flag-1 entry -> state 400
#:
#: instruction for instruction the scan at 0x00420590 that `_TM0SML_INIT`
#: documents, which is why this shares that constant's shape exactly:
#:
#:     +0x00  u32   count
#:     +0x08  entry[8], STRIDE 48
#:              +0x00  u64  id     getter 0x003abc10 (buffer +0x08 + i*48)
#:              +0x0c  u32  flag   getter 0x00427f80 (buffer +0x14 + i*48)
#:
#: and the arithmetic closes exactly on the 392-byte payload we already declare
#: in `_FETCH_PATHLEN` (measured at TMaster.pex 0x003ab118, `a3 = 392`):
#: `0x08 + 8*48 = 392`. That is the same independent check that confirmed
#: TM0IML (0x10 + 8*48 = 400) and TM0SML (0x08 + 16*48 = 776).
#:
#: KEY: THE ID IS A DESTINATION HERE TOO. On a flag-1 hit the scan reads that
#: entry's u64 (0x003abc10) and hands it to 0x003ab9d0, which builds **`@CvReq=`**
#: -- `/NN=` `/HID=` `/Dm=` `/Vol=` `/CN=` -- and waits for **`@CvEnter=/EN=`**.
#: We have answered that pair since 2026-08-16 (`tetramaster.MSG_CVREQ`), so
#: nothing else needs building: this is the card-shop lesson one file over,
#: where `_TM0SML_INIT` had the right shape and a ZERO id and the shop died at
#: `0-37085`. Addressing it to `_TM_SERVICE_PEER` is what fixed that one.
#:
#: PS2-ONLY, so unlike `b/g/TM0RkData` there is no PC layout to keep: the PC's
#: `b/g/` set is ZL / RL / PTL / TM0RkData / TM0AucData / TM0Event{...} and
#: TM.dll carries no TM0CVML string at all -- the PC reaches `@CvReq=` without
#: any manifest, which is why the Prize Center worked there and not here.
#:
#: POL_RESOURCE_INIT_B_G_TM0CVML=<hex> overrides the blob (the generic
#: `POL_RESOURCE_INIT_<PATH>` lookup below); an empty count restores today's
#: "the screen will not open" behaviour.
_TM0CVML_INIT = (struct.pack("<I", 1)                   # +0x00 count = 1
                 + struct.pack("<I", 0)                 # +0x04 (reader ignores)
                 + struct.pack("<Q", _TM_SERVICE_PEER)  # +0x08 entry[0].id
                 + struct.pack("<I", 0)                 # +0x10 entry[0] pad
                 + struct.pack("<I", 1))                # +0x14 entry[0].flag = 1

#: The `b/g/TM0IML` MANIFEST -- laid out from TMaster.pex, 2026-09-08.
#:
#: AN ALL-ZERO MANIFEST IS FATAL, and this is the instruction that proves it
#: (module load base 0x280000; a plaintext TMaster.pex lives on
#: E:\ps2hdd\build\pol-plaintext.img, the repo has no decrypted copy):
#:
#:     0x003b7210  lui at,0x005c / lw v0,0x64a0(at)   <- getter: manifest +0x00
#:     0x00423944  jal  0x003b7210                    <- s2 = that count
#:     0x00423950  bne  s2, zero, 0x0042398c          <- non-zero: run the loop
#:     0x0042396c  addiu a3, zero, 92                 <- zero: RAISE ERROR 92
#:
#: and the error table's base is 37000, so selector 92 is the on-screen
#: **0-37092**, "Unrecoverable error occurred" -- measured live on a console
#: 2026-09-08 and matched 5/5 against the launches that fetched this file.
#: That retires the old note here ("empty manifest = nothing to chain-load"):
#: empty is not neutral, it is the wall.
#:
#: LAYOUT, and the arithmetic closes exactly on the 400-byte payload:
#:
#:     +0x00  u32   count
#:     +0x04  bytes      indexed by [0x00450d18]  (the TM0RkData chain, 0x003aac48)
#:     +0x08  halfwords  indexed by [0x00450d1c]  (       "            , 0x003aac50)
#:     +0x10  entry[8], STRIDE 48                 -> 0x10 + 8*48 = 400
#:              +0x00  u64  id      getter 0x003b7a90 (manifest +0x10 + i*48)
#:              +0x0c  u32  flag    getter 0x00423dc0 (manifest +0x1c + i*48)
#:
#: The loop at 0x00423994 walks i in 0..count-1 looking for the FIRST entry
#: whose flag == 1, stores its index and stashes its u64 id; if it runs off the
#: end without finding one it raises selector 193 instead. So a valid manifest
#: needs BOTH a non-zero count AND a flag-1 entry -- one without the other just
#: trades 0-37092 for 0-37193.
#:
#: The id is the one we already hand the client as `Shm=000000384EA5822C` in
#: `@TeachDVAns`, which is also the subject it fetches `b/g/TM0IML` and `b/g/ZL`
#: under -- so it is an id this client is known to accept, rather than a guess.
#:
#: WARNING: NOT YET PROVEN PAST THIS POINT. What consumes the stashed id (stored to
#: 0x005d15e0) is unread, so the next wall may simply be a different selector.
#: The `b/g/TM0RkData` chain (0x003aabd0, 24-byte sub-reads) hangs off a
#: DIFFERENT state machine (called only from 0x003fc0f4) and is deliberately not
#: served here; add it if a launch shows the client asking for it.
_TM0IML_INIT = (
    struct.pack("<I", 1)                    # +0x00 count = 1
    + b"\x00" * 12                          # +0x04 byte / +0x08 halfword arrays
    + struct.pack("<Q", _TM_SERVICE_PEER)    # +0x10 entry[0].id
    + struct.pack("<I", 0)                  # +0x18 entry[0] pad
    + struct.pack("<I", 1)                  # +0x1c entry[0].flag = 1  <- selected
).ljust(400, b"\x00")

#: `b/g/TM0CQL` -- the COM-battle SERVER LIST. Same family again, and the
#: arithmetic closes again: `0x08 + 32*48 = 1544`, the payload we serve.
#:
#:     +0x00  u32   count            getter 0x004231f0 (lw [0x005c6630])
#:     +0x08  entry[32], STRIDE 48
#:              +0x00  u64   id      getter 0x003ab260
#:              +0x10  char  host[32]
#:
#: KEY: **entry+0x10 IS A HOSTNAME OR IP, AS TEXT** -- not an id, which is what
#: makes this list different from TM0IML/TM0SML. Read out of the code rather
#: than guessed: the COM screen calls `sqMgOpen('TM0', entry+0x10, <u64>,
#: '46.49')` (TMaster.pex 0x0029901c, the ONLY sqMgOpen call site in the whole
#: module), which hands entry+0x10 as the first argument to polcore
#: `[0x00101210]` = `0x00135850` -> `0x00135558`, and there:
#:
#:     0x00135600  lb   v0, 0(s0)          <- the field's FIRST BYTE
#:     0x0013560c  addu v0, v0, <ctype>    <- index the ctype table
#:     0x00135614  andi a0, a0, 0x0004     <- test the DIGIT flag
#:     0x00135618  beq  a0, zero, 0x135638 <- not a digit: resolve as a HOSTNAME
#:     0x00135620  jal  0x00132b48         <- a digit: parse as a dotted IP
#:
#: So a NUL-terminated string, 32 bytes, and the client itself decides IP vs
#: name by whether it starts with a digit. On failure the session state at
#: 0x0043CB60 never reaches 1, and that is the gate the COM screen tests
#: (0x00422838 -> selector 138 -> the measured **0-37138**).
#:
#: WARNING: THE PORT IS NOT ESTABLISHED. It is not a constant on the polcore path that
#: was read, so it comes from somewhere unread. Serving our own advertised
#: address makes the console TELL US the port -- its connect attempt lands in
#: our logs -- which is a measurement rather than a guess, and is the intended
#: next step. Until something answers on that port the COM screen will still
#: fail; this only gets the attempt out of the client.
#:
#: POL_TM_COM_HOST overrides; empty (and no POL_ADVERTISE) serves zeros, i.e.
#: exactly today's behaviour.
_TM_COM_HOST = (os.environ.get("POL_TM_COM_HOST")
                or os.environ.get("POL_ADVERTISE") or "").strip()

if _TM_COM_HOST:
    _TM0CQL_INIT = (
        struct.pack("<I", 1)                            # +0x00 count = 1
        + struct.pack("<I", 0)                          # +0x04
        + struct.pack("<Q", _TM_SERVICE_PEER)           # entry[0] +0x00 id
        + b"\x00" * 8                                   # entry[0] +0x08
        + _TM_COM_HOST.encode("ascii", "replace")[:31].ljust(32, b"\x00")
    ).ljust(1544, b"\x00")                              # entry[0] +0x10 host
else:
    _TM0CQL_INIT = b""


#: Fresh (never stored) blobs that must not be all zeros. See the banners
#: above each: an empty manifest is the 0-37092 wall, not 'nothing to load'.
RESOURCE_INIT = {
    "b/g/TM0SML": _TM0SML_INIT,
    "b/g/TM0SML2": _TM0SML_INIT,
    "b/g/TM0IML": _TM0IML_INIT,
    "b/g/TM0CVML": _TM0CVML_INIT,
}
if _TM0CQL_INIT:
    RESOURCE_INIT["b/g/TM0CQL"] = _TM0CQL_INIT
