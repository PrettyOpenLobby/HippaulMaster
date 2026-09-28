"""Imports shared by the package's modules, and the optional sibling services (tmauction, tmsave,
tmprize, tmroll).
"""
import inspect
import json
import os
import random
import shutil
import sys
import tempfile
import threading

#: WARNING: THIS MODULE'S DIAGNOSTICS WERE INVISIBLE IN A CONTAINER, and that is
#: why it exists. `responders.log()` prints with `flush=True`; a bare `print()`
#: does not, and a container's stdout is a PIPE, so Python block-buffers it.
#: Every "tm: ..." line in this file -- the money moves, the guild persist, the
#: save patches -- sat in a 8 KB buffer that a long-lived server never fills.
#: Measured 2026-08-20: `docker logs | grep "^tm:"` returned **0** across 4,000
#: lines while the handler that writes them was demonstrably running.
#:
#: WARNING: THAT IS RULE 5 FAILING SILENTLY. "The fix did not run" and "the fix ran
#: and logged nothing" are indistinguishable when the log cannot leave the
#: process, and this project has lost days to exactly that distinction.
#: `_say()` is defined FURTHER DOWN (search `def _say(*args`). A second, broken
#: definition used to sit right here and was removed 2026-08-26: its body called
#: `_say(msg, flush=True)` against its own one-positional-arg signature, so it
#: raised `TypeError` instead of logging -- and the `except UnicodeEncodeError`
#: wrapped around it could not catch that. It was harmless only by accident,
#: because the later definition rebinds the name at module scope; any
#: module-level call reaching it first would have raised. (Checked before
#: removal: no module-level `_say(` call precedes the real definition.)


import tm_cardprm
#: The board, the arrows and the battle arithmetic -- see `services/tmbattle.py`.
#: NOT optional: a match that cannot resolve a battle stops dead on (0x43, 12),
#: which is exactly the wall this module spent a long round in front of.
import tmbattle
try:
    # The auction's store, for what Check Out is owed. Optional so an import
    # failure degrades to "nothing to collect" -- an empty Check Out is a
    # legitimate state and a wrong one is a fabricated sale, which this screen
    # has already caused once.
    import tmauction
except ImportError:                                          # pragma: no cover
    tmauction = None
try:
    # The save writer. Optional so an import failure degrades to record-only
    # (what this module did before delivery was solved) rather than taking the
    # whole title down -- but it is a sibling in services/ and is mounted, so a
    # miss here means something is wrong with the image, and it says so.
    import tmsave
except ImportError as _e:                                    # pragma: no cover
    tmsave = None
    _say("tm: tmsave unavailable (%s) -- collections will be RECORDED but not "
          "delivered; every launch will start empty" % _e)
try:
    # PRIZE POINTS: the second currency, the weekly lucky cards, and the
    # `@ExcInit=` body the Prize Center opens on. Optional for the same reason
    # tmsave is -- a missing sibling must cost the Prize Center, not the title.
    import tmprize
except ImportError as _e:                                    # pragma: no cover
    tmprize = None
    _say("tm: tmprize unavailable (%s) -- the Prize Center will fall back to "
          "the CARD SHOP's opener and no lucky card will ever be counted" % _e)
try:
    # CARD ROLL + GROWTH: a card is rolled below its CardPrm ceiling at
    # acquisition and grows toward it through use. Optional for the same reason
    # as the siblings above -- a missing module must cost progression, not the
    # title (cards then fall back to nominal = the old all-maxed behaviour).
    import tmroll
except ImportError as _e:                                    # pragma: no cover
    tmroll = None
    _say("tm: tmroll unavailable (%s) -- cards will serve NOMINAL (all-maxed) "
          "stats; no roll or growth" % _e)
import re
import struct
import time
from enum import Enum


#: THE EVENT CLOCK IS THE SERVER'S, NOT THE PLAYER'S. The client shows Time
#: Left as End - Now from @EventTimeReqCheck and never reads its own clock, so
#: answering every entry End=7200/Now=0 gave each player a private two hours
#: from the moment THEY walked in. One window for
#: everyone: Now = seconds since the window opened, End = its length. The
#: Event List's Start/End columns and date read the same window.
#:
#:   POL_TM_EVENT_WINDOW="HH:MM-HH:MM" (UTC, daily) pins a real schedule.
#:   Unset: a rolling test window that ends on the next even UTC hour at least
#:   30 minutes away, so every process and every restart agrees on it.
# The tournament's calendar logic lives in tmcup (which a website's API can
# share); these names are kept here because the rest of this module uses them.
from tmcup import (event_info, event_window, event_missions,  # noqa: E402
                   event_prizes, event_phase, _COUNTED_MISSIONS)


#: The flat module's path. Code that looked for data files beside it
#: (`os.path.dirname(__file__)`) looks beside the facade, as before.
FACADE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "tetramaster.py")
