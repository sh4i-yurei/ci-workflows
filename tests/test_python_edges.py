"""Boundary and error cases for the Python check commands."""

import io
import json
import runpy
import sys
import builtins
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

from ci_workflows.checks import main


def invoke(*args):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main([*map(str, args)])
    return code, stdout.getvalue(), stderr.getvalue()


def report(tmp_path, rate="0.85", lines=((12, 1),)):
    path = tmp_path / "coverage.xml"
    values = "".join(
        f'<line number="{number}" hits="{hits}"/>' for number, hits in lines
    )
    path.write_text(
        f'<coverage line-rate="{rate}" branch-rate="{rate}"><packages><package>'
        f'<classes><class filename="pkg/f.py"><lines>{values}</lines></class>'
        "</classes></package></packages></coverage>"
    )
    return path


def diff(tmp_path, count=1):
    path = tmp_path / "change.patch"
    added = "".join(f"+line {number}\n" for number in range(count))
    path.write_text(
        "diff --git a/pkg/f.py b/pkg/f.py\n--- a/pkg/f.py\n+++ b/pkg/f.py\n"
        f"@@ -11,0 +12,{count} @@\n{added}"
    )
    return path


def meta(tmp_path, statuses):
    path = tmp_path / "f.py.meta"
    path.write_text(json.dumps({"exit_code_by_key": statuses}))
    return path


def survivors_file(tmp_path, content):
    path = tmp_path / ".mutation-survivors.yml"
    path.write_text(content)
    return path


def test_module_entry_point_in_process(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ci_workflows",
            "floor",
            "--report",
            str(report(tmp_path)),
            "--head-floor",
            "80",
        ],
    )
    with pytest.raises(SystemExit) as exited:
        runpy.run_module("ci_workflows", run_name="__main__")
    assert exited.value.code == 0
    assert capsys.readouterr() == ("coverage 85.0% meets floor 80%\n", "")


def test_two_uncovered_lines_are_listed_exactly(tmp_path):
    result = invoke(
        "changed-lines",
        "--report",
        report(tmp_path, lines=((12, 0), (13, 0))),
        "--diff-file",
        diff(tmp_path, count=2),
    )
    assert result == (1, "uncovered changed lines: pkg/f.py:12, pkg/f.py:13\n", "")


def test_changed_source_missing_from_report_fails(tmp_path):
    coverage = report(tmp_path)
    patch = diff(tmp_path)
    patch.write_text(patch.read_text().replace("pkg/f.py", "outside.py"))
    assert invoke("changed-lines", "--report", coverage, "--diff-file", patch) == (
        2,
        "configuration error: changed source absent from coverage: outside.py\n",
        "",
    )


def test_cobertura_source_root_resolves_package_filename(tmp_path):
    coverage = tmp_path / "coverage.xml"
    coverage.write_text(
        f'<coverage branch-rate="1"><sources><source>{Path.cwd() / "ci_workflows"}</source>'
        '</sources><packages><package><classes><class filename="checks.py"><lines>'
        '<line number="3" hits="1"/></lines></class></classes></package></packages></coverage>'
    )
    patch = tmp_path / "change.patch"
    patch.write_text(
        "diff --git a/ci_workflows/checks.py b/ci_workflows/checks.py\n"
        "--- a/ci_workflows/checks.py\n+++ b/ci_workflows/checks.py\n"
        "@@ -2,0 +3,1 @@\n+changed\n"
    )
    assert invoke("changed-lines", "--report", coverage, "--diff-file", patch) == (
        0,
        "all changed lines covered\n",
        "",
    )


def test_python_source_config_excludes_test_files(tmp_path):
    config = tmp_path / "pyproject.toml"
    config.write_text('[tool.mutmut]\nsource_paths = ["ci_workflows"]\n')
    patch = diff(tmp_path)
    patch.write_text(patch.read_text().replace("pkg/f.py", "tests/test_f.py"))
    assert invoke(
        "changed-lines",
        "--report",
        report(tmp_path),
        "--diff-file",
        patch,
        "--source-config",
        config,
    ) == (
        0,
        "no changed Python lines to check; skipped changed source outside declared paths: tests/test_f.py\n",
        "",
    )


def test_diff_cover_is_final_for_normalized_cobertura_branch(tmp_path):
    coverage = report(tmp_path, lines=((12, 1),))
    coverage.write_text(
        coverage.read_text()
        .replace(
            'filename="pkg/f.py"',
            f'filename="{Path.cwd() / "pkg/f.py"}"',
        )
        .replace(
            'number="12" hits="1"',
            'number="12" hits="1" branch="true" condition-coverage="50% (1/2)"',
        )
    )
    result = invoke(
        "changed-lines", "--report", coverage, "--diff-file", diff(tmp_path)
    )
    assert result == (1, "uncovered changed lines: pkg/f.py:12\n", "")
    tree = __import__("xml.etree.ElementTree", fromlist=["ElementTree"]).parse(coverage)
    assert tree.find(".//class").attrib["filename"] == "pkg/f.py"
    assert tree.find(".//line").attrib["hits"] == "0"


def test_diff_cover_result_does_not_get_overruled(tmp_path, monkeypatch):
    def fake_run(*args, **kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout=json.dumps({"src_stats": {"pkg/f.py": {"violation_lines": [12]}}}),
            stderr="",
        )

    monkeypatch.setattr("ci_workflows.checks.subprocess.run", fake_run)
    result = invoke(
        "changed-lines", "--report", report(tmp_path), "--diff-file", diff(tmp_path)
    )
    assert result == (1, "uncovered changed lines: pkg/f.py:12\n", "")


def test_floor_compares_only_named_caller_job(tmp_path):
    workflow = tmp_path / "ci.yml"
    workflow.write_text(
        "jobs:\n  python:\n    with:\n      coverage_floor: 80\n"
        "  shell:\n    with:\n      coverage_floor: 90\n"
    )
    assert invoke(
        "floor",
        "--report",
        report(tmp_path),
        "--head-floor",
        "80",
        "--base-workflow",
        workflow,
        "--caller-job",
        "python",
    ) == (0, "coverage 85.0% meets floor 80%\n", "")


def test_named_caller_job_floor_cannot_fall(tmp_path):
    workflow = tmp_path / "ci.yml"
    workflow.write_text(
        "jobs:\n  python:\n    with:\n      coverage_floor: 80\n"
        "  shell:\n    with:\n      coverage_floor: 90\n"
    )
    assert invoke(
        "floor",
        "--report",
        report(tmp_path),
        "--head-floor",
        "79",
        "--base-workflow",
        workflow,
        "--caller-job",
        "python",
    ) == (1, "coverage floor fell from 80% to 79%\n", "")


def test_floor_reports_missing_caller_job(tmp_path):
    workflow = tmp_path / "ci.yml"
    workflow.write_text("jobs:\n  python:\n    with:\n      coverage_floor: 80\n")
    assert invoke(
        "floor",
        "--report",
        report(tmp_path),
        "--head-floor",
        "80",
        "--base-workflow",
        workflow,
        "--caller-job",
        "shell",
    ) == (2, "configuration error: base workflow has no job 'shell'\n", "")


def test_floor_first_run_when_named_base_job_has_no_floor(tmp_path):
    workflow = tmp_path / "ci.yml"
    workflow.write_text("jobs:\n  python:\n    runs-on: ubuntu-latest\n")
    assert invoke(
        "floor",
        "--report",
        report(tmp_path),
        "--head-floor",
        "80",
        "--base-workflow",
        workflow,
        "--caller-job",
        "python",
    ) == (0, "coverage 85.0% meets floor 80%\n", "")


def test_floor_first_run_when_named_base_job_has_empty_with(tmp_path):
    workflow = tmp_path / "ci.yml"
    workflow.write_text("jobs:\n  python:\n    with: {}\n")
    assert invoke(
        "floor",
        "--report",
        report(tmp_path),
        "--head-floor",
        "80",
        "--base-workflow",
        workflow,
        "--caller-job",
        "python",
    ) == (0, "coverage 85.0% meets floor 80%\n", "")


def test_mutmut_no_tests_status_fails(tmp_path):
    assert invoke(
        "survivors",
        "--format",
        "mutmut",
        "--results",
        meta(tmp_path, {"pkg.f.x__mutmut_3": 5}),
        "--survivors-file",
        survivors_file(tmp_path, "[]\n"),
    ) == (1, "mutant ran without tests: pkg.f.x__mutmut_3\n", "")


def test_mutmut_second_no_tests_status_fails(tmp_path):
    assert invoke(
        "survivors",
        "--format",
        "mutmut",
        "--results",
        meta(tmp_path, {"pkg.f.x__mutmut_3": 33}),
        "--survivors-file",
        survivors_file(tmp_path, "[]\n"),
    ) == (1, "mutant ran without tests: pkg.f.x__mutmut_3\n", "")


def test_two_no_tests_mutants_are_named_exactly(tmp_path):
    assert invoke(
        "survivors",
        "--format",
        "mutmut",
        "--results",
        meta(tmp_path, {"pkg.f.x__mutmut_3": 5, "pkg.f.x__mutmut_4": 33}),
        "--survivors-file",
        survivors_file(tmp_path, "[]\n"),
    ) == (1, "mutant ran without tests: pkg.f.x__mutmut_3, pkg.f.x__mutmut_4\n", "")


def test_changed_lines_passes_patch_bytes_to_diff_cover(tmp_path, monkeypatch):
    patch = diff(tmp_path)
    expected = patch.read_text()

    def fake_run(command, **kwargs):
        assert command[2:4] == ["--diff-file", "/dev/stdin"]
        kwargs.pop("cwd", None)
        assert kwargs == {
            "input": expected,
            "capture_output": True,
            "text": True,
            "check": False,
        }
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"src_stats": {"pkg/f.py": {"violation_lines": []}}}),
            stderr="",
        )

    monkeypatch.setattr("ci_workflows.checks.subprocess.run", fake_run)
    assert invoke(
        "changed-lines", "--report", report(tmp_path), "--diff-file", patch
    ) == (0, "all changed lines covered\n", "")


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        (2, "bad report", "configuration error: diff-cover failed: bad report\n"),
        (1, "", "configuration error: diff-cover did not return a JSON report\n"),
    ],
)
def test_diff_cover_failure_is_exact(tmp_path, monkeypatch, status, error, expected):
    def fake_run(*args, **kwargs):
        kwargs.pop("cwd", None)
        assert kwargs == {
            "input": diff_text,
            "capture_output": True,
            "text": True,
            "check": False,
        }
        return SimpleNamespace(returncode=status, stdout="", stderr=error)

    monkeypatch.setattr("ci_workflows.checks.subprocess.run", fake_run)
    patch = diff(tmp_path)
    diff_text = patch.read_text()
    assert invoke(
        "changed-lines", "--report", report(tmp_path), "--diff-file", patch
    ) == (2, expected, "")


@pytest.mark.parametrize(
    ("rate", "floor", "expected"),
    [
        ("0", "0", "coverage 0.0% meets floor 0%\n"),
        ("1", "100", "coverage 100.0% meets floor 100%\n"),
    ],
)
def test_floor_bounds(tmp_path, rate, floor, expected):
    assert invoke(
        "floor", "--report", report(tmp_path, rate=rate), "--head-floor", floor
    ) == (0, expected, "")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "[]\n",
            (2, "configuration error: base workflow must be a YAML mapping\n", ""),
        ),
        (
            "jobs:\n  checks:\n    with: {}\n",
            (0, "coverage 85.0% meets floor 80%\n", ""),
        ),
        (
            "jobs:\n  a:\n    with:\n      coverage_floor: 80\n  b:\n    with:\n      coverage_floor: 90\n",
            (
                2,
                "configuration error: base workflow has multiple coverage floors\n",
                "",
            ),
        ),
        (
            "jobs:\n  a: none\n  b:\n    with: none\n  c:\n    with:\n      coverage_floor: 80\n",
            (0, "coverage 85.0% meets floor 80%\n", ""),
        ),
    ],
)
def test_base_workflow_shapes(tmp_path, text, expected):
    base = tmp_path / "base.yml"
    base.write_text(text)
    assert (
        invoke(
            "floor",
            "--report",
            report(tmp_path),
            "--head-floor",
            "80",
            "--base-workflow",
            base,
        )
        == expected
    )


def test_missing_branch_rate_is_configuration_error(tmp_path):
    path = report(tmp_path)
    path.write_text(path.read_text().replace(' branch-rate="0.85"', ""))
    assert invoke("floor", "--report", path, "--head-floor", "80") == (
        2,
        "configuration error: coverage report has no valid branch-rate\n",
        "",
    )


def test_method_and_decorator_selection(tmp_path):
    source = tmp_path / "pkg" / "f.py"
    source.parent.mkdir()
    source.write_text(
        "class A:\n    @staticmethod\n    async def changed():\n        return 1\n\n"
        "    def untouched(self):\n        return 2\n"
    )
    changed = tmp_path / "method.patch"
    changed.write_text(
        "diff --git a/pkg/f.py b/pkg/f.py\n--- a/pkg/f.py\n+++ b/pkg/f.py\n"
        "@@ -1,0 +2,1 @@\n+    @staticmethod\n"
    )
    assert invoke(
        "select-functions",
        "--source",
        source,
        "--source-name",
        "pkg/f.py",
        "--diff-file",
        changed,
    ) == (0, '["pkg.f.A.changed"]\n', "")


def test_changed_other_source_selects_nothing(tmp_path):
    source = tmp_path / "pkg" / "f.py"
    source.parent.mkdir()
    source.write_text("def untouched():\n    return 1\n")
    changed = diff(tmp_path)
    changed.write_text(changed.read_text().replace("pkg/f.py", "pkg/other.py"))
    assert invoke(
        "select-functions",
        "--source",
        source,
        "--source-name",
        "pkg/f.py",
        "--diff-file",
        changed,
    ) == (0, "[]\n", "")


def test_hidden_test_scratch_meta_is_not_a_result(tmp_path):
    result_dir = tmp_path / "mutants"
    (result_dir / ".cache" / "pytest-tmp").mkdir(parents=True)
    (result_dir / ".cache" / "pytest-tmp" / "fake.py.meta").write_text(
        json.dumps({"exit_code_by_key": {"pkg.fake__mutmut_1": 0}})
    )
    (result_dir / "real.py.meta").write_text(
        json.dumps({"exit_code_by_key": {"pkg.real__mutmut_1": 1}})
    )
    assert invoke(
        "survivors",
        "--format",
        "mutmut",
        "--results",
        result_dir,
        "--survivors-file",
        survivors_file(tmp_path, "[]\n"),
    ) == (0, "mutation survivors accepted\n", "")


@pytest.mark.parametrize(
    ("statuses", "entries", "expected"),
    [
        (
            [],
            "[]\n",
            "configuration error: mutmut result statuses must be unique mappings\n",
        ),
        ({}, "{}\n", "configuration error: survivors file must be a list\n"),
        (
            {},
            "- mutant: pkg.f.x\n",
            "configuration error: each survivor needs mutant and reason\n",
        ),
        (
            {},
            "- mutant: ''\n  reason: Why\n",
            "configuration error: survivor names must be unique non-empty strings\n",
        ),
        (
            {},
            "- mutant: pkg.f.x\n  reason: Why\n- mutant: pkg.f.x\n  reason: Why\n",
            "configuration error: survivor names must be unique non-empty strings\n",
        ),
    ],
)
def test_invalid_survivor_formats(tmp_path, statuses, entries, expected):
    assert invoke(
        "survivors",
        "--format",
        "mutmut",
        "--results",
        meta(tmp_path, statuses),
        "--survivors-file",
        survivors_file(tmp_path, entries),
    ) == (2, expected, "")


def test_missing_survivors_file_means_no_explanations(tmp_path):
    assert invoke(
        "survivors",
        "--format",
        "mutmut",
        "--results",
        meta(tmp_path, {"pkg.f.x__mutmut_3": 0}),
        "--survivors-file",
        tmp_path / "absent.yml",
    ) == (1, "unexplained mutant: pkg.f.x__mutmut_3\n", "")


def test_two_unexplained_mutants_are_listed_exactly(tmp_path):
    assert invoke(
        "survivors",
        "--format",
        "mutmut",
        "--results",
        meta(tmp_path, {"pkg.f.x__mutmut_3": 0, "pkg.f.x__mutmut_4": 0}),
        "--survivors-file",
        survivors_file(tmp_path, "[]\n"),
    ) == (1, "unexplained mutant: pkg.f.x__mutmut_3, pkg.f.x__mutmut_4\n", "")


def test_base_workflow_without_jobs_is_first_run(tmp_path):
    base = tmp_path / "base.yml"
    base.write_text("name: First run\n")
    assert invoke(
        "floor",
        "--report",
        report(tmp_path),
        "--head-floor",
        "80",
        "--base-workflow",
        base,
    ) == (0, "coverage 85.0% meets floor 80%\n", "")


def test_nested_function_name_includes_its_parent(tmp_path):
    source = tmp_path / "pkg" / "f.py"
    source.parent.mkdir()
    source.write_text(
        "def outer():\n    def inner():\n        return 1\n    return inner()\n"
    )
    changed = tmp_path / "nested.patch"
    changed.write_text(
        "diff --git a/pkg/f.py b/pkg/f.py\n--- a/pkg/f.py\n+++ b/pkg/f.py\n"
        "@@ -1,0 +2,1 @@\n+    def inner():\n"
    )
    assert invoke(
        "select-functions",
        "--source",
        source,
        "--source-name",
        "pkg/f.py",
        "--diff-file",
        changed,
    ) == (0, '["pkg.f.outer", "pkg.f.outer.inner"]\n', "")


def test_mutmut_selection_uses_generated_owner_for_nested_change(tmp_path):
    source = tmp_path / "pkg" / "f.py"
    source.parent.mkdir()
    source.write_text(
        "def outer():\n    def inner():\n        return 1\n    return inner()\n"
    )
    changed = tmp_path / "nested.patch"
    changed.write_text(
        "diff --git a/pkg/f.py b/pkg/f.py\n--- a/pkg/f.py\n+++ b/pkg/f.py\n"
        "@@ -2,0 +3,1 @@\n+        return 1\n"
    )
    assert invoke(
        "select-functions",
        "--source",
        source,
        "--source-name",
        "pkg/f.py",
        "--diff-file",
        changed,
        "--mutmut",
    ) == (0, '["pkg.f.x_outer"]\n', "")


def test_mutmut_decorated_function_is_named_gap(tmp_path):
    source = tmp_path / "pkg" / "f.py"
    source.parent.mkdir()
    source.write_text('@app.route("/")\ndef contact():\n    return 1\n')
    changed = tmp_path / "decorated.patch"
    changed.write_text(
        "diff --git a/pkg/f.py b/pkg/f.py\n--- a/pkg/f.py\n+++ b/pkg/f.py\n"
        "@@ -2,0 +3,1 @@\n+    return 1\n"
    )
    assert invoke(
        "select-functions",
        "--source",
        source,
        "--source-name",
        "pkg/f.py",
        "--diff-file",
        changed,
        "--mutmut",
    ) == (0, "[]\n", "mutation tool gap: pkg.f.contact (mutmut generated no mutants)\n")


def test_mutmut_import_failure_is_configuration_error(tmp_path, monkeypatch):
    source = tmp_path / "pkg" / "f.py"
    source.parent.mkdir()
    source.write_text("def changed():\n    return 1\n")
    patch = tmp_path / "change.patch"
    patch.write_text(
        "diff --git a/pkg/f.py b/pkg/f.py\n--- a/pkg/f.py\n+++ b/pkg/f.py\n"
        "@@ -1,0 +2,1 @@\n+    return 1\n"
    )
    original_import = builtins.__import__

    def unavailable(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "mutmut.mutation.file_mutation":
            raise ModuleNotFoundError("No module named 'mutmut.mutation'")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", unavailable)
    assert invoke(
        "select-functions",
        "--source",
        source,
        "--source-name",
        "pkg/f.py",
        "--diff-file",
        patch,
        "--mutmut",
    ) == (
        2,
        "",
        "configuration error: mutmut 3.8.0 is required for Python function selection\n",
    )


def test_mutmut_version_mismatch_is_configuration_error(tmp_path, monkeypatch):
    source = tmp_path / "pkg" / "f.py"
    source.parent.mkdir()
    source.write_text("def changed():\n    return 1\n")
    patch = tmp_path / "change.patch"
    patch.write_text(
        "diff --git a/pkg/f.py b/pkg/f.py\n--- a/pkg/f.py\n+++ b/pkg/f.py\n"
        "@@ -1,0 +2,1 @@\n+    return 1\n"
    )
    monkeypatch.setattr("ci_workflows.checks.version", lambda name: "3.9.0")
    assert invoke(
        "select-functions",
        "--source",
        source,
        "--source-name",
        "pkg/f.py",
        "--diff-file",
        patch,
        "--mutmut",
    ) == (
        2,
        "",
        "configuration error: mutmut 3.8.0 is required for Python function selection\n",
    )


def test_invalid_python_source_names_the_file(tmp_path):
    source = tmp_path / "pkg" / "f.py"
    source.parent.mkdir()
    source.write_text("def broken(:\n")
    assert invoke(
        "select-functions",
        "--source",
        source,
        "--source-name",
        "pkg/f.py",
        "--diff-file",
        diff(tmp_path),
    ) == (2, "", "configuration error: invalid Python source pkg/f.py:1\n")


@pytest.mark.parametrize(
    ("source_text", "line", "expected"),
    [
        ("def changed():\n    return 1\n", 2, '["pkg.f.x_changed"]\n'),
        (
            "class A:\n    def changed(self):\n        return 1\n",
            3,
            '["pkg.f.x\\u01c1A\\u01c1changed"]\n',
        ),
    ],
)
def test_selection_uses_mutmut_three_names(tmp_path, source_text, line, expected):
    source = tmp_path / "pkg" / "f.py"
    source.parent.mkdir()
    source.write_text(source_text)
    changed = tmp_path / "mutmut.patch"
    changed.write_text(
        "diff --git a/pkg/f.py b/pkg/f.py\n--- a/pkg/f.py\n+++ b/pkg/f.py\n"
        f"@@ -{line - 1},0 +{line},1 @@\n+changed\n"
    )
    assert invoke(
        "select-functions",
        "--source",
        source,
        "--source-name",
        "pkg/f.py",
        "--diff-file",
        changed,
        "--mutmut",
    ) == (0, expected, "")


def test_filter_mutatable_source_paths(tmp_path, monkeypatch):
    config = tmp_path / "pyproject.toml"
    config.write_text('[tool.mutmut]\nsource_paths = ["src/pkg", "scripts/check.py"]\n')
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            "tests/test_pkg.py\nsrc/pkg/f.py\nsrc/pkg/sub/g.py\n"
            "scripts/check.py\nREADME.md\n"
        ),
    )
    assert invoke("filter-sources", "--config", config) == (
        0,
        "src/pkg/f.py\nsrc/pkg/sub/g.py\nscripts/check.py\n",
        "",
    )


def test_filter_sources_requires_declared_mutation_paths(tmp_path, monkeypatch):
    config = tmp_path / "pyproject.toml"
    config.write_text("[tool.mutmut]\n")
    monkeypatch.setattr(sys, "stdin", io.StringIO("pkg/f.py\n"))
    assert invoke("filter-sources", "--config", config) == (
        2,
        "",
        "configuration error: mutmut source_paths must be a non-empty list\n",
    )


@pytest.mark.parametrize(
    "config_text",
    [
        "",
        "[tool]\n",
        "[tool.mutmut]\nsource_paths = []\n",
        '[tool.mutmut]\nsource_paths = "pkg"\n',
        "[tool.mutmut]\nsource_paths = [1]\n",
        '[tool.mutmut]\nsource_paths = [""]\n',
    ],
)
def test_filter_sources_rejects_invalid_source_paths(
    tmp_path, monkeypatch, config_text
):
    config = tmp_path / "pyproject.toml"
    config.write_text(config_text)
    monkeypatch.setattr(sys, "stdin", io.StringIO("pkg/f.py\n"))
    assert invoke("filter-sources", "--config", config) == (
        2,
        "",
        "configuration error: mutmut source_paths must be a non-empty list\n",
    )


def test_two_stale_survivors_are_listed_exactly(tmp_path):
    assert invoke(
        "survivors",
        "--format",
        "mutmut",
        "--results",
        meta(tmp_path, {}),
        "--survivors-file",
        survivors_file(
            tmp_path,
            "- mutant: pkg.f.x__mutmut_3\n  reason: Equivalent\n"
            "- mutant: pkg.f.x__mutmut_4\n  reason: Equivalent\n",
        ),
    ) == (1, "stale mutant: pkg.f.x__mutmut_3, pkg.f.x__mutmut_4\n", "")


def test_filter_changed_javascript_sources_from_json_roots(monkeypatch):
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            "src/app.js\nsrc/app.test.js\nlib/util.mjs\ntests/check.js\nREADME.md\n"
        ),
    )
    assert invoke(
        "filter-sources",
        "--roots-json",
        '["src", "lib"]',
        "--extension",
        ".js",
        "--extension",
        ".mjs",
    ) == (0, "src/app.js\nlib/util.mjs\n", "")


def test_filter_colocated_javascript_test_directories(monkeypatch):
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            "src/test/check.js\nsrc/tests/check.js\nsrc/app.test.js\nsrc/app.spec.mjs\nsrc/app.js\n"
        ),
    )
    assert invoke(
        "filter-sources",
        "--roots-json",
        '["src"]',
        "--extension",
        ".js",
        "--extension",
        ".mjs",
    ) == (0, "src/app.js\n", "")


def test_filter_rejects_invalid_json_roots(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("src/app.js\n"))
    assert invoke("filter-sources", "--roots-json", "[]", "--extension", ".js") == (
        2,
        "",
        "configuration error: mutation source paths must be a non-empty list\n",
    )
