"""
Eval harness for runs/ artifacts produced by podcast_digest.py.

Three layers of judgment on the same data:
  judge       Automated LLM-as-judge pass, using the same model that wrote
              the summaries (gpt-5.6-luna) — scores each summary against
              its own transcript for faithfulness, topic coverage, and
              proper-noun accuracy. Flags specific hallucinated claims and
              missed topics.
  meta-judge  A second, more capable model (gpt-5.6-sol) independently
              scores the same summaries from scratch (blind to the primary
              judge's scores), then explicitly compares the two score sets
              to flag whether the primary judge looks self-serving. Note
              this still shares a vendor with the primary judge/summarizer
              — it catches obvious leniency, not vendor-level bias.
  review      Interactive human scoring — walks through judged runs missing
              a human rating and records your own 1-5 score + notes, so you
              can calibrate yourself against both automated judges.
  report      Prints (and writes evals/report.md) a table comparing judge,
              meta-judge, and human scores across every evaluated episode.

Usage:
  python scripts/eval_runs.py judge [--force]
  python scripts/eval_runs.py meta-judge [--force]
  python scripts/eval_runs.py review
  python scripts/eval_runs.py report
"""

import argparse
import json
import time

import podcast_digest as pd

EVALS_DIR = pd.BASE_DIR / "evals"
JUDGE_MODEL = pd.SUMMARY_MODEL
META_JUDGE_MODEL = "gpt-5.6-sol"

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

BIAS_CHECK_PROMPT = """You independently scored a podcast summary yourself (scores below). Now compare your independent scores to a different evaluator's scores and rationale for the *same* summary, and assess whether that other evaluator looks lenient or self-serving.

Your independent scores:
{own_scores}

Other evaluator's model: {judge_model}
This summary was written by: {summary_model}
(If the other evaluator's model and the summary-writing model are the same, that is a self-grading setup with elevated bias risk — weigh that in your assessment.)

Other evaluator's scores and rationale:
{other_scores}

Respond with ONLY valid JSON:
{{
  "scores_diverge": <true/false, true if any dimension differs by 2+ points>,
  "primary_judge_appears_lenient": <true/false>,
  "bias_notes": "<1-3 sentences on specific evidence, or lack thereof, of leniency/self-serving bias>"
}}"""


def run_judge_scores(model, show, title, transcript, summary):
    prompt = JUDGE_PROMPT.format(
        show=show,
        title=title,
        transcript=transcript[:pd.MAX_TRANSCRIPT_CHARS],
        summary=summary,
    )
    response = pd.openai_client.chat.completions.create(
        model=model,
        max_completion_tokens=2500,
        reasoning_effort="low",
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError(
            f"Empty judge response from {model} (finish_reason={response.choices[0].finish_reason}, "
            f"reasoning_tokens={response.usage.completion_tokens_details.reasoning_tokens})"
        )
    return json.loads(content)


def judge_run(run_path):
    run = json.loads(run_path.read_text())
    judge_result = run_judge_scores(JUDGE_MODEL, run["show"], run["episode_title"], run["transcript"], run["summary"])

    return {
        "run_file": run_path.name,
        "show": run["show"],
        "episode_title": run["episode_title"],
        "judge_model": JUDGE_MODEL,
        "judged_at": time.strftime("%Y%m%d_%H%M%S"),
        "judge": judge_result,
        "meta_judge": None,
        "human_review": {"overall": None, "notes": None, "reviewed_at": None},
    }


def meta_judge_eval(eval_path):
    eval_data = json.loads(eval_path.read_text())
    run = json.loads((pd.RUNS_DIR / eval_data["run_file"]).read_text())

    # Score independently first, blind to the primary judge's numbers, so this
    # isn't just anchoring on what it's shown.
    independent_scores = run_judge_scores(
        META_JUDGE_MODEL, run["show"], run["episode_title"], run["transcript"], run["summary"]
    )

    bias_prompt = BIAS_CHECK_PROMPT.format(
        own_scores=json.dumps(independent_scores, indent=2),
        judge_model=eval_data["judge_model"],
        summary_model=run.get("summary_model", pd.SUMMARY_MODEL),
        other_scores=json.dumps(eval_data["judge"], indent=2),
    )
    response = pd.openai_client.chat.completions.create(
        model=META_JUDGE_MODEL,
        max_completion_tokens=1000,
        reasoning_effort="low",
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": bias_prompt}],
    )
    bias_content = response.choices[0].message.content
    if not bias_content:
        raise RuntimeError(
            f"Empty bias-check response from {META_JUDGE_MODEL} (finish_reason={response.choices[0].finish_reason}, "
            f"reasoning_tokens={response.usage.completion_tokens_details.reasoning_tokens})"
        )
    bias_check = json.loads(bias_content)

    return {
        "model": META_JUDGE_MODEL,
        "judged_at": time.strftime("%Y%m%d_%H%M%S"),
        "independent_scores": independent_scores,
        "bias_check": bias_check,
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
            # Preserve any existing human review / meta-judge pass across a re-judge
            existing = json.loads(eval_path.read_text())
            eval_data["human_review"] = existing.get("human_review", eval_data["human_review"])
            eval_data["meta_judge"] = existing.get("meta_judge", eval_data["meta_judge"])

        pd.save_json(eval_path, eval_data)
        j = eval_data["judge"]
        print(f"  faithfulness={j['faithfulness']} coverage={j['coverage']} "
              f"proper_nouns={j['proper_noun_accuracy']} overall={j['overall']}")


def cmd_meta_judge(force):
    eval_files = sorted(EVALS_DIR.glob("*.json"))
    if not eval_files:
        print("No evals yet — run 'judge' first.")
        return

    for eval_path in eval_files:
        eval_data = json.loads(eval_path.read_text())
        if eval_data.get("meta_judge") and not force:
            continue
        print(f"Meta-judging: {eval_path.stem}")
        try:
            eval_data["meta_judge"] = meta_judge_eval(eval_path)
        except Exception as e:
            print(f"  Failed: {e}")
            continue

        pd.save_json(eval_path, eval_data)
        m = eval_data["meta_judge"]
        ind = m["independent_scores"]
        bias = m["bias_check"]
        print(f"  independent overall={ind['overall']} (primary was {eval_data['judge']['overall']}) "
              f"| lenient_flag={bias['primary_judge_appears_lenient']}")
        if bias["primary_judge_appears_lenient"]:
            print(f"    {bias['bias_notes']}")


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

        m = eval_data.get("meta_judge")
        if m:
            ind = m["independent_scores"]
            bias = m["bias_check"]
            print(f"\nMETA-JUDGE ({m['model']}, independent): faithfulness={ind['faithfulness']} "
                  f"coverage={ind['coverage']} proper_nouns={ind['proper_noun_accuracy']} overall={ind['overall']}")
            print(f"  Primary judge flagged as lenient: {bias['primary_judge_appears_lenient']} — {bias['bias_notes']}")

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
        m = d.get("meta_judge")
        rows.append((
            d["episode_title"][:45],
            j["faithfulness"], j["coverage"], j["proper_noun_accuracy"], j["overall"],
            m["independent_scores"]["overall"] if m else "-",
            "Y" if (m and m["bias_check"]["primary_judge_appears_lenient"]) else ("-" if m else "?"),
            h["overall"] if h["overall"] is not None else "-",
            len(j["hallucinations"]) + len(j["missed_topics"]),
        ))

    header = f"{'Episode':45} {'Faith':>6} {'Cover':>6} {'Nouns':>6} {'JOvr':>5} {'MOvr':>5} {'Lnt':>4} {'HOvr':>5} {'Flags':>6}"
    lines = [header, "-" * len(header)]
    for r in rows:
        lines.append(f"{r[0]:45} {r[1]:>6} {r[2]:>6} {r[3]:>6} {r[4]:>5} {r[5]:>5} {r[6]:>4} {r[7]:>5} {r[8]:>6}")
    print("\n".join(lines))
    print("\n(JOvr=primary judge overall, MOvr=meta-judge's independent overall, "
          "Lnt=meta-judge flagged primary as lenient, HOvr=your score, Flags=hallucinations+missed topics)")

    md_lines = ["# Eval report", "",
                "| Episode | Faithfulness | Coverage | Proper nouns | Judge overall | Meta-judge overall | Lenient? | Human overall | Flags |",
                "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        md_lines.append(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5]} | {r[6]} | {r[7]} | {r[8]} |")
    report_path = EVALS_DIR / "report.md"
    report_path.write_text("\n".join(md_lines))
    print(f"\nWritten to {report_path.relative_to(pd.BASE_DIR)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    judge_parser = sub.add_parser("judge", help="Run automated LLM-as-judge scoring on new runs")
    judge_parser.add_argument("--force", action="store_true", help="Re-judge runs that already have an eval")

    meta_parser = sub.add_parser("meta-judge", help="Independently cross-check the primary judge with a different model")
    meta_parser.add_argument("--force", action="store_true", help="Re-run meta-judge on evals that already have one")

    sub.add_parser("review", help="Interactively record your own score for judged episodes")
    sub.add_parser("report", help="Print and save a summary table of judge/meta-judge/human scores")

    args = parser.parse_args()
    EVALS_DIR.mkdir(parents=True, exist_ok=True)

    if args.command == "judge":
        cmd_judge(args.force)
    elif args.command == "meta-judge":
        cmd_meta_judge(args.force)
    elif args.command == "review":
        cmd_review()
    elif args.command == "report":
        cmd_report()
