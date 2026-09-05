"""
Eval harness for runs/ artifacts produced by podcast_digest.py.

Two layers of judgment on the same data:
  judge   Automated LLM-as-judge pass — scores each summary against its own
          transcript for faithfulness, topic coverage, and proper-noun
          accuracy. Flags specific hallucinated claims and missed topics.
  review  Interactive human scoring — walks through runs missing a human
          rating and records your own 1-5 score + notes alongside the
          judge's, so you can calibrate one against the other.
  report  Prints (and writes evals/report.md) a table comparing judge vs.
          human scores across every evaluated episode.

Usage:
  python scripts/eval_runs.py judge [--force]
  python scripts/eval_runs.py review
  python scripts/eval_runs.py report
"""

import argparse
import json
import time

import podcast_digest as pd

EVALS_DIR = pd.BASE_DIR / "evals"
JUDGE_MODEL = pd.SUMMARY_MODEL

JUDGE_PROMPT = """You are an evaluator grading how well a podcast episode summary represents its transcript. Be strict — flag anything in the summary that isn't actually supported by the transcript, and anything significant the transcript covers that the summary skips.

Show: {show}
Episode: {title}

Transcript:
{transcript}

Summary to evaluate:
{summary}

Score 1-5 (5 = best) on each dimension:
- faithfulness: summary makes no claims unsupported by the transcript
- coverage: summary captures the episode's major topics/questions
- proper_noun_accuracy: names, companies, and terms in the summary match the transcript

Respond with ONLY valid JSON in this exact shape:
{{
  "faithfulness": <1-5>,
  "hallucinations": [<specific unsupported claim strings>],
  "coverage": <1-5>,
  "missed_topics": [<specific topic strings>],
  "proper_noun_accuracy": <1-5>,
  "flagged_terms": [<specific name/term strings that look wrong>],
  "overall": <1-5>,
  "rationale": "<1-3 sentence justification>"
}}"""


def judge_run(run_path):
    run = json.loads(run_path.read_text())
    prompt = JUDGE_PROMPT.format(
        show=run["show"],
        title=run["episode_title"],
        transcript=run["transcript"][:pd.MAX_TRANSCRIPT_CHARS],
        summary=run["summary"],
    )
    response = pd.openai_client.chat.completions.create(
        model=JUDGE_MODEL,
        max_completion_tokens=1500,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
    )
    judge_result = json.loads(response.choices[0].message.content)

    return {
        "run_file": run_path.name,
        "show": run["show"],
        "episode_title": run["episode_title"],
        "judge_model": JUDGE_MODEL,
        "judged_at": time.strftime("%Y%m%d_%H%M%S"),
        "judge": judge_result,
        "human_review": {"overall": None, "notes": None, "reviewed_at": None},
    }


def cmd_judge(force):
    run_files = sorted(pd.RUNS_DIR.glob("*.json"))
    if not run_files:
        print(f"No run artifacts found in {pd.RUNS_DIR}. Run the pipeline or backfill_runs.py first.")
        return

    for run_path in run_files:
        eval_path = EVALS_DIR / run_path.name
        if eval_path.exists() and not force:
            continue
        print(f"Judging: {run_path.stem}")
        try:
            eval_data = judge_run(run_path)
        except Exception as e:
            print(f"  Failed: {e}")
            continue

        if eval_path.exists() and force:
            # Preserve any existing human review across a re-judge
            existing = json.loads(eval_path.read_text())
            eval_data["human_review"] = existing.get("human_review", eval_data["human_review"])

        pd.save_json(eval_path, eval_data)
        j = eval_data["judge"]
        print(f"  faithfulness={j['faithfulness']} coverage={j['coverage']} "
              f"proper_nouns={j['proper_noun_accuracy']} overall={j['overall']}")


def cmd_review():
    eval_files = sorted(EVALS_DIR.glob("*.json"))
    pending = [f for f in eval_files if json.loads(f.read_text())["human_review"]["overall"] is None]

    if not pending:
        print("Nothing to review — every judged episode already has a human score. Run 'judge' first if needed.")
        return

    print(f"{len(pending)} episode(s) to review. Ctrl+C any time — progress is saved as you go.\n")
    for eval_path in pending:
        eval_data = json.loads(eval_path.read_text())
        run_data = json.loads((pd.RUNS_DIR / eval_data["run_file"]).read_text())
        j = eval_data["judge"]

        print("=" * 70)
        print(f"{eval_data['show']} — {eval_data['episode_title']}")
        print("-" * 70)
        print(f"SUMMARY:\n{run_data['summary']}\n")
        print(f"JUDGE: faithfulness={j['faithfulness']} coverage={j['coverage']} "
              f"proper_nouns={j['proper_noun_accuracy']} overall={j['overall']}")
        if j["hallucinations"]:
            print(f"  Flagged hallucinations: {j['hallucinations']}")
        if j["missed_topics"]:
            print(f"  Flagged missed topics: {j['missed_topics']}")
        print(f"  Rationale: {j['rationale']}")
        print(f"\n(Full transcript is in runs/{eval_data['run_file']} if you want to check it directly.)")

        try:
            raw_score = input("\nYour score 1-5 (blank to skip): ").strip()
        except KeyboardInterrupt:
            print("\nStopped. Progress saved.")
            return

        if not raw_score:
            continue

        notes = input("Notes (optional): ").strip()
        eval_data["human_review"] = {
            "overall": int(raw_score),
            "notes": notes or None,
            "reviewed_at": time.strftime("%Y%m%d_%H%M%S"),
        }
        pd.save_json(eval_path, eval_data)
        print("Saved.\n")


def cmd_report():
    eval_files = sorted(EVALS_DIR.glob("*.json"))
    if not eval_files:
        print("No evals yet — run 'judge' first.")
        return

    rows = []
    for f in eval_files:
        d = json.loads(f.read_text())
        j = d["judge"]
        h = d["human_review"]
        rows.append((
            d["episode_title"][:45],
            j["faithfulness"], j["coverage"], j["proper_noun_accuracy"], j["overall"],
            h["overall"] if h["overall"] is not None else "-",
            len(j["hallucinations"]) + len(j["missed_topics"]),
        ))

    header = f"{'Episode':45} {'Faith':>6} {'Cover':>6} {'Nouns':>6} {'JOvr':>5} {'HOvr':>5} {'Flags':>6}"
    lines = [header, "-" * len(header)]
    for r in rows:
        lines.append(f"{r[0]:45} {r[1]:>6} {r[2]:>6} {r[3]:>6} {r[4]:>5} {r[5]:>5} {r[6]:>6}")
    print("\n".join(lines))

    md_lines = ["# Eval report", "", "| Episode | Faithfulness | Coverage | Proper nouns | Judge overall | Human overall | Flags |",
                "|---|---|---|---|---|---|---|"]
    for r in rows:
        md_lines.append(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5]} | {r[6]} |")
    report_path = EVALS_DIR / "report.md"
    report_path.write_text("\n".join(md_lines))
    print(f"\nWritten to {report_path.relative_to(pd.BASE_DIR)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    judge_parser = sub.add_parser("judge", help="Run automated LLM-as-judge scoring on new runs")
    judge_parser.add_argument("--force", action="store_true", help="Re-judge runs that already have an eval")

    sub.add_parser("review", help="Interactively record your own score for judged episodes")
    sub.add_parser("report", help="Print and save a summary table of judge vs. human scores")

    args = parser.parse_args()
    EVALS_DIR.mkdir(parents=True, exist_ok=True)

    if args.command == "judge":
        cmd_judge(args.force)
    elif args.command == "review":
        cmd_review()
    elif args.command == "report":
        cmd_report()
