"""Glue the core used to carry inline: the channel-less PART echo and which band a line arrived on.
"""
import os
from . import corenames


def _part_echo(nick, srv, sess):
    """Acknowledge a channel-less `PART :` -- the PS2 VS. COM leave.

    A VS. COM session on the console sends PART with an EMPTY channel after the
    game, from a session in no channel; an unacknowledged one leaves the client
    in state 3 until 0-37160 "Timed out while disconnecting from the server."
    It must be a SUCCESS echo (state 3 does `blez` on the result, so an error
    numeric fails the same way). `PART :<nick>` was tried live and rejected;
    the exact mirror of the request (an empty channel) is what the console
    accepts -- pinned live 2026-09-09. `POL_TM_PART_ECHO=nick|both` restores the
    other shapes; `POL_TM_PART_EMPTY=0` restores the old silence.

    Returns the lines to send, or None when this connection IS in a channel (the
    core then parts that channel properly)."""
    if sess is None or os.environ.get("POL_TM_PART_EMPTY", "1") != "1":
        return None
    if corenames.ROOMS.channels_of(sess):
        return None
    _form = os.environ.get("POL_TM_PART_ECHO", "mirror")
    _pre = b":" + nick + b"!~x@" + corenames._irc_host(srv) + b" PART"
    _mirror = _pre + b" :"            # exact mirror of `PART :`
    _withnick = _pre + b" :" + nick   # tried live, rejected
    if _form == "mirror":
        _acks = [_mirror]
    elif _form == "nick":
        _acks = [_withnick]
    else:
        _acks = [_mirror, _withnick]
    corenames.log("authserv",
        f"channel-less PART (the PS2 VS. COM leave) -- "
        f"acknowledging so sqMgCommandReqCheck completes; this "
        f"session is in no channel, nothing mutated "
        f"(POL_TM_PART_EMPTY=0 restores the old silence); "
        f"echo form {_form!r}: {_acks!r}")
    return _acks


def _band_role(cmd_txt):
    """(priority, label) for what this in-session line says the band is.

    MATCH THE CODE HEADER, NOT THE KEY NAME. `@Init=` alone is not a game-band
    marker -- the card shop's SHOPINIT arm uses the same key on code 0xA2 -- so
    this reads the class letter AND the two-hex code out of the `GTM0G<code>`
    envelope: `GTM0GE1` = 0xE1 @TeachDV, `GTM0G80` = 0x80 @Init. Those two codes
    are the handshake and nothing else speaks them. The class-L `<D..>` roster
    poll marks the ROOM band. The game handshake outranks the room poll if a
    connection somehow carries both, because it is the handshake that makes TM
    record this socket as its GameIrcID."""
    if b"GTM0GE1" in cmd_txt or b"GTM0G80" in cmd_txt:
        return (2, "GAME band (@TeachDV/@Init handshake)")
    if b"GTM0L<D" in cmd_txt:
        return (1, "ROOM band (class-L <D..> roster poll)")
    return None
