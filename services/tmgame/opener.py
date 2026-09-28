"""The opener: @TeachDV default-data versions, the teach control file, @Init's answer and the /Shm=
peer id.
"""
import os
import time


def _init_enabled():
    return os.environ.get("POL_TM_INIT", "1") == "1"


def _init_ans():
    try:
        return int(os.environ.get("POL_TM_INIT_ANS", "1"), 0)
    except ValueError:
        return 1

#: `b/g/TM0IML` IS THE GATE, and its layout is measured, not guessed
#: (TMaster.pex, 2026-08-14). The blob is read to 0x005c64a0, 400 bytes
#: (0x003aaf18 passes a3 = 400), by the read at 0x003aaec0.
#:
#:   +0x00  u32 count      0x00423950 `bne s2, zero` -- ZERO takes the dead
#:                         branch, which is what we have been serving and what
#:                         ends in the 0-37092 timeout. 0x0042398c then loops
#:                         i = 0..count-1.
#:   +0x04  u8  array      chain key, indexed by cursor [0x00450d08]/[0x00450d18]
#:   +0x08  u16 array      chain key, indexed by cursor [0x00450d0c]/[0x00450d1c]
#:   +0x10  entry[i]       48 bytes each. TWO accessors pin the base, and the
#:                         second corrects the first reading of it:
#:                           0x00423dc0 -> u32 [0x005c64bc + i*48]  = entry+0x0c
#:                                         (the FLAG the selection loop matches)
#:                           0x003b7a90 -> u64 [0x005c64b0 + i*48]  = entry+0x00
#:                                         (read on a match, at 0x004239bc)
#:                         0x005c64b0 - 0x005c64a0 = 0x10, so the entry BASE is
#:                         +0x10 and the flag sits at entry+0x0c (= manifest
#:                         +0x1c for entry 0). An earlier note here said "entries
#:                         at +0x1c"; that was the flag offset, not the base.
#:
#: This is the SAME record shape as `_TM0SML_INIT` in responders.py -- 48-byte
#: records, u32 flag at record+0x0c, u64 payload at record+0x00 -- which is a good
#: sign the reading is right, since that one was reversed independently.
#:
#: Minimal manifest: count=1, entry[0].flag=1 -- 400 bytes, everything else zero.
#: Written per-member to `/data/resources/<member>.b_g_TM0IML.bin`, which
#: `_resource_blob` prefers over `RESOURCE_INIT`, so it takes effect with NO
#: restart and NO dropped session. Delete those files to revert, also live.
#:
#: WARNING: THE ERROR-CODE DECODE THIS WORKSTREAM RELIES ON IS UNSOURCED. A full-tree
#: search for `37088` returns exactly two hits, both in the Janhourou save-data notes,
#: and both merely repeat "TM's error table decodes 37088/37092/37312 as *timed
#: out while reading default data*" as a group. There is no per-code decode
#: anywhere in the repo, no such table in the PS2 `tm-xlate` string table (1638 rows), and
#: TMaster.pex materialises none of the three as an immediate. So the family
#: reading is an assertion carried forward, not a measurement, and **which code is
#: "further along" cannot currently be answered**. Anyone using the on-screen
#: number as a progress signal should find or rebuild that table first.
#:
#: MEASURED 2026-08-14 from a savestate at the error (PS2 savestate probe):
#:
#: * **the manifest LANDS.** `0x005c64a0` reads count=1 with entry[0].flag=1 at
#:   +0x1c, exactly as written. The stored-file delivery route
#:   (`/data/resources/<member>.b_g_TM0IML.bin`, no restart) works, and the layout
#:   reversal above is confirmed against the client's own copy.
#: * **the chain still does not run.** Both destination buffers (`0x005c6ef0`,
#:   `0x005c6ed0`) are entirely zero and no `b/g/TM0RkData` request has ever
#:   appeared on the wire. The chain cursors are non-zero (3 and 2) but there is
#:   no baseline for them, so do not read that as "it iterated".
#: * the matched entry's **u64 at entry+0x00 is zero**, and `0x004239bc` reads it
#:   the instant the flag matches. Still the best candidate for the next field.
#:
#: WARNING: **AND A CORRECTION: "count=1 changed the error" is NOT established.** It was
#: reported as 0-37092 before and 0-37088 after, but the tester was unsure the
#: second was new, and **37092 does not exist in the client's own error table**
#: (396 records, codes 30024..39912, live at `0x0072f0f4..0x00730378`, records
#: `{u32 code, u32 category, u32 extra}`; 37088 -> {1542, 32} and 37312 ->
#: {1541, 32} are both present). So the safe reading is that we have ONE confirmed
#: code, 37088, and no reliable evidence the manifest moved it at all. Do not
#: build on "the error responds to manifest content" until a controlled
#: before/after pair says so.
#:
#: WARNING: CORRECTION 2026-08-14: `b/g/TM0RkData` is **RANKING** data
#: ("Rk" -- cf. the POLpro class 'R' = RANKING / sqMgRkcp* in
#: the Janhourou save-data reading), fetched by a DIFFERENT subsystem. Its only caller
#: is 0x003fc0f4, inside an s4-based state machine with fields around +0x5bb0,
#: not the default-data path at 0x004238xx. The manifest's u8/u16 arrays are read
#: BY that ranking reader when ranking runs -- which it does not during startup.
#: So "no b/g/TM0RkData request appeared" was never evidence the manifest failed,
#: and it was chosen as the pass/fail signal in error.
#:
#: THE MANIFEST WORKS, PROVEN: a traceable sentinel u64 (0x5a5a5a5a00000001)
#: written to entry[0]+0x00 turned up at **[0x005d15e0]**, and 0x004239cc stores
#: there ONLY on a flag match. So the selection loop runs, matches entry[0], and
#: reads our u64. Everything from the fetch through the match is confirmed
#: end-to-end against the client's own memory. The failure is strictly AFTER the
#: match -- 0x00423a18 onward, and whatever consumes [0x005d15e0] (readers at
#: 0x0037726c, 0x003b7438, 0x003b7810, 0x003e6cb4).
#:
#: WHAT THE CHAIN THEN DOES, and why this is staged: a non-zero count makes the
#: client run chained sub-reads (`sqMgCpReadFile` 0x003ab910) of
#: **`b/g/TM0RkData`** (0x00486470, 24-byte records, buffer 0x005c6ef0,
#: 0x003aabd0) and a second 20-byte reader at 0x003ab740. Neither path is in
#: `_FETCH_PATHLEN`, so they would currently fall back to the opcode default --
#: the same class of wrong-length read that stalled `U/g/TM0DataFile` at 664.
#: **First launch answers only "does the chain start at all?"** If `b/g/TM0RkData`
#: appears in lobby.log, the model is confirmed and the lengths get added next.
TM0IML_LAYOUT = {"count": 0x00, "u8_array": 0x04, "u16_array": 0x08,
                 "entries": 0x1c, "entry_size": 48, "entry_flag": 0x00,
                 "total": 400}

#: The default-data version we teach the client. Re-read per line, from a CONTROL
#: FILE first (bind-mounted /logs, so it can be retuned with NO container recreate
#: and NO dropped connection), falling back to env. Format: one line `D V`
#: (decimal). D is a byte, V a halfword.
#:
#: ~~HYPOTHESIS (2026-08-13): D/V=0/0 reads as "default data version 0 = nothing
#: to fetch" ... Non-zero D/V may make it FETCH.~~ **REFUTED LIVE 2026-08-14 on
#: the PS2 client.** Served `D=1/V=1` (confirmed on the wire in authserv.log) and
#: the client fetched exactly the same two paths as with 0/0 -- `b/g/TM0IML` and
#: `U/g/TM0DataFile`, never `TM0SML`/`TM0SML2` -- and raised the same 0-37092.
#: **D and V do not select which files the client asks for.** They are passed as
#: PARAMETERS of the read itself (`0x005b7ca8` byte and `0x005b7cb8` halfword are
#: both loaded into the `sqMgCpReadFile` argument list at 0x003aaef8/0x003aaf10),
#: so they are a version the SERVER is meant to key its answer on, not a client
#: switch. Left at 0/0; do not spend another run toggling them.
#:
#: What actually gates the default-data path is the TM0IML manifest -- see
#: TM0IML_LAYOUT below.
_TEACH_FILE = os.environ.get("POL_TM_TEACH_FILE", "/logs/tm_teach.txt")


def _teach_dv():
    try:
        with open(_TEACH_FILE) as f:
            d, v = f.read().split()[:2]
            return int(d) & 0xFF, int(v) & 0xFFFF
    except (OSError, ValueError):
        return (int(os.environ.get("POL_TM_TEACH_D", "0")) & 0xFF,
                int(os.environ.get("POL_TM_TEACH_V", "0")) & 0xFFFF)


def _teach_d():
    return _teach_dv()[0]


def _teach_v():
    return _teach_dv()[1]


def _teach_enabled():
    return os.environ.get("POL_TM_TEACH", "1") == "1"


#: THE PC BUILD WANTS FOUR VERSION SETS, NOT ONE -- and this is why Tetra Master
#: on the PC died at `TRM-0-37088 "Timed out while reading default data."` while
#: the PS2 sailed past on the same reply. Measured 2026-08-15 in the UNPACKED
#: `TM.dll` (the disk copy has .text rawsize=0; these come from a full-memory dump
#: of a live pol.exe, TM.dll based at 0x05340000 in that process).
#:
#: The handler at 0x053c134d parses `@TeachDVAns` field by field, and the field
#: extractor 0x53eb080 takes an OCCURRENCE INDEX as its second argument -- so a
#: name repeated in the message yields successive values:
#:
#:   /D=   x1  -> byte [0x55cbb68]          | debug 'LN D,V = %d %d'
#:   /V=   x1  -> word [0x55f2950]          |
#:   /AC=  x2  -> D byte [0x55c36cc], V word [0x55c36a8]   'AC D,V = %d %d'
#:   /RK=  x2  -> D byte [0x55b95e0], V word [0x55ef4c0]   'RK D,V = %d %d'
#:   /Shm= x3  -> occurrence 0 goes to a DIFFERENT parser (0x53eb470) that fills
#:                two dwords at [0x558f590]/[0x558f594] -- the `%08x%08x` of
#:                'SH = %08x%08x, D,V = %d %d' -- then D byte [0x55d8248] and
#:                V word [0x55ba8f0].
#:
#: PROOF IT IS THE PAYLOAD AND NOT A TIMING RACE: our own reply
#: `GTM0GE2000000@TeachDVAns/D=1/V=1` was found verbatim in the client's receive
#: buffer at 0x0565fa85 in that dump. It arrived and was parsed; it simply had
#: nothing for the other three sets, so the client kept waiting. (The `<CI>`
#: 1500 ms delay note above therefore still stands -- do not re-run it for this.)
#:
#: AC/RK/Shm are the AUCTION, RANKING and Shm data sets, matching the PC-only
#: paths `b/g/TM0AucData` and `b/g/TM0RkData`. The PC build has NO `b/g/TM0IML`
#: string at all, so the PS2's manifest step does not exist here.
#:
#: All zero = "version 0 of everything", the same least-committal answer `D=0/V=0`
#: already makes for LN. Tunable live -- the control file is re-read per line, so
#: no restart:
#:
#:     0 0                     <- D V, as before
#:     ac=0,0                  <- AC D,V
#:     rk=0,0                  <- RK D,V
#:     shm=0000000000000000,0,0  <- Shm 64-bit hex, D, V
#:     ext=0                   <- omit AC/RK/Shm entirely (the old PS2-only reply)
#:
#: `ext` defaults to 1. Sending the extra fields to a PS2 client is expected to be
#: harmless -- its parser (TMaster.pex 0x00298dc0) strstr's for the names it wants
#: and ignores the rest -- but `ext=0` reverts instantly if that turns out wrong.
def _teach_delay():
    """Hold a TM0 reply back `delay=<ms>` before sending it.

    Default 1500 ms, matching POL_POLPRO_DELAY_MS -- the same race, on the same
    client, one channel over. `delay=0` restores the instant reply, which is what
    the PS2 has always had and been fine with; if this turns out not to be the
    fault, set it back to 0 rather than leaving an unexplained pause in the path.
    """
    try:
        ms = int(_teach_opts().get("delay", "1500"), 0)
    except ValueError:
        return
    if ms > 0:
        time.sleep(min(ms, 30000) / 1000.0)


def _teach_opts():
    """`key=value` tokens from the teach control file; {} if absent/unreadable."""
    out = {}
    try:
        with open(_TEACH_FILE) as f:
            text = f.read()
    except OSError:
        return out
    for tok in text.split():
        key, sep, val = tok.partition("=")
        if sep:
            out[key.strip().lower()] = val.strip()
    return out


def _teach_shm(opts, peer_nick):
    """The `/Shm=` u64: the peer id, derived from `peer_nick`. `shm=` overrides."""
    raw = opts.get("shm")
    if raw is None and peer_nick:
        try:
            import polnick
            pid = polnick.polid_for_nick(
                peer_nick.decode("ascii", "ignore")
                if isinstance(peer_nick, bytes) else peer_nick)
            guid = 0
            for ch in pid:
                guid = guid * 36 + polnick.ALPHA.index(ch)
            return "%016X" % (guid & 0xFFFFFFFFFFFFFFFF), 0, 0
        except Exception:
            pass                       # unknown nick shape -> fall through to zeros
    shm = (raw or "0000000000000000,0,0").split(",")
    shm_hex = (shm[0] if shm and shm[0] else "0").strip()
    try:
        shm_hex = "%016X" % (int(shm_hex, 16) & 0xFFFFFFFFFFFFFFFF)
    except ValueError:
        shm_hex = "0" * 16
    try:
        return shm_hex, int(shm[1], 0) & 0xFF, int(shm[2], 0) & 0xFFFF
    except (IndexError, ValueError):
        return shm_hex, 0, 0


def _teach_pair(opts, key, default=(0, 0)):
    """`key=D,V` -> (D & 0xFF, V & 0xFFFF)."""
    raw = opts.get(key)
    if not raw:
        return default
    parts = raw.split(",")
    try:
        return int(parts[0], 0) & 0xFF, int(parts[1], 0) & 0xFFFF
    except (IndexError, ValueError):
        return default


#: WARNING: THE `=` AFTER THE COMMAND NAME IS LOAD BEARING ON THE PC (2026-08-16).
#: The PS2 parser strstr's `@TeachDVAns` out of the raw body, so `@TeachDVAns/D=0`
#: parses there. The PC does NOT: `TM.dll` rva 0xAA3C0 tokenises the whole body
#: into an associative container keyed on the text from `@` up to the NEXT `=`,
#: and rva 0xAA920 (`@TeachDVAns` / `@InitAns`) is a lookup in THAT container --
#: not a substring search. With `@TeachDVAns/D=0` the key stored is
#: `@TeachDVAns/D`, every lookup misses, and no field is ever parsed. The value
#: is taken from the first `/` after the `=` up to the next `@` or end of body,
#: which is why the fields still follow the `=` unchanged. TM's own outgoing
#: commands all carry it (`@TeachDV=`, `@Init=`, `@Opt=`, `@Save=` ... rva
#: 0x22241C ff), and the `Ans` literals do not -- because they are keys.
#: Adding it is safe for the PS2: `@TeachDVAns=` still contains `@TeachDVAns`.


def _teachdvans_body(d, v, peer_nick=None):
    """The `@TeachDVAns` payload, PC field set included unless `ext=0`.

    Field ORDER here is the order the PC handler reads them in, which costs
    nothing and keeps the line comparable to a real capture if one ever turns up.
    Occurrence count is what matters to the parser, not position.
    """
    body = b"@TeachDVAns=/D=%d/V=%d" % (d, v)
    opts = _teach_opts()
    if opts.get("ext", "1") != "1":
        return body
    ac_d, ac_v = _teach_pair(opts, "ac")
    rk_d, rk_v = _teach_pair(opts, "rk")
    shm_hex, shm_d, shm_v = _teach_shm(opts, peer_nick)
    return body + (
        b"/AC=%d/AC=%d/RK=%d/RK=%d/Shm=%s/Shm=%d/Shm=%d"
        % (ac_d, ac_v, rk_d, rk_v, shm_hex.encode("ascii"), shm_d, shm_v))
