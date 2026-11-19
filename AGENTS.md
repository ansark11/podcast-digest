# podcast-digest

Weekly pipeline: finds new episodes of the shows in `config/shows.json`,
transcribes them, summarizes them into structured JSON in `summaries/`, and an
Astro web app in `web/` (deployed on Vercel) turns each summary into a page.

## Commands
- Test (before every commit): `./scripts/test.sh` (about 1 second)
- Web app: `cd web && npm run dev` (local), `npm run build` (what Vercel runs)
- Fill missing metadata without new episodes: `python scripts/podcast_digest.py --refresh-metadata-only`
- Evals: `python scripts/evals/resummarize.py --tag <tag>`, then
  `python scripts/evals/eval_runs.py judge | review | report`

## Done means
- `./scripts/test.sh` passes and new behaviour has a test.
- Web changes: `npm run build` in `web/` succeeds.
- Prompt changes: `PROMPT_VERSION` in `podcast_digest.py` is bumped and a new eval
  take was judged against the previous one.

## Conventions
- One summary file per episode, matched by the episode's guid. Re-processing
  replaces the file; never add a second one.
- Each summary records `prompt_version` and `summary_model`.
- Summaries come back as JSON via structured outputs; keep the shape stable,
  the web app reads every field.
- Eval work stays in `evals/` (gitignored). Eval scripts never write to
  `summaries/`; only the production pipeline does.
- `JUDGE_MODEL` must be a different model family from `SUMMARY_MODEL`. The judge
  lists errors and their severity; code turns that into the score.

## Gotchas
- The weekly job does not run this folder. launchd runs its own clone of `main`
  in `~/Library/Application Support/podcast-digest/repo`, so changes reach it
  only once pushed to `main`. Re-run `scripts/scheduler/install.sh` after
  changing `.env` or `requirements.txt`.
- The weekly job commits to `main` itself, so local `main` is often behind:
  `git pull` before branching.
- GitHub Actions can't fetch Substack feeds (403), which is why the schedule
  runs on the Mac.

## Boundaries
- Ask before running `scripts/podcast_digest.py`, `backfill.py`,
  `resummarize.py`, `eval_runs.py judge` or `weekly_run.py`: they spend OpenAI
  or Anthropic credits, and the pipeline pushes to `main`, which redeploys the
  live site.
- Never commit `.env` or anything under `evals/`.
