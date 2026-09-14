#!/usr/bin/env python3
"""Build Tetra Master's `b/g/PTL` -- the room's MEMBER and TABLE list.

THE LAST LINK OF THE LOBBY CHAIN. `b/g/ZL` picks a zone, `b/g/RL%03d` picks a
room, and entering the room fetches `b/g/PTL`. The screen it feeds is the one
titled **Tables / Members / Back** with "Select tables." at the foot -- and with
our 49,232 zeros it renders EMPTY, both panes, which is what this tool fixes.

WARNING: THE CONTAINER IS THE SAME FILE FORMAT AS JANHOUROU'S, because the loader is
`sqMg`'s `cp.c` and both games link it. `tools/janptl.py` reversed it off
`JanHouRou.pex`; every offset below was RE-MEASURED off `TM.dll.unpacked`
(image base 0x04F90000, so file offset == RVA) and the two agree exactly:

    0x1A1600  the read     sqMgReadFileOffset("b/g/PTL", buf 0x52A37A8, 0xC050)
    0x1A16B0  the poll     logs `sqMg:PTList Load Finished (%d)`
    0x1A1F50  the driver   state at 0x52AF808: 0 start -> 1 poll -> 2 done,
                           and it logs `pn = %d tn = %d` from blob +0x44/+0x48
    0x1A0C90  the UNPACKER blob -> the cp context at 0x52A377C

    +0x40  u32   SERIAL          -> ctx+0x08
    +0x44  s32   MEMBER count    records from +0x0050, stride  88 (0x58)
    +0x48  s32   TABLE  count    records from +0x5850, stride 104 (0x68)

    0x50   + 256 *  88 == 0x5850          0x5850 + 256 * 104 == 0xC050 == 49232

WARNING: THE COUNTS ARE NOT BOUNDS-CHECKED on the way in (`0x1A0C90` copies blindly),
but the destination arrays are the client's, so overrunning them is ours to
avoid. The consumer's own ceiling is not read yet, so this tool keeps Janhourou's
measured 256 members / 64 tables, which are certainly not too generous.

THE SERIAL IS THE FREE TELL-TALE. Once a second the client sends class-L `<DR>`
carrying ctx+0x08 verbatim (`0x1A0732` when the cp state is 0xF), so
`polpro L <- <DR>(N)` in authserv.log says which blob it is holding. A zero-fill
reports 0; serve serial 7 and the next poll says 7. Use it -- it turns "did it
even read the file" into a one-line log check.

THE GATES, both read off `0x1A14B0`, the accessor the table screen calls
(`0x86010` -> `0x1A14B0(members, tables, &nmem, &ntbl)`):

    member  the u64 at +0x00 must be NON-ZERO   (0x1A14E9, via 0x1A3600)
    table   the BYTE at +0x18 must be non-zero  (0x1A1552) -- the first
            character of the 13-byte channel-style name, so "unnamed" and
            "absent" are the same thing here, exactly as in Janhourou

A record that fails its gate is not copied, and the count the screen sees is the
count of SURVIVORS -- so a bad record is invisible rather than an error.

THE MEMBER RECORD (88 bytes) IS READ FROM THE CLIENT'S OWN MOUTH. `0x1A3340`
serialises this very struct as the class-L `<DE>` group the client sends on room
entry, field by field, so the map below is measured from the SENDING side and
each offset is paired with the value our live client actually put on the wire:

    off    type        <DE> value      what our client sent
    +0x00  u64         v0              0            the id -- the SERVER's to fill
    +0x08  u64         v9              0x2000000001 the ROOM id
    +0x10  8 B time    v6              1786939535   a unix timestamp
    +0x18  u32         v1              0
    +0x1C  u32         v7              1
    +0x20  u32         v8              0
    +0x24  u16         v3              0
    +0x26  u8          v2              0
    +0x27  u8          v5              2            PLAYER_STAT: 2 in, 0 out
    +0x28  char[16]    v4              ""           unused by the PC build
    +0x38  char[32]    v10             "Lex            DAA0AAABABAADIAB"

WARNING: **`+0x38` IS TWO FIELDS IN ONE 32-BYTE STRING**: a 15-byte space-padded NAME
followed by SIXTEEN LETTERS. That is why `build()` takes `name` and `stats`
separately and packs them for you. The client sends its own id as ZERO, so a
member row copied straight from a `<DE>` capture would be dropped by the gate --
give it an id.

**AND THE FIRST LETTER OF THAT BLOCK IS THE NAME'S LENGTH** -- `0x66A39`, the
member-row decoder, reads `+0x47` (block[0]), subtracts `'A'`, and copies that
many bytes from `+0x38` as the displayed name, ignoring the result unless it is
in 1..15. Our client sent `D` and a name of `Lex`, which is 3, and that is the
check: the padding is not a terminator, the length letter is. Everything after
it in that block is letter-encoded too, and the same decoder reads

    block[1..2]  a u16, packed as two nibbles      (esp+0x64)
    block[7]     a nibble                          (esp+0x68)
    block[8]     a nibble                          (esp+0x6C)
    block[9]     a nibble, and it is compared to 1 -- a flag

which is where VS. Rating / Title / Card Level / Guild in the Member Info panel
must come from. Our client's own block is `DAA0AAABABAADIAB`; note the `'0'` at
index 3, which is NOT a letter, so a block is copied verbatim rather than
re-encoded when it comes from a capture.

THE TABLE RECORD (104 bytes), off the clear at `0x1A2720` and the readers:

    +0x00  u64   table id      -- non-zero: 0x7F9CB picks the first table with one
    +0x08  u32   \  cleared as a pair; Janhourou's are capacity and seated and
    +0x0C  u32   /  no TM reader is measured yet, so they are AUTHORED, not known.
                    WARNING: ONE reader IS measured: `0x76338`, the auto-pick, refuses a
                    table whose **+0x0C is >= 8**, so that field is a count and 8
                    is its ceiling.
    +0x10  u32   Janhourou requires exactly 1 here; no TM reader found. Kept 1.
    +0x14  u8    a state byte (Janhourou: an 8-value enum)
    +0x18  13 B  NAME -- THE GATE, and channel-shaped like the room record's +0xB8
    +0x28  64 B  ONE string field, and TM's own layout:
                 [0]      TYPE -- and see "THE TABLE LIST IS ALSO THE SERVICE
                          DIRECTORY" below, because this one char is doing two
                          jobs. 'A' and 'B' are playable tables and draw a tile;
                          'F' and 'D' are SERVICE ENDPOINTS and draw nothing.
                 [1..3]   a letter-encoded triple, decoded at 0x8609C as
                          `((s1<<4)+s2-0x455)<<4)+s3`, i.e. the room list's
                          packing PLUS ONE, into 0x5227FF0[i]; 0x66D7F skips the
                          row when that value is <= 0, so "AAA" (= 1) is the
                          floor, not "  " and not zeros
                 [14..29] SIXTEEN letters, unpacked nibble-wise at 0x66DBB into a
                          64-bit value. Authored as 'A' (= 0) throughout: what it
                          drives is not read yet, and a wrong id here would be
                          fed to a consumer rather than merely drawn.

THE TABLE LIST IS ALSO THE SERVICE DIRECTORY, and that is the load-bearing
finding here (2026-08-17). Earlier disassembly established that the client
never sends `@CardReq=` because the state machine at rva `0x9A171` linear-scans a
"directory of typed service endpoints" for **`byte[entry+0x28] == 'F'`**, finds
nothing, and takes the other branch -- and it left open "which cp message fills
sqMg cache B". It is this file. The addresses line up exactly:

    that cache B  : count rva 0x31378C, array 0x2AF508, stride 0x68, capacity
                    128, gated on byte[+0x18] != 0
    this file's   : ctx 0x52A377C +0x10 = rva 0x31378C, +0x14 -> the array,
                    stride 0x68, gated on byte[+0x18] != 0   -- the same fields
                    `0x1A0C90` unpacks the TABLE records into

So a table record whose type char is `'F'` IS the card-service endpoint, and its
u64 at +0x00 is the DESTINATION the client addresses `@CardReq=` to. Known types:

    'A' 'B'   a playable table; `0x86097` draws a tile, anything else stores -1
              for the row and `0x66D7F` skips it -- so a service entry is
              invisible on the table screen, which is how SE got away with it
    'F'       the CARD SERVICE          scan at rva 0x9A13C, u64 -> 0x8B720
    'D'       a second service          scan at rva 0x156F1, u64 -> obj+0x1A8
    'E'       the auto-pick's type      scan at rva 0x762C7, and it additionally
                                        refuses an entry whose +0x0C is >= 8

WARNING: The u64 is a PEER ID in the `/Shm=` sense -- the same synthetic service
id the client derives from a nick, not one of our member ids. We serve the one
the live `@Init` handshake already proved: `/Shm=000000384EA5822C`, which is nick
`USH6MZJA7` -> POL ID `AAAC0001` -> base-36 over `polnick.ALPHA`. That id is
synthetic and constant, so it does not need recomputing per session, but it is
DERIVED and `tools/tmptl.py --shm` exists to change it in one place if a launch
shows `@CardReq=` addressed to a nick nobody answers.

THE LETTER ENCODING, TM's house style and the same one the room list uses
(`0x3BCE0`): three characters, `'A'` = 0 .. `'P'` = 15, packed as base-16 digits

    value = ((c0-'A') << 8) | ((c1-'A') << 4) | (c2-'A')

VERIFIED: +0x08/+0x0C/+0x10/+0x14 AND THE 16-LETTER BLOCK ARE NOW MEASURED
(2026-08-20, `TM.dll.unpacked` disassembled after live testing reported that a
reserved table shows NO change to anybody else). What the room's table screen
actually reads, per record, is the register-walk loop at **0x66D7A**
(`mov esi, array+0x4E`, `add esi, 0x68`) and it touches exactly these:

    0x66D92  [esi-0x4E]  +0x00  id.lo
    0x66DAA  [esi-0x4A]  +0x04  id.hi
    0x66DA0  [esi-0x3A]  +0x14  THE STATE BYTE
    0x66D9D  [esi-0x26]  +0x28  block[0], the type char
    0x66DBB  [esi+i-0x18] +0x36 block[14..29], the SIXTEEN LETTERS
    0x6720D  [esi-0x22/-0x21]  block[4]/block[5]
    0x67176  [esi+0x0A]  +0x58  block[48]

and a whole-image scan of absolute references to the array agrees with it:

    +0x08 capacity   0 references   ANYWHERE IN THE CLIENT
    +0x0C seated     1 reference    -- rva 0x7633A, and that is the auto-pick's
                                      ">= 8" refusal this file already documents
    +0x10            0 references
    +0x14 state      0 absolute refs (it is read ONLY by the walk above)

WARNING: SO **+0x0C IS NEVER DRAWN**. Publishing a seat count changes nothing on
anyone's screen, which is exactly what live testing showed. It is a gate, not a
display, and +0x08 and +0x10 are neither.

THE STATUS COMES FROM +0x14, through a 7-entry jump table at **rva 0x674C4**
(`0x67223 jmp [edx*4 + 0x4FF74C4]`, `state > 6` falls to 0x6736D):

    state 3,4,5  -> 0x67361   a FIXED code 2
    state 2      -> 0x67344   code (x << 4) | 3, with bit 8 set when bl != 0
    state 6      -> 0x6722A   code 22 if x == 1 else 2
    state 0      -> 0x6723B   compares the table id against 0x52461D0/4 --
                              "is this the table I am at"
    state 1      -> 0x67265   compares the block's TWO 64-BIT IDS against
                              0x5245308/C, i.e. against the local player

VERIFIED: AND THAT IS WHERE OCCUPANCY LIVES: the 16 letters at block[14..29] --
the ones this file authors as 'A' throughout because "what it drives is not read
yet" -- are ONE 64-BIT ID, and the state-1 arm compares it against the player's
own (`0x5245308`/`0x524530C`, copied there from `0x5242918` at `0x14BC50`).

WARNING: CORRECTION: an earlier reading of this called it "two 64-bit values, two
seats". It is one. Sixteen nibbles is exactly 64 bits, the four groups below are
four quarters of ONE number, and `[esp+0x34]`/`[esp+0x38]` are its lo and hi
halves, not two ids.

**THE NIBBLE ORDER IS PINNED** (2026-08-20). The unpack looks scattered because
the compiler interleaved four groups, each built as `(<<4, <<8, <<12, <<0)` and
scaled by a multiplier pushed to `__allmul` at 0x1E2E10:

    0x66DCC  nibbles  8, 9,10,11  x 0x10000            -> bits 16..31
    0x66E1C  nibbles  4, 5, 6, 7  x 0x100000000        -> bits 32..47
    0x66E6C  nibbles  0, 1, 2, 3  x 0x1000000000000    -> bits 48..63
    0x66EBF  nibbles 12,13,14,15  x 1                  -> bits  0..15

which is simply **the 64-bit value as 16 base-16 letters, most significant
first** -- `letters(value, 16)`, this file's own encoding, nothing special. Proved
by modelling the four groups and their multipliers verbatim and round-tripping
2,004 values including 0, 1 and 0xFFFFFFFFFFFFFFFF: zero mismatches.

WARNING: AND IT EXPLAINS THE WHOLE SYMPTOM. All-'A' decodes to **0**, and the
state-1 arm opens with `or ebp, edx; je` on exactly that -- a zero id short-
circuits to display code 2. Every table this server has ever served says "nobody
is here" in the one field the screen believes.

THE DISPLAY CODE at `[esp+0x8d8]` is packed: low nibble = a KIND, bits 4+ = a
COUNT, bit 8 = a flag (`bl`, and the loop reads block[4]/block[5] just above it).

    code 2                    the EMPTY tile -- states 3/4/5 unconditionally,
                              and any state whose id is 0
    (n << 4) | 3   state 2    ( | 0x100 when bl )
    0x11 / 0x111   state 1, count 1, id present and not mine
    (n << 4) | 5   state 1, count > 1
    22             state 6 when x == 1

WARNING: STILL NOT MEASURED: where the COUNT `n` comes from on the not-my-table path,
what `bl` is exactly, and how these codes map to Lobby.BIN strings.

WARNING: MEASURED EARLIER: the container, both strides, both counts, both gates, the
whole member record (from the client's own serialiser) and the table record's
+0x00, +0x18 and +0x28[0..3].

    python tools/tmptl.py --out data/resources/1.b_g_PTL.bin
    python tools/tmptl.py --dump data/resources/1.b_g_PTL.bin
"""
import argparse
import os
import re
import struct
import sys

# WARNING: THIS FILE'S OUTPUT HAS CARRIED NON-ASCII MARKERS, AND IT IS RUN ON WINDOWS, where
# stdout defaults to cp1252 and any of them raises UnicodeEncodeError. That is
# not cosmetic: the over-PAYLEN warning in `build()` is a print, so without this
# the tool CRASHES on exactly the run that needed to warn you. Same line the
# other Windows-side tools carry for the same reason.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TOTAL = 49232           # 0x1A1644: push 0xc050
SERIAL_OFF = 0x40
MEMBER_COUNT_OFF = 0x44
TABLE_COUNT_OFF = 0x48

MEMBER_OFF = 0x0050
MEMBER_REC = 88         # 0x58 -- rep movsd ecx=0x16 at 0x1A0CC6
MEMBER_SLOTS = 256      # and 0x50 + 256*88 == 0x5850 exactly

TABLE_OFF = 0x5850
TABLE_REC = 104         # 0x68 -- rep movsd ecx=0x1a at 0x1A0D2E
TABLE_SLOTS = 256       # and 0x5850 + 256*104 == 49232 exactly

#: Janhourou's measured consumer ceilings (janptl.py). No TM reader has been read
#: that bounds either list, so these stand in: they are known-safe for the twin
#: game and far above anything this tool serves.
MEMBER_MAX = 256
TABLE_MAX = 64

# --- member record, from the <DE> serialiser at 0x1A3340 ---------------------
M_ID = 0x00             # u64 -- non-zero or 0x1A14E9 drops the row
M_ROOM = 0x08           # u64 -- the room the member is in
M_TIME = 0x10           # 8 B -- a unix timestamp
M_F18 = 0x18            # u32
M_F1C = 0x1C            # u32 -- our client sends 1
M_F20 = 0x20            # u32
M_F24 = 0x24            # u16
M_F26 = 0x26            # u8
M_STAT = 0x27           # u8  -- 2 = in the room, 0 = leaving
M_STR16 = 0x28          # char[16] -- THE DISPLAY NAME, ours to fill -- empty in everything we have seen
M_STR32 = 0x38          # char[32] = name[15] + 16 letters      -> 0x58 = 88

M_NAME_LEN = 15
M_STATS_LEN = 16

# --- table record, from the clear at 0x1A2720 and the readers ----------------
T_ID = 0x00             # u64 -- non-zero: 0x7F9CB looks for the first with one
T_F08 = 0x08            # u32 -- authored (Janhourou: capacity)
T_F0C = 0x0C            # u32 -- authored (Janhourou: seated)
T_F10 = 0x10            # u32 -- Janhourou requires 1; no TM reader measured
T_STATE = 0x14          # u8
T_NAME = 0x18           # 13 B -- THE GATE (first byte non-zero)
T_BLOCK = 0x28          # 64 B -- the string block            -> 0x68 = 104

#: `block[0]`, and every reader that scans for one. A playable table draws a tile;
#: a service entry is a typed endpoint and draws nothing, because 0x86097 stores
#: -1 for any type it does not know and 0x66D7F skips a row on that.
TABLE_TYPES = {
    "A": "a playable table, the plain row builder    (0x86097 / 0x3A040)",
    "B": "a playable table, the full row builder     (0x86097 / 0x3CFB0)",
    "D": "service endpoint -> obj+0x1A8              (0x156F1)",
    "E": "service endpoint, the auto-pick's type     (0x762C7, needs +0x0C < 8)",
    "F": "service endpoint -> @CardReq= (0x8B720)    (0x9A13C)",
}

T_NAME_MAX = 0x28 - 0x18                # 16, but the clear only wipes 13
T_BLOCK_MAX = TABLE_REC - T_BLOCK       # 64
T_BLOCK_LETTERS = 14                    # +0x36 - +0x28, read at 0x66DBB
T_BLOCK_LETTERS_LEN = 16

# --- THE TABLE RESTRICTIONS / RULES PANEL, block[30..48] ---------------------
# SOLVED 2026-08-17 off `TM.dll.unpacked`. The panel is drawn by the loop that
# starts at 0x66D10; it walks the SAME table array (`mov esi, 0x521BB70+0x4E`
# at 0x66D7A, `add esi, 0x68` at 0x673B6), so `esi == record + 0x4E`, i.e.
# **block[0x26]**, and every field below is at a fixed offset from it.
#
# WARNING: THIS IS WHY THREE DIFFERENT BLOBS PRINTED IDENTICAL NUMBERS. The fields
# begin at block[30] and the old builder wrote letters only through block[29],
# NUL from [30] on -- so every variant we ever served was NUL exactly where the
# panel reads. `0x00 - 'A'` is 0xBF and the decoders never mask to a nibble, so
# NUL is not zero, it is 0xBF per digit:
#
#     4 letters of NUL & 0xFFFF   -> 0xBBAF   = 48047      "Wager 48047"
#     5 letters of NUL            -> 0xCBBBAF = 13351855   "Card Level 13351855"
#
# which is the whole of the "one value read at two widths" clue: it is one
# accumulator loop run at two widths over the same NUL padding, not two reads
# of one address. The two-letter fields decode to 0xCAF the same way, and
# 0xAF's low bits are what printed Password On, all four rules On, Time Limit
# 10:00 and Quitting a Game Loss. Every number on that screenshot is reproduced
# by feeding this decoder zeros -- nothing was ever read from the room record,
# from a TM.dll literal, or from anything we did not serve.
T_SET = 30                              # block[30] -- first settings byte
T_SET_END = 49                          # block[49] -- and NUL from here (0x86074)

#: block[35], and 0x6701F reads anything above 4 as 5.
TIME_LIMITS = ["0:15", "0:30", "1:00", "3:00", "5:00", "10:00"]
#: block[36], ONE-BASED: 0x67053 does `(v - 1) & 3`, so 'A' (0) reads as "Loss".
QUITTING = ["Substitute", "Invalid", "Loss"]
#: block[34], one bit each, in the order 0x66FEA..0x6701B stores them.
RULE_BITS = ["double_up", "chance_block", "special_tile", "rotating_block"]

#: An open table with no restriction anybody can fail. WARNING: `card_max` is the one
#: field whose zero is a TRAP: a maximum of 0 is a card-level range nothing
#: satisfies, which is the same shape of refusal 13351855 was.
#:
#: VERIFIED: THE CLIENT PUBLISHES ITS OWN DEFAULTS, AND THEY ARE MEASURED NOW.
#: Reported 2026-08-20 from live testing -- the served panel was "not the actual
#: defaults", and a game had to have Default clicked before it would accept the
#: table. The client says what it wants in the clear: pressing Default and
#: confirming sends game code 20 (authserv.log 2026-08-20T00:27:25Z), and it is
#: one line carrying BOTH halves of the panel:
#:
#:     @Tet=/bm=0/du=0/st=0/cb=0/ca=0/gs=0/tl=0
#:     @Tab=/in=0/lu=99999/ll=0/au=300/al=100/co=0/pa=0
#:
#: `lu`/`ll` are the Card Level range the panel draws, and SE's ceiling is
#: **99999**. Ours was 0xFFFFF = 1048575, which is not a value the client's own
#: UI can produce -- it is simply the largest number five letter-encoded nibbles
#: hold, i.e. our encoder's limit mistaken for a default. So this line makes the
#: served panel match SE's.
#:
#: WARNING: AND THAT IS ALL IT DOES -- IT IS **NOT** WHY A RESERVATION IS REFUSED.
#: That was measured on 2026-08-18, and this warning was written for
#: exactly this edit: at 06:32:58 the client sent `lu=99999/ll=0` and was
#: **still refused**. The discriminator is `au`/`al` -- every refusal carried
#: `au=0/al=0`, and the one accepted set is the only one with `au=300/al=100`.
#: A band of 0-0 admits nobody. Do not cite this constant as the acceptance fix.
#:
#: WARNING: AND `au`/`al` CANNOT BE AUTHORED HERE. Checked: `block[30..48]` has NO
#: rank-shaped field at all -- `block[38]`, once the candidate, is the COMMENT
#: preset index (0..8 = Lobby.BIN 385..393; settled 2026-08-22, see
#: DEFAULT_SETTINGS), and nothing else in the block can
#: hold 100 or 300. Either the band lives elsewhere in the 104-byte table record
#: or it only ever reaches the client over `@Tab=` -- settle that
#: BEFORE authoring, because it decides whether this is fixed in the blob or in a
#: message. Same for `@Tet=`'s unmapped `bm`, `ca`, `gs`, and its `tl=0` against
#: the `time_limit=5` ("10:00") we serve where `TIME_LIMITS[0]` is "0:15".
#:
#: THE REAL FIX IS STILL NOT DONE: `@Save=` is game code
#: **0x24** and carries `/Tbl=` (the table number) plus the whole `@Tet=`/`@Tab=`
#: set -- it is the Save button, and the server answers `no answer for this
#: command -- captured, silent`. Store it per table and serve it back (`0x87220`
#: is a PARSER, so the field set is two-way) and persistence, the refusal and the
#: seeding of the screen all close together.
DEFAULT_SETTINGS = {
    "wager": 0,                 # block[30..33], the client masks to 0xFFFF
    "double_up": False,         # block[34] bit 0
    "chance_block": False,      # block[34] bit 1
    "special_tile": False,      # block[34] bit 2
    "rotating_block": False,    # block[34] bit 3
    "time_limit": 5,            # block[35] -> TIME_LIMITS
    "quitting": 0,              # block[36] -> QUITTING (stored +1)
    "observe": 0,               # block[37] bits 0-1; 0 = Possible
    "password": False,          # block[37] bit 2
    # block[38] -> the COMMENT preset (Lobby.BIN 385..393 via the u16 table at
    # 0x51B2F7C, drawn by the table-info dialog 0x601B0 at its 'Comment' line).
    # 0x6708C's 0..8 bound is the PRESET COUNT -- the old "rank" reading of this
    # byte is retracted (2026-08-22); see tetramaster._settings_block.
    "comment": 0,
    "card_min": 0,              # block[39..43]; the client's own `ll=0`
    # block[44..48]; the client's own `lu=99999`. NOT 0xFFFFF -- see the banner.
    "card_max": 99999,
}


def table_settings(**kw):
    """The 19 letters at `block[30..48]` -- the Table Restrictions and Rules.

    Keys are `DEFAULT_SETTINGS`'; anything omitted takes its default. Returns
    exactly `T_SET_END - T_SET` bytes, so the caller can splice it in place.
    """
    bad = set(kw) - set(DEFAULT_SETTINGS)
    if bad:
        raise ValueError("no such table setting: %s (have: %s)"
                         % (", ".join(sorted(bad)),
                            ", ".join(sorted(DEFAULT_SETTINGS))))
    s = dict(DEFAULT_SETTINGS, **kw)

    if not 0 <= s["wager"] <= 0xFFFF:
        raise ValueError("wager %d does not fit the 16 bits 0x66FCC masks it to"
                         % s["wager"])
    if not 0 <= s["time_limit"] < len(TIME_LIMITS):
        raise ValueError("time_limit %d is not an index into %s"
                         % (s["time_limit"], TIME_LIMITS))
    if not 0 <= s["quitting"] < len(QUITTING):
        raise ValueError("quitting %d is not an index into %s"
                         % (s["quitting"], QUITTING))
    if not 0 <= s["observe"] <= 2:
        raise ValueError("observe must be 0..2; 0x66F7B reads 3 as 0")
    if not 0 <= s["comment"] <= 8:
        raise ValueError("comment must be a preset index 0..8 ('No Comment'.."
                         "'Flexible rules!'); 0x6708C reads anything above 8 as 0")
    for k in ("card_min", "card_max"):
        if not 0 <= s[k] <= 0xFFFFF:
            raise ValueError("%s %d does not fit 5 letter-encoded nibbles"
                             % (k, s[k]))
    if s["card_max"] < s["card_min"]:
        raise ValueError("card_max %d is below card_min %d -- a range nothing "
                         "satisfies is exactly what 13351855 was"
                         % (s["card_max"], s["card_min"]))

    rules = sum(bool(s[n]) << i for i, n in enumerate(RULE_BITS))
    flags = (int(bool(s["password"])) << 2) | (s["observe"] & 3)
    out = (letters(s["wager"], 4)
           + letters(rules, 1)
           + letters(s["time_limit"], 1)
           + letters(s["quitting"] + 1, 1)
           + letters(flags, 1)
           + letters(s["comment"], 1)
           # WARNING: MAX FIRST. block[44..48] is the "from" side of the range the
           # panel draws, NOT block[39..43] -- measured on screen 2026-08-18:
           # we wrote block[39..43]=0 / block[44..48]=0xFFFFF and the panel drew
           # "Card Level 1048575~0", i.e. it draws block[44..48] FIRST and a
           # range reads from~to. An earlier field map had the two labelled the
           # other way round; the addresses in it are right, the names were not.
           + letters(s["card_max"], 5)
           + letters(s["card_min"], 5))
    assert len(out) == T_SET_END - T_SET
    return out


def decode_settings(block):
    """Read `block[30..48]` back EXACTLY as 0x66F91..0x67155 does.

    Deliberately reproduces the no-mask subtraction, so a block that is NUL or
    carries stray text decodes to the same nonsense the panel prints instead of
    to a tidy zero. Returns a dict of display-ready values.
    """
    def acc(raw, mask=None):
        v = 0
        for c in raw:
            v = (v << 4) + ((c - ord("A")) & 0xFF)
        return v & mask if mask else v

    lo = acc(block[34:35]) & 0xFF
    hi = acc(block[35:36]) & 0xFF
    tl = hi if hi <= 4 else 5
    q = (acc(block[36:37]) - 1) & 3
    v = (acc(block[38:39]) << 4) + acc(block[37:38])
    com = (v >> 4) & 0xF
    obs = v & 3
    return {
        "wager": acc(block[30:34], 0xFFFF),
        "double_up": bool(lo & 1), "chance_block": bool(lo & 2),
        "special_tile": bool(lo & 4), "rotating_block": bool(lo & 8),
        "time_limit": TIME_LIMITS[tl],
        "quitting": QUITTING[q if q < 2 else 2],
        "observe": obs if obs < 3 else 0,
        "password": bool((v >> 2) & 1),
        # block[38] -> the dialog's Comment line, 0x51B2F7C[i] = Lobby.BIN
        # 385..393; >8 clamps to 0 ("No Comment") exactly as 0x6708C does
        "comment": com if com <= 8 else 0,
        "card_max": acc(block[39:44]), "card_min": acc(block[44:49]),
    }


def letters(value, n=3):
    """TM's letter encoding: base-16 digits as 'A'..'P', most significant first.

    `0x3BCE0` (room list) and `0x8609C` (table block) both read it; the table
    one adds 1 to the result, which `decode_letters` mirrors so a round trip
    through this module is honest about which number the client will see.
    """
    if value < 0 or value >= 16 ** n:
        raise ValueError("%d does not fit in %d letter-encoded nibbles (max %d)"
                         % (value, n, 16 ** n - 1))
    return "".join(chr(ord("A") + ((value >> (4 * (n - 1 - i))) & 0xF))
                   for i in range(n)).encode("ascii")


def decode_letters(raw):
    """Inverse of `letters`, for --dump. Returns None if a byte is not 'A'..'P'."""
    v = 0
    for c in raw:
        d = c - ord("A")
        if not 0 <= d <= 15:
            return None
        v = (v << 4) | d
    return v


def _text(s, limit):
    """Shift-JIS, NUL-terminated, hard-truncated to the field width."""
    return s.encode("cp932", "replace")[:limit - 1].ljust(limit, b"\x00")


def member_tail(name, stats=None):
    """The 32-byte `+0x38` field: a 15-byte space-padded name then 16 letters.

    `stats` is copied VERBATIM when given, so a capture can be replayed byte for
    byte -- our client sends `DAA0AAABABAADIAB`, which is not all letters ('0' at
    index 3), and re-encoding it would be inventing. Its first letter must still
    be the name's length, because that is what `0x66A39` displays by, so this
    checks a supplied block rather than silently disagreeing with it.
    """
    n = name.encode("cp932", "replace")[:M_NAME_LEN]
    want = chr(ord("A") + len(n)).encode("ascii")
    if stats is None:
        stats = want + b"A" * (M_STATS_LEN - 1)
    if len(stats) != M_STATS_LEN:
        raise ValueError("the stats block is %d bytes and the field is %d"
                         % (len(stats), M_STATS_LEN))
    if stats[:1] != want:
        raise ValueError(
            "the block starts %r but %r is %d bytes, and 0x66A39 displays the "
            "first (block[0] - 'A') bytes of the name -- it wants %r"
            % (stats[:1], name, len(n), want))
    return (n.ljust(M_NAME_LEN, b" ") + stats).ljust(32, b"\x00")


def table_block(kind, value=1, label="", stats=b"A" * T_BLOCK_LETTERS_LEN,
                settings=None):
    """The 64-byte `+0x28` field of a table record.

    WARNING: THE WHOLE BLOCK IS LETTER-ENCODED, SO ITS ZERO IS `'A'` AND NOT NUL, AND
    NOTHING HUMAN-READABLE MAY GO IN IT. Measured from the screen 2026-08-17:
    writing a tag like `"T-CAP8"` into the bytes after the triple made the
    client's Table Restrictions panel read

        Password  On    Card Level  13351855    Wager  48047

    because `'T'-'A'` is 19, `'-'-'A'` is -20, and those land in the restriction
    and rule fields. A table demanding Card Level 13 million behind a password
    is one nobody can reserve at -- which is what greyed Reservation and VS. COM
    through four rounds of blaming the member record. NUL filler is no better:
    `0x00 - 0x41` is negative too, so even the "empty" fields were garbage.

    `kind` is the type char; `value` is the triple at [1..3] AS THE CLIENT WILL
    SEE IT (0x8609C adds one, so pass 1 to store "AAA"); `stats` is the 16-letter
    block at [14..29]; `settings` is a `table_settings` dict (or its 19 raw
    bytes) for [30..48].

    WARNING: `label` writes [4..13], and those are NOT spare bytes: [4..5] is a
    two-letter count the panel reads at 0x671DD, and **[6..13] is the table
    PASSWORD**, which 0x67161 copies out eight bytes at a time and terminates on
    `*`. It DEFAULTS TO NOTHING and should stay that way unless you are
    deliberately probing -- read the Table Restrictions panel to see what moved.
    """
    if kind not in TABLE_TYPES:
        raise ValueError("table type %r is not a type any reader scans for: %s"
                         % (kind, ", ".join("%r (%s)" % kv
                                            for kv in sorted(TABLE_TYPES.items()))))
    if len(stats) != T_BLOCK_LETTERS_LEN:
        raise ValueError("the letter block is %d bytes and the field is %d"
                         % (len(stats), T_BLOCK_LETTERS_LEN))
    # WARNING: 'A' ONLY AS FAR AS THE DECODED FIELDS REACH, THEN NUL. Both halves of
    # this matter and each one crashed or corrupted the client once:
    #
    #   * NUL inside a decoded field is not "empty" -- 0x00 - 0x41 is negative,
    #     which is how the restrictions panel read Card Level 13351855.
    #   * NO NUL AT ALL is worse. `0x86074` strcpy's this block into a stack
    #     buffer, byte by byte, until it finds a zero:
    #         0x86081  mov cl,[eax] / mov [edx+eax],cl / inc eax / test cl,cl
    #     Filling all 64 bytes with 'A' left it no terminator, so it ran past
    #     the record into the next one and smashed the stack. That crashed a
    #     live client on 2026-08-17.
    #
    # WARNING: AND THE FIELDS RUN TO [48], NOT [29]. Stopping the letters at the
    # stats block is what left the Table Restrictions panel reading NUL, and
    # NUL is 0xBF per digit -- see T_SET. 0x86074 copies into `[esp+0xc]` of a
    # `sub esp, 0x40` frame, so it has 64 bytes: 49 letters plus a terminator
    # fits with room to spare, and it was only the UN-TERMINATED 64 that ran
    # off the end.
    #
    # So: letters through the last decoded field at [48], NUL from [49] on.
    out = bytearray(b"\x00" * T_BLOCK_MAX)
    out[0:T_SET_END] = b"A" * T_SET_END
    out[0:1] = kind.encode("ascii")
    out[1:4] = letters(value - 1)
    if label:
        gap = T_BLOCK_LETTERS - 4
        out[4:4 + gap] = _text(label, gap)
    out[T_BLOCK_LETTERS:T_BLOCK_LETTERS + T_BLOCK_LETTERS_LEN] = stats
    out[T_SET:T_SET_END] = (settings if isinstance(settings, (bytes, bytearray))
                            else table_settings(**(settings or {})))
    assert out[T_SET_END] == 0, "the block must be NUL-terminated -- 0x86074 strcpy's it"
    return bytes(out)


def build(members, tables, serial=1):
    """`members` = [(id, name, stats_or_None), ...];
    `tables`  = [(id, name, kind, value, label, f08, f0c, state[, f10]), ...].
    Returns the 49,232-byte blob."""
    if len(members) > MEMBER_MAX or len(tables) > TABLE_MAX:
        raise ValueError("%d members / %d tables; the ceilings are %d / %d"
                         % (len(members), len(tables), MEMBER_MAX, TABLE_MAX))
    if len(tables) > PAYLEN_TABLES:
        # WARNING: QUOTE THE REQUIREMENT FOR WHAT WAS ACTUALLY AUTHORED. This used to
        # hardcode "16 tables = ...", which is the wrong number the moment the
        # list carries service endpoints too -- and the endpoints are exactly
        # what must survive the cut (`0x9A13C` needs the 'F', `0x76235` the 'E').
        lost = [t[1] for t in tables[PAYLEN_TABLES:]]
        print("WARNING: %d records authored but POL_RESOURCE_PAYLEN cuts b/g/PTL at "
              "%d bytes = %d records. These never reach the client -- it drops "
              "them on the name gate: %s. Raise the override to %d (= TABLE_OFF "
              "+ %d*%d + 4) or expect that."
              % (len(tables), 24588, PAYLEN_TABLES, ", ".join(lost),
                 TABLE_OFF + len(tables) * TABLE_REC + 4, len(tables), TABLE_REC))

    buf = bytearray(TOTAL)
    struct.pack_into("<I", buf, SERIAL_OFF, serial)
    struct.pack_into("<i", buf, MEMBER_COUNT_OFF, len(members))
    struct.pack_into("<i", buf, TABLE_COUNT_OFF, len(tables))

    for i, row in enumerate(members):
        mid, name, stats = row[:3]
        room = row[3] if len(row) > 3 else DEFAULT_ROOM
        if mid == 0:
            raise ValueError("member %r has id 0, which 0x1A14E9 reads as an "
                             "empty slot and drops -- the client sends 0 for "
                             "itself and the server is what fills it in" % name)
        if room == 0:
            raise ValueError(
                "member %r has room 0. MEASURED 2026-08-17 from a live dump: "
                "the accessor copied three such members and reported count 3, "
                "and the Members pane still drew NOTHING -- a member in room 0 "
                "is not in the room the player is standing in. The client's own "
                "<DE> puts the room id at +0x08 (value 9); so must we." % name)
        # WARNING: EVERY numeric field defaults to what the client sends when it has
        # NOTHING. `f1c` especially: it is `(card level << 16) | flags` and a
        # hardcoded 1 publishes "no cards, no deck" over whatever the player
        # actually has. `from_log` supplies the real set.
        f = row[4] if len(row) > 4 else {}
        o = MEMBER_OFF + i * MEMBER_REC
        struct.pack_into("<Q", buf, o + M_ID, mid)
        struct.pack_into("<Q", buf, o + M_ROOM, room)
        struct.pack_into("<Q", buf, o + M_TIME, f.get("time", 0))
        struct.pack_into("<I", buf, o + M_F18, f.get("f18", 0))
        struct.pack_into("<I", buf, o + M_F1C, f.get("f1c", 1))
        struct.pack_into("<I", buf, o + M_F20, f.get("f20", 0))
        struct.pack_into("<H", buf, o + M_F24, f.get("f24", 0) & 0xFFFF)
        buf[o + M_F26] = f.get("f26", 0) & 0xFF
        buf[o + M_STAT] = f.get("stat", 2) & 0xFF
        # +0x28, THE DISPLAY NAME, and it is the SERVER'S to fill. The client
        # sends exactly two fields empty in its own <DE> -- v0, the member id,
        # and v4, this 16-byte string -- and both are blank for the same reason.
        # The row builder memcpy's 16 bytes from here at 0x669A8, separately
        # from the block-derived name, so leaving it empty publishes a member
        # with no name: measured 2026-08-17, the tester's own row rendered
        # BLANK while the client still recognised the row as theirs.
        buf[o + M_STR16:o + M_STR16 + 16] = _text(name, 16)
        buf[o + M_STR32:o + M_STR32 + 32] = member_tail(name, stats)

    for i, row in enumerate(tables):
        tid, name, kind, value, label, f08, f0c, state = row[:8]
        f10 = row[8] if len(row) > 8 else 1
        settings = row[9] if len(row) > 9 else None
        if not name:
            raise ValueError("table %d has no name; 0x1A1552 gates on the first "
                             "BYTE at +0x18 and drops the row" % tid)
        if tid == 0:
            raise ValueError("table %r has id 0; 0x7F9CB scans for the first "
                             "table with a non-zero id" % name)
        o = TABLE_OFF + i * TABLE_REC
        struct.pack_into("<Q", buf, o + T_ID, tid)
        struct.pack_into("<I", buf, o + T_F08, f08)
        struct.pack_into("<I", buf, o + T_F0C, f0c)
        struct.pack_into("<I", buf, o + T_F10, f10)
        buf[o + T_STATE] = state & 0xFF
        buf[o + T_NAME:o + T_NAME + T_NAME_MAX] = _text(name, T_NAME_MAX)
        buf[o + T_BLOCK:o + T_BLOCK + T_BLOCK_MAX] = table_block(
            kind, value, label, settings=settings)

    assert len(buf) == TOTAL
    return bytes(buf)


#: `polpro L <- <DE>(v0,...,v10)` as authserv.log prints it. The client sends this
#: on every room entry and it IS its own member record (serialiser 0x1A3340), so
#: the log is a live feed of exactly what we should be publishing back.
_DE_RE = re.compile(r"polpro L <- <DE>\((.*)\)\s*$")
#: The client states its own id in `@GameM=/ID=` and in class-R `<PI>`; it sends
#: ZERO for itself in `<DE>` (the server is what fills that in), so it has to come
#: from one of these.
_ID_RE = re.compile(r"(?:@GameM=/ID=|<PI>\(0x)([0-9A-Fa-f]{8,16})")


def from_log(path, member_id=None):
    """Build the member list from the client's OWN latest `<DE>`.

    WHY THIS EXISTS. The record we publish for a player is their eligibility as
    the ROOM sees it, and it goes stale the moment anything about them changes:
    measured 2026-08-17, `<DE>` index 7 of the letter block moved `B`(1) -> `J`(9)
    and `+0x1C` moved 1 -> 6881289 when the tester acquired cards, while we
    went on serving the `B` snapshot seeded from a capture hours earlier. Echoing
    the client's own words removes the whole class of error, and it is what SE's
    server must have done -- the client announces, the server publishes.

    Returns `[(id, name, stats, room)]` ready for `build()`, or [] if the log has
    no `<DE>` yet (the client sends one per room entry).
    """
    de = ident = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _DE_RE.search(line)
            if m:
                de = m.group(1)
            m = _ID_RE.search(line)
            if m:
                ident = int(m.group(1), 16)
    if not de:
        return []
    vals = de.split(",")
    if len(vals) < 11:
        raise ValueError("a <DE> has 11 values; this line has %d: %r"
                         % (len(vals), de))
    room = int(vals[9], 16) if vals[9].startswith("0x") else int(vals[9])
    tail = vals[10]
    # v10 is name[15] then the 16-letter block, as one string with the name's
    # trailing NUL trimmed by the serialiser -- so split by width, not by space.
    name = tail[:M_NAME_LEN].rstrip()
    stats = tail[M_NAME_LEN:M_NAME_LEN + M_STATS_LEN].encode("ascii", "replace")
    if len(stats) != M_STATS_LEN:
        raise ValueError("v10 %r is %d chars; expected %d name + %d block"
                         % (tail, len(tail), M_NAME_LEN, M_STATS_LEN))
    mid = member_id or ident
    if not mid:
        raise ValueError(
            "the client sends id 0 for itself and no @GameM=/ID= or <PI> was "
            "found in the log to supply one -- pass --id")

    def num(s):
        s = s.strip()
        return int(s, 16) if s.startswith("0x") else int(s or 0)

    # EVERY numeric field, not just the ones we happened to author. `+0x1C` (v7)
    # is the one that matters most and the one build() used to hardcode to 1:
    # measured 2026-08-17 it is TWO halves, `(card level << 16) | flags`, and it
    # read 0x00420001 with cards, 0x00690009 with cards AND a deck, 1 with
    # neither -- i.e. it is precisely the player's eligibility as the room sees
    # it. Publishing a hardcoded 1 told the room the player had nothing.
    fields = {
        "f18": num(vals[1]), "f26": num(vals[2]), "f24": num(vals[3]),
        "stat": num(vals[5]), "time": num(vals[6]), "f1c": num(vals[7]),
        "f20": num(vals[8]),
    }
    return [(mid, name, stats, room, fields)]


def dump(blob):
    serial = struct.unpack_from("<I", blob, SERIAL_OFF)[0]
    nm = struct.unpack_from("<i", blob, MEMBER_COUNT_OFF)[0]
    nt = struct.unpack_from("<i", blob, TABLE_COUNT_OFF)[0]
    print("total %d B, serial = %d (this is what <DR> will report), "
          "%d members, %d tables" % (len(blob), serial, nm, nt))

    shown_m = 0
    for i in range(min(max(nm, 0), MEMBER_SLOTS)):
        o = MEMBER_OFF + i * MEMBER_REC
        mid = struct.unpack_from("<Q", blob, o + M_ID)[0]
        tail = blob[o + M_STR32:o + M_STR32 + 32]
        room = struct.unpack_from("<Q", blob, o + M_ROOM)[0]
        shown_m += bool(mid) and bool(room)
        why = []
        if not mid:
            why.append("id 0 is an empty slot")
        if not room:
            why.append("room 0 -- the accessor COPIES it and the pane still "
                       "draws nothing (measured)")
        print("  member[%3d] id=%#x room=%#x f1c=%#x (lvl %d, flags %d) "
              "name=%r stats=%r stat=%d%s" % (
            i, mid, room,
            struct.unpack_from("<I", blob, o + M_F1C)[0],
            struct.unpack_from("<I", blob, o + M_F1C)[0] >> 16,
            struct.unpack_from("<I", blob, o + M_F1C)[0] & 0xFFFF,
            tail[:M_NAME_LEN].rstrip().decode("cp932", "replace"),
            tail[M_NAME_LEN:M_NAME_LEN + M_STATS_LEN].decode("ascii", "replace"),
            blob[o + M_STAT],
            "  <-- DROPPED: " + "; ".join(why) if why else ""))

    shown_t = 0
    for i in range(min(max(nt, 0), TABLE_SLOTS)):
        o = TABLE_OFF + i * TABLE_REC
        name = blob[o + T_NAME:o + T_NAME + T_NAME_MAX].split(b"\x00")[0]
        block = blob[o + T_BLOCK:o + T_BLOCK + T_BLOCK_MAX]
        kind = block[:1].decode("ascii", "replace")
        why = []
        if not name:
            why.append("no name (+0x18 byte is 0)")
        if kind in ("D", "E", "F"):
            why.append("type %r is a SERVICE endpoint, so it draws no tile "
                       "on purpose" % kind)
        elif kind not in ("A", "B"):
            why.append("type %r is not a type any reader scans for" % kind)
        tri = decode_letters(block[1:4])
        if tri is None:
            why.append("the triple %r is not letters" % block[1:4])
        elif tri + 1 <= 0:
            why.append("the triple decodes to %d, and 0x66D7F skips <= 0" % (tri + 1))
        shown_t += not why
        print("  table [%3d] id=%#x name=%r type=%r value=%s "
              "+0x08=%d +0x0C=%d state=%d%s" % (
                  i, struct.unpack_from("<Q", blob, o + T_ID)[0],
                  name.decode("cp932", "replace"), kind,
                  "?" if tri is None else tri + 1,
                  struct.unpack_from("<I", blob, o + T_F08)[0],
                  struct.unpack_from("<I", blob, o + T_F0C)[0],
                  blob[o + T_STATE],
                  "  <-- DROPPED: " + "; ".join(why) if why else ""))
        print("              +0x28 %r" % block[:T_SET_END + 1])
        s = decode_settings(block)
        print("              panel  Table Restrictions  Password %s   Observe %d"
              "   Card Level %d - %d" % ("On" if s["password"] else "Off",
                                         s["observe"], s["card_min"], s["card_max"]))
        print("                     Rules  Wager %d   Double Up %s   Chance Block %s"
              "   Special Tile %s   Rotating Block %s   Time Limit %s"
              "   Quitting a Game %s"
              % (s["wager"], "On" if s["double_up"] else "Off",
                 "On" if s["chance_block"] else "Off",
                 "On" if s["special_tile"] else "Off",
                 "On" if s["rotating_block"] else "Off",
                 s["time_limit"], s["quitting"]))
        print("                     Comment preset %d (Lobby.BIN %d)"
              % (s["comment"], 385 + s["comment"]))
        if s["card_max"] < s["card_min"] or s["card_min"] > 0xFFFFF:
            # ASCII on purpose: this fires on the blob we are actually serving
            # today, and a cp1252 console must not die on the way to saying so.
            print("              !! THAT IS A CARD LEVEL RANGE NOBODY SATISFIES -- "
                  "the table cannot be reserved at, and block[30..48] is not "
                  "letters. Rebuild it: the fields do not stop at [29].")

    print("Tetra Master will list %d of %d members and %d of %d tables."
          % (shown_m, max(nm, 0), shown_t, max(nt, 0)))


#: WARNING: ONLY THE FIRST FOUR TABLES REACH THE CLIENT TODAY. `docker-compose.yml` sets
#: `POL_RESOURCE_PAYLEN` with `b/g/PTL=24588`, which is `0x5850 + 4*104 + 4` --
#: the header and exactly four table records. It is the Jan track's, it is
#: deliberate (a PS2 reply declared at the reader's full buffer size truncates on
#: the wire and hangs the console), and the path is shared. Sixteen tables, which
#: is what SE served per room, is 24276. `build()` warns rather than silently
#: authoring rows nobody will see.
#:
#: THE SWEEP, round three. Round one varied every field at once and still put
#: rows on screen; round two varied one field each off an all-zero baseline. What
#: they settled, between them:
#:
#:   * the `value` triple is drawn as the TABLE NUMBER on the tile
#:   * the tile LABEL is a function of the state byte ALONE -- +0x08 and +0x0C
#:     went from 2/4/6/8 to 0 with no change to any label
#:   * states 0, 1 and 3 all render "Setting Up"; state 2 renders "Playing" and
#:     asks for a password. Reserving at any of them is refused with Lobby.BIN
#:     337 "You cannot make a reservation with current table status", which
#:     `0x4BD67` raises when the reservation call `0x841E0` returns exactly 0.
#:
#: So "Open Table" (`Lobby.BIN` 18), the state SE's own screenshots show for a
#: free table, is a value we have not served. This round takes the four slots we
#: have and spends them on 4, 5, 6, 7 -- the rest of Janhourou's 0..7 enum.
#: Round three answered it: STATE 7 IS "Open Table". Confirmed on screen -- the
#: tile went from "Setting Up" to empty -- so the label is state 2 -> Playing,
#: 7 -> Open, and 0/1/3/4/5/6 -> Setting Up.
#:
#: WARNING: AND AN OPEN TABLE IS STILL NOT JOINABLE. Reservation and "play COM" both
#: render GREYED (not refused -- the earlier Lobby.BIN 337 came from trying a
#: non-open table), which is the same conclusion reached for the room rows arriving one
#: level down: it is player ELIGIBILITY, and the player has no cards.
#:
#: WHICH IS WHY THREE OF THESE FOUR SLOTS ARE NOW SERVICE ENDPOINTS. See "THE
#: TABLE LIST IS ALSO THE SERVICE DIRECTORY" above. They draw no tile, so the
#: table screen still shows exactly one row -- the open table -- and the client
#: finally has somewhere to send `@CardReq=`.
SERVICE_ID = 0x000000384EA5822C     # the `/Shm=` peer id, confirmed live by @Init

#: VERIFIED: THE VS. COM GATE, READ END TO END (2026-08-20, static off TM.dll).
#: `Lobby.BIN` 443 is the Table Menu's "VS. COM" row, built at rva `0x457D0` as
#: one of eight 20-byte entries (Y=0x2d, action `0x3001158`). FOUR things must
#: hold before the client will let you press it, and this server satisfied NONE:
#:
#:  1. THE TABLE'S STATE must be 0. The variant selector at `0x45ABC` picks one
#:     of five menu builds off the state; variants 1-4 (`0x46176`, `0x4684A`,
#:     `0x46BED`, `0x46F76`) each `push 1; call 0x18CEA0` -- grey -- with help
#:     code 0x13, "This command cannot be executed." ONLY variant 0 (state 0)
#:     reaches the tests below.
#:     WARNING: AND THAT COLLIDES WITH an earlier live measurement, which read authored 7 as SE's
#:     deterministic empty tile. Left at 7 here on purpose: a live measurement
#:     outranks a static read, and one variable at a time. See
#:     `tmroom.EMPTY_TABLE_STATE`. So VS. COM stays greyed with 0x13 until this
#:     is settled -- everything else below is what makes settling it possible.
#:  2. THE TABLE'S TYPE LETTER must be 'A'. The panel loop copies `block[0]`
#:     into the scene's table model -- `0x66D9D` reads `[esi-0x26]` (esi is
#:     wire_rec+0x4E, so that is `+0x28` = block[0]) and `0x66DA3` stores
#:     `[esp+0x8d4]`; the staged record base is `[esp+0x2c]` (`0x6739B`, inserted
#:     at `0x673A8`), so that slot IS `record+0x8A8` -- and `0x45D70` compares it
#:     to `'A'`.
#:  3. THE ROOM'S LETTER (`0x52454FC`, `0x45D78`) must be 'A'. UNTESTED: nothing
#:     has ever reached this compare, because (2) fails first.
#:  4. FIVE CARDS (`0xB8B30(collection, 0x406) >= 5`, `0x45D94`).
#:
#: (2) or (3) failing raises help code 0x19 = `Lobby.BIN` 454, "You cannot play
#: against the computer in this room."; (4) raises 0x17 = 446. The code->string
#: map is the jump table at rva `0x48468`, indexed with NO bias by `0x47A59`.

#: VERIFIED: AND 'E' IS THE VS. COM SERVER -- the menu row was only half the problem.
#: Passing the gate runs the picker at rva `0x761F0`:
#:
#:     0x76235  cmp byte [0x521bb98], 0x45   ; count the 'E' entries
#:     0x76252  je  0x76660                  ; ZERO of them -> give up
#:     0x76260  call 0x1E2E44                ; rand() -- pick one at RANDOM
#:     0x76338  cmp dword [rec + 0x0C], 8    ; is that one seated >= 8 ?
#:     0x7633F  jge -> refused (returns 0/0)
#:     0x76341  eax/ecx = rec+0x00/+0x04     ; its u64 id is the DESTINATION,
#:                                           ; -> 0x8BB30, as 'F' -> 0x8B720
#:
#: So an 'E' row is a COM GAME SERVER, not a tile (`0x8608B` draws only 'A'/'B'),
#: chosen at random for load balancing and skipped when full. That places the two
#: shipped strings nobody could place before: `Title.BIN` 98 "No VS. COM tables
#: available." (no 'E' entry at all) and `Playmes.BIN` 121 "The VS. COM server is
#: full." (every 'E' entry at +0x0C >= 8).
#:
#: WARNING: THIS RETRACTS the older note that read `0x76338`'s ">= 8" as a PLAYABLE
#: table's seat ceiling, and with it "every open table we served had NO SEATS".
#: That compare only ever runs on the 'E' entry the picker chose. What
#: `+0x08`/`+0x0C` mean on an 'A'/'B' row is still UNREAD -- `+0x08` must be
#: non-zero (below), and `+0x0C` has exactly one reader in the image: this one.

#: SE SERVED 16 PLAYABLE TABLES PER ROOM (period evidence, 2026-08-20). The client's own
#: ceiling is TABLE_MAX (64 rows), so 16 is arithmetic, not protocol -- but the
#: WIRE has to carry them: `POL_RESOURCE_PAYLEN` must be at least
#: TABLE_OFF + n*TABLE_REC + 4, and `build()` prints the figure when it is not.
PLAYABLE_TABLES = 16


def playable_table_id(number):
    """A stable 64-bit id for the table NUMBERED `number` (1-based).

    WARNING: AUTHOR THE ID, DO NOT LET IT BE DERIVED FROM THE ARRAY INDEX.
    `tmroom._normalise_table_ids` rewrites any id below `TABLE_ID_MIN` (0x10000)
    to `tmroom.canonical_table_id(ARRAY INDEX)` -- so with the ids left as 1..16
    the whole set SHIFTS the moment a row is inserted ahead of them, which is
    precisely what putting the service endpoints first just did (`#TM0T001` went
    from `0x2100001001` to `0x2100003003`). Live rows and `tm-roster.json` are
    keyed by NAME and would then re-overlay their OLD id onto the new fixture,
    leaving the set internally inconsistent for no reason.

    This reproduces `canonical_table_id(number - 1)` exactly, so tables 1..3 keep
    the ids this server has already been serving. Keep the magic in step with
    `tmroom.TABLE_ID_MAGIC`; both halves are non-trivial on purpose, because the
    client compares the two 32-bit halves separately.
    """
    return 0x0000002100000000 | (int(number) << 12) | int(number)

#: WARNING: SERVICE ENDPOINTS FIRST, AND THAT ORDER IS LOAD-BEARING for as long as
#: `POL_RESOURCE_PAYLEN` cuts `b/g/PTL` at four records: everything past the cut
#: never reaches the client, so a service row at the END is silently dropped and
#: the card shop loses `@CardReq=` (`0x9A13C` finds no 'F'). Nothing reads a
#: table's ARRAY INDEX -- the tile's number comes from `block[1..3]`, live rows
#: match by NAME (`tmroom.fixture_table`), `tmroom.canonical_table_id` derives
#: ids from the index consistently, and `0x7F9CB` scans for the first slot whose
#: id is ZERO (a free-slot finder, not "the first real table") -- so putting them
#: first costs nothing.
#: WARNING: BOTH SERVICES CARRY THE SAME `SERVICE_ID`, which is what `--shm` already
#: assumes (it rewrites F/D/E alike). The scans are BY TYPE and each takes its
#: own entry's id, so sharing one peer is consistent -- but it is inherited from
#: that design, NOT independently verified against a real SE capture.
#: VERIFIED: FREE TABLES ARE AUTHORED STATE 0, AND IT IS MEASURED LIVE (2026-08-21).
#: Condition (1) of THE VS. COM GATE above needs state 0: the Table Menu variant
#: selector at `0x45ABC` gives variants 1-4 -- each `push 1; call 0x18CEA0`, grey,
#: help code 0x13 = Lobby.BIN 428 -- for every other state, and ONLY variant 0
#: reaches the VS. COM tests at all.
#:
#: THE FLIP-FLOP ENDS HERE, ON A LIVE READ. 7 -> 0 shipped, was
#: retracted on a measurement of "Setting up" appearing on a free
#: table, and is now RESTORED because live testing drove it end to end on
#: 2026-08-21: state 0 reaches menu variant 0, tables render the same as they did
#: at 7, and **a state-0 table still RESERVES -- Lobby.BIN 337 ("You cannot make
#: a reservation with current table status") did NOT fire**, which was the single
#: risk that had never been re-measured.
#:
#: WHY THE RETRACTION WAS WRONG. It blamed state 0 for a "Setting up"
#: tile, but the whole-function read (`0x671DD`-`0x67395`) says states 0 and 7
#: render BYTE-IDENTICALLY for a free row that is not yours; they diverge only on
#: the MY-TABLE arm -- which was firing wrongly at the time because every room's
#: table 1 carried the same id (`0x2100001001`). The room fold in
#: `tmroom._normalise_table_ids` fixed that separately, and with it gone the
#: original symptom does not reproduce.
#:
#: WARNING: "Setting up" ON A RESERVED TABLE IS A DIFFERENT BUG AND IS STILL OPEN.
#: Reserving moves a table to state 1, whose OWNER tile arm `0x672E4` reads the
#: reservation count `[0x51def2c]` (= `screenobj+0x108`) and renders display code
#: 2 -- "Setting up", owner locked out -- while that slot is 0. Tables authored 7
#: hit the identical arm once reserved, so this is NOT the authored state. It is
#: the start-game gate's `+0x108` thread: the lock is meant to be momentary and
#: sticks because our reply never fills the slot.
#:
#: WARNING: AND DO NOT RESERVE THE TABLE YOU WANT VS. COM ON. Reserving takes it
#: 0 -> 1, and state 1 is menu variant 2, which greys VS. COM again. VS. COM is a
#: menu action on a FREE tile.
#:
#: TO REVERT: set this back to 7 and re-run `tools/tmptl.py --template` -- the
#: fixture is the artifact, and `tmroom.EMPTY_TABLE_STATE` must move with it.
FREE_TABLE_STATE = 0                    # menu variant 0, the only ungreying arm

DEFAULT_TABLES = [
    # id, +0x18 name, type, number, block label, +0x08, +0x0C, state[, +0x10]
    (SERVICE_ID, "#TM0COM",  "E", 1, "", 0, 0, 0),   # 0x76235 -> the COM server
    (SERVICE_ID, "#TM0CARD", "F", 1, "", 0, 0, 0),   # 0x9A13C -> @CardReq=
] + [
    # WARNING: +0x08 IS CAPACITY and must be non-zero. Zeroing it (serial 17) greyed the
    # WHOLE Table Menu including Table Members, which had worked at serial 15
    # where table 1 carried +0x08=8.
    # WARNING: No labels: `label` writes [4..13] and [6..13] is the PASSWORD field,
    # so a readable tag there sets a password nobody can type. See table_block().
    # TYPE 'A', NOT 'B' -- see THE VS. COM GATE above. A whole-image scan found
    # no consumer of a playable table's letter besides the tile/number decoder at
    # `0x86074`, which takes 'A' and 'B' on the SAME branch (`0x8608B`/`0x86093`),
    # and `0x45D70`. So the letter costs nothing on screen and buys condition (2).
    # STATE is `FREE_TABLE_STATE` = `tmroom.EMPTY_TABLE_STATE`, now 0 -- see
    # FREE TABLES ARE AUTHORED STATE 0 above.
    (playable_table_id(n), "#TM0T%03d" % n, "A", n, "", 8, 0,
     FREE_TABLE_STATE)
    for n in range(1, PLAYABLE_TABLES + 1)
]

#: What the wire can carry, so `build()` can say so instead of the screen saying
#: it for us. Keep in step with POL_RESOURCE_PAYLEN.
PAYLEN_TABLES = (24588 - 4 - TABLE_OFF) // TABLE_REC      # 4

#: THE ROOM THESE MEMBERS ARE IN, and it is not optional -- see `build()`. The
#: file is per-MEMBER, not per-room, so this is the one room whose Members pane
#: can be populated at a time; `#TM0R001` is `0x0000002000000001`, the value the
#: client's own `<DE>` reported while standing in Freewheeler Room 1.
DEFAULT_ROOM = 0x0000002000000001

#: Three members: our own `<DE>` block verbatim, and two authored ones whose
#: length letters differ. WARNING: The ids are deliberately NOT room-shaped any more --
#: using `0x20000000NN` for both a member id and a room id is how `+0x08` being
#: unset went unnoticed through four rounds of looking at an empty pane.
#: WARNING: SUPERSEDED FOR THE LIVE PATH. `services/tmroom.py` now builds the member
#: half from the roster the clients themselves send, so
#: these rows reach a player only if someone authors a blob by hand and serves
#: it. `MEMBER-B` / `MEM-C` were fixture names doing exactly that, and they are
#: gone: a hand-authored blob is a debugging aid and should LOOK like one.
DEFAULT_MEMBERS = [
    (0xAB12CEB20D9067C4, "Lex", b"DAA0AAABABAADIAB"),   # a real member's guid, kept as the fixture
]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="write the blob here")
    ap.add_argument("--dump", help="decode an existing blob instead")
    ap.add_argument("--serial", type=int, default=7,
                    help="goes to +0x40 and comes back on the <DR> poll")
    ap.add_argument("--from-log", metavar="AUTHSERV.LOG",
                    help="take the member list from the client's own latest "
                         "<DE> in this log -- the only way to publish a player "
                         "as they currently ARE rather than as they were")
    ap.add_argument("--id", type=lambda s: int(s, 16),
                    help="the player's member id, hex (the client sends 0 for "
                         "itself); default is whatever @GameM=/ID= last said")
    ap.add_argument("--shm", type=lambda s: int(s, 16),
                    help="the service endpoints' peer id, hex (default %016X); "
                         "the `/Shm=` value @Init is addressed to"
                         % SERVICE_ID)
    ap.add_argument("--template", action="store_true",
                    help="write the SHIPPED fixture: the table half only, no "
                         "members, serial 1, to services/tmdata/b_g_PTL.bin "
                         "unless --out says otherwise. This is what a member "
                         "with no stored blob is served, and `tmroom.build_ptl` "
                         "fills its member half in from the live roster")
    a = ap.parse_args()
    if a.shm is not None:
        DEFAULT_TABLES = [(a.shm,) + t[1:] if t[2] in ("F", "D", "E") else t
                          for t in DEFAULT_TABLES]
    members = DEFAULT_MEMBERS
    if a.template:
        # NO MEMBERS ON PURPOSE. The fixture answers EVERY member who has no
        # stored copy, so a baked-in row would put a phantom player in every
        # room on the server -- and the member half is generated now anyway
        # (`tmroom.build_ptl`). Serial 1 for the same reason: `build_ptl` stamps
        # the room's own sequence over it whenever we know the room.
        members = []
        if a.serial == ap.get_default("serial"):
            a.serial = 1
        if not a.out:
            a.out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 os.pardir, "services", "tmdata", "b_g_PTL.bin")
    if a.from_log:
        members = from_log(a.from_log, a.id)
        if not members:
            print("no <DE> in %s yet -- the client sends one per room entry"
                  % a.from_log)
            raise SystemExit(1)
        print("from <DE>: id=%#x room=%#x name=%r block=%r"
              % (members[0][0], members[0][3], members[0][1],
                 members[0][2].decode("ascii", "replace")))
    if a.dump:
        with open(a.dump, "rb") as f:
            dump(f.read())
    else:
        blob = build(members, DEFAULT_TABLES, a.serial)
        dump(blob)
        if a.out:
            with open(a.out, "wb") as f:
                f.write(blob)
            print("wrote %s (%d B)" % (a.out, len(blob)))
