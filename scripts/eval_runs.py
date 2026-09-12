"""
Eval harness for runs/ artifacts produced by podcast_digest.py.

Two layers of judgment on the same data:
  judge   Automated LLM-as-judge pass. JUDGE_MODEL must stay a different
          model from podcast_digest.SUMMARY_MODEL — a judge grading its own
          family's output inflates scores. The model reports *what* is wrong
          (hallucinations, distortions, missed topics, flagged terms) and how
          severe each item is; the 1-5 scores are derived from that evidence
          in code, never assigned by the model. Requires ANTHROPIC_API_KEY
          only when JUDGE_MODEL is a Claude model.
  review  Interactive human scoring — walks through judged runs missing a
          human rating and records your own 1-5 score + notes, so you can
          calibrate yourself against the judge.
  report  Prints (and writes evals/report.md) a table comparing judge and
          human scores across every evaluated episode.

Usage:
  python scripts/eval_runs.py judge [--force]           # one call per run
  python scripts/eval_runs.py judge --batch [--force]   # one batch, 50% cheaper, async
  python scripts/eval_runs.py judge-collect <batch id>  # resume an interrupted batch
  python scripts/eval_runs.py review
  python scripts/eval_runs.py report
"""

import argparse
import json
import os
import time

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

import podcast_digest as pd

EVALS_DIR = pd.BASE_DIR / "evals"
JUDGE_MODEL = "claude-opus-5"

# Built lazily: the judge only needs an Anthropic key when JUDGE_MODEL is a
# Claude model, and `report` / `review` need no API access at all.
_anthropic_client = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _anthropic_client

# "overall" is computed from the three dimension scores, which are themselves
# derived in code from the judge's evidence lists — see derive_scores(). The
# model reports *what* is wrong and how severe; it never assigns a number.
# Faithfulness weighted highest since a fabrication actively misleads, vs.
# coverage where an omission just leaves a gap.
FAITHFULNESS_WEIGHT = 0.45
COVERAGE_WEIGHT = 0.35
PROPER_NOUN_WEIGHT = 0.20

SEVERITIES = ("trivial", "substantive", "central")
EVIDENCE_LISTS = ("hallucinations", "distortions", "missed_topics", "flagged_terms")

JUDGE_PROMPT = """You are an evaluator grading how well a podcast episode summary represents its transcript. Be strict — flag anything in the summary that isn't actually supported by the transcript, and anything significant the transcript covers that the summary skips.

Show: {show}
Episode: {title}

Transcript:
{transcript}

Summary to evaluate:
{summary}

Report problems in four lists. Do NOT assign any numeric scores — scores are calculated from your lists, so what matters is that each list is complete and each item's severity is honest.

1. hallucinations — claims in the summary that the transcript does not support at all (invented facts, figures, or events).
2. distortions — claims that ARE grounded in the transcript but are misrepresented. This covers:
   - overclaiming: a hedged statement ("we think this might work") presented as certain ("this works")
   - misattribution: a real claim pinned on the wrong speaker
   - decontextualization: a caveat or qualifier dropped in a way that changes the meaning
3. missed_topics — substantive topics, questions, or segments the transcript covers that the summary omits entirely.
4. flagged_terms — names, companies, or technical terms that are wrong or unsupported. For each, set "kind":
   - "contradicted": the transcript says something different (e.g. a misspelled name)
   - "absent": the term may be true in the real world but never appears in the transcript
   The transcript is machine-generated and can mishear names. The episode title comes from the publisher and is authoritative — if a name matches the episode title but not the transcript, it is CORRECT; do not flag it. Check names against the title first, and against the transcript only for terms the title doesn't cover.

CATEGORY RULES — each problem belongs in exactly one list:
- A wrong or unsupported name/entity goes ONLY in flagged_terms, never also in hallucinations.
- An unsupported claim goes in hallucinations; a misrepresented real claim goes in distortions, never both.
- Do not repeat the same underlying problem across two lists.

SEVERITY — tag every item with exactly one of:
- "trivial": a reader's understanding of the episode is unaffected. For flagged_terms, "absent" items are at most trivial.
- "substantive": a reader would take away something meaningfully wrong or incomplete, but the episode's core subject is still correctly conveyed.
- "central": the problem concerns the episode's core subject, its main guest/entity, or its central argument — a reader would materially misunderstand what the episode was about or who said what.

If a list has no problems, return it empty. An empty list is a real finding, not a failure — do not invent items to fill it.

Respond with ONLY valid JSON in this exact shape:
{{
  "hallucinations": [{{"item": "<specific claim>", "severity": "trivial|substantive|central"}}],
  "distortions": [{{"item": "<specific claim and how it's distorted>", "severity": "trivial|substantive|central"}}],
  "missed_topics": [{{"item": "<specific topic>", "severity": "trivial|substantive|central"}}],
  "flagged_terms": [{{"item": "<specific name/term>", "kind": "contradicted|absent", "severity": "trivial|substantive|central"}}],
  "rationale": "<1-3 sentence justification>"
}}"""


def _extract_json(text):
    """Strip an optional markdown code fence — Claude has no forced JSON mode,
    so unlike an OpenAI json_object call this isn't guaranteed to be bare JSON."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return text


def call_llm_json(model, prompt, max_tokens=2500):
    """Call whichever provider owns `model` and return parsed JSON. Anthropic
    models (name starts with "claude") have no assistant-prefill or forced
    JSON mode on Sonnet 5/Opus 5, so we rely on prompt instructions + fence
    stripping there instead of OpenAI's response_format=json_object."""
    if model.startswith("claude"):
        response = _get_anthropic_client().messages.create(
            model=model,
            max_tokens=max_tokens,
            output_config={"effort": "low"},  # scoring against a rubric, not hard reasoning —
                                                # adaptive thinking is on by default on these
                                                # models and can eat the whole token budget
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        if not text:
            raise RuntimeError(f"Empty judge response from {model} (stop_reason={response.stop_reason})")
        return json.loads(_extract_json(text))
    else:
        response = pd.openai_client.chat.completions.create(
            model=model,
            max_completion_tokens=max_tokens,
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


def _validate_evidence(result):
    """Check the judge returned tagged evidence objects, not bare strings.

    The severity tags are load-bearing — every score is derived from them — so a
    malformed list has to fail loudly rather than silently score as "no problems".
    """
    for name in EVIDENCE_LISTS:
        items = result.get(name)
        if not isinstance(items, list):
            raise ValueError(f"judge returned no '{name}' list (got {type(items).__name__})")
        for entry in items:
            if not isinstance(entry, dict) or "item" not in entry:
                raise ValueError(f"'{name}' entry is not an object with an 'item' field: {entry!r}")
            if entry.get("severity") not in SEVERITIES:
                raise ValueError(f"'{name}' entry has invalid severity {entry.get('severity')!r}: {entry['item']!r}")
    for entry in result["flagged_terms"]:
        if entry.get("kind") not in ("contradicted", "absent"):
            raise ValueError(f"flagged_terms entry has invalid kind {entry.get('kind')!r}: {entry['item']!r}")


def _dedupe_evidence(result):
    """Drop items from the claim lists that restate a flagged_terms entry.

    A wrong name is one problem; without this it lands in two lists and moves two
    independent dimensions, which is exactly what inflated the old Flags count.
    """
    term_texts = {e["item"].strip().lower() for e in result["flagged_terms"]}
    for name in ("hallucinations", "distortions", "missed_topics"):
        result[name] = [
            e for e in result[name]
            if not any(t in e["item"].strip().lower() or e["item"].strip().lower() in t for t in term_texts)
        ]


def _severity_counts(*lists):
    counts = dict.fromkeys(SEVERITIES, 0)
    for items in lists:
        for entry in items:
            counts[entry["severity"]] += 1
    return counts


def derive_faithfulness(result):
    """5: nothing flagged. 4: only trivial. 3: 1-2 substantive. 2: 3+ substantive
    or one central. 1: multiple central."""
    c = _severity_counts(result["hallucinations"], result["distortions"])
    if c["central"] >= 2:
        return 1
    if c["central"] == 1 or c["substantive"] >= 3:
        return 2
    if c["substantive"] >= 1:
        return 3
    if c["trivial"]:
        return 4
    return 5


def derive_coverage(result):
    """5: nothing missed. 4: only trivial tangents. 3: one substantive topic
    missing. 2: two or more. 1: the episode's core subject is missing."""
    c = _severity_counts(result["missed_topics"])
    if c["central"]:
        return 1
    if c["substantive"] >= 2:
        return 2
    if c["substantive"] == 1:
        return 3
    if c["trivial"]:
        return 4
    return 5


def derive_proper_nouns(result):
    """5: nothing flagged. 4: one trivial. 3: two or more trivial. 2: one central
    name wrong. 1: multiple central names wrong."""
    c = _severity_counts(result["flagged_terms"])
    if c["central"] >= 2:
        return 1
    if c["central"] == 1:
        return 2
    if c["substantive"] >= 1 or c["trivial"] >= 2:
        return 3
    if c["trivial"] == 1:
        return 4
    return 5


def build_judge_prompt(run):
    return JUDGE_PROMPT.format(
        show=run["show"],
        title=run["episode_title"],
        transcript=run["transcript"][:pd.MAX_TRANSCRIPT_CHARS],
        summary=run["summary"],
    )


def score_from_evidence(result):
    """Validate, dedupe, then derive every score from the judge's evidence.

    Shared by the sync and batch paths so both produce identical records.
    """
    _validate_evidence(result)
    _dedupe_evidence(result)
    result["faithfulness"] = derive_faithfulness(result)
    result["coverage"] = derive_coverage(result)
    result["proper_noun_accuracy"] = derive_proper_nouns(result)
    result["overall"] = compute_overall(result)
    return result


def run_judge_scores(model, show, title, transcript, summary):
    prompt = build_judge_prompt(
        {"show": show, "episode_title": title, "transcript": transcript, "summary": summary}
    )
    return score_from_evidence(call_llm_json(model, prompt))


def compute_overall(scores):
    return round(
        scores["faithfulness"] * FAITHFULNESS_WEIGHT
        + scores["coverage"] * COVERAGE_WEIGHT
        + scores["proper_noun_accuracy"] * PROPER_NOUN_WEIGHT,
        2,
    )


def build_eval_record(run_path, run, judge_result):
    return {
        "run_file": run_path.name,
        "show": run["show"],
        "episode_title": run["episode_title"],
        "judge_model": JUDGE_MODEL,
        "judged_at": time.strftime("%Y%m%d_%H%M%S"),
        "judge": judge_result,
        "human_review": {"overall": None, "notes": None, "reviewed_at": None},
    }


def save_eval(eval_path, eval_data):
    """Write an eval, carrying any existing human review across a re-judge."""
    if eval_path.exists():
        existing = json.loads(eval_path.read_text())
        eval_data["human_review"] = existing.get("human_review", eval_data["human_review"])
    pd.save_json(eval_path, eval_data)


def judge_run(run_path):
    run = json.loads(run_path.read_text())
    judge_result = run_judge_scores(JUDGE_MODEL, run["show"], run["episode_title"], run["transcript"], run["summary"])
    return build_eval_record(run_path, run, judge_result)


def pending_runs(force):
    run_files = sorted(pd.RUNS_DIR.glob("*.json"))
    if not force:
        run_files = [p for p in run_files if not (EVALS_DIR / p.name).exists()]
    return run_files


def cmd_judge(force):
    run_files = pending_runs(force)
    if not run_files:
        print(f"Nothing to judge in {pd.RUNS_DIR} (use --force to re-judge).")
        return

    for run_path in run_files:
        print(f"Judging: {run_path.stem}")
        try:
            eval_data = judge_run(run_path)
        except Exception as e:
            print(f"  Failed: {e}")
            continue

        eval_path = EVALS_DIR / run_path.name
        save_eval(eval_path, eval_data)
        j = eval_data["judge"]
        print(f"  faithfulness={j['faithfulness']} coverage={j['coverage']} "
              f"proper_nouns={j['proper_noun_accuracy']} overall={j['overall']}")


def _batch_manifest_path(batch_id):
    return EVALS_DIR / f"_batch_{batch_id}.json"


def cmd_judge_batch(force, poll_seconds):
    """Submit all pending runs as one Anthropic batch — 50% of standard pricing.

    Judging is never latency-sensitive (summaries already exist), so the async
    tradeoff is free here. The batch id and its custom_id -> run file mapping are
    written to disk immediately after submit, so an interrupted poll can be
    resumed with `judge-collect <batch id>` rather than re-paying for the batch.
    """
    if not JUDGE_MODEL.startswith("claude"):
        raise SystemExit(f"Batch judging uses the Anthropic Batches API, but JUDGE_MODEL is {JUDGE_MODEL!r}.")

    run_files = pending_runs(force)
    if not run_files:
        print(f"Nothing to judge in {pd.RUNS_DIR} (use --force to re-judge).")
        return

    requests, mapping = [], {}
    for i, run_path in enumerate(run_files):
        custom_id = f"run-{i}"
        mapping[custom_id] = run_path.name
        requests.append(
            Request(
                custom_id=custom_id,
                params=MessageCreateParamsNonStreaming(
                    model=JUDGE_MODEL,
                    max_tokens=2500,
                    output_config={"effort": "low"},
                    messages=[{"role": "user", "content": build_judge_prompt(json.loads(run_path.read_text()))}],
                ),
            )
        )

    batch = _get_anthropic_client().messages.batches.create(requests=requests)
    pd.save_json(_batch_manifest_path(batch.id), {"batch_id": batch.id, "mapping": mapping})
    print(f"Submitted batch {batch.id} with {len(requests)} request(s).")
    print(f"Manifest: {_batch_manifest_path(batch.id).relative_to(pd.BASE_DIR)}")
    print("Most batches finish within an hour (24h max). Ctrl+C is safe — resume with:")
    print(f"  python scripts/eval_runs.py judge-collect {batch.id}")

    _poll_batch(batch.id, poll_seconds)
    cmd_judge_collect(batch.id)


def _poll_batch(batch_id, poll_seconds):
    client = _get_anthropic_client()
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        if batch.processing_status == "ended":
            counts = batch.request_counts
            print(f"Batch ended — succeeded={counts.succeeded} errored={counts.errored} "
                  f"canceled={counts.canceled} expired={counts.expired}")
            return
        print(f"  status={batch.processing_status} processing={batch.request_counts.processing}")
        time.sleep(poll_seconds)


def cmd_judge_collect(batch_id):
    """Write evals from a finished batch. Results arrive in any order, so every
    result is keyed back to its run file by custom_id, never by position."""
    manifest_path = _batch_manifest_path(batch_id)
    if not manifest_path.exists():
        raise SystemExit(f"No manifest for batch {batch_id} at {manifest_path}")
    mapping = json.loads(manifest_path.read_text())["mapping"]

    written = failed = 0
    for result in _get_anthropic_client().messages.batches.results(batch_id):
        run_name = mapping.get(result.custom_id)
        if run_name is None:
            print(f"  {result.custom_id}: not in manifest, skipping")
            continue

        if result.result.type != "succeeded":
            print(f"  {run_name}: {result.result.type}")
            failed += 1
            continue

        text = "".join(b.text for b in result.result.message.content if b.type == "text")
        run_path = pd.RUNS_DIR / run_name
        try:
            judge_result = score_from_evidence(json.loads(_extract_json(text)))
        except Exception as e:
            print(f"  {run_name}: unusable response — {e}")
            failed += 1
            continue

        run = json.loads(run_path.read_text())
        save_eval(EVALS_DIR / run_name, build_eval_record(run_path, run, judge_result))
        j = judge_result
        print(f"  {run_path.stem[:58]} faith={j['faithfulness']} cover={j['coverage']} "
              f"nouns={j['proper_noun_accuracy']} overall={j['overall']}")
        written += 1

    print(f"Wrote {written} eval(s), {failed} failed.")


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
        for name in EVIDENCE_LISTS:
            for entry in j.get(name, []):
                kind = f", {entry['kind']}" if "kind" in entry else ""
                print(f"  [{name} · {entry['severity']}{kind}] {entry['item']}")
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
            sum(len(j.get(name, [])) for name in EVIDENCE_LISTS),
        ))

    header = f"{'Episode':45} {'Faith':>6} {'Cover':>6} {'Nouns':>6} {'JOvr':>5} {'HOvr':>5} {'Flags':>6}"
    lines = [header, "-" * len(header)]
    for r in rows:
        lines.append(f"{r[0]:45} {r[1]:>6} {r[2]:>6} {r[3]:>6} {r[4]:>5} {r[5]:>5} {r[6]:>6}")
    print("\n".join(lines))
    print("\n(JOvr=judge overall, HOvr=your score, Flags=all evidence items across "
          "hallucinations/distortions/missed topics/flagged terms)")

    md_lines = ["# Eval report", "",
                "| Episode | Faithfulness | Coverage | Proper nouns | Judge overall | Human overall | Flags |",
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
    judge_parser.add_argument("--batch", action="store_true",
                              help="Submit as one Anthropic batch at 50%% of standard pricing (async)")
    judge_parser.add_argument("--poll-seconds", type=int, default=60,
                              help="Seconds between batch status checks (default 60)")

    collect_parser = sub.add_parser("judge-collect", help="Write evals from an already-submitted batch")
    collect_parser.add_argument("batch_id")

    sub.add_parser("review", help="Interactively record your own score for judged episodes")
    sub.add_parser("report", help="Print and save a summary table of judge vs. human scores")

    args = parser.parse_args()
    EVALS_DIR.mkdir(parents=True, exist_ok=True)

    if args.command == "judge":
        if args.batch:
            cmd_judge_batch(args.force, args.poll_seconds)
        else:
            cmd_judge(args.force)
    elif args.command == "judge-collect":
        cmd_judge_collect(args.batch_id)
    elif args.command == "review":
        cmd_review()
    elif args.command == "report":
        cmd_report()
