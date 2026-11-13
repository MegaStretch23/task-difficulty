# task-difficulty

Score how hard a task description looks **before** you hand it to an agent. Give it text (or a small JSON object) and it returns a 0-100 score, a tier (`easy`, `medium`, `hard`), a recommended model tier (`small`, `medium`, `large`) and a token and minute budget estimate.

It is a few keyword and count heuristics in one Python file: standard library only, no network calls, no model calls, same input gives the same output. The weights sit in one dict (`CONFIG`) so you can change them, and an optional `--history` file of your own past runs re-weights them.

**What it is not:** a measured predictor. The default weights and budgets are starting values I chose, not fitted to real agent runs, and I have not measured how well the score predicts failures or cost. Treat the number as a rough triage signal and calibrate it on your own history before relying on it.

## Quickstart

Python 3.9+, nothing to install.

```bash
git clone https://github.com/MegaStretch23/task-difficulty && cd task-difficulty
python3 task_difficulty.py --file samples/example_task.txt
```

`samples/example_task.txt`:

```text
Refactor the retry logic in worker.py and queue.py so failed jobs are handled properly, then:
1. update tests/test_worker.py
2. deploy to staging with docker and check the logs in the browser
3. verify nothing regresses and run the tests
Clean up anything else that looks off, etc.
```

Output (saved from a real run as `samples/example_output.json`; a test fails if this README and the saved run drift apart):

```json
{
  "score": 64,
  "tier": "hard",
  "recommended_model_tier": "large",
  "budget": {
    "tokens": 104000,
    "minutes": 40.4
  },
  "features": {
    "length": 47,
    "files": 3,
    "steps": 4,
    "ambiguity": 3,
    "tools": 3,
    "verify": 4
  },
  "points": {
    "length": 2.82,
    "files": 10.0,
    "steps": 13.33,
    "ambiguity": 11.25,
    "tools": 11.25,
    "verify": 15.0
  }
}
```

`features` are the raw counts it found, `points` is what each feature added to the score (they sum to the score before rounding). Pass text directly with `python3 task_difficulty.py "Rename tmp to buffer in utils.py"` (score 4, `easy`, `small`, see `samples/easy_task.txt`), read stdin with `--file -`, or use `--compact` for one-line JSON.

JSON input: `{"description": "...", "files": ["a.py"], "steps": ["..."], "needs_browser": true, "needs_tools": ["docker"], "verify": true}`. All fields optional.

As a library:

```python
import task_difficulty as td
r = td.score_task("Fix the retry bug in worker.py, then run the tests")
r["score"], r["tier"], r["recommended_model_tier"], r["budget"]
```

## How the score is built

Six features, each counted, divided by a cap, clipped to 0..1 and multiplied by its weight (the weights sum to 100):

| feature | weight | cap | what is counted |
|---|---|---|---|
| length | 15 | 250 words | words in the description and steps |
| files | 20 | 6 | distinct file names (`name.ext` with a common extension) plus the JSON `files` list |
| steps | 20 | 6 | numbered or bulleted lines, JSON `steps`, and sequence words (then, next, finally...) |
| ambiguity | 15 | 4 | vague words: somehow, maybe, etc, improve, clean up, whatever, as needed... |
| tools | 15 | 4 | tool or browser needs: browser, deploy, docker, login, scrape, sql, ... (`needs_browser` counts 2) |
| verify | 15 | 3 | test or verify requirements: tests, pytest, verify, benchmark, must pass, ... |

Tiers: below 30 is `easy`, below 60 is `medium`, otherwise `hard`. Model tier: `small`, `medium`, `large` (generic names, map them to your own models in `CONFIG["models"]`). Budget = base + per-point rate times the score: `{tokens: 8000 + 1500 x score, minutes: 2.0 + 0.6 x score}`. Those budget numbers are assumed, not measured. The word lists are plain Python lists at the top of the file; extend them for your domain.

## Calibrating with your own history

`--history outcomes.jsonl` takes one JSON object per line: `{"task": "<text or object>", "tokens": 52000, "minutes": 14}` and/or `"outcome": "success"|"fail"`. At least 5 rows. Each feature's weight is multiplied by `1 + r`, where `r` is the Pearson correlation between that feature and log(tokens) (or 1 for a failed run, 0 for a success, when tokens are missing), clipped to +-0.5; weights are rescaled to still sum to 100. Budgets are scaled by the median of actual / estimated.

`samples/history_example.jsonl` is **8 invented rows** I wrote to show the format, not real agent runs, so its numbers say nothing about real workloads:

```json
{"task": "Rename the variable tmp to buffer in utils.py.", "tokens": 9000, "minutes": 2}
{"task": "Fix the typo in README.md", "tokens": 6000, "minutes": 1}
{"task": "Add a --verbose flag to cli.py and update tests/test_cli.py, then run the tests", "tokens": 52000, "minutes": 14}
{"task": "Update config.yaml and settings.py so the timeout is 30 seconds. Verify with pytest.", "tokens": 41000, "minutes": 11}
{"task": "1. refactor db.py\n2. migrate the database schema with sql\n3. deploy with docker\n4. verify and run the tests", "tokens": 160000, "minutes": 55}
{"task": "Scrape the pricing page in the browser, then log in and download the csv, clean up whatever looks off, etc.", "tokens": 120000, "minutes": 48}
{"task": "Improve the error handling somehow in api.py, server.py and client.py, then test it properly", "tokens": 98000, "minutes": 30}
{"task": "Delete the unused import in main.py", "tokens": 7000, "minutes": 1}
```

```bash
python3 task_difficulty.py --file samples/example_task.txt --history samples/history_example.jsonl
```

Saved run (`samples/example_calibrated.json`): `steps` weight went 20 to 20.9 and `files` 20 to 16.41, token scale 1.165, minute scale 0.849, and the same task's budget became 121,200 tokens and 34.3 minutes. A bad history file stops with a `FAILED: <cause>` line and a non-zero exit code.

## What was measured

| item | value |
|---|---|
| speed | 0.63 ms per call on the 47-word example (2,000 calls averaged, Python 3.12.3, 8 logical CPUs, x86_64 Linux) |
| tests | 34 offline tests, about 0.4 s, `python3 -m pip install pytest && python3 -m pytest -q` |
| network | none: a test checks that the source imports no network or subprocess modules |

Nothing else is claimed: no accuracy, no cost savings, no comparison with other tools.

## Known limits

- English keywords only; a task written in another language scores mostly on length.
- Word matching is literal, so "tests" in a sentence like "no tests needed" still counts as a verify requirement.
- File detection looks for `name.ext` with a fixed list of extensions; paths without an extension are missed.
- A short task can be hard ("prove this theorem") and a long one easy; length is only one of six signals.

## Context

I use this idea for dispatching tasks in a multi-agent setup (see the [Fleet Supervisor](https://huggingface.co/spaces/JohnVeds15/fleet-supervisor) landing page for Claude / LLM agent-fleet operators, and the [Claude Code Fleet Dashboard](https://huggingface.co/spaces/JohnVeds15/claude-code-fleet-dashboard), which shows one row per session from your own session logs in the browser). This repo is independent of those and of Anthropic.

## Also by the author

Unrelated to this tool, there is no paid version of task-difficulty. Other things I publish: a pay-per-event [SEC Form 4 insider cluster scanner](https://apify.com/johnveds15/sec-form4-insider-clusters) on Apify ($0.0004 per Form 4 filing parsed, $0.02 per insider cluster detected, price read from the Apify API on 2026-11-13), and two source-code packs for agent work on Gumroad: the [Agent-Turn Delta-Debugger CLI](https://vedovino.gumroad.com/l/zdwyic) ($25) and the [Agent log policy checker](https://vedovino.gumroad.com/l/agent-log-policy-checker) ($19), prices as listed on 2026-11-13.

## License

MIT, see `LICENSE`.
