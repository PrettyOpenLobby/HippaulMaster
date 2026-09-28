"""Tetra Master chat lines: re-broadcast to the room with the sender's name filled in."""
import os
import struct
import tmroom
from . import corenames


#: *** A TETRA MASTER CHAT LINE HAS TO BE RE-BROADCAST, NOT ECHOED. ***
#:
#: The client SENDS `42 00 08 00 @Chat=...` -- code 0x42, msgid **8**. Relaying
#: that verbatim is what we did for weeks, and it produces exactly the symptom
#: seen in live testing: the "new message" icon lights up and the chat panel
#: stays empty. TM.dll dispatches msgid at rva 0x07E3FF and the two arms do
#: different jobs:
#:
#:     msgid 8 (0x07E8A9)  parses @Chat, sets flags        -> THE INDICATOR
#:     msgid 4 (0x07E42F)  parses @Chat + /Num= + /C=,
#:                         then formats a display line     -> THE CHAT LOG
#:
#: Only msgid 4 ever puts text in the panel, and nobody had ever been sent one --
#: which is also why the SENDER never saw their own line. This client does not
#: self-render; the log is fed only by what comes back from the server.
#:
#: VERIFIED: AND ITS GATE IS ENTIRELY OURS TO SATISFY -- read off the binary rather than
#: guessed, after two rounds of guessing were offered and declined. Both operands
#: come from the same `0xA9780(char_offset)` two-hex-digit reader that decoded the
#: save header:
#:
#:     a9780(0) -> frame+0x41C = CODE
#:     a9780(4) -> frame+0x420 = MSGID    (the 4/6/8 dispatch reads this)
#:     a9780(6) -> frame+0x424 = SHOP
#:
#:     0x07E4A2  mov ecx, [esp+0x444]   -> frame+0x424 = the header's SHOP byte
#:     0x07E4A9  mov eax, [esp+0x34]    -> frame+0x14  = the parsed `/Num=`
#:     0x07E4B0  cmp ecx, eax / jne bail
#:
#: So **`/Num=` must equal the header's shop byte**. Not a sequence, not a
#: counter, nothing derived from client state -- we write both halves. `/C=` is
#: parsed just before the gate and takes no part in it.
#:
#: WARNING: SENT TO THE SENDER TOO, deliberately: `ROOMS.broadcast`'s usual `exclude`
#: is right for a protocol where the client draws its own line, and this one does
#: not. If a duplicate ever appears for the sender, that is the knob to turn
#: first. `POL_TM_CHAT_RELAY=0` restores the verbatim relay.


def _tm_chat_rebroadcast(body):
    """A TM0 `@Chat=` line rewritten into the msgid-4 form, or None.

    None means "not a Tetra Master chat line" and the caller relays verbatim --
    group chat on `#XXL` is a different subsystem with its own measured envelope
    and must not be touched by this.
    """
    # WARNING: OFF BY DEFAULT -- THE msgid-4 HYPOTHESIS IS FALSIFIED. Measured on CASPC
    # with the receiver instrumented, 2026-08-20:
    #
    #   msgid 8 (verbatim)  -> the "new message" indicator LIGHTS, panel empty
    #   msgid 4 (rewritten) -> no indicator, panel empty, and the receiver's
    #                          trace shows NO `find("@Chat")` AT ALL -- the only
    #                          two find() calls in the whole session are
    #                          @TeachDVAns and @InitAns
    #
    # So msgid 4 never reaches the chat handler; it is dropped before it. msgid 8
    # WAS being ingested -- the indicator is the proof -- and this transform took
    # a message that got in and made one that does not.
    #
    # What that re-establishes: the empty panel under msgid 8 is a problem AFTER
    # ingestion, not a dispatch problem. The 4/6/8 switch at 0x07E3FF is real and
    # reads the msgid, but arm 4 is evidently not reachable for a line arriving
    # this way -- reading the switch correctly did not mean the arm was ours to
    # aim at.
    #
    # KEPT, not deleted: the rewrite is correct as written and one env flip away,
    # and the next question needs it. Nobody has yet watched a msgid-8 chat line
    # arrive at an INSTRUMENTED receiver -- every observation of msgid 8 was made
    # before CASPC had its trace back. Do that before changing anything else.
    if os.environ.get("POL_TM_CHAT_RELAY", "0") != "1":
        return None
    if not body.startswith(b"GTM0G") or b"@Chat=" not in body:
        return None
    head, cmd = body[5:13], body[13:]
    if len(head) != 8:
        return None
    try:
        code = struct.unpack("<I", bytes.fromhex(head.decode("ascii")))[0]
    except (ValueError, UnicodeDecodeError):
        return None
    try:
        msgid = int(os.environ.get("POL_TM_CHAT_MSGID", "4"), 0) & 0xFF
        # SHOP 1, NOT 0 -- and this value satisfies BOTH readings of the gate.
        #
        # MEASURED 2026-08-20: with msgid 8 the client lit its "new message"
        # indicator and drew nothing; with msgid 4 the indicator STOPPED. So the
        # rewritten header is reaching the client and being dispatched on, and
        # the msgid-4 arm is running and BAILING -- delivery was never the issue.
        #
        # The only gate in that arm compares the header's shop byte against a
        # slot the `/Num=` parse fills (0xAB080's 4th argument). What was never
        # verified is WHICH of that parser's two output pointers gets what:
        #
        #   if it holds the parsed VALUE     -> /Num=1 vs shop 1 matches
        #   if it holds a FOUND-FLAG/LENGTH  -> present => 1, vs shop 1 matches
        #
        # shop 0 / Num=0 passes only the first reading, and it did not render --
        # which is itself evidence for the second. 1 covers both, so this is one
        # test rather than a permutation sweep.
        shop = int(os.environ.get("POL_TM_CHAT_SHOP", "1"), 0) & 0xFF
    except ValueError:
        msgid, shop = 4, 0
    out = (body[:5]
           + struct.pack("<I", (code & 0xFF) | (msgid << 16) | (shop << 24))
                 .hex().upper().encode("ascii")
           + cmd)
    if b"/Num=" not in out:
        out += b"/Num=%d" % shop          # THE GATE: must equal the shop byte
    return out


#: *** THE CHAT-LINE FIX: THE SENDER'S NAME GOES BETWEEN `#CHAT#` AND THE TAB. ***
#:
#: Read off TM.dll 2026-08-21. The wire
#: text of a chat line is `/Dt=#CHAT#<name>\t<text>` -- Jan's drain spells it out
#: (`sprintf("%s%s%c%s", "#CHAT#", name, '\t', text)`, janwire.py) -- and the
#: client sends the name EMPTY, the same server-fills-it shape as `<DE>`'s name
#: (tmroom.note_name) and `@Chat=/NN=`. On receive, the parser at 0xAB810
#: REQUIRES the marker and strips it, then the panel widget (event 0x300005D arm,
#: 0xCF97) splits on the TAB and DISCARDS the line unless the name part is
#: 1..15 bytes (0xCFDE jle / 0xCFE7 jge -> bail). A verbatim relay therefore
#: delivers `<empty>\t<text>` and every line dies there -- on every screen, for
#: every receiver, and for the sender too (the client's local self-echo carries
#: its own empty name, so it bails identically; the panel is fed only by what
#: comes back from the server).
#:
#: Hence also: SENT TO THE SENDER TOO, like the msgid-4 experiment above and for
#: the same measured reason ("this client does not self-render").
#:
#: The name is trimmed, never padded: a 15-space-padded record name would land
#: the tab at byte 16 and hit the other side of the same gate. Fallback is the
#: IRC nick -- a real identifier of the sender, not a fabrication -- because an
#: unknown name would otherwise mean the line stays invisible, which is the bug.
#:
#: `POL_TM_CHAT_SENDER_NAME=0` restores the verbatim relay. A line that already
#: carries a name between the marker and the tab is left alone.
def _tm_chat_fill_name(body, sess, nick, member_id=None):
    """A TM0 `@Chat=` chat line (code 0x42 msgid 8) with the sender's name
    filled into `/Dt=#CHAT#<name>\\t`, or None to relay verbatim.

    The sender is named either by their ChatSession (`sess`, the room-relay
    caller) or by an explicit `member_id` (the whisper caller, where the game
    envelope handler has only the thread-local session)."""
    if os.environ.get("POL_TM_CHAT_SENDER_NAME", "1") == "0":
        return None
    if not body.startswith(b"GTM0G") or b"@Chat=" not in body:
        return None
    head = body[5:13]                     # 8 hex chars: code . msgid shop
    if len(head) != 8 or head[0:2] != b"42" or head[4:6] != b"08":
        return None                       # roster arms and other codes: not ours
    at = body.find(b"/Dt=#CHAT#")
    if at < 0:
        return None
    ins = at + len(b"/Dt=#CHAT#")
    tab = body.find(b"\t", ins)
    if tab < 0 or tab != ins:
        return None                       # no tab, or a name is already there
    if member_id is None and sess is not None:
        member_id = corenames._sess_member_id(sess)
    name = ""
    if tmroom is not None and member_id is not None:
        try:
            name = tmroom.name_of(member_id) or ""
        except Exception:
            name = ""
    source = "tmroom"
    if not name:
        name = (nick or b"").decode("latin1", "replace")
        source = "nick"
    # The gate is a BYTE position: 1..15 bytes before the tab. cp932 because
    # that is the client's text encoding; names are ASCII in practice.
    nb = name.strip().encode("cp932", "replace")[:15]
    if not nb:
        return None
    corenames.log("authserv", f"  TM chat: sender name '{nb.decode('cp932', 'replace')}' "
                    f"({source}) filled into /Dt= for {nick.decode('latin1')}")
    return body[:ins] + nb + body[ins:]
