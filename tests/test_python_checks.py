import json
import os
import io
import subprocess
import sys
import tomllib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest
from ci_workflows.checks import main, survivor_check


def invoke(*args):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main([*map(str, args)])
    return SimpleNamespace(
        returncode=code, stdout=stdout.getvalue(), stderr=stderr.getvalue()
    )


def test_module_entry_point_runs(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ci_workflows",
            "floor",
            "--report",
            str(report(tmp_path)),
            "--head-floor",
            "80",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "coverage 85.0% meets floor 80%\n",
        "",
    )


def report(tmp_path, rate="0.85", lines=((12, 1, None),)):
    entries = "".join(
        f'<line number="{number}" hits="{hits}"'
        + (f' branch="true" condition-coverage="{branch}"' if branch else "")
        + "/>"
        for number, hits, branch in lines
    )
    path = tmp_path / "coverage.xml"
    path.write_text(
        f'<coverage line-rate="{rate}" branch-rate="{rate}"><packages><package name="pkg">'
        f'<classes><class filename="pkg/f.py"><lines>{entries}</lines></class>'
        "</classes></package></packages></coverage>"
    )
    return path


def patch(tmp_path, added_line=12, file="pkg/f.py"):
    path = tmp_path / "change.patch"
    path.write_text(
        f"diff --git a/{file} b/{file}\n"
        f"--- a/{file}\n+++ b/{file}\n"
        f"@@ -{added_line - 1},0 +{added_line},1 @@\n+new code\n"
    )
    return path


@pytest.mark.parametrize(
    ("lines", "expected_code", "expected_output"),
    [
        (((12, 1, None),), 0, "all changed lines covered\n"),
        (((12, 0, None),), 1, "uncovered changed lines: pkg/f.py:12\n"),
        (((12, 1, "50% (1/2)"),), 1, "uncovered changed lines: pkg/f.py:12\n"),
    ],
)
def test_changed_python_lines(tmp_path, lines, expected_code, expected_output):
    result = invoke(
        "changed-lines",
        "--report",
        report(tmp_path, lines=lines),
        "--diff-file",
        patch(tmp_path),
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        expected_code,
        expected_output,
        "",
    )


def test_no_changed_python_lines(tmp_path):
    result = invoke(
        "changed-lines",
        "--report",
        report(tmp_path),
        "--diff-file",
        patch(tmp_path, file="README.md"),
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "no changed Python lines to check\n",
        "",
    )


def test_deleted_only_hunk_with_context_has_no_added_lines(tmp_path):
    repository = tmp_path / "repo"
    source = repository / "pkg" / "f.py"
    source.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    source.write_text("first\nremoved\nlast\n")
    subprocess.run(["git", "add", "pkg/f.py"], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "baseline",
        ],
        cwd=repository,
        check=True,
    )
    source.write_text("first\nlast\n")
    generated = subprocess.run(
        ["git", "diff", "--unified=3", "HEAD", "--", "pkg/f.py"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    change = tmp_path / "change.patch"
    change.write_text(generated.stdout)
    result = invoke(
        "changed-lines", "--report", report(tmp_path), "--diff-file", change
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "no changed Python lines to check\n",
        "",
    )


def base_workflow(tmp_path, floor):
    path = tmp_path / "base.yml"
    path.write_text(f"jobs:\n  checks:\n    with:\n      coverage_floor: {floor}\n")
    return path


@pytest.mark.parametrize(
    ("base", "head", "rate", "expected_code", "expected_output"),
    [
        ("80", "80", "0.85", 0, "coverage 85.0% meets floor 80%\n"),
        ("80", "79", "0.85", 1, "coverage floor fell from 80% to 79%\n"),
        ("80", "80", "0.799", 1, "coverage 79.9% is below floor 80%\n"),
        (None, "80", "0.85", 0, "coverage 85.0% meets floor 80%\n"),
    ],
)
def test_floor_ratchet(tmp_path, base, head, rate, expected_code, expected_output):
    args = ["floor", "--report", report(tmp_path, rate=rate), "--head-floor", head]
    if base is not None:
        args.extend(("--base-workflow", base_workflow(tmp_path, base)))
    result = invoke(*args)
    assert (result.returncode, result.stdout, result.stderr) == (
        expected_code,
        expected_output,
        "",
    )


def test_python_floor_uses_branch_rate(tmp_path):
    path = report(tmp_path, rate="0.799")
    path.write_text(path.read_text().replace('line-rate="0.799"', 'line-rate="1.0"'))
    result = invoke("floor", "--report", path, "--head-floor", "80")
    assert (result.returncode, result.stdout, result.stderr) == (
        1,
        "coverage 79.9% is below floor 80%\n",
        "",
    )


@pytest.mark.parametrize("floor", ["", "abc", "-1", "101"])
def test_invalid_floor_is_configuration_error(tmp_path, floor):
    result = invoke("floor", "--report", report(tmp_path), "--head-floor", floor)
    assert (result.returncode, result.stdout, result.stderr) == (
        2,
        f"configuration error: invalid coverage floor {floor!r}\n",
        "",
    )


def test_changed_function_selection(tmp_path):
    source = tmp_path / "pkg" / "f.py"
    source.parent.mkdir()
    source.write_text(
        "def a():\n    return 1\n\ndef b():\n    return 2\n\ndef c():\n    return 3\n"
    )
    result = invoke(
        "select-functions",
        "--source",
        source,
        "--source-name",
        "pkg/f.py",
        "--diff-file",
        patch(tmp_path, added_line=5),
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, '["pkg.f.b"]\n', "")


def test_nested_changed_function_names_both_enclosing_functions(tmp_path):
    source = tmp_path / "pkg" / "f.py"
    source.parent.mkdir()
    source.write_text(
        "def outer():\n    def inner():\n        return 1\n    return inner()\n"
    )
    result = invoke(
        "select-functions",
        "--source",
        source,
        "--source-name",
        "pkg/f.py",
        "--diff-file",
        patch(tmp_path, added_line=3),
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        '["pkg.f.outer", "pkg.f.outer.inner"]\n',
        "",
    )


def test_deleted_line_selects_its_function(tmp_path):
    source = tmp_path / "pkg" / "f.py"
    source.parent.mkdir()
    source.write_text("def a():\n    return 1\n\ndef b():\n    return 2\n")
    diff = tmp_path / "deletion.patch"
    diff.write_text(
        "diff --git a/pkg/f.py b/pkg/f.py\n--- a/pkg/f.py\n+++ b/pkg/f.py\n"
        "@@ -5,2 +5 @@\n-    guard()\n     return 2\n"
    )
    result = invoke(
        "select-functions",
        "--source",
        source,
        "--source-name",
        "pkg/f.py",
        "--diff-file",
        diff,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, '["pkg.f.b"]\n', "")


def test_deletion_after_context_selects_new_file_position(tmp_path):
    source = tmp_path / "pkg" / "f.py"
    source.parent.mkdir()
    source.write_text("def a():\n    return 1\n\ndef b():\n    return 2\n")
    diff = tmp_path / "deletion.patch"
    diff.write_text(
        "diff --git a/pkg/f.py b/pkg/f.py\n--- a/pkg/f.py\n+++ b/pkg/f.py\n"
        "@@ -4,3 +4,2 @@\n def b():\n-    guard()\n     return 2\n"
    )
    result = invoke(
        "select-functions",
        "--source",
        source,
        "--source-name",
        "pkg/f.py",
        "--diff-file",
        diff,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, '["pkg.f.b"]\n', "")


def test_multiple_deletions_do_not_shift_later_function_position(tmp_path):
    source = tmp_path / "pkg" / "f.py"
    source.parent.mkdir()
    source.write_text("def a():\n    return 1\n\ndef b():\n    return 2\n")
    diff = tmp_path / "deletions.patch"
    diff.write_text(
        "diff --git a/pkg/f.py b/pkg/f.py\n--- a/pkg/f.py\n+++ b/pkg/f.py\n"
        "@@ -1,7 +1,5 @@\n def a():\n-    first_guard()\n     return 1\n \n def b():\n-    second_guard()\n     return 2\n"
    )
    result = invoke(
        "select-functions",
        "--source",
        source,
        "--source-name",
        "pkg/f.py",
        "--diff-file",
        diff,
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        '["pkg.f.a", "pkg.f.b"]\n',
        "",
    )


def test_trailing_deletion_selects_preceding_function(tmp_path):
    source = tmp_path / "pkg" / "f.py"
    source.parent.mkdir()
    source.write_text("def a():\n    value = 1\n\ndef b():\n    return 2\n")
    diff = tmp_path / "deletion.patch"
    diff.write_text(
        "diff --git a/pkg/f.py b/pkg/f.py\n--- a/pkg/f.py\n+++ b/pkg/f.py\n"
        "@@ -1,4 +1,3 @@\n def a():\n     value = 1\n-    return value\n \n"
    )
    result = invoke(
        "select-functions",
        "--source",
        source,
        "--source-name",
        "pkg/f.py",
        "--diff-file",
        diff,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, '["pkg.f.a"]\n', "")


@pytest.mark.parametrize(
    ("old_source", "new_source", "expected"),
    [
        (
            "def first():\n    return 1\n\ndef target():\n    if True:\n        return 2\n    return 3\n",
            "def first():\n    return 1\n\ndef target():\n    return 3\n",
            '["pkg.f.target"]\n',
        ),
        (
            "def target():\n    value = 1\n    return value\n\ndef after():\n    return 2\n",
            "def target():\n    value = 1\n\ndef after():\n    return 2\n",
            '["pkg.f.target"]\n',
        ),
        (
            "def first(): return 1\n\ndef after(): return 2\n",
            "def first(): return 1\ndef after(): return 2\n",
            '["pkg.f.after", "pkg.f.first"]\n',
        ),
    ],
)
def test_real_zero_context_deletion_selects_function(
    tmp_path, old_source, new_source, expected
):
    repo = tmp_path / "repo"
    source = repo / "pkg" / "f.py"
    source.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    source.write_text(old_source)
    subprocess.run(["git", "add", "pkg/f.py"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "baseline",
        ],
        cwd=repo,
        check=True,
    )
    source.write_text(new_source)
    result = subprocess.run(
        ["git", "diff", "--unified=0", "HEAD", "--", "pkg/f.py"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    diff = tmp_path / "real.patch"
    diff.write_text(result.stdout)
    selected = invoke(
        "select-functions",
        "--source",
        source,
        "--source-name",
        "pkg/f.py",
        "--diff-file",
        diff,
    )
    assert (selected.returncode, selected.stdout, selected.stderr) == (0, expected, "")


def mutation_result(tmp_path, survivors):
    path = tmp_path / "f.py.meta"
    path.write_text(json.dumps({"exit_code_by_key": survivors}))
    return path


def test_mutmut_gate_does_not_judge_freshness_by_timestamps(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "ci_workflows" / "checks.py"
    source.parent.mkdir()
    source.write_text("pass\n")
    result = mutation_result(tmp_path, {"pkg.f.x__mutmut_3": 1})
    os.utime(source, (200, 200))
    os.utime(result, (100, 100))
    assert survivor_check(result, survivor_list(tmp_path, "[]\n"), "mutmut") == (
        0,
        "mutation survivors accepted",
    )


def test_mutmut_gate_accepts_standalone_fixture_without_package(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = mutation_result(tmp_path, {"pkg.f.x__mutmut_3": 1})
    assert survivor_check(result, survivor_list(tmp_path, "[]\n"), "mutmut") == (
        0,
        "mutation survivors accepted",
    )


def test_mutmut_gate_names_every_selected_function_without_results(tmp_path):
    result = mutation_result(tmp_path, {"pkg.f.other__mutmut_1": 1})
    assert survivor_check(
        result,
        survivor_list(tmp_path, "[]\n"),
        "mutmut",
        selected_functions=["pkg.f.first", "pkg.f.second"],
    ) == (1, "selected functions have no mutation results: pkg.f.first, pkg.f.second")


def test_killed_explanation_outside_selected_function_is_stale(tmp_path):
    old = "pkg.f.old__mutmut_1"
    selected = "pkg.f.new__mutmut_1"
    result = mutation_result(tmp_path, {old: 1, selected: 1})
    explanations = survivor_list(
        tmp_path, f"- mutant: {old}\n  reason: no observable difference\n"
    )
    assert survivor_check(
        result, explanations, "mutmut", selected_functions=["pkg.f.new"]
    ) == (1, f"stale mutant: {old}")


def test_mutmut_reruns_cached_mutants_after_test_file_change(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    settings = tomllib.loads((repository / "pyproject.toml").read_text())["tool"][
        "mutmut"
    ]
    assert "tests/*.py" in settings["cache_invalidation_files"]
    assert settings["on_dependency_change"] == "rerun"

    (tmp_path / "sample").mkdir()
    (tmp_path / "sample" / "__init__.py").write_text("def answer():\n    return 42\n")
    (tmp_path / "tests").mkdir()
    test_file = tmp_path / "tests" / "test_sample.py"
    test_file.write_text(
        "from sample import answer\n\ndef test_answer():\n    assert answer() == 42\n"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[tool.mutmut]\nsource_paths = ["sample"]\n'
        'pytest_add_cli_args_test_selection = ["tests/test_sample.py"]\n'
        'cache_invalidation_files = ["tests/*.py"]\n'
        'on_dependency_change = "rerun"\n'
    )
    command = [sys.executable, "-m", "mutmut", "run", "--max-children", "1"]
    first = subprocess.run(
        command, cwd=tmp_path, capture_output=True, text=True, timeout=180
    )
    assert first.returncode == 0, first.stdout + first.stderr
    test_file.write_text(
        test_file.read_text() + "\n# Test-only edit invalidates cached verdicts.\n"
    )
    second = subprocess.run(
        command, cwd=tmp_path, capture_output=True, text=True, timeout=180
    )
    assert second.returncode == 0, second.stdout + second.stderr
    assert "rerunning all mutants: tests/test_sample.py" in second.stdout


def survivor_list(tmp_path, entries):
    path = tmp_path / ".mutation-survivors.yml"
    path.write_text(entries)
    return path


def test_python_survivor_gate_ignores_tailnet_explanation(tmp_path):
    result = mutation_result(tmp_path, {"pkg.f.x__mutmut_3": 0})
    explanations = survivor_list(
        tmp_path,
        "- mutant: pkg.f.x__mutmut_3\n  reason: Equivalent\n"
        "- mutant: tailscale:grant:abc:source\n  reason: Explained policy mutant\n",
    )
    completed = invoke(
        "survivors",
        "--format",
        "mutmut",
        "--results",
        result,
        "--survivors-file",
        explanations,
    )
    assert (completed.returncode, completed.stdout, completed.stderr) == (
        0,
        "mutation survivors accepted\n",
        "",
    )


@pytest.mark.parametrize(
    ("mutants", "entries", "code", "output"),
    [
        ({}, "[]\n", 0, "mutation survivors accepted\n"),
        (
            {"pkg.f.x__mutmut_3": 0},
            "[]\n",
            1,
            "unexplained mutant: pkg.f.x__mutmut_3\n",
        ),
        (
            {"pkg.f.x__mutmut_3": 0},
            "- mutant: pkg.f.x__mutmut_3\n  reason: Equivalent\n",
            0,
            "mutation survivors accepted\n",
        ),
        (
            {"pkg.f.x__mutmut_3": 0},
            "- mutant: pkg.f.x__mutmut_3\n  reason: '   '\n",
            1,
            "blank reason: pkg.f.x__mutmut_3\n",
        ),
        (
            {},
            "- mutant: pkg.f.x__mutmut_3\n  reason: Equivalent\n",
            1,
            "stale mutant: pkg.f.x__mutmut_3\n",
        ),
    ],
)
def test_mutmut_survivors(tmp_path, mutants, entries, code, output):
    result = invoke(
        "survivors",
        "--format",
        "mutmut",
        "--results",
        mutation_result(tmp_path, mutants),
        "--survivors-file",
        survivor_list(tmp_path, entries),
    )
    assert (result.returncode, result.stdout, result.stderr) == (code, output, "")


def test_mutmut_native_result_directory(tmp_path):
    result_dir = tmp_path / "mutants"
    result_dir.mkdir()
    (result_dir / "a.py.meta").write_text(
        json.dumps({"exit_code_by_key": {"pkg.a__mutmut_1": 1}})
    )
    (result_dir / "b.py.meta").write_text(
        json.dumps({"exit_code_by_key": {"pkg.b__mutmut_2": 0}})
    )
    result = invoke(
        "survivors",
        "--format",
        "mutmut",
        "--results",
        result_dir,
        "--survivors-file",
        survivor_list(tmp_path, "[]\n"),
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        1,
        "unexplained mutant: pkg.b__mutmut_2\n",
        "",
    )


def test_survivor_scope_rejects_stale_unchanged_function(tmp_path):
    result = invoke(
        "survivors",
        "--format",
        "mutmut",
        "--results",
        mutation_result(tmp_path, {"pkg.f.x__mutmut_3": 0}),
        "--survivors-file",
        survivor_list(
            tmp_path,
            "- mutant: pkg.f.x__mutmut_3\n  reason: Equivalent\n"
            "- mutant: pkg.f.y__mutmut_7\n  reason: Unchanged function\n",
        ),
        "--selected-function",
        "pkg.f.x",
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        1,
        "stale mutant: pkg.f.y__mutmut_7\n",
        "",
    )


def test_unrun_existing_mutmut_survivor_stays_explained(tmp_path):
    result = invoke(
        "survivors",
        "--format",
        "mutmut",
        "--results",
        mutation_result(tmp_path, {"pkg.f.x__mutmut_3": 0, "pkg.f.y__mutmut_7": None}),
        "--survivors-file",
        survivor_list(
            tmp_path,
            "- mutant: pkg.f.x__mutmut_3\n  reason: Equivalent\n- mutant: pkg.f.y__mutmut_7\n  reason: Previously explained\n",
        ),
        "--selected-function",
        "pkg.f.x",
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "mutation survivors accepted\n",
        "",
    )


def test_selected_unrun_mutmut_is_a_gap(tmp_path):
    result = invoke(
        "survivors",
        "--format",
        "mutmut",
        "--results",
        mutation_result(tmp_path, {"pkg.f.x__mutmut_3": None}),
        "--survivors-file",
        survivor_list(tmp_path, "[]\n"),
        "--selected-function",
        "pkg.f.x",
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        1,
        "mutant ran without tests: pkg.f.x__mutmut_3\n",
        "",
    )


def test_survivor_scope_checks_later_entry_after_unchanged_function(tmp_path):
    result = invoke(
        "survivors",
        "--format",
        "mutmut",
        "--results",
        mutation_result(tmp_path, {"pkg.f.x__mutmut_3": 0}),
        "--survivors-file",
        survivor_list(
            tmp_path,
            "- mutant: pkg.f.y__mutmut_7\n  reason: Unchanged function\n"
            "- mutant: pkg.f.x__mutmut_3\n  reason: Equivalent\n",
        ),
        "--selected-function",
        "pkg.f.x",
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        1,
        "stale mutant: pkg.f.y__mutmut_7\n",
        "",
    )


@pytest.mark.parametrize(
    "selected,expected",
    [
        (
            "pkg.f.missing",
            "selected functions have no mutation results: pkg.f.missing\n",
        ),
    ],
)
def test_selected_function_without_results_fails(tmp_path, selected, expected):
    result = invoke(
        "survivors",
        "--format",
        "mutmut",
        "--results",
        mutation_result(tmp_path, {"pkg.f.x__mutmut_1": 1}),
        "--survivors-file",
        survivor_list(tmp_path, "[]\n"),
        "--selected-function",
        selected,
    )
    assert (result.returncode, result.stdout, result.stderr) == (1, expected, "")


def test_each_selected_function_needs_results(tmp_path):
    result = invoke(
        "survivors",
        "--format",
        "mutmut",
        "--results",
        mutation_result(tmp_path, {"pkg.f.x__mutmut_1": 1}),
        "--survivors-file",
        survivor_list(tmp_path, "[]\n"),
        "--selected-function",
        "pkg.f.x",
        "--selected-function",
        "pkg.f.missing",
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        1,
        "selected functions have no mutation results: pkg.f.missing\n",
        "",
    )


def test_blank_reason_outside_selected_function_fails(tmp_path):
    result = invoke(
        "survivors",
        "--format",
        "mutmut",
        "--results",
        mutation_result(tmp_path, {"pkg.f.x__mutmut_1": 1}),
        "--survivors-file",
        survivor_list(tmp_path, "- mutant: pkg.f.y__mutmut_7\n  reason: '   '\n"),
        "--selected-function",
        "pkg.f.x",
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        1,
        "blank reason: pkg.f.y__mutmut_7\n",
        "",
    )


def test_deleted_function_survivor_is_stale(tmp_path):
    result = invoke(
        "survivors",
        "--format",
        "mutmut",
        "--results",
        mutation_result(tmp_path, {"pkg.f.x__mutmut_1": 1}),
        "--survivors-file",
        survivor_list(
            tmp_path, "- mutant: pkg.f.deleted__mutmut_7\n  reason: Former guard\n"
        ),
        "--selected-function",
        "pkg.f.x",
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        1,
        "stale mutant: pkg.f.deleted__mutmut_7\n",
        "",
    )


def test_blank_reason_is_named_even_when_selection_has_no_results(tmp_path):
    result = invoke(
        "survivors",
        "--format",
        "mutmut",
        "--results",
        mutation_result(tmp_path, {}),
        "--survivors-file",
        survivor_list(tmp_path, "- mutant: pkg.f.deleted__mutmut_7\n  reason: '  '\n"),
        "--selected-function",
        "pkg.f.x",
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        1,
        "blank reason: pkg.f.deleted__mutmut_7\n",
        "",
    )


def test_empty_survivors_file_is_valid(tmp_path):
    result = invoke(
        "survivors",
        "--format",
        "mutmut",
        "--results",
        mutation_result(tmp_path, {}),
        "--survivors-file",
        survivor_list(tmp_path, ""),
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "mutation survivors accepted\n",
        "",
    )
