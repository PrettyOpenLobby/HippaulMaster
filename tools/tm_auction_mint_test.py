#!/usr/bin/env python3
"""tm_auction_mint_test.py -- a new auction's `<AI>` never reuses an id that
still has a bid file.

2026-09-13: every earlier listing had settled, the store was empty, and a new
listing minted as auction 1 -- inheriting auction-1.bids.bin from 08-20 (two
test bids). The board showed "High bid 0T by PCTest (2 bids)" on a card nobody
had bid on, and at its end the sweep would have sold it to PCTest.

    python tools/tm_auction_mint_test.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="tm-auction-mint-")
os.environ["POL_RESOURCE_DIR"] = TMP
os.environ["POL_DATA_DIR"] = TMP
sys.path.insert(0, os.path.join(HERE, os.pardir, "services"))

import tmtitle as responders   # noqa: E402  the auction store is the title's
import tmauction    # noqa: E402

fails = []


def check(name, ok, detail=""):
    print("  %-66s %s%s" % (name, "PASS" if ok else "FAIL",
                            "  " + str(detail) if detail and not ok else ""), flush=True)
    if not ok:
        fails.append(name)


def bids(ai, *rows):
    with open(os.path.join(TMP, "auction-%d.bids.bin" % ai), "wb") as fh:
        for r in rows:
            fh.write(r)


check("the store points at the test directory", responders.RESOURCE_DIR == TMP,
      responders.RESOURCE_DIR)
check("nothing listed, nothing bid: the first auction is 1", responders._auction_next_id() == 1,
      responders._auction_next_id())
bids(1, tmauction.build_bid("LaptopTest2", 50, 1787200000, 7),
     tmauction.build_bid("PCTest", 100, 1787200100, 8))
check("a settled auction 1 left its bid file: the next is 2, not 1",
      responders._auction_next_id() == 2, responders._auction_next_id())
bids(7)
check("ids with a bid file count even when out of order (7 -> 8)",
      responders._auction_next_id() == 8, responders._auction_next_id())
with open(os.path.join(TMP, "auction-x.bids.bin"), "wb"):
    pass
with open(os.path.join(TMP, "auction-99.bids.bin.tmp"), "wb"):
    pass
check("files that are not an auction's bid file are ignored",
      responders._auction_next_id() == 8, responders._auction_next_id())

if fails:
    raise SystemExit("[tm_auction_mint_test] %d FAILED: %s" % (len(fails), ", ".join(fails)))
print("[tm_auction_mint_test] OK -- 5 checks")
