#!/usr/bin/env python3
"""The weekly ranking tally as a long-running job: publish Tetra Master's
ranking lists every Sunday at 00:05 UTC (the original service rebuilt them
weekly), sleeping in between.

    python tools/tmrank_job.py            # in the compose `tmrank` service
    TM_RANK_AT=now python tools/tmrank_job.py   # publish once at start, too

Runs `tools/tmrank.py --publish` (the tally itself; see that file for what
it does and where the numbers come from). A failed publish is logged and
retried at the next tick; the job never exits on its own.
"""
import datetime
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TALLY = os.path.join(HERE, "tmrank.py")


def next_sunday_0005(now):
    """The next Sunday 00:05 UTC strictly after `now` (a datetime)."""
    days = (6 - now.weekday()) % 7                # Monday = 0 ... Sunday = 6
    cand = now.replace(hour=0, minute=5, second=0, microsecond=0) \
        + datetime.timedelta(days=days)
    if cand <= now:
        cand += datetime.timedelta(days=7)
    return cand


def publish():
    print("tmrank_job: publishing", flush=True)
    r = subprocess.run([sys.executable, TALLY, "--publish"], cwd=HERE)
    print("tmrank_job: publish exit %d" % r.returncode, flush=True)
    return r.returncode == 0


def main():
    if os.environ.get("TM_RANK_AT", "").strip().lower() == "now":
        publish()
    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        due = next_sunday_0005(now)
        wait = (due - now).total_seconds()
        print("tmrank_job: next publish %s (in %.0f s)" % (due.isoformat(), wait),
              flush=True)
        time.sleep(max(1.0, wait))
        publish()


if __name__ == "__main__":
    main()
