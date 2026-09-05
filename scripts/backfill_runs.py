"""
One-off backfill: process a batch of past episodes into runs/ artifacts,
without sending a digest email. Used to seed real data for eval_runs.py.

Usage:
  python scripts/backfill_runs.py --count 12
  python scripts/backfill_runs.py --count 5 --show "Lenny's Podcast"
"""

import argparse

import podcast_digest as pd


def backfill(show_name_filter, count):
    shows = pd.load_json(pd.SHOWS_FILE, [])
    seen = pd.load_json(pd.STATE_FILE, {})

    for show in shows:
        name = show["name"]
        if show_name_filter and name != show_name_filter:
            continue

        seen_guids = set(seen.get(name, []))
        episodes = pd.get_latest_episodes(show["rss_url"], limit=count + len(seen_guids) + 5)
        candidates = [e for e in episodes if e["guid"] not in seen_guids][:count]

        print(f"Backfilling {len(candidates)} episode(s) for {name}")
        for ep in candidates:
            print(f"Processing: {name} — {ep['title']}")
            try:
                pd.process_episode(name, ep)
                seen_guids.add(ep["guid"])
                # Checkpoint after every episode, not just at the end, so a
                # dropped connection or interrupted run loses nothing already
                # completed — re-running the same command just picks up
                # where it left off.
                seen[name] = list(seen_guids)
                pd.save_json(pd.STATE_FILE, seen)
            except Exception as e:
                print(f"  Failed: {e}")

    print("Backfill complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=12, help="Episodes to backfill per show")
    parser.add_argument("--show", default=None, help="Only backfill this show (default: all configured shows)")
    args = parser.parse_args()
    backfill(args.show, args.count)
