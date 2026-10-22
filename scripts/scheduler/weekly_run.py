"""
Weekly runner for the Mac's launchd job (installed by install.sh).

launchd starts this every Wednesday at 19:00, at login, and once an hour. It only
does work when a run is due, meaning no completed run since the most recent
Wednesday 19:00, so a Mac that was asleep, off or offline then catches up at
the next chance and the other triggers exit straight away. "Asleep" includes
dark wakes, the few seconds a sleeping Mac wakes for background upkeep:
launchd fires then, but a run started then is frozen mid-request when the Mac
dozes off again, so it waits for a real wake.

A run: bring this clone up to date with origin/main, run podcast_digest.py,
commit the new summaries and transcripts, and push to main (which makes
Vercel rebuild the web app). It runs from its own clone under
~/Library/Application Support/podcast-digest, never your working copy.

Usage:
  python weekly_run.py            run if due (what launchd does)
  python weekly_run.py --force    run now regardless of the schedule
"""

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APP_DIR = REPO.parent
LAST_RUN_FILE = APP_DIR / "last_run"
LOCK_DIR = APP_DIR / "run.lock"
LOG_FILE = Path.home() / "Library" / "Logs" / "podcast-digest" / "run.log"

RUN_WEEKDAY = 2          # Wednesday (Monday is 0)
RUN_HOUR = 19
STALE_LOCK_SECONDS = 3 * 3600
SLEPT_THRESHOLD_SECONDS = 60


def log(message):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}", flush=True)


def notify(message):
    script = f"display notification {json.dumps(message, ensure_ascii=False)} with title \"Podcast Digest\""
    subprocess.run(["osascript", "-e", script], check=False)


def last_scheduled_time(now):
    """The most recent Wednesday 19:00 at or before `now`."""
    candidate = (now - timedelta(days=(now.weekday() - RUN_WEEKDAY) % 7)).replace(
        hour=RUN_HOUR, minute=0, second=0, microsecond=0)
    return candidate if candidate <= now else candidate - timedelta(days=7)


def is_due(now):
    try:
        last_run = datetime.fromisoformat(LAST_RUN_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return True
    return last_run < last_scheduled_time(now)


def awake():
    """False in a dark wake: the screen and graphics stay off while the Mac
    does a few seconds of upkeep before sleeping again."""
    try:
        state = subprocess.run(["pmset", "-g", "systemstate"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return True  # can't tell; don't block the run on it
    capabilities = next((line for line in state.splitlines() if "Capabilities" in line), None)
    return capabilities is None or "Graphics" in capabilities


def seconds_asleep(start_wall, start_uptime):
    """How long the Mac slept since the two readings: wall-clock time keeps
    going during sleep, CLOCK_UPTIME_RAW doesn't."""
    return (time.time() - start_wall) - (time.clock_gettime(time.CLOCK_UPTIME_RAW) - start_uptime)


def online():
    try:
        urllib.request.urlopen("https://github.com", timeout=15)
        return True
    except Exception:
        return False


class GitError(Exception):
    pass


def git(*args):
    result = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True)
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def has_unpushed_commits():
    """A summary commit from a run whose push failed, still waiting to go out."""
    try:
        return git("rev-list", "--count", "origin/main..main") != "0"
    except GitError:
        return False


def push():
    try:
        git("push", "-q", "origin", "main")
    except GitError:
        # main moved while we ran (e.g. a merged PR): replay our commit on top once.
        git("pull", "-q", "--rebase", "origin", "main")
        git("push", "-q", "origin", "main")


def commit_outputs():
    """Commit any new or changed summaries and transcripts. Returns the new summary paths."""
    git("add", "summaries", "transcripts")
    new = [p for p in git("diff", "--cached", "--name-only", "--diff-filter=A", "--", "summaries").splitlines() if p]
    if git("diff", "--cached", "--name-only"):
        git("commit", "-q", "-m", "Add podcast summaries")
    return new


def run(now):
    # Hold off idle sleep until this process exits (closing the lid on
    # battery still sleeps; the check after the pipeline handles that).
    subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    start_wall, start_uptime = time.time(), time.clock_gettime(time.CLOCK_UPTIME_RAW)

    git("checkout", "-q", "main")
    # Keep what an interrupted run already paid to produce: commit its
    # leftover files, then rebase (not reset) so that commit, or one whose
    # push failed last time, goes out with this run.
    new = commit_outputs()
    git("pull", "-q", "--rebase", "origin", "main")

    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"], cwd=REPO, check=True)
    pipeline = subprocess.run([sys.executable, "scripts/podcast_digest.py"], cwd=REPO)

    new += commit_outputs()
    if has_unpushed_commits():
        push()
        log(f"Pushed to main: {len(new)} new summar{'y' if len(new) == 1 else 'ies'}")

    # A failure while the Mac slept is most likely dropped connections, so
    # leave the week open and retry within the hour. A paid-for transcript is
    # cached, so the retry doesn't transcribe again.
    if pipeline.returncode != 0 and seconds_asleep(start_wall, start_uptime) > SLEPT_THRESHOLD_SECONDS:
        notify(f"Interrupted by sleep ({len(new)} new); will retry within the hour")
        log(f"Run interrupted by sleep (pipeline exit code {pipeline.returncode}); will retry within the hour")
        return

    # Otherwise record the week as done even if an episode failed: the
    # failure is likely to repeat, and each retry can cost transcription.
    # Failed episodes are still in the 14-day window next Wednesday, and the
    # notification says to check.
    LAST_RUN_FILE.write_text(now.isoformat(timespec="seconds"))
    if pipeline.returncode != 0:
        notify(f"Finished with errors ({len(new)} new). See {LOG_FILE}")
    elif new:
        notify(f"{len(new)} new summar{'y' if len(new) == 1 else 'ies'} added")
    log(f"Run complete (pipeline exit code {pipeline.returncode})")


def main():
    parser = argparse.ArgumentParser(description="Weekly podcast digest runner")
    parser.add_argument("--force", action="store_true", help="Run now regardless of the schedule")
    args = parser.parse_args()

    now = datetime.now()
    if not args.force and not is_due(now) and not has_unpushed_commits():
        return  # the hourly and at-login triggers land here almost every time

    try:
        LOCK_DIR.mkdir()
    except FileExistsError:
        if time.time() - LOCK_DIR.stat().st_mtime < STALE_LOCK_SECONDS:
            log("Another run is in progress; skipping")
            return
        log("Clearing a stale lock left by an interrupted run")
    try:
        if not awake():
            log("Mac is in a dark wake (asleep); will run once it's awake")
            return
        if not online():
            log("Offline; will retry within the hour")
            return
        log(f"Starting run (scheduled for {last_scheduled_time(now):%a %b %d %H:%M})")
        run(now)
    except Exception:
        # No last_run update, so the next hourly trigger tries again.
        log("Run failed:\n" + traceback.format_exc())
        notify(f"Run failed; will retry within the hour. See {LOG_FILE}")
        sys.exit(1)
    finally:
        LOCK_DIR.rmdir()


if __name__ == "__main__":
    main()
