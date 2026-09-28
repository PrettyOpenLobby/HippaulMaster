"""The TM0 game channel: message codes, the E-body header (encode_code, decode_ebody) and the
capture-log summaries.
"""
import os
import struct
from enum import Enum


CONTENT_ID = 2
CONTENT_NAME = "TetraMaster"


# --- TM0 game-channel text protocol -----------------------------------------
#
# REVERSED 2026-08-13 from the decrypted PS2 module `TMaster.pex` (base
# 0x00280000), corroborated by the live capture in logs/authserv.log. Tetra
# Master rides the SAME auth-band IRC envelope Janhourou uses (see
# jan-world-protocol / responders._game_notice_reply):
#
#     NOTICE <peer> :G TM0 G <E-body> <4-char checksum> CRLF
#
# The class char after the 3-char tag is 'G' (same as Janhourou's binary class),
# but the body is NOT janwire's 'B' record -- it is TM's own text format:
#
#     <8 hex chars> <command...>
#     ^ message code, a u32 serialized LITTLE-ENDIAN then hex (upper). The
#       serializer is TMaster.pex 0x0029bd70 (nibble-hex via table 0x0043cd30);
#       the request builder at 0x00298b30 passes code 225 (0xE1), which is why
#       the captured opener reads `E1000000@TeachDV=`.
#
# THE OPENER -- "retrieving default data":
#   request  code 225  `E1000000@TeachDV=`     builder  TMaster.pex 0x00298b30
#   reply    code 226  `E2000000@TeachDVAns/D=<d>/V=<v>`   parser  0x00298dc0
#
# The parser (0x00298dc0) requires the message to present code 226 to the
# message reader (0x0029a880), then strstr's `@TeachDVAns` and extracts the
# integer fields `/D=` (stored as a BYTE at 0x005b7ca8) and `/V=` (a HALFWORD at
# 0x005b7cb8). Both are initialised to the sentinel 255 ("unknown") at
# 0x00298814 and overwritten by our answer. The client then uses D and V to
# version-check the default card/rule data files it reads over the file-transfer
# service (`sqMgCpReadFile` at 0x003aad50, filenames `b/g/TM0SML`, `b/g/TM0SML2`,
# `b/g/TM0IML` -- see 0x004864a8). Answering TeachDV clears the FIRST timeout
# ("trm-0-37312 timed out when retrieving default data"); the file read is the
# next layer and will now surface in the logs.

# --- THE SEQUENCE, as of 2026-08-15. Each step measured, not inferred. -------
#
#   @TeachDV=            -> @TeachDVAns/D=0/V=0        (D/V are read PARAMETERS,
#                                                       not a client switch; the
#                                                       "non-zero unlocks a fetch"
#                                                       hypothesis is REFUTED)
#   b/g/TM0IML           -> a manifest with count=1, entry[0].flag=1 and a
#                           NON-ZERO entry[0].u64. All three are required: a zero
#                           u64 never produced @Init. Proven by a sentinel u64
#                           surfacing at [0x005d15e0], which 0x004239cc writes
#                           only on a flag match.
#   @Init=/NN=.../GID=.. -> @InitAns/Ans=<positive>    (code 129; parser
#                                                       0x003a9240; caller blez's
#                                                       the returned /Ans=)
#   class P <CR>...      -> ??? THE CURRENT WALL
#
# THE WALL, named from the screen and from the module:
#
#   error 0-37246 = "player data timed out while SAVING"
#   (プレイヤーのデータを保存中にタイムアウトしました)
#
# So `<CR>` on the POLpro P channel is the player-profile SAVE, not a read. It
# carries GID, the content id, and card state ("Card Level 0"). The API is
# sqMgPfcSetCharacterProfile / ...Check; the Check is 0x003aa8f0 -> 0x00417518,
# which polls a state object at **0x004755e0**: `+0xb8` is the in-flight flag
# (0 = never started -> -8704 "pfc.c:424", 1 = still pending) and `+0xbc` is the
# result. Both are readable from a savestate -- that is the anchor for the next
# reading.
#
# Our reply is currently the polpro `*` wildcard, echo + <OK>(0). It goes out
# (confirmed: `polpro P ->` in authserv.log) and the client then sends NOTHING
# and times out. **This is the SAME failure Janhourou has on `<PG>`**
# (the Janhourou save-data reading: "our echo+<OK> reply reaches the sqMg gateway but
# does not satisfy it"). One protocol, one bug, now with two independent test
# cases -- worth solving jointly with whoever holds Jan.
#
# WARNING: THE ON-SCREEN NUMBER STILL CANNOT BE DECODED FROM THE 396-RECORD TABLE at
# 0x0072f0f4. 37088 and 37312 are in it; the codes actually displayed -- 37246
# and 37092 -- are not. Do not treat that table as the error list. The screen
# text is the reliable decode, and a screenshot gets it in one step.

# --- 2026-08-15 LIVE RESULT: <PR> and <CI> BOTH REJECTED. It is not the code. --
#
# WARNING: SUPERSEDED -- IT *WAS* THE CODE. See "THE ANSWER" at the end of this block:
# the id<->code table was misread by one, so <PR> and <CI> were never the codes
# this section believes they were. The live measurements below all stand; only
# the names attached to the numbers were wrong.
#
# Two replies tried against the real client, both delivered (confirmed
# `polpro P ->` in authserv.log), both leaving the pool state UNCHANGED at 1:
#
#   <PR>(0)   wrong sibling -- 82 is the PROFILE handler, and the savestate
#             showed profile state [+0xb8]=0 (never started) while pool
#             [+0xb0]=1 (pending). My error.
#   <CI>(0)   the POOL handler's own success code (75), read off
#             pfcCharaPoolResult 0x004175c8. Still [+0xb0]=1 afterwards.
#
# The code model itself checks out: 0x0040bd30 -- whose return the handler
# compares against 75/76 -- does read the 102-code table at 0x0045eb04, so it is
# returning a code id as assumed. The handler simply never took its branch.
#
# SO THE GAP IS DISPATCH, NOT CONTENT. Nothing has established HOW a class-P
# reply reaches pfcCharaPoolResult at all -- whether a callback is registered per
# request, whether the reply must carry a transaction/sequence field we are not
# echoing, or whether the target nick in the NOTICE matters (each TM0 message so
# far used a DIFFERENT peer id: TeachDV USH6MZJA7, @Init UOADRAHW1, <CR>
# UUIRT71SR). That is the next thing to reverse, and it is offline work.
#
# DO NOT try a third code without it. Three console launches have now been spent
# on reply CONTENT, and the pool state has never once moved off 1 -- which is the
# signature of a reply that is not being consumed at all, not of a wrong value.

# --- 2026-08-15: FOUR THEORIES TRIED, ALL REFUTED BY MEASUREMENT. STOP HERE. --
#
#   <OK>  (the polpro wildcard)  -> maps to type 44, served by no pfc handler
#   <PR>  (82)                   -> type 36 = the PROFILE handler, whose state is
#                                   0, so it errors -8704. Wrong sibling.
#   <CI>  (75)                   -> type 33 = the POOL handler. CORRECT code,
#                                   correct route. No effect.
#   <CI> delayed 1500 ms         -> no effect either.
#
# The delay tested a real race read off the client: 0x00417254 sets the pending
# flag AFTER the send returns, and pfcCharaPoolResult drops anything arriving
# while state != 1. The mechanism is real; it is not what is happening here.
# [0x004755e0+0xb0] stayed 1 across both runs.
#
# WARNING: AND A CORRECTION TO THE EARLIER CLAIM. The earlier reading said the reply was "delivered and
# consumed" because its bytes sit in the client's message ring at 0x0045a630.
# That was wrong. The ring reads **read == write == 3** with our `<CI>` sitting in
# slot 3 -- i.e. slot 3 is the NEXT slot to write, so the entry is NOT committed.
# Entries 0..2 (which include @TeachDVAns and @InitAns, both of which worked) were
# written AND committed. So our reply is staged and then backed out.
#
# The consumer does exactly that: on a gate failure it branches to 0x00417080 and
# the path through 0x00417078 writes the WRITE index back (`sw v0, 0xa4(s3)`),
# compacting the rejected entry out. Which means a gate DID fail --
#   gate 1  0x0040e020 : byte at msg+0x1d must be 'G'
#   gate 2  0x00418238 : byte at msg+0x21 must be 'P'
# -- even though the ring dump shows our entry as `.....GTM0P<CI>.0`, i.e. 'G' at
# +0x1d and 'P' at +0x21, which should pass both. That contradiction is the whole
# remaining question and it could not be resolved here.
#
# NEXT PERSON: do not try a fifth reply code. Work out why a gate rejects an entry
# that visibly satisfies both byte checks -- most likely the gates read a
# DIFFERENT buffer than the ring slot we are reading (the staging buffer, at a
# different offset), so the bytes we are inspecting are not the bytes being
# tested. Find the producer that fills the slot and confirm the offsets against
# a message that DID commit (@InitAns, entry[2]) rather than against ours.

# --- THE ANSWER, 2026-08-15: THE CODE TABLE WAS READ OFF BY ONE. -------------
#
# No gate ever rejected anything. The reply was accepted, dequeued, and thrown
# away because `<CI>` is not the code the handler wants -- `<CS>` is.
#
# THE MISREAD. The 102-code table is 8-byte records **[u32 id][2-char code][2
# pad]** -- id FIRST. The notes above read it as {code, pad, id} and so paired
# every code with the id of the record BELOW it. Every code we derived was +1:
#
#     believed        actual
#     CI = 75         CI = 74   (75 is CS)
#     PR = 82         PR = 81   (82 is GK)
#     OK = 89         OK = 88   (89 is OG)
#
# Read off the code three independent ways that agree -- not off a hex dump:
#   1. RESOLVER  0x0040bd30 walks the codes at 0x0045eb04 + i*8 and returns the
#      id with `lw v0, -4(v1)`, i.e. from code_ptr MINUS 4.
#   2. BUILDER   Janhourou's group builder 0x002f32e0 materialises the code
#      string as `0x003f146c + id*8` -- the same pairing, from the other side.
#   3. WIRE      TMaster's own request builder passes 73 (0x004171d4) and 74
#      (0x004171e4) for the two groups the captured request shows as <AN><CI>.
#
# And `polpro.py`'s TAGS/TAG_ID was GENERATED from the module and had CI=74 /
# CS=75 correct the whole time. The bug lived only in prose. Trust TAG_ID.
#
# WHY EVERY ATTEMPT FAILED, all four from the one cause. The type step
# (0x00418188) maps id-72 through a sparse 26-entry jump table at 0x0048da40;
# ids with no arm get no type and are dropped:
#
#     CI 74 -> NO ARM        consumed and discarded  <-- what we kept sending
#     PR 81 -> NO ARM        consumed and discarded
#     OK 88 -> type 43       a real handler, but not the pool one
#     CS 75 -> type 33 -> pfcCharaPoolResult 0x004175b8, whose 75 branch does
#                         [0x004755e0+0xb4] = old state; [+0xb0] = 2
#                         which IS the completion the poll is waiting for.
#
# THE CHAIN CLOSES, verified offline end to end. `sqMgPfcSetCharacterPoolCheck`
# (0x00417290 -- the POOL sibling; 0x00417518 is the PROFILE one and reads
# +0xb8/+0xbc, do not confuse them) polls exactly the two fields the handler
# writes:
#     s1 = [+0xb0]  state      s0 = [+0xb4]  result
#     s1 == 0  -> -8703 at pfc.c:286  "never started"
#     s1 == 1  -> return 0            still pending  <-- every run so far
#     s0 >= 0  -> return s0           SUCCESS
# Our <CS> leaves state 2 and result 1, so the poll returns 1 and the caller's
# blez takes the success path. Nothing after this is inferred.
#
# THE RING READING WAS WRONG TOO, and this is the reusable part. `read == write`
# does NOT mean "staged and backed out" -- it means the entry WAS dequeued:
#   * consume path: compacts the following entries down and DECREMENTS write
#     (0x0041707c), so a consumed last entry leaves read == write with its bytes
#     still in the slot (nothing is cleared);
#   * reject path (0x00417080): advances only a LOCAL cursor and leaves write
#     ahead -- it never stores the write index at all.
# So the observed read == write == 3 with our bytes in slot 3 was proof the
# message was ACCEPTED. Both gates passed, as the bytes said they should. The
# contradiction that closed the earlier reading was an artefact of reading the
# reject path as the writer of [+0xa4].
#
# Also measured while settling this: the ring is ONE shared queue with FOUR
# pumps, one per POLpro class byte -- 0x0040ebc8 'L', 0x00411810 'A',
# 0x00416460 'R', 0x00416e78 'P' -- so nothing competes for a class-P reply.
# And gate 1 has a second half nobody had noticed: msg+0x1e..0x20 must equal the
# RUNTIME tag at 0x0045a618 ('TM0'), not just the 'G' at +0x1d.
#
# FIX: config/polpro.json now answers <CR> with <CS>. It is mtime-reloaded, so
# it is live with no restart. WATCH [0x004755e0+0xb0]: 1 = still waiting,
# 2 = satisfied. Anything else means read this block again, not a new code.
#
# The same off-by-one had reached Janhourou -- its deployed <PG> answer <PR> is
# id 81 with no arm. Its handler's constants are 82/85 = <GK>/<GG>; corrected in
# polpro.json and flagged to that worker in STATUS.

MSG_TEACHDV = 225        # request  (client -> us)
MSG_TEACHDVANS = 226     # reply    (us -> client)
MSG_INIT = 128           # request  `@Init=/NN=.../CN=.../GID=.../L=0`
MSG_INITANS = 129        # reply    `@InitAns/Ans=<n>`; parser 0x003a9240
MSG_SHREQ = 176          # request  `@ShReq=/NN=.../CN=...`  (Player Data -> Cards)
MSG_SHENTER = 177        # reply    `@ShEnter=/EN=<n>`; handler TM.dll 0x88E06
MSG_OPT = 132            # request  `@Opt=/NN=.../HNSS=<n>`; builder TM.dll 0x88390
MSG_OPTANS = 133         # reply    `@OptAns=/Ans=<n>`;      parser  TM.dll 0x889B0

#: THE CARD SHOP'S OPENING MESSAGE -- and the reason every earlier attempt at it
#: looked like silence. Measured 2026-08-16 in the unpacked `TM.dll`.
#:
#: **THE 8-HEX HEADER CARRIES TWO FIELDS, NOT ONE.** The message store keeps each
#: line's text at `rec+0x25`, and `0xA9780(rec, off)` parses TWO ASCII hex chars
#: at `rec+0x25+off` into a byte. The matcher `0x81CB0` tests exactly two of them:
#:
#:     0xA9780(rec, 0) == code     <- header chars 0..1  (u32 byte 0)
#:     0xA9780(rec, 4) == msgid    <- header chars 4..5  (u32 byte 2)
#:
#: So a wait is on a (code, msgid) PAIR. Everything we have answered so far had
#: msgid 0 by accident, because `encode_code` packs a u32 and we only ever set
#: byte 0 -- which is why `encode_code(0x001300B2)` is all it takes to address
#: byte 2. Nothing new is needed on the wire.
#:
#: **This retires the earlier "0xB2 produced a screen indistinguishable from serving
#: nothing".** 0xB2 was never the wrong code; it was served with msgid 0 while
#: the client waits on msgid 0x13, so `0x81CB0` never matched it and the message
#: sat in the store unread.
#:
#: The waiter is `0x101A10(msgid)` -- a thiscall on `0x52463E0` that polls
#: (0x43, msgid), then (0xB2, msgid), then (0xA2, msgid), and dispatches through
#: three jump tables. **The card shop's three waits are the only `0x101A10` call
#: sites inside the shop module** (rva 0x117000..0x11C000):
#:
#:     0x11B5A9  msgid 0x13  -> arm 0x105201, logs `---->Recv=SHOPINIT`
#:     0x11A64E  msgid 0x14  -> arm 0x105E85, right after the screen prints
#:                              Cardsh.BIN record 62 `Transaction in progress...`
#:     0x11C012  msgid 0x17  -> arm 0x105A97
#:
#: and each resolves under code 0xB2 (their 0x43 table entries are the
#: unhandled-default target, 0x106CCD).
#:
#: Arm 0x105201 parses the body -- the key `@Init` (a map key, so it needs the
#: `=`, per the PC delimiter note above), then six integer fields:
#:
#:     /N=   -> byte at obj+0x30      /RA=  -> dword at obj+0x04
#:     /M=   -> ...                   /CP=  -> ...
#:     /S=   -> word at obj+0x00      /SP=  -> word at obj+0xEC
#:
#: WARNING: THE FIELD NAMES AND THE ORDER ARE MEASURED; THE VALUES ARE NOT. They are all
#: env-overridable so the meanings can be A/B'd without a rebuild. `/M=` is very
#: likely Money (Cardsh.BIN has `Money`, `Average Rank`, `Card Level`, `Page`),
#: and it defaults to a probe value rather than 0 so a pack is affordable and the
#: msgid-0x14 transaction path can be exercised on the same launch.
MSG_SHOPINIT = 0x001300B2   # code 0xB2 + msgid 0x13, packed for `encode_code`
#: The buy reply's own header, for pushing `@Card=` unsolicited. Taken from the
#: live wire (`B2001401`), so the trailing 0x01 is the SHOP NUMBER the handler's
#: second equality gate compares -- not padding.
MSG_SHOPCARD = 0x011400B2
MSG_CVREQ = 0x70         # request  `@CvReq=`  -- THE PRIZE CENTER, inside Rankings
MSG_CVENTER = 0x71       # reply    `@CvEnter=/EN=<n>`; parser TM.dll 0x8B490
MSG_CARDREQ = 0x90       # request  `@CardReq=`   (builder TM.dll 0x8B7C0)
MSG_CARDENT = 0x91       # reply    `@CardEnt=/Ans=<n>`; handler 0x8BA1E

#: THE EVENT CHAIN. All four are read off TM.dll and none has ever been seen on a
#: wire -- the event branch has never opened here, so
#: these are a specification, not a measurement. The two
#: that matter are that `@EInitReq=` is what the Event Shop's card machine sends
#: (0x9A1C8) and `@EventTimeReq=` is what BOTH event pumps block on.
#:
#: `@EventTimeReq=` is the one message in this family whose reply carries a msgid
#: rather than only a code: the sender registers its wait as `(0xD1, 5)` at
#: 0x8C1B6 and then sends `(0xD1, 4)` at 0x8C1CF, so the answer has to be packed
#: with msgid 5 or the client never sees it.
MSG_EVENTTIMEREQ = 0x000400D1   # request  `@EventTimeReq=/NN=...`  builder 0x8C190
MSG_EVENTTIMEANS = 0x000500D1   # reply    `@EventTimeReqCheck=/Start=../End=../Now=..`
MSG_EINITREQ = 0x98      # request  `@EInitReq=`   (builder TM.dll 0x8BBD8)
MSG_EINITENT = 0x99      # reply    `@EInitEnt=/Card=<n>/Ans=<n>`; handler 0x8BE34

#: THE TABLE'S MEMBER LIST -- the first message of the MATCH family (0x41) that
#: we can answer, and the first place the msgid rather than the code carries the
#: request/reply direction.
#:
#: Measured live 2026-08-17: standing at a table in #TM0R001 and choosing to view
#: the table's members sends
#:
#:     GTM0G41000300@GameM=/ID=AB12CEB20D9067C4
#:
#: -- code 0x41, **msgid 3** -- and the screen hangs, because `TM0: no answer for
#: this command`. An earlier reading took the family's convention as "reply = request code + 1",
#: which is right for 0xE1/0x80/0xB0/0x90 but WRONG here: the sender at TM.dll
#: `0x83730` builds `(@GameM=, code 0x41, msgid 3)` and the waiter three
#: instructions into the same scene's poll, `0x827F0(buf, 0x41, 4)`, looks for
#: **code 0x41 with msgid 4**. The code stays put and the msgid steps.
#:
#: THE REPLY, field for field, off the parser at `0x83870`:
#:
#:     0xAA920(msg, "@GameML")           the command name -- no '=' on this one
#:     0xAB470("/ID=", msg, 0, &u64)     must EQUAL our own player id, i.e. the
#:                                       `/ID=` the client just sent, or 0x838E3
#:                                       drops the whole message
#:     0xAB080("/Num=", msg, 0, &n)      the row count -> [obj+0xA8]
#:     for i in 0..n-1, each by OCCURRENCE INDEX, into a 0x38-byte row:
#:         /MLID=  u64    row+0x00      0xAB080 pair at 0x839A0/0x839AF
#:         /MLNN=  string row+0x08      0xAAD60, copied at 0x83975
#:         /Po=    int    row+0x18
#:         /Lv=    int    row+0x1C
#:         /Rk=    int    row+0x20
#:         /HN=    string row+0x24      0xAAD60, copied at 0x83A20
#:
#: Four numbers per row against a Member Info panel that shows VS. Rating, Title,
#: Card Level and Guild is a strong hint at the mapping, but it is a HINT: none of
#: the four has been watched on screen yet. `POL_TM_GAMEML_ROWS=1` serves one
#: synthetic row to settle it; the default is 0, which is the honest answer for
#: the empty table this was measured at and cannot mislead a later state machine
#: into thinking somebody is seated.
MSG_GAMEM = 0x00030041   # request  `@GameM=/ID=<u64 hex>`  (builder TM.dll 0x83730)
MSG_GAMEML = 0x00040041  # reply    `@GameML=/ID=.../Num=<n>...`; parser 0x83870
#: THE RESERVATION-LIST reply, same @GameML body at MSGID 0xC. The client has
#: TWO consumers of @GameML: the members parser 0x83830 reads only msgid 4 and
#: fills the members pane (+0x10c); the RESERVATION parser 0x83B80 reads msgid 4
#: FIRST but msgid **0xC** as its dedicated fallback and fills +0x108 -- the
#: count the Start-Game gate 0x5C0D0 requires (>= 2). The members branch runs
#: first (0xA918F before 0xA940C) and the msgid-4 reply never reaches +0x108
#: (proven by a live owner dump 2026-08-21: +0x10c=2 but +0x108=0 with the
#: reservation arm +0x41c set). msgid 0xC is the channel members never touches,
#: so a copy there fills the reservation slot without disturbing the pane.
#: The reservation parser has NO /ID gate (0x83B80 pushes no `/ID=`), so this
#: rides the SAME body; /MLID must be the POL-ID (same id-space as the room
#: member list [0x51DF120] the gate looks each row up in).
MSG_GAMEML_RESV = 0x000C0041


def _resv_gameml_code():
    """The code+msgid to carry the reservation @GameML re-feed.

    WARNING: ROOT-CAUSE CORRECTION 2026-08-22 (STATIC, UNPROVEN LIVE -- disasm of the
    dispatcher poll at 0xA9180, base 0x04F90000). The msgid-0xC channel CANNOT
    fill the owner's reservation slot +0x108, by construction:

      * The per-frame poll runs three arms in order:
          0xA918F  MEMBERS   (gated +0x414): 0x83830 POPS (0x41, msgid 4) -> +0x10c
          0xA92D5  @GameKick (gated on the selected-table id [0x52461d0:d4]):
                   0x84360 POPS (0x41, msgid 0xC) -- store lookups 0x827F0 are a
                   POP (0x81DB0 copies out then re-inits the slot), NOT a peek --
                   and only treats it as a kick if it says @GameKick; a @GameML
                   on 0xC is popped-then-discarded here.
          0xA940C  RESERV    (gated +0x41c): 0x83b80 reads (0x41,4) FIRST, then
                   (0x41,0xC); >0 -> copies blob into +0x108/+0x110.
      * For the OWNER of a reserved table the selected-table gate 0x52461d0:d4 is
        SET, so the @GameKick poll runs and eats the 0xC before the reservation
        arm can read it. msgid 0xC therefore only survives when that gate is 0
        (not at your own table) -- never for the owner.
      * The reservation arm's PRIMARY channel is msgid 4, which the kick poll
        never touches. It is contested only by the MEMBERS arm (+0x414), which is
        armed just for the frames right after "View Table Members" is clicked. On
        a plain @Pong re-feed +0x414 is NOT armed, so a msgid-4 @GameML survives
        the members arm and reaches the reservation arm -> +0x108 fills.

    This also explains the 2026-08-22 live finding (+0x10c filled, +0x108 stayed 0
    with +0x41c armed): the @GameM= reply arrived with +0x414 armed, the members
    arm popped the msgid-4 copy first, and both the members(4) and reservation(0xC)
    copies were then gone before the reservation arm ran.

    WARNING: NOT CONFIRMED LIVE. This is a disassembly hypothesis
    until +0x108 >= 2 and Start Game ungreys on a real owner's screen. Default is
    msgid 4 (the channel the kick poll cannot eat); POL_TM_RESV_MSGID=C restores
    the proven-broken 0xC for an A/B.
    """
    if (os.environ.get("POL_TM_RESV_MSGID", "4") or "4").strip().upper() == "C":
        return MSG_GAMEML_RESV
    return MSG_GAMEML

#: THE RESERVATION'S ANSWER, and the client never asks the question.
#:
#: Decompiled 2026-08-17 after four rounds of `b/g/PTL` experiments failed to
#: un-grey Reservation. The refusal a tester sees is `Lobby.BIN` 337, "You
#: cannot make a reservation with current table status", and it is raised at
#: EXACTLY ONE site in the whole module -- `0x4BD6F`, resolved by disassembling
#: backwards from every caller of the two message functions rather than scanning
#: for the id as an immediate (which produced three false positives: an
#: `new(424)` allocation, a colour component, and a table of card ids).
#:
#: That site shows 337 when `0x841E0` returns exactly 0, and `0x841E0` is:
#:
#:     0x84221  call 0x827F0(buf, 0x41, 6)   look for @GameQA in the message store
#:     0x8422A  jl   0x842B2                 not there -> fall to the tail
#:     0x8426A  push "/QT=" ; call 0xAB080   else read /QT= into [esp+0x10]
#:     0x84344  mov  eax, edi                edi = [esp+0x10], initialised to 0
#:
#: so it RETURNS THE `/QT=` VALUE out of an `@GameQA` message and returns 0 when
#: no such message exists. Its caller treats > 0 as success, < 0 as an error and
#: 0 as 337. The client has never sent `@GameQT=` in the entire log -- so this is
#: not a reply we are failing to give, it is an UNSOLICITED PUSH the server owes,
#: the same shape as SHOPINIT for the card shop and the Prize Center
#: where a scene likewise waits on a message it never requests.
#:
#: WARNING: UNPROVEN ON THE WIRE. The chain above is read, not measured: `@GameQA` has
#: never existed on this server, so "the client parses this" and "the client is
#: satisfied by this" remain different claims. `/QT=1` is the least-committal
#: value that is > 0; if the greying persists, the value is the next thing to
#: sweep, not the mechanism. `POL_TM_GAMEQA=0` disables it.
#: WARNING: AND IT IS NOT A PUSH AFTER ALL -- THE CLIENT DOES REQUEST IT (2026-08-18).
#: Everything above stays true about the CONSUMER; what was wrong was "unsolicited".
#: Once `@GameEN=` is answered (see `MSG_GAMEEA`) the client reaches the table
#: settings screen, and Confirm sends
#:
#:     GTM0G41000500@GameQT=
#:
#: -- code 0x41 **msgid 5**, no fields at all, from either of two builders
#: (`0x83FEF` and `0x84080`, both `0xA97C0("@GameQT=", 0x41, 5, 0, 0)`), each of
#: which drains `(0x41, 6)` immediately before sending. So `@GameQA` is an
#: ordinary reply on the msgid-steps-not-code rule, exactly like
#: `@GameM=`/`@GameML` and `@GameEN=`/`@GameEA`, and the whole reservation family
#: is now consistent.
#:
#: **Unanswered, this is the tester's "Reservations cannot be made with current
#: table restrictions"** -- `0x841E0` finds nothing, returns 0, and `0x4BD6F`
#: renders Lobby.BIN 337. Measured 2026-08-18 04:25:07Z: one `@GameQT=`, one
#: `TM0: no answer for this command`, and that dialog on screen.
#:
#: The push on `@GameM=` is KEPT. It costs nothing (the reply drains the slot,
#: and both carry the same value), and it is what put a `@GameQA` in the store
#: for the run that first sent `@GameEN=` -- so it may still matter to whatever
#: decides the button's greying, which is a different question from this dialog.
MSG_GAMEQA = 0x00060041  # push     `@GameQA=/QT=<n>`; consumer TM.dll 0x841E0

#: THE RESERVATION ITSELF -- `@GameEN=` finally arrives, and it is UNANSWERED.
#:
#: WARNING: THIS CLOSES THE EARLIER HARD FACT AND `MSG_GAMEQA`'s "the client never asks the
#: question". Both were true when written; they are not any more. Measured
#: 2026-08-18 off a second machine's shipped shim log
#: (`logs/uploads/FRAMEWORK-*.log`, `[tm] +821A1 <bf = 41000100@GameEN=...>`) and
#: the matching `authserv.log` lines -- twenty sends across the day, every one of
#: them met with `TM0: no answer for this command -- captured, silent`:
#:
#:     GTM0G41000100@GameEN=/NN=AB12CD56EB0F5932/HID=0/Dm=0/Vol=0
#:                          /CN=/HN=4C6170746F705465737432/L=1
#:
#: So Reservation is no longer greyed -- the unsolicited `@GameQA=/QT=1` above
#: did its job -- and the screen a tester is stuck on ("Retrieving table
#: info...", `tm-xlate` id 28) is the client polling for a reply that never comes.
#:
#: THE REPLY, off the waiter at TM.dll `0x83580` (the function the reservation
#: scene's poll calls; base 0x04F90000, so these are RVAs):
#:
#:     0x835C1  call 0x827F0(buf, 0x41, 2)    code 0x41, **msgid 2** -- and the
#:                                            sender at 0x832DE drains exactly
#:                                            that slot before it sends, which is
#:                                            the same msgid-steps-not-code rule
#:                                            `@GameM=`/`@GameML` follows
#:     0x835ED  0xAA920(msg, "@GameEA")       the command name, no '=' in the
#:                                            literal (as with `@GameML`)
#:     0x8360A  0xAB080("/EN=", msg, 0, &n)   the ONLY field the waiter reads
#:
#: and `/EN=` is a verdict, read three ways at `0x8361B`:
#:
#:     n  > 0   success -- global rva 0x298200 := 0xF, and the scene advances
#:              (`0x8F971 jle` is the failure branch, so > 0 is the whole gate)
#:     n == 0   the waiter substitutes **-99**, which `tm-errors.tsv` renders as
#:              37399 "Timed out while updating table data."
#:     n  < 0   passed through as that error code
#:
#: 1 is not an arbitrary pick above the threshold: `0x90114` compares the stored
#: value to **1** specifically and takes a different branch (posting 0x3000E79 or
#: 0x3000E7A) than any other positive, which reads as "you are the only entrant"
#: versus a countdown at 0x90145. So the number is a state, not a boolean, and
#: `POL_TM_GAMEEA_EN` exists to sweep it.
#:
#: WARNING: MEASURED OFF THE BINARY, NOT YET ON A WIRE. What is measured is that the
#: client asks and we say nothing; what follows a `/EN=1` has never been seen.
#: `POL_TM_GAMEEA=0` restores the silent control for an A/B.
MSG_GAMEEA = 0x00020041  # reply    `@GameEA=/EN=<n>`; waiter TM.dll 0x83580

#: VERIFIED: THE VS. COM SESSION'S FIRST REPLY, and the wall the gate fix uncovered.
#: With the VS. COM row finally reachable (room record `+0x29`, see
#: `tools/tmroomlist.py`), pressing it hangs on **"Preparing game"** and the
#: client re-sends this every ~2 s, measured live 2026-08-22:
#:
#:     NOTICE UGRAWNN3J :GTM0G41001200@GameENC=/NN=<guid>/HID=0/Dm=0/Vol=0
#:                       /CN=/HN=<name as hex ASCII>/L=1
#:
#: `@GameENC=` is the COM-game twin of `@GameEN=`: same field shape, code 0x41,
#: msgid **0x12** (vs 0x01), plus `/CN=`, `/HN=` and `/L=`. Note the PEER --
#: `UGRAWNN3J`, not the room peer -- which is the `'E'` row the picker at
#: `0x761F0` chose, and the first traffic that makes the shared `SERVICE_ID`
#: between our `'E'` and `'F'` rows matter.
#:
#: THE ANSWER IS `@GameECA`, and TM.dll's string pool at `0x2227B4` gives its
#: shape by neighbourhood -- the pool runs
#:     `@GameENC=` `@GameECA` `/TblId=` `/TblNo=` `@MuchMake` `/Ans=`
#:     `@MuchMakeAns=` `/Start=`
#: so `@GameECA` carries **`/TblId=` and `/TblNo=`** exactly as `@GameEA` carries
#: `/EN=`, and the session does NOT end here: `@MuchMake` follows.
#:
#: WARNING: THE MSGID IS INFERRED, NOT MEASURED. Every documented pair in this protocol
#: answers at request msgid + 1 (`@GameEN=` 0x01 -> `@GameEA` 0x02), so 0x12 ->
#: **0x13**. If the client ignores this reply, the msgid is the first thing to
#: sweep -- `POL_TM_GAMEECA_MSGID` does it without a redeploy. A wrong msgid is
#: indistinguishable from silence on this band.
MSG_GAMEECA = 0x00130041  # reply    `@GameECA=/TblId=<id>/TblNo=<n>`; see above

#: VERIFIED: THE JOIN HANDSHAKE, and the reply we have never sent. Decompiled
#: 2026-08-20 after live testing reported "after creating a reservation, the
#: table shows as setting up for everyone, but nothing else can be done".
#:
#: The client SENDS `@CheckJoinTable=` -- builder 0x86507,
#: `0xA97C0(buf, "@CheckJoinTable=", 0x2D, 0, ...)`, so code **0x2D** msgid 0 --
#: and waits on `0x825A0(buf, 0x2E, -1)`: code **0x2E**, the request+1 convention
#: this family follows everywhere (0xE1->0xE2, 0x80->0x81, 0x2D->0x2E). Its
#: name literal is `@CheckJoinTable_Ans` with NO trailing `=`, exactly like
#: `@GameML` and `@GameEA`; the `=` still goes on the wire (the family's delimiter).
#:
#: The parser at 0x865DA reads TWO integer fields and packs them into one word:
#:
#:     0x86601  0xAB080("/Join=", ..)   ->  result |= Join
#:     0x86620  0xAB080("/Mem=",  ..)   ->  result |= Mem << 1     (`lea eax,[edx+edx]`)
#:
#: so each is a BOOLEAN, `/Join=` at bit 0 and `/Mem=` at bit 1. The string pool
#: puts the pair immediately after `sqMgCpEnterTable` and `CpEnterTableCheck`,
#: which is what this is: the check a client runs before entering a table.
#:
#: WARNING: THE BIT MEANINGS ARE NOT MEASURED -- only their positions are. `1/1` is
#: the least-refusing answer and is what `POL_TM_JOIN` / `POL_TM_JOIN_MEM` exist
#: to sweep. What IS established is that an unanswered request is what kills this
#: client (see `@Quit=`), and this one has never been answered at all.
MSG_CHECKJOIN = 0x2D     # request  `@CheckJoinTable=`  (builder TM.dll 0x86507)
MSG_CHECKJOINANS = 0x2E  # reply    `@CheckJoinTable_Ans=/Join=<b>/Mem=<b>`

#: THE CARD-SERVICE ENDPOINT, found while chasing persistence and implemented
#: because an UNANSWERED request is what kills this client -- see `@Quit`.
#:
#: Same shape as `@ShReq`/`@ShEnter`: the handler at 0x8BA1E finds the key
#: `@CardEnt`, reads `/Ans=` and requires it **> 0** (`jle` skips), then stores
#: OUR message's sender id into `[0x5242958/0x524295C]` -- `tm_send`'s fallback
#: destination. So this teaches the client where to address card traffic.
#: The sender (0x8B7C0) drains codes 0x41, 0x42, 0x91 and 0xD0 first, which is
#: how the reply code 0x91 was identified rather than guessed.
#:
#: WARNING: NOT YET OBSERVED ON THE WIRE. `@CardReq=` has never appeared in
#: authserv.log, so what triggers it is unknown; this is here so that if the
#: client ever does ask, it gets an answer instead of hanging. Everything in the
#: reply is measured off the handler.

#: THE PURCHASE. `@Buy=/No=<pack>` arrives as `B2001401` -- **the client sets the
#: msgid field itself**, which is the live proof of the header layout above:
#: chars 0..1 `B2` = code, chars 4..5 `14` = msgid, chars 6..7 `01` = the shop
#: number, which is the `/N=` we sent in SHOPINIT.
#:
#: The answer is arm 0x105E85, `---->Recv=SHOPBUY`. Its gate is three equalities
#: before it will even log:
#:
#:     0xAA920("@Card")                       key must be present  -> `@Card=`
#:     0x10B6B0 = msg[+0x08]/[+0x0C]  == shop[+0x28]/[+0x2C]   sender id
#:     0x10B6C0 = msg[+0x3D0]         == shop[+0x30]           the `/N=` byte
#:
#: Both are satisfied for free by ECHOING THE REQUEST'S 8-HEX HEADER: same
#: service peer, same shop number. That is why `_shopbuy_body` reuses it rather
#: than rebuilding one.
#:
#: Then `/S=` = how many cards were drawn (must be > 0, else 0x105878), and for
#: each i in 0..S-1 a key `@N<i>=` carrying EIGHT `/D=` values read by index:
#:
#:     idx 0 -> word [rec+0x06]  CARD NUMBER, must be < the CardPrm.BIN table
#:                               (250 records), and becomes texture `cad%05d`
#:     idx 1 -> byte [rec+0x00]
#:     idx 2 -> byte [rec+0x01]  **must be < 4** -- the four Tetra Master card
#:                               types (P / M / X / A)
#:     idx 3 -> byte [rec+0x02]      idx 4 -> byte [rec+0x03]
#:     idx 5 -> byte [rec+0x04]      idx 6 -> byte [rec+0x05]
#:     idx 7 -> byte [rec+0x0E]
#:
#: A card that fails the number or type check is discarded (0x106093 ->
#: 0xB86B0) instead of added, so a bad value costs one card, not the screen.
#:
#: WARNING: Only the two RANGE checks are measured. Which of the remaining bytes is
#: attack / defence / arrows is NOT -- they go straight into the record and are
#: read back by the card renderer. `POL_TM_SHOP_CARDS` overrides the whole list
#: as `no:d1:d2:d3:d4:d5:d6:d7`, comma-separated, so it can be swept live.
CARDPRM_RECORDS = 250       # data/CardPrm.BIN slot count = the card-number ceiling

#: VERIFIED: CODE 0x13 -- THE TABLE'S SETTINGS, SERVER -> CLIENT, AND THE MIRROR OF
#: THE 0x14 THE CLIENT SENDS US. Decompiled 2026-08-20 while chasing "the other
#: player can only Reserve or View Table Members".
#:
#:     0x86ED0  0x825A0(buf, 0x13, -1)   code 0x13, any msgid
#:              then it takes the message's u64, LINEAR-SCANS the table array at
#:              0x521BB70 (stride 0x68) for a record whose +0x00 matches, and on
#:              a hit calls 0x87090 and sets that table's info record
#:              (0x521EF78 + i*0x34) `+0x30 = 2` -- "info received".
#:
#:     0x87090  parses SEVENTEEN fields, and they are the client's own 0x14 set:
#:              @Tet= /bm= /du= /st= /cb= /ca= /gs= /tl=
#:              @Tab= /in= /lu= /ll= /au= /al= /co= /pa=  **/pw=**
#:
#: `/pw=` is the one field the client never SENDS and only ever receives, which
#: is consistent with it being the table password.
#:
#: So a player looking at somebody else's table learns nothing about it until we
#: push this, and its info record stays at 0 instead of 2. We have the settings
#: already -- the owner handed them to us on 0x14 -- so this is an ECHO, not an
#: invention, which is why it is the first candidate in this whole chase that
#: needs no guessed values at all.
#:
#: WARNING: THE TABLE IS IDENTIFIED BY THE MESSAGE'S u64, NOT BY A FIELD. 0x86ED0
#: reads it straight out of the message rather than through `0xAB080`/`0xAB470`,
#: so it is the envelope's sender -- i.e. the TABLE PEER the reply goes out on.
#: That is why this rides a reply to something the client addressed to that
#: table's peer, and why the log line below records which peer it went out on.
#: VERIFIED: AND THE CLIENT ASKS FOR IT: `@Rule=`, code **0x12**. Read at 0x86DF0 --
#: it drains the 0x13 slot, sets the table's info flag to 0, builds
#: `0xA97C0(buf, "@Rule=", 0x12, ..)` carrying that table's u64, sends it, and
#: sets the flag to 1 ("asked, waiting"). Our 0x13 sets it to 2 ("received") at
#: 0x86F37 -- and it does that on ARRIVAL, unconditionally, so an unsolicited
#: push works as well as an answer. The command table already had `@Rule=`
#: at 0x12; nobody had connected it to the info record.
#:
#: WARNING: THE CLIENT HAS NEVER SENT IT -- 0 occurrences in the whole log. So this
#: arm is a completion of the pair, not the fix; the push is what does the work.
TABLE_INFO_REQ = 0x12           # `@Rule=` -- the client asking for a table's rules
#: The client's CONFIRM. `0x14` is the bare `@Tet=`/`@Tab=` pair; `0x24` is
#: `@Save=`, the preset write, which arrives repeatedly while the dialog is open
#: and must NOT be read as "the owner is done" -- see `_TABLE_CONFIRMED`.
TABLE_SETTINGS_CONFIRM = 0x14
#: `@Save=`, the PRESET write -- the player's own default table values, which
#: `0x14D9F0` reads back out of the save on the next login. See
#: `table_preset_fields` and `tmsave.TABLE_PRESET`.
TABLE_SETTINGS_PRESET = 0x24

TABLE_INFO_CODE = 0x13
TABLE_INFO_TET = (b"bm", b"du", b"st", b"cb", b"ca", b"gs", b"tl")
TABLE_INFO_TAB = (b"in", b"lu", b"ll", b"au", b"al", b"co", b"pa", b"pw")


#: VERIFIED: THE IN-MATCH COMMAND TABLE -- CODE 0x43, AND THE `msgid` IS A **COMMAND
#: ID**, NOT A SEQUENCE. Decompiled 2026-08-20 from `TM.dll.unpacked`.
#:
#: An earlier reading took the live trace line
#:
#:     [tm] store lookup: code=0x43 msgid=1 store_count=9 -> not found
#:
#: as "the board is a numbered stream -- expect to send 1, 2, 3...". It is not.
#: `0x101A86` is three instructions inside **`0x101A10`**, the client's shared
#: *wait for the answer to command N* helper -- the very one the card shop has
#: been using all along (the banner above `MSG_SHOPINIT` already names it):
#:
#:     0x101A10(this=0x52463E0, cmd)          __thiscall, one arg, `ret 4`
#:       if [rva 0x2B624C] == 0:  return 1    no session -> everything allowed
#:       edi = cmd & 0xFF                     <- the ARGUMENT, not a counter
#:       0x825A0(buf, 0x43, edi)              <- the line the trace prints
#:       found:   idx = [0x106D6C + cmd-1];  jmp [0x106D14 + idx*4]
#:       else:    0x825A0(buf, 0xB2, edi) -> [0x106DBC + cmd-7] / [0x106D98]
#:       the unhandled-default arm 0x106CCD returns **0**
#:
#: and every one of its 90 call sites does `cmp eax, 1 / jne <refuse>`. So the
#: polled msgid is the command being waited on, `msgid=1` means command 1, and
#: the answer belongs in `(0x43, 1)` -- not at a position in a stream.
#:
#: WARNING: AND THE SAME READING NAMES THE WRONG PAYLOAD FOR THAT SLOT. `@Test` +
#: `/Count=` and `@WatchInfo` + `/N=` + `/CN=` are simply the two parses that sit
#: next after the dispatch in ADDRESS order, so a linear read attributes them to
#: msgid 1. They are arms **0x101ACD (command 43)** and **0x101B15 (command
#: 37)**; the `jmp [edx*4 + ...]` jumps over both. Command 1 is `@VsGameInit`.
#:
#: THE WHOLE TABLE, arm by arm, with the client's own `---->Recv=` debug label --
#: which matters more than it looks: `0x93260` IS the sink `[tmlog] enable`
#: hooks (pol-shim/src/tmlog.cpp, `TMLOG_RVA`), so every label below appears in
#: `logs/shim-<HOST>.log` the moment the client READS our message. Fields are in
#: parse order; `str` is 0xAAD60, everything else 0xAB080 (int).
#:
#:   cmd  name             Recv=          fields
#:     1  @VsGameInit      PLGAMEINIT     /N= /ID=* /CN=* /R=x7 then @No<i>=
#:     1  @EventGameInit   EVGAMEINIT     (same arm, second name, 0x102964)
#:     2  @ComGameInit     COMGAMEINIT    /RA= /M= /CP= /S= /R=x7
#:     2  @ComList         COMLIST        /L= then /D= per row (same arm)
#:     4  @ComList         COMLIST        /L= /D=            (arm 0x102F74)
#:     5  @Quit            QUIT           /No= /B=
#:     7  @CardSelect      CARDSELECT     /Ans= /Ok=   (also @EventCard)
#:     8  @StartData       STARTDATA      /S= /B= /E= /F= /L=
#:     9  @TurnData        TURNINFO       /A= /S= /R=
#:    10  @PutCard         PUTCARD        /A= /H= /F= /D=
#:    12  @BattleData      BATTLESET      /A= /D= /R= /CBC=
#:    13  @ResultData      RESULTINFO     /R= /P= /D= /T= /RN=(str)
#:                                        (also @EventResult)
#:    14  @Ready           READY          /Go=
#:    15  @GetSelect       GETSELECT      /E= /H=
#:    17  @Continue        CONTINUE       /Ans= /B=
#:    26  @TCList          TRADELIST      /H= /D=      (also @TradeCard)
#:    35  @GetAway         GETAWAY        /P=
#:    36  @Change          PLCHANGE       /P= /C=
#:    37  @WatchInfo       WATCHINFO      /N= /CN=(str) /R= /Q= /C= /G= /H= /T=
#:                                        /S= /A= /F= /CH= /L=
#:    38  @WFCard          WATCHCARD      /T= /D=      (also @WatchCard)
#:    39  @Dead            DEAD           /N=
#:    40  @DataError       ERROR          /No=
#:    43  @Test            TEST:Count%d   /Count=
#:
#: Commands 3, 6, 11, 16, 18..25, 27..34, 41 and 42 have NO 0x43 arm; they fall
#: through to the 0xB2 table -- the card shop / auction set this file already
#: answers (7 @Select `/A=`, 19 @Init SHOPINIT, 20 @Card SHOPBUY, 23 @Event
#: SHOPQUIT, 32 @Data AUCMONEY, 33 @Card AUCCARDS, 39 @Dead, 40 @DataError) --
#: and on a second miss to a THIRD table at `0x1064BD` keyed `cmd - 0x18`, bound
#: 0x10, arms at `0x106DF0`: **code 0xA2 is CARD TRADE** (24 @Init TRADEINIT,
#: 25 @Quit TRADEQUIT, 26 @Card TRADELIST, 27 @Data TRADESELECT, 28 @Data
#: TRADEDECIDE, 30 @Quit TRADECLOSE, 39 @Dead, 40 @DataError). All three are
#: reproducible from the client's command table.
#:
#: VERIFIED: AND IT RETIRES THE "VS. COM IS A UI GATE" THREAD, TWO WAYS.
#: That reading asks for the site that pushes `tm-xlate` **string id 40**. There
#: is no such id: column 2 of the PS2 `tm-xlate` table is `jp_bytes`, the JAPANESE
#: BYTE LENGTH, and 40 is simply twice the 20 characters of that sentence (44 is
#: the two-character-longer "...now." variant). The real Lobby.BIN id is **428**,
#: which pol-shim's own `probe_msg` banner already records. Scanning all 312
#: callers of the two message-display functions (0x195260 / 0x1952A0) and reading
#: the id out of the third argument slot -- the technique that banner recommends
#: over hooking -- finds **exactly one** site pushing 428: `0xFCE55`, whose scene
#: waits on commands 0x13 / 0x17 / 0x27, i.e. SHOPINIT / SHOPQUIT / DEAD. That is
#: the CARD SHOP, not vs-COM. And the refusal is not a UI predicate at all: it is
#: the generic arm taken when `0x101A10` returns != 1, i.e. "the server never
#: answered that command". vs-COM's own waiter is `0xBADDA`, which asks for
#: command **2** and on success sets the match scene's state `[esi+0x16F] = 5`.
#: So that reading's instinct was right for the wrong reason: vs-COM IS
#: server-initiated, and the way to un-grey it is to answer `(0x43, 2)` with
#: `@ComGameInit` -- which needs no second player, no table and no room.
IN_MATCH_CODE = 0x43            # the in-match / vs-COM command family

#: WARNING: EVERYTHING FROM HERE DOWN IS READ OFF THE PARSER AND HAS NEVER BEEN SEEN
#: TO LAND. The client tells us itself, in one line, whether it did: turn
#: `[tmlog] enable` on and look for `---->Recv=PLGAMEINIT` (it read the message)
#: followed by either `ID=<n>` (it found itself, n = its seat) or `ID=NOT_FOUND`
#: / `StartAloneERROR` (it read the message and rejected it). Those three strings
#: are pushed at 0x102530, 0x102574 and 0x102943 and all three go to the traced
#: sink. Do not mark this fixed on anything less.
VSGAME_MSGID = 0x00010043       # code 0x43, command 1  -- `@VsGameInit`
COMGAME_MSGID = 0x00020043      # code 0x43, command 2  -- `@ComGameInit`
#: `@Quit` -- code 0x43, command 5. The console's LEAVE ACK, and it is a
#: PUSH: `TMaster.pex` only ever parses `@Quit` (receive arm 0x003eedd8),
#: never builds one, so nothing arrives for `_handle_line` to answer.
COMQUIT_MSGID = 0x00050043      # code 0x43, command 5  -- `@Quit` (leave ack)
STARTDATA_CMD = 8               # code 0x43, command 8  -- `@StartData` (the deal)
TURNDATA_CMD = 9                # code 0x43, command 9  -- `@TurnData` (whose turn)


#: *** THE THIRD ENVELOPE BYTE IS THE TURN NUMBER, AND IT IS A GATE. ***
#:
#: Read off the parser 2026-08-20, and it explains the shape of every in-match
#: message at once. `describe_line` already calls this byte `sh` because the
#: card shop uses it for the shop number (`@Quit=` came back `sh=11` after we
#: served `/N=11`); in the 0x43 match family it is the TURN COUNTER.
#:
#:   0x1036EC  @TurnData  : `[obj+0x18D] = sh`   -- the SERVER sets the turn
#:   0x103536  @StartData : the same store, so the deal seeds it
#:   0x103876  @PutCard   : `cmp [obj+0x18D], sh` -> mismatch prints
#:             **`Errot!!Turn%d!=%d`** and the arm bails to the error path
#:   0x103C0D  @BattleData: the same compare, the same bail
#:
#: and the client's own senders put its current turn in the same slot:
#: `@TurnData=` (0x107954), `@PutData=` (0x1079F8), `@BattleSelect=` (0x107B3B)
#: and `@Ready=` (0x107BD1) all push `byte [0x2B63C4]+0x18D`. `@GetSelect=`
#: (0x107C6F) and `@Continue=` (0x107CD9) push the player's SEAT there instead
#: -- different scene, different meaning for the same byte.
#:
#: WARNING: THE CLIENT OWNS THE COUNTER BETWEEN TURNS. 0xC76A2 (state 0x28, the arm
#: that prints `GamePlayEnd:Turn=%d`) does `inc al; mov [esi+0x18D], al` and
#: then advances the active player `[esi+0x178] = ([esi+0x178]+1) % N`, skipping
#: anyone out of cards. So the server does not get to choose freely: the next
#: `@TurnData` must carry the turn the client has already counted to, or the
#: first `@PutCard` of that turn is refused with `Errot!!Turn`.
#:
#: The match is exactly `5 * N` turns -- 0xC7701 loads N from [0x2B64C5],
#: `lea eax,[eax+eax*4]` makes 5N, and `cmp [esi+0x18D], eax / jge` prints
#: `End` instead of `Next`. Ten turns for two players, five cards each.
PUTCARD_CMD = 10                #: code 0x43 command 10 -- `@PutCard`, arm 0x103820
PUTDATA_CMD = 10                #: ...and the slot the client's `@PutData=` rides
TURNDATA_ACK_CMD = 9            #: `@TurnData=` comes back on the slot it was sent on


def _turn_code(cmd, turn):
    """The wire header for an in-match message: code, command, turn."""
    return encode_code(IN_MATCH_CODE | ((cmd & 0xFF) << 16)
                       | ((int(turn) & 0xFF) << 24))

BATTLEDATA_CMD = 12             #: code 0x43 command 12 -- `@BattleData`, arm 0x103BB7


RESULTDATA_CMD = 13             #: code 0x43 command 13 -- `@ResultData`, arm 0x104141
TCLIST_CMD = 26                 #: code 0x43 command 26 -- `@TCList`, arm 0x104648
GETAWAY_CMD = 35                #: code 0x43 command 35 -- `@GetAway`, arm 0x104FF6
GETSELECT_CMD = 15              #: code 0x43 command 15 -- `@GetSelect`, arm 0x104E5B
WATCHINFO_CMD = 37              #: code 0x43 command 37 -- `@WatchInfo`, arm 0x101B15
WFCARD_CMD = 38                 #: code 0x43 command 38 -- `@WFCard`, arm 0x1020F4
CHANGE_CMD = 36                 #: code 0x43 command 36 -- `@Change`, arm 0x1050A2


CONTINUE_CMD = 17               #: code 0x43 command 17 -- `@Continue`, arm 0x104EE1


#: --------------------------------------------------------------------------
#: A PLAYER WHO LEAVES A RUNNING MATCH IS REPLACED BY THE SERVER (2026-09-04)
#: --------------------------------------------------------------------------
#: THE HOLE: `@GameExit=` cleaned the leaver's own state and told the other
#: players NOTHING. The client has no in-match receive path for "a player
#: left" -- its board scene polls exactly one slot at a time ((0x43, 9) for
#: the turn, (0x43, 10) for the opponent's card) and `@Dead` (cmd 39) is
#: polled only by WAITING scenes (shop init, VS. COM init, trade: 26 sites,
#: none in 0xC0000-0xD3000). So the opponent sat on (0x43, 10) until their
#: own timeout, and seat expiry could not help either: it is suspended for
#: POL_TM_MATCH_HOLD_S (45 min) while a match is "running".
#:
#: THE FIX is the one Janhourou already uses for a mid-hand quit (`116ce73c`):
#: the server plays the empty seat. Network mode makes the server the
#: authority for every seat (0xCEE51 skips the client's own resolver), and
#: the VS. COM engine (`_queue_com_turns` / `_com_play_acked`) already plays
#: a seat out of a server-held hand -- this reuses its move choice, its
#: ack-gate (the GamePlayEnd purge race) and the PvP per-recipient framing.
#: The departed seat keeps its OWN hand (learned at `@CardSelect=`), so the
#: take flow at the end moves a card the leaver really held.
#:
#: Trigger: `@Break=` (0x43 msgid 39, `/ID=0` -- measured on prod 09-03, sent
#: ~2 s BEFORE `@GameExit=`) or `@GameExit=` from a seat in a running match.
#: A leaver during CARD SELECT gets a synthetic hand (a COM deck row set) and
#: the deal proceeds; that match's take flow is disabled (`bot_synth`) so no
#: card the leaver never owned changes hands. Pushes to a departed seat are
#: skipped everywhere in the match flow (`_is_bot`); its stake stays in the
#: PvP pot and cannot win it. `POL_TM_BOT_SEATS=0` restores the old
#: behaviour (the opponent hangs).
CARDSELECT_SLOT = 7             #: code 0x43 command 7 -- `@CardSelect`, arm 0x1030C9
DATAERROR_CMD = 40              #: code 0x43 command 40 -- `@DataError=/No=`, arm 0x103481


COMLIST_CMD = 4                 #: code 0x43 command 4 -- `@ComList`, arm 0x102F74


def encode_code(code):
    """A TM message code -> its 8-char wire header (u32, little-endian, hex)."""
    return struct.pack("<I", code & 0xFFFFFFFF).hex().upper().encode("ascii")


def decode_ebody(body):
    """Split a TM E-body into (code, command_bytes), or (None, body) if it has
    no leading 8-hex-char header. `body` is the payload with the 'G' class char
    already stripped (as _game_notice_reply hands it over)."""
    if len(body) >= 8 and all(c in b"0123456789ABCDEFabcdef" for c in body[:8]):
        code = struct.unpack("<I", bytes.fromhex(body[:8].decode("ascii")))[0]
        return code, body[8:]
    return None, body


class TMState(Enum):
    """Placeholder match lifecycle -- rename/renumber once the protocol is known."""
    CONNECT = 0        # client just dialed the world address
    HELLO = 1          # expected crypto/session handshake (may reuse login K)
    MATCHMAKING = 2    # lobby-of-players / opponent select
    MATCH = 3          # board + card-play exchange
    RESULT = 4         # win/loss, card award
    CLOSED = 5


def describe():
    """One-liner for the harness log."""
    return ("TM0 text protocol: zones, rooms, tables, VS. COM and player matches, "
            "trade, shop, auction, rankings, events.")


def describe_line(body):
    """Human-readable summary of one TM E-body, for the capture log.

    The 8-hex header is a u32 with THREE meaningful bytes, not one -- byte 0 is
    the code, byte 2 the msgid a waiter matches on, byte 3 the shop/channel
    number (see the banner above `MSG_SHOPINIT`). Printing the u32 whole gave
    lines like `code=18088114` for what is really code 178 msgid 0x14.
    """
    code, cmd = decode_ebody(body)
    if code is None:
        return "no-header cmd=%r" % (cmd[:120],)
    codestr = "code=%d" % (code & 0xFF)
    if (code >> 16) & 0xFF:
        codestr += " msgid=0x%02x" % ((code >> 16) & 0xFF)
    if (code >> 24) & 0xFF:
        codestr += " sh=%d" % ((code >> 24) & 0xFF)
    if (code >> 8) & 0xFF:
        codestr += " b1=0x%02x" % ((code >> 8) & 0xFF)
    return "%s cmd=%r" % (codestr, cmd[:120])
