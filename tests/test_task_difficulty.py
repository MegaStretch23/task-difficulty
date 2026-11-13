import copy, json, subprocess, sys
from pathlib import Path
import pytest
import task_difficulty as td

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
HARD = (SAMPLES / "example_task.txt").read_text()


def test_empty_text_scores_zero_easy():
    r = td.score_task("")
    assert r["score"] == 0 and r["tier"] == "easy" and r["recommended_model_tier"] == "small"


def test_simple_rename_is_easy():
    assert td.score_task("Rename the variable tmp to buffer in utils.py.")["tier"] == "easy"


def test_example_is_hard():
    r = td.score_task(HARD)
    assert r["tier"] == "hard" and r["recommended_model_tier"] == "large"


def test_score_bounds_on_huge_input():
    r = td.score_task("maybe somehow etc deploy browser verify test a.py b.py c.py d.py e.py f.py g.py " * 200)
    assert 80 <= r["score"] <= 100


def test_weights_sum_to_100():
    assert sum(td.CONFIG["weights"].values()) == 100


def test_deterministic():
    assert td.score_task(HARD) == td.score_task(HARD)


def test_files_counted_once_case_insensitive():
    assert td.extract_features("edit a.py and A.PY and b.py")["files"] == 2


def test_files_directory_paths():
    assert td.extract_features("see src/app/main.ts and tests/x.py")["files"] == 2


def test_version_numbers_are_not_files():
    assert td.extract_features("upgrade to 3.11 and 2.5")["files"] == 0


def test_numbered_steps_counted():
    assert td.extract_features("1. a\n2. b\n3. c")["steps"] == 3


def test_sequence_words_count_as_steps():
    assert td.extract_features("do a, then b, finally c")["steps"] == 2


def test_ambiguity_words():
    assert td.extract_features("Improve it somehow, etc.")["ambiguity"] == 3


def test_ambiguity_word_boundaries():
    assert td.extract_features("The goodwill nicety")["ambiguity"] == 0


def test_tool_and_verify_words():
    f = td.extract_features("deploy with docker, then run the tests and verify")
    assert f["tools"] == 2 and f["verify"] == 2


def test_json_input_fields():
    t = {"description": "Do the thing", "files": ["a.py", "b.py"], "steps": ["one", "two"], "needs_browser": True, "verify": True}
    f = td.extract_features(t)
    assert f["files"] == 2 and f["steps"] == 2 and f["tools"] == 2 and f["verify"] == 1


def test_json_string_input_equals_dict():
    t = {"description": "Fix a.py", "steps": ["x"]}
    assert td.score_task(json.dumps(t)) == td.score_task(t)


def test_malformed_json_text_is_treated_as_text():
    assert td.score_task("{not json at all")["features"]["length"] == 4


def test_bad_types_raise():
    with pytest.raises(ValueError):
        td.score_task(42)
    with pytest.raises(ValueError):
        td.score_task({"description": 5})
    with pytest.raises(ValueError):
        td.score_task({"description": "x", "files": "a.py"})


def test_more_files_never_lower_score():
    base = "Change {} please"
    scores = [td.score_task(base.format(" ".join(f"f{i}.py" for i in range(n))))["score"] for n in range(0, 8)]
    assert scores == sorted(scores)


def test_tier_cuts_are_configurable():
    cfg = copy.deepcopy(td.CONFIG)
    cfg["tier_cuts"] = {"easy_below": 1, "medium_below": 2}
    assert td.score_task("Rename tmp in utils.py.", cfg)["tier"] == "hard"


def test_weights_in_one_dict_change_score():
    cfg = copy.deepcopy(td.CONFIG)
    cfg["weights"] = {k: 0 for k in td.FEATURES}
    cfg["weights"]["files"] = 100
    assert td.score_task("a.py b.py c.py d.py e.py f.py", cfg)["score"] == 100


def test_budget_grows_with_score():
    lo, hi = td.score_task("Rename tmp in utils.py."), td.score_task(HARD)
    assert hi["budget"]["tokens"] > lo["budget"]["tokens"] and hi["budget"]["minutes"] > lo["budget"]["minutes"]


def test_default_config_not_mutated_by_calibrate():
    before = copy.deepcopy(td.CONFIG)
    td.calibrate(td.load_history(str(SAMPLES / "history_example.jsonl")))
    assert td.CONFIG == before


def test_calibrate_keeps_weight_total_and_adds_scales():
    cfg = td.calibrate(td.load_history(str(SAMPLES / "history_example.jsonl")))
    assert abs(sum(cfg["weights"].values()) - 100) < 0.1
    assert cfg["calibrated_from_rows"] == 8 and 0.25 <= cfg["budget"]["tokens_scale"] <= 4


def test_calibrate_raises_weight_of_predictive_feature():
    rows = [{"task": " ".join(f"f{i}.py" for i in range(n)) + " x", "tokens": 5000 * (n + 1)} for n in range(7)]
    cfg = td.calibrate(rows)
    assert cfg["weights"]["files"] > td.CONFIG["weights"]["files"]


def test_calibrate_with_outcomes_only():
    rows = [{"task": "ok.py", "outcome": "success"}] * 3 + [{"task": "maybe somehow etc improve", "outcome": "fail"}] * 3
    cfg = td.calibrate(rows)
    assert cfg["weights"]["ambiguity"] > td.CONFIG["weights"]["ambiguity"]


def test_calibrate_rejects_too_few_rows_and_bad_rows():
    with pytest.raises(ValueError):
        td.calibrate([{"task": "a", "tokens": 5}])
    with pytest.raises(ValueError):
        td.calibrate([{"task": "a"}] * 5)


def test_load_history_errors(tmp_path):
    p = tmp_path / "h.jsonl"
    p.write_text('{"task": "a"}\nnot json\n')
    with pytest.raises(ValueError, match=":2:"):
        td.load_history(str(p))


def run_cli(*args, stdin=None):
    return subprocess.run([sys.executable, str(ROOT / "task_difficulty.py"), *args], capture_output=True, text=True, input=stdin)


def test_cli_matches_saved_sample():
    r = run_cli("--file", str(SAMPLES / "example_task.txt"))
    assert r.returncode == 0 and json.loads(r.stdout) == json.loads((SAMPLES / "example_output.json").read_text())


def test_cli_calibrated_matches_saved_sample():
    r = run_cli("--file", str(SAMPLES / "example_task.txt"), "--history", str(SAMPLES / "history_example.jsonl"))
    assert r.returncode == 0 and json.loads(r.stdout) == json.loads((SAMPLES / "example_calibrated.json").read_text())


def test_cli_stdin_and_compact():
    r = run_cli("--file", "-", "--compact", stdin="Rename tmp in utils.py")
    assert r.returncode == 0 and r.stdout.count("\n") == 1


def test_cli_failures_say_failed():
    assert "FAILED" in run_cli().stderr
    r = run_cli("--file", "/nonexistent/x.txt")
    assert r.returncode == 1 and r.stderr.startswith("FAILED")
    assert run_cli("x", "--history", "/nonexistent/h.jsonl").returncode == 1


def test_readme_sample_block_is_the_saved_run():
    readme = (ROOT / "README.md").read_text()
    saved = (SAMPLES / "example_output.json").read_text().strip()
    assert saved in readme
    assert (SAMPLES / "example_task.txt").read_text().strip() in readme
    assert "(SAMPLES_SAVED)" not in readme


def test_no_network_imports():
    src = (ROOT / "task_difficulty.py").read_text()
    for bad in ("import socket", "import requests", "urllib", "http.client", "subprocess"):
        assert bad not in src
