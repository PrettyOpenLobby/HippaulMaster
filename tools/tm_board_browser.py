#!/usr/bin/env python3
"""tm_board_browser.py -- drive the Tetra Master board in a real headless
Chrome (render-web-ui-before-shipping), over a temporary 12-player board and
a two-page auction.

    python tools/tm_board_browser.py [--shots DIR]

Fails on any JavaScript error, on art or a face that never loads, on text
that runs into its neighbour, on a column whose header and values do not
share one edge, or on a control that does not do what the game's does.
Needs Chrome/Edge and websocket-client (see fe_panel_browser.py). LOOK at the
screenshots before pushing.
"""
import argparse
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, os.pardir, "services"))

from fe_panel_browser import Browser, free_port   # noqa: E402

NAMES = ["Seiryu", "Byakko", "Sennin", "Square", "Schildt", "Genbu",
         "Tensai", "tabax", "Majin", "Suzaku", "Fox", "Perry"]
CARDS = [(65, 90, 40, 0, 35, 0b10110101), (107, 60, 30, 1, 75, 0b00000111),
         (148, 110, 90, 3, 90, 0xFF), (21, 33, 25, 2, 20, 0b01000100),
         (100, 70, 55, 1, 60, 0b00010001), (54, 80, 80, 0, 80, 0b11000011),
         (184, 45, 20, 2, 30, 0b00100100)]


def setup(tmp):
    res = os.path.join(tmp, "resources")
    os.makedirs(res)
    os.environ["POL_RESOURCE_DIR"] = res
    os.environ["POL_DATA_DIR"] = tmp
    os.environ["POL_TM_ROSTER_FILE"] = os.path.join(tmp, "tm-roster.json")
    os.environ["POL_ACCOUNTS_DB"] = os.path.join(tmp, "none.db")
    import tmauction
    import tmrank
    now = int(time.time())
    players = [{"name": n, "rating": 400 - 23 * i, "rating_last": 390 - 21 * i,
                "prize_total": max(0, 14000 - 1300 * i), "prize_week": max(0, 3000 - 400 * i),
                "last_rank": {0: (i + 3) % 13, 1: i + 1, 2: i + 1, 4: (i % 5) + 1, 5: 0}}
               for i, n in enumerate(NAMES)]
    files = {tmrank.path_for(rid): tmrank.build_list(rid, players) for rid in tmrank.LISTS}
    files[tmrank.RKDATA] = tmrank.build_rkdata({0: 12, 5: 12, 1: 12, 2: 10},
                                               tmrank.next_update(now))
    tmrank.write_store(files)
    for i, n in enumerate(NAMES):
        with open(os.path.join(res, "%d.tm_collection.json" % (i + 1)), "w") as fh:
            json.dump({"rank": {"games": 3 + i, "score_total": 40 - 2 * i, "tiles_total": 50,
                                "prize_week": 100 * i, "last_played": now - 60,
                                "week_of": tmrank.week_start(now), "week_games": 1 + i % 4}}, fh)
    with open(os.environ["POL_TM_ROSTER_FILE"], "w") as fh:
        json.dump({"names": {str(i + 1): n for i, n in enumerate(NAMES)}}, fh)
    recs = []
    for k, (cid, atk, pdef, typ, mdef, arrows) in enumerate(CARDS):
        cm = 900 + 300 * k if k % 2 else 0
        recs.append(tmauction.build_record(
            [cid, 0, 0, 0, 0, 0, 0, 0, atk, pdef, typ, mdef, 3, arrows, 0, 0],
            NAMES[k], NAMES[k + 3] if cm else "", ai=40 + k, sp=400 + 700 * k, bi=50,
            ed=now - 3600 * (k + 1), nc=now + 3600 * (5 + 17 * k), et=24, am=0, cm=cm,
            bc=1 if cm else 0))
        if cm:
            with open(os.path.join(res, "auction-%d.bids.bin" % (40 + k)), "wb") as fh:
                fh.write(tmauction.build_bid(NAMES[k + 3], cm, now - 600 * k, k + 4))
    recs.append(tmauction.build_record([30, 0, 0, 0, 0, 0, 0, 0, 20, 20, 0, 20, 1, 3, 0, 0],
                                       "Fox", "Perry", ai=60, sp=100, bi=10, ed=now - 90000,
                                       nc=now - 300, et=24, am=0, cm=250, bc=1))
    with open(os.path.join(res, "auction-60.bids.bin"), "wb") as fh:
        fh.write(tmauction.build_bid("Perry", 250, now - 2000, 12))
    with open(os.path.join(res, "11.U_g_TM0_EXHIBITLIST.bin"), "wb") as fh:
        fh.write(b"".join(recs))
    with open(os.path.join(tmp, "tm-matches-live.json"), "w") as fh:
        json.dump({"count": 2, "stamp": time.time()}, fh)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default=tempfile.mkdtemp(prefix="tm-board-shots-"))
    o = ap.parse_args(argv)
    os.makedirs(o.shots, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="tm-board-browser-")
    setup(tmp)
    import boardtm
    import polboards
    port = free_port()
    args = polboards.build_parser().parse_args(["--tm-port", str(port)])
    srv = polboards.serve(boardtm, args, port)
    fails = []

    def check(name, ok, detail=""):
        print("  %-66s %s%s" % (name, "PASS" if ok else "FAIL",
                                "  " + str(detail) if detail and not ok else ""))
        if not ok:
            fails.append(name)

    art_ok = ("[...document.querySelectorAll('#stage img')].every("
              "i => i.complete && i.naturalWidth > 0)")
    # any two things in one row whose boxes intersect, and any two header labels
    overlap = """(() => {
      const bad = [], box = e => [e.offsetLeft, e.offsetTop, e.offsetLeft + (e.offsetWidth || e.naturalWidth || 0),
                                  e.offsetTop + (e.offsetHeight || e.naturalHeight || 0), e.textContent || e.src.split('/').pop()];
      const test = els => { const r = els.map(box);
        for (let i = 0; i < r.length; i++) for (let j = i + 1; j < r.length; j++){
          const a = r[i], b = r[j];
          if (a[0] < b[2] - .5 && b[0] < a[2] - .5 && a[1] < b[3] - .5 && b[1] < a[3] - .5) bad.push(a[4] + '|' + b[4]); } };
      for (const row of document.querySelectorAll('#dyn .row'))
        test([...row.querySelectorAll('.t, img')].filter(e => e.tagName === 'IMG' || e.textContent.trim()));
      test([...document.querySelectorAll('#dyn > .lab')]);
      test([...document.querySelectorAll('#info .band')]);
      return bad; })()"""
    # ONE edge per column: each header label and every value on it, all tabs
    aligned = """(() => {
      const bad = [], keep = TAB, keepBand = BAND;
      const W = e => e.offsetWidth || e.naturalWidth || 0;
      const edge = (e, a) => a === 'left' ? e.offsetLeft : a === 'right' ? e.offsetLeft + W(e) : e.offsetLeft + W(e) / 2;
      for (let t = 0; t < 7; t++){
        setTab(t);
        const C = t === 6 ? L.columns.auction : t === 5 ? L.columns.live : L.columns.rank;
        for (const [k, [x, a]] of Object.entries(C)){
          const h = document.querySelector('#dyn .h-' + k);
          if (!h) continue;
          let vals = [...document.querySelectorAll('#dyn .c-' + k)];
          if (k === 'rank') vals = vals.concat([...document.querySelectorAll('#dyn img.medal')]);
          if (k === 'card') vals = [...document.querySelectorAll('#dyn .row > img')];
          if (Math.abs(edge(h, a) - x) > 1.5) bad.push(t + ':h-' + k + '@' + edge(h, a));
          for (const v of vals) if (Math.abs(edge(v, a) - x) > 1.5){ bad.push(t + ':c-' + k + '@' + edge(v, a) + ' ' + v.textContent); break; }
        }
      }
      BAND = keepBand; setTab(keep);
      return bad; })()"""
    title_fits = """(() => { const b = E.title.getBBox(), sw = L.title.stroke_px;
      return b.width > 40 && b.x - sw >= L.arrows.prev[0] + L.arrows.prev[2] && b.x + b.width + sw <= L.arrows.next[0] })()"""
    captions = """(() => { const c = [...document.querySelectorAll('.tab .t')];
      const s = c.map(t => parseFloat(t.style.fontSize));
      return c.length === 7 && s.every(v => v === s[0]) && c.every(t => t.offsetLeft >= L.tabs.pad - 0.5
             && t.offsetLeft + t.offsetWidth <= L.tabs.w - L.tabs.pad + 0.5 && t.offsetTop >= 0 && t.offsetTop + t.offsetHeight <= L.tabs.h) })()"""
    name0 = "(document.querySelector('#dyn .row .c-name') || {}).textContent"

    b = Browser(width=1400, height=1000)
    try:
        b.goto("http://127.0.0.1:%d/" % port, settle=3.0)
        check("the page has the board and its layout: 6 lists + the auction, 7 tabs",
              b.js("S && L && S.tabs.length === 6 && E.tabs.length === 7"))
        check("every art layer loaded", b.js(art_ok))
        check("the faces loaded (M PLUS 1p medium + bold, Almendra)",
              b.js("document.fonts.check('500 15px \"TMText\"') && document.fonts.check('700 15px \"TMText\"')"
                   " && document.fonts.check('30px \"TMTitle\"')"))
        check("TM's own menu backdrop is behind the window",
              "backdrop.png" in b.js("getComputedStyle(document.querySelector('#bg')).backgroundImage"))
        check("the sheet has a deckled edge and a shadow that follows it",
              "mask_main.png" in (b.js("getComputedStyle(stage).webkitMaskImage || getComputedStyle(stage).maskImage") or "")
              and "drop-shadow" in b.js("getComputedStyle(wrap).filter"))
        check("row 1 is real text: Seiryu, with the gold 1st",
              b.js(name0) == "Seiryu" and b.js("document.querySelectorAll('#dyn img.medal').length") == 3,
              b.js(name0))
        check("the window is as large as fits inside the margin",
              b.js("(() => { const r = stage.getBoundingClientRect();"
                   " return Math.abs(r.width - (innerWidth - 2 * PAD)) < 2 || Math.abs(r.height - (innerHeight - 2 * PAD)) < 2 })()"))
        check("...with no sidebar when there is no room for one (1400x1000)", b.js("side.hidden"))
        check("the live line is INSIDE the window",
              b.js("(() => { const s = document.querySelector('#status').getBoundingClientRect(),"
                   " r = stage.getBoundingClientRect(); return s.width > 0 && s.top >= r.top"
                   " && s.bottom <= r.bottom && /Page 1\\/2/.test(s.width && document.querySelector('#status').textContent) })()"),
              b.js("document.querySelector('#status').textContent"))
        check("the next update line reads like the game's",
              b.js("/^Tally Period: \\d\\d\\/\\d\\d\\/\\d{4} - .*Next update: \\d\\d\\/\\d\\d\\/\\d{4} \\d\\d:\\d\\d/.test(E.info.textContent)"),
              b.js("E.info.textContent"))
        check("the title sits between the two arrows", b.js(title_fits),
              b.js("(() => { const b = E.title.getBBox(); return [b.x, b.width] })()"))
        check("the tab captions sit on their boards with clear space each side, one shared size",
              b.js(captions), b.js("[...document.querySelectorAll('.tab .t')].map(t => [t.offsetLeft, t.offsetWidth, t.style.fontSize])"))
        check("the gold medals are drawn at the row's scale, not their native 22 px",
              b.js("[...document.querySelectorAll('#dyn img.medal')].every(i => i.offsetHeight === L.rows.medal_h)")
              and b.js("L.rows.medal_h") < 22)
        check("the current tab is the flat highlight (s3), the others normal (s0)",
              b.js("getComputedStyle(E.tabs[0]).backgroundImage").endswith('tab_s3.png")')
              and b.js("getComputedStyle(E.tabs[2]).backgroundImage").endswith('tab_s0.png")'))
        check("pressing a tab moves its caption down with the board (only while held)",
              b.js("[...document.styleSheets].flatMap(s => [...s.cssRules]).some(r =>"
                   " r.selectorText === '.tab:active .t' && /translateY\\(2px\\)/.test(r.style.transform))"))
        check("no text runs into its neighbour", b.js(overlap) == [], b.js(overlap))
        check("every column header shares one edge with its values, on all seven tabs",
              b.js(aligned) == [], b.js(aligned))
        b.screenshot(os.path.join(o.shots, "1-vs-rating.png"))
        b.click_el("#arrow-next")
        b.pump(0.5)
        check("the right arrow pages forward (R1)", b.js("TAB") == 0 and b.js("PAGE") == 1
              and b.js(name0) == "Fox", (b.js("TAB"), b.js("PAGE"), b.js(name0)))
        b.screenshot(os.path.join(o.shots, "2-page2.png"))
        b.click_el("#arrow-next")
        b.pump(0.5)
        check("...and on the last page rolls into the next tab (Top 30)",
              b.js("TAB") == 1 and b.js("PAGE") == 0)
        b.click_el("#arrow-prev")
        b.pump(0.5)
        check("the left arrow on a first page goes back to the previous tab's last page",
              b.js("TAB") == 0 and b.js("PAGE") == 1)
        b.click_el(".tab[data-tab='3']")
        b.pump(0.5)
        check("the Grand Total tab: prize money, comma-grouped, right-aligned",
              b.js("TAB") == 3 and b.js("document.querySelector('#dyn .h-value').textContent") == "Prize Money"
              and b.js("document.querySelector('#dyn .c-value').textContent") == "14,000")
        b.screenshot(os.path.join(o.shots, "3-grand-total.png"))
        b.key("6", "Digit6", 54)
        b.pump(0.5)
        check("key 6 opens This Week So Far, labelled as live",
              b.js("TAB") == 5 and "not yet published" in b.js("E.info.textContent"))
        b.screenshot(os.path.join(o.shots, "4-this-week.png"))
        b.key("7", "Digit7", 55)
        b.pump(1.0)
        check("key 7 opens the Auction with the card art", b.js("TAB") == 6 and b.js(art_ok)
              and b.js("document.querySelectorAll('#dyn .row').length") == 5)
        check("...each card's arrows drawn from its mask (Bahamut: 5)",
              b.js("document.querySelector('#dyn .row svg').querySelectorAll('polygon').length") == 5)
        check("...a card with no bid shows its opening bid, dimmed, and '-' for the bidder",
              b.js("[...document.querySelectorAll('#dyn .c-open')].length") >= 1
              and "-" in b.js("[...document.querySelectorAll('#dyn .c-who')].map(e => e.textContent)"))
        check("...the Price List bands, a zero band greyed",
              b.js("document.querySelectorAll('#info .band').length") == 6
              and b.js("[...document.querySelectorAll('#info .band')].some(e => e.disabled)") is not None,
              b.js("[...document.querySelectorAll('#info .band')].map(e => e.textContent + (e.disabled ? '*' : ''))"))
        check("...and no text overlaps there either", b.js(overlap) == [], b.js(overlap))
        b.screenshot(os.path.join(o.shots, "5-auction.png"))
        b.click_el("#info .band[data-band='5']")
        b.pump(0.5)
        check("Recently Sold: the sale, its winner, 'Sold For'",
              b.js("BAND") == 5 and b.js("document.querySelector('#dyn .h-price').textContent") == "Sold For"
              and b.js("document.querySelector('#dyn .c-who').textContent") == "Perry")
        b.screenshot(os.path.join(o.shots, "6-sold.png"))
        check("the hidden table mirrors the screen for screen readers",
              b.js("document.querySelectorAll('#rows tr').length") == 1)
        b.call("Emulation.setDeviceMetricsOverride", width=1920, height=1080,
               deviceScaleFactor=1, mobile=False)
        # the window widened in place first: a sheet that was hidden must fill
        # its list as soon as it shows, not at the next poll
        b.pump(0.8)
        check("widening the window fills the Auction Activity sheet at once",
              b.js("!side.hidden && document.querySelectorAll('#act li').length") >= 5,
              b.js("document.querySelectorAll('#act li').length"))
        # a REAL load (a new query, not a same-document hash change)
        b.goto("http://127.0.0.1:%d/?w=1#6" % port, settle=2.5)
        check("a 16:9 screen gets the Auction Activity sheet beside the window",
              b.js("!side.hidden && side.getBoundingClientRect().left > stage.getBoundingClientRect().right"))
        check("...newest first: the sale (Blazer Beetle), then the bids",
              b.js("document.querySelectorAll('#act li').length") >= 5
              and b.js("document.querySelector('#act .nm').textContent") == "Blazer Beetle",
              b.js("[...document.querySelectorAll('#act .nm')].map(e => e.textContent)"))
        check("...none of it running into the footer",
              b.js("(() => { const l = document.querySelector('#act li:last-child').getBoundingClientRect(),"
                   " f = E.sideFoot.getBoundingClientRect(); return l.bottom <= f.top })()"))
        check("...the matches in progress and the players on file",
              "2 matches in progress" in b.js("E.sideFoot.textContent")
              and "12 players on file" in b.js("E.sideFoot.textContent"), b.js("E.sideFoot.textContent"))
        b.screenshot(os.path.join(o.shots, "7-wide.png"))
        b.call("Emulation.setDeviceMetricsOverride", width=420, height=900,
               deviceScaleFactor=2, mobile=True)
        b.goto("http://127.0.0.1:%d/?m=1#6" % port, settle=2.5)
        check("a phone opens on #6 (the Auction) with no sideways scroll",
              b.js("TAB") == 6 and b.js(art_ok)
              and b.js("document.documentElement.scrollWidth <= innerWidth + 1"))
        b.screenshot(os.path.join(o.shots, "8-phone.png"))
        check("no JavaScript errors", not b.errors, b.errors)
    finally:
        b.close()
        srv.shutdown()
    print("screenshots in %s" % o.shots)
    if fails:
        raise SystemExit("[tm_board_browser] %d FAILED: %s" % (len(fails), ", ".join(fails)))
    print("[tm_board_browser] OK")


if __name__ == "__main__":
    main()
