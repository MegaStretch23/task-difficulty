#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""task_difficulty.py - score how hard a task description looks BEFORE you dispatch it to an agent.

Input: free text, or JSON with any of description / files / steps / needs_browser / needs_tools / verify.
Output: score 0-100, tier (easy / medium / hard), recommended model tier (small / medium / large),
and a token + minute budget estimate. Deterministic keyword and count heuristics, standard library
only, no network calls, no model calls. The weights live in one dict, CONFIG. An optional history
file of past outcomes re-weights the features and rescales the budget (see calibrate()).

    python3 task_difficulty.py "Refactor auth.py and db.py, then run the tests"
    python3 task_difficulty.py --file task.json --history outcomes.jsonl
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import re
import sys

CONFIG = {
    # Each feature is turned into a value in 0..1 (count / cap, clipped), then multiplied by its weight.
    # The weights sum to 100, so a task that saturates every feature scores 100.
    "weights": {"length": 15, "files": 20, "steps": 20, "ambiguity": 15, "tools": 15, "verify": 15},
    "caps": {"length": 250, "files": 6, "steps": 6, "ambiguity": 4, "tools": 4, "verify": 3},
    "tier_cuts": {"easy_below": 30, "medium_below": 60},
    "models": {"easy": "small", "medium": "medium", "hard": "large"},
    # Budget = base + per_point * score. Assumed starting values, not measured on any real agent;
    # calibrate() rescales them from your own history.
    "budget": {"tokens_base": 8000, "tokens_per_point": 1500, "minutes_base": 2.0, "minutes_per_point": 0.6},
    "min_history_rows": 5,
}

FILE_RE = re.compile(r"(?<![\w/.-])(?:[\w.-]+/)*[\w-]+\.(?:py|js|jsx|ts|tsx|json|jsonl|md|txt|yaml|yml|toml|ini|cfg|sh|sql|html|css|c|h|cpp|go|rs|java|rb|csv)\b", re.I)
STEP_RE = re.compile(r"(?m)^\s*(?:\d+[.)]|[-*])\s+\S")
SEQ_RE = re.compile(r"\b(?:then|after that|afterwards|next|finally|followed by|once (?:that|it|done))\b", re.I)
AMBIGUITY = ["somehow", "maybe", "perhaps", "something", "somewhat", "etc", "and so on", "whatever", "appropriate", "appropriately", "properly",
             "as needed", "if needed", "if possible", "figure out", "improve", "clean up", "nice", "better", "good", "robust", "tbd", "various", "several", "some kind of", "or similar"]
TOOLS = ["browser", "chrome", "playwright", "selenium", "click", "log in", "login", "sign in", "scrape", "deploy", "ssh", "docker", "kubernetes", "database",
         "sql", "api call", "curl", "download", "install", "migrate", "migration", "web search", "screenshot", "cron", "oauth", "webhook"]
VERIFY = ["test", "tests", "pytest", "verify", "validate", "benchmark", "lint", "typecheck", "type check", "prove", "read back", "reproduce", "must pass",
          "assert", "coverage", "regression", "ensure", "double-check", "confirm"]
FEATURES = list(CONFIG["weights"])


def _count_phrases(text: str, phrases) -> int:
    low = text.lower()
    return sum(len(re.findall(r"(?<![a-z])" + re.escape(p) + r"(?![a-z])", low)) for p in phrases)


def normalize(task) -> dict:
    """Accept a str, a JSON string or a dict. Returns {description, files, steps, needs_browser, needs_tools, verify}."""
    if isinstance(task, str):
        s = task.strip()
        if s.startswith("{"):
            try:
                task = json.loads(s)
            except ValueError:
                pass
    if isinstance(task, str):
        task = {"description": task}
    if not isinstance(task, dict):
        raise ValueError("task must be text or a JSON object")
    desc = task.get("description", task.get("task", task.get("text", "")))
    if not isinstance(desc, str):
        raise ValueError("description must be a string")
    files = task.get("files") or []
    steps = task.get("steps") or []
    if not isinstance(files, list) or not isinstance(steps, list):
        raise ValueError("files and steps must be lists")
    return {"description": desc, "files": [str(f) for f in files], "steps": [str(s) for s in steps],
            "needs_browser": bool(task.get("needs_browser", False)), "needs_tools": [str(t) for t in (task.get("needs_tools") or [])],
            "verify": bool(task.get("verify", False))}


def extract_features(task) -> dict:
    """Raw counts per feature (before caps and weights)."""
    t = normalize(task)
    text = " ".join([t["description"], " ".join(t["steps"])])
    files = {m.group(0).lower() for m in FILE_RE.finditer(text)} | {f.lower() for f in t["files"]}
    steps = len(STEP_RE.findall(t["description"])) + len(t["steps"]) + len(SEQ_RE.findall(t["description"]))
    tools = _count_phrases(text, TOOLS) + len(t["needs_tools"]) + (2 if t["needs_browser"] else 0)
    verify = _count_phrases(text, VERIFY) + (1 if t["verify"] else 0)
    return {"length": len(text.split()), "files": len(files), "steps": steps,
            "ambiguity": _count_phrases(text, AMBIGUITY), "tools": tools, "verify": verify}


def _unit(raw: dict, cfg: dict) -> dict:
    return {k: min(raw[k] / cfg["caps"][k], 1.0) for k in FEATURES}


def tier_of(score: float, cfg: dict) -> str:
    cuts = cfg["tier_cuts"]
    return "easy" if score < cuts["easy_below"] else "medium" if score < cuts["medium_below"] else "hard"


def score_task(task, config: dict | None = None) -> dict:
    cfg = config or CONFIG
    raw = extract_features(task)
    unit = _unit(raw, cfg)
    parts = {k: round(unit[k] * cfg["weights"][k], 2) for k in FEATURES}
    score = int(round(min(100.0, max(0.0, sum(unit[k] * cfg["weights"][k] for k in FEATURES)))))
    tier = tier_of(score, cfg)
    b = cfg["budget"]
    scale_t, scale_m = b.get("tokens_scale", 1.0), b.get("minutes_scale", 1.0)
    tokens = (b["tokens_base"] + b["tokens_per_point"] * score) * scale_t
    minutes = (b["minutes_base"] + b["minutes_per_point"] * score) * scale_m
    return {"score": score, "tier": tier, "recommended_model_tier": cfg["models"][tier],
            "budget": {"tokens": int(round(tokens, -2)), "minutes": round(minutes, 1)},
            "features": raw, "points": parts}


def _pearson(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def load_history(path: str) -> list:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError as e:
                raise ValueError(f"{path}:{i}: not JSON ({e})")
            if not isinstance(r, dict) or "task" not in r:
                raise ValueError(f"{path}:{i}: each row needs a 'task' field")
            rows.append(r)
    return rows


def calibrate(rows: list, config: dict | None = None) -> dict:
    """Return a NEW config. History rows: {"task": text-or-object, "tokens": N?, "minutes": M?, "outcome": "success"|"fail"?}.
    Target per row = log(tokens) when tokens is given, else 1 for a failed run and 0 for a success. Each feature's weight is multiplied by
    (1 + r), r = Pearson correlation of the feature with the target clipped to [-0.5, 0.5], then the weights are rescaled to the original sum.
    The budget scales become the median of actual / estimated for the rows that carry tokens / minutes, clipped to [0.25, 4]."""
    cfg = copy.deepcopy(config or CONFIG)
    if len(rows) < cfg["min_history_rows"]:
        raise ValueError(f"need at least {cfg['min_history_rows']} history rows, got {len(rows)}")
    unit_rows, targets = [], []
    for r in rows:
        unit_rows.append(_unit(extract_features(r["task"]), cfg))
        if isinstance(r.get("tokens"), (int, float)) and r["tokens"] > 0:
            targets.append(math.log(r["tokens"]))
        elif r.get("outcome") in ("success", "fail"):
            targets.append(1.0 if r["outcome"] == "fail" else 0.0)
        else:
            raise ValueError("each history row needs a positive 'tokens' or an 'outcome' of success/fail")
    total = sum(cfg["weights"].values())
    new = {}
    for k in FEATURES:
        r = max(-0.5, min(0.5, _pearson([u[k] for u in unit_rows], targets)))
        new[k] = cfg["weights"][k] * (1 + r)
    s = sum(new.values())
    cfg["weights"] = {k: round(v * total / s, 2) for k, v in new.items()}
    base = copy.deepcopy(CONFIG if config is None else config)
    for key, field, name in (("tokens", "tokens", "tokens_scale"), ("minutes", "minutes", "minutes_scale")):
        ratios = []
        for r in rows:
            if isinstance(r.get(field), (int, float)) and r[field] > 0:
                est = score_task(r["task"], base)["budget"][key]
                if est > 0:
                    ratios.append(r[field] / est)
        if ratios:
            ratios.sort()
            med = ratios[len(ratios) // 2] if len(ratios) % 2 else (ratios[len(ratios) // 2 - 1] + ratios[len(ratios) // 2]) / 2
            cfg["budget"][name] = round(max(0.25, min(4.0, med)), 3)
    cfg["calibrated_from_rows"] = len(rows)
    return cfg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Score how hard a task looks before dispatching it. Deterministic heuristics, no network.")
    ap.add_argument("text", nargs="?", help="task description (or a JSON object as text)")
    ap.add_argument("--file", help="read the task from a text or JSON file ('-' = stdin)")
    ap.add_argument("--history", help="JSONL of past outcomes used to re-weight the features and rescale the budget")
    ap.add_argument("--compact", action="store_true", help="one-line JSON")
    a = ap.parse_args(argv)
    try:
        if a.file:
            raw = sys.stdin.read() if a.file == "-" else open(a.file, encoding="utf-8").read()
        elif a.text is not None:
            raw = a.text
        else:
            print("FAILED: give a task description or --file", file=sys.stderr)
            return 2
        cfg = None
        if a.history:
            cfg = calibrate(load_history(a.history))
        out = score_task(raw, cfg)
        if cfg:
            out["calibrated"] = {"rows": cfg["calibrated_from_rows"], "weights": cfg["weights"],
                                 "tokens_scale": cfg["budget"].get("tokens_scale", 1.0), "minutes_scale": cfg["budget"].get("minutes_scale", 1.0)}
    except (OSError, ValueError) as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out, sort_keys=False) if a.compact else json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
