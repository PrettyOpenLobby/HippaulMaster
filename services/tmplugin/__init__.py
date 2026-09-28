"""The Tetra Master title plugin, one module per concern.

    deps.py          Imports shared by the package's modules.
    settings.py      The title's release defaults (set into the environment at import) and where its
                     files are.
    corenames.py     The core names the title uses, bound from titles.core at import and again when
                     the core rebinds.
    fetches.py       Declared lengths of Tetra Master's resource fetches (FETCH_PATHLEN) and
                     resource_length.
    roster.py        The room roster on the auth band: update sequence, deltas, member records,
                     departures and retirement.
    auction.py       The auction house: listing store, bids, notices, the expiry sweep, the class-A
                     replies and Check Out files.
    rankings.py      The ranking lists: the class-R reply, the rank list blob and TM0RkData per
                     client build.
    pool.py          The character pool the client hands over (<CR>), and names and records looked
                     up in it.
    chat.py          Tetra Master chat lines: re-broadcast to the room with the sender's name filled
                     in.
    envelope.py      The game envelope (class G on the auth band): notice, which hands each line to
                     the game or answers it.
    manifests.py     The service manifests the client loads first (TM0SML, TM0CVML, TM0IML, TM0CQL)
                     and fresh blob defaults.
    zones.py         Zones on the lobby band: occupancy, live player and room counts, the dial host
                     and zone names per build.
    ptl.py           b/g/PTL, the room's member and table list, rebuilt from the live roster.
    savedefaults.py  A fresh player save's factory header.
    events.py        Tournaments on the lobby band: the event zone, its room list, the Event List
                     and the missions mask.
    templates.py     The authored lobby blobs shipped with the tree, and how one is found.
    profile.py       The member profile fields for content id 2 (card level, title, average rank).
    bandglue.py      Glue the core used to carry inline: the channel-less PART echo and which band a
                     line arrived on.
    plugin.py        The TetraMaster titles.Title subclass and register(), the hooks the core calls.

tmtitle.py (one directory up) is the name the rest of the tree
imports, and the compatibility facade over these modules.


The plugin
----------
Tetra Master as a title plugin for the OpenLobby core.

This module is what `POL_TITLES=tmtitle` loads into the core's `login` and
`authsess` processes. It holds every piece of Tetra Master logic that used to
live inside the core's responders.py -- the room roster behind `b/g/PTL`, the
auction house, the ranking lists, the trade relay on the game envelope, the
lobby-list live counts and the player save's factory defaults -- registered
with the core through `titles.Title` (see services/titles.py in OpenLobby for
the contract). The game itself (board, battle, cards, shop, VS. COM) is
`tetramaster.py` and its siblings; this module is the seam that hands the
core's traffic to it.

Nothing here imports `responders`. The core plumbing these functions use is
bound by name through `titles.core` (`_CORE_NAMES` below): the moved code
keeps the names it always had, and running this module outside the core (the
selftests) binds the standalone defaults instead.
"""
