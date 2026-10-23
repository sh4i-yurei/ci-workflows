"""Shared coverage and Stryker gates for phase 1.3."""

import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from ci_workflows.checks import main


def invoke(*args):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main([*map(str, args)])
    return code, stdout.getvalue(), stderr.getvalue()


def patch(tmp_path, name, line=2):
    path = tmp_path / "change.patch"
    path.write_text(
        f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n"
        f"@@ -{line - 1},0 +{line},1 @@\n+changed\n"
    )
    return path


def lcov(tmp_path, hits=1, branches=(1, 1)):
    path = tmp_path / "coverage.lcov"
    path.write_text(
        "TN:\nSF:src/app.js\n"
        f"DA:2,{hits}\nBRDA:2,0,0,{branches[0]}\nBRDA:2,0,1,{branches[1]}\n"
        f"LF:1\nLH:{int(hits > 0)}\nBRF:2\nBRH:{sum(hit > 0 for hit in branches)}\nend_of_record\n"
    )
    return path


def cobertura(tmp_path, hits=1, rate="1.0"):
    path = tmp_path / "coverage.xml"
    path.write_text(
        f'<coverage line-rate="{rate}" branch-rate="1.0"><packages><package>'
        f'<classes><class filename="scripts/task.sh"><lines><line number="2" hits="{hits}"/>'
        "</lines></class></classes></package></packages></coverage>"
    )
    return path


def jacoco(tmp_path, covered=1, missed=0, package="scripts"):
    path = tmp_path / "coverage.xml"
    path.write_text(
        f'<report name="Pester"><package name="{package}"><sourcefile name="health.ps1">'
        f'<line nr="2" mi="{missed}" ci="{covered}" mb="0" cb="0"/>'
        "</sourcefile></package>"
        f'<counter type="LINE" missed="{missed}" covered="{covered}"/></report>'
    )
    return path


@pytest.mark.parametrize(
    ("hits", "branches", "expected"),
    [
        (1, (1, 1), (0, "all changed lines covered\n", "")),
        (0, (0, 0), (1, "uncovered changed lines: src/app.js:2\n", "")),
        (1, (1, 0), (1, "uncovered changed lines: src/app.js:2\n", "")),
    ],
)
def test_javascript_changed_lines(tmp_path, hits, branches, expected):
    assert (
        invoke(
            "changed-lines",
            "--kind",
            "javascript",
            "--report",
            lcov(tmp_path, hits, branches),
            "--diff-file",
            patch(tmp_path, "src/app.js"),
        )
        == expected
    )


def test_javascript_floor_uses_reported_branch_denominator(tmp_path):
    report = lcov(tmp_path, branches=(1, 0))
    assert invoke(
        "floor", "--kind", "javascript", "--report", report, "--head-floor", "80"
    ) == (1, "coverage 50.0% is below floor 80%\n", "")


def test_javascript_floor_sums_multiple_branch_records(tmp_path):
    report = lcov(tmp_path)
    report.write_text("TN:\nSF:src/app.js\nBRF:1\nBRH:1\nBRF:1\nBRH:0\nend_of_record\n")
    assert invoke(
        "floor", "--kind", "javascript", "--report", report, "--head-floor", "100"
    ) == (1, "coverage 50.0% is below floor 100%\n", "")


@pytest.mark.parametrize("malformed", ["BRF:2:1", "BRH:1:x"])
def test_javascript_floor_rejects_malformed_branch_count(tmp_path, malformed):
    report = lcov(tmp_path)
    tag = malformed.partition(":")[0]
    report.write_text(report.read_text().replace(f"{tag}:2", malformed))
    assert invoke(
        "floor", "--kind", "javascript", "--report", report, "--head-floor", "80"
    ) == (
        2,
        f"configuration error: invalid literal for int() with base 10: '{malformed.partition(':')[2]}'\n",
        "",
    )


def test_javascript_unmeasured_branch_is_uncovered(tmp_path):
    report = lcov(tmp_path)
    report.write_text(report.read_text().replace("BRDA:2,0,1,1", "BRDA:2,0,1,-"))
    assert invoke(
        "changed-lines",
        "--kind",
        "javascript",
        "--report",
        report,
        "--diff-file",
        patch(tmp_path, "src/app.js"),
    ) == (1, "uncovered changed lines: src/app.js:2\n", "")


def test_javascript_line_without_branch_records_is_covered(tmp_path):
    report = lcov(tmp_path)
    report.write_text("TN:\nSF:src/app.js\nDA:2,1\nLF:1\nLH:1\nend_of_record\n")
    assert invoke(
        "changed-lines",
        "--kind",
        "javascript",
        "--report",
        report,
        "--diff-file",
        patch(tmp_path, "src/app.js"),
    ) == (0, "all changed lines covered\n", "")


@pytest.mark.parametrize(
    ("hits", "expected"),
    [
        (1, (0, "all changed lines covered\n", "")),
        (0, (1, "uncovered changed lines: src/app.js:2\n", "")),
    ],
)
def test_javascript_function_metadata_precedes_line_records(tmp_path, hits, expected):
    report = lcov(tmp_path)
    report.write_text(
        f"TN:\nSF:src/app.js\nFN:2,work\nFNDA:1,work\nDA:2,{hits}\n"
        "BRDA:2,0,0,1\nBRDA:2,0,1,1\nLF:1\nLH:1\nBRF:2\nBRH:2\nend_of_record\n"
    )
    assert (
        invoke(
            "changed-lines",
            "--kind",
            "javascript",
            "--report",
            report,
            "--diff-file",
            patch(tmp_path, "src/app.js"),
        )
        == expected
    )


@pytest.mark.parametrize(
    ("kind", "filename", "extension"),
    [
        ("javascript", "src/app.js", ".cjs"),
        ("shell", "scripts/task.sh", ".bash"),
        ("powershell", "scripts/health.ps1", ".psm1"),
    ],
)
def test_changed_lines_accepts_each_supported_source_suffix(
    tmp_path, kind, filename, extension
):
    source = str(Path(filename).with_suffix(extension))
    report = {"javascript": lcov, "shell": cobertura, "powershell": jacoco}[kind](
        tmp_path
    )
    report.write_text(
        report.read_text()
        .replace(filename, source)
        .replace(Path(filename).name, Path(source).name)
    )
    options = ["--source-root", "scripts"] if kind != "javascript" else []
    assert invoke(
        "changed-lines",
        "--kind",
        kind,
        "--report",
        report,
        "--diff-file",
        patch(tmp_path, source),
        *options,
    ) == (0, "all changed lines covered\n", "")


def test_powershell_report_without_package_name_uses_root_source(tmp_path):
    report = jacoco(tmp_path)
    report.write_text(report.read_text().replace(' name="scripts"', ""))
    assert invoke(
        "changed-lines",
        "--kind",
        "powershell",
        "--report",
        report,
        "--diff-file",
        patch(tmp_path, "health.ps1"),
    ) == (0, "all changed lines covered\n", "")
    assert ET.parse(report).find(".//package").attrib["name"] == "."


def test_javascript_filter_excludes_colocated_cjs_test(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("src/app.cjs\nsrc/app.test.cjs\n"))
    assert invoke(
        "filter-sources", "--roots-json", '["src"]', "--extension", ".cjs"
    ) == (0, "src/app.cjs\n", "")


def test_javascript_multiple_changed_sources_keep_lcov_records(tmp_path):
    report = lcov(tmp_path)
    original = report.read_text()
    second = original.replace("src/app.js", "src/other.js")
    report.write_text(original + second)
    combined = tmp_path / "combined.patch"
    combined.write_text(
        patch(tmp_path, "src/app.js").read_text()
        + patch(tmp_path, "src/other.js").read_text()
    )
    assert invoke(
        "changed-lines",
        "--kind",
        "javascript",
        "--report",
        report,
        "--diff-file",
        combined,
    ) == (0, "all changed lines covered\n", "")
    assert report.read_text() == original + second


@pytest.mark.parametrize("position", ["before", "after"])
def test_javascript_stray_record_end_keeps_valid_record(tmp_path, position):
    report = lcov(tmp_path)
    valid = report.read_text()
    report.write_text(
        "end_of_record\n" + valid if position == "before" else valid + "end_of_record\n"
    )
    assert invoke(
        "changed-lines",
        "--kind",
        "javascript",
        "--report",
        report,
        "--diff-file",
        patch(tmp_path, "src/app.js"),
    ) == (0, "all changed lines covered\n", "")


def test_javascript_second_changed_source_is_uncovered(tmp_path):
    report = lcov(tmp_path)
    first = report.read_text()
    second = first.replace("src/app.js", "src/other.js").replace("DA:2,1", "DA:2,0")
    report.write_text(first + second)
    combined = tmp_path / "combined.patch"
    combined.write_text(
        patch(tmp_path, "src/app.js").read_text()
        + patch(tmp_path, "src/other.js").read_text()
    )
    assert invoke(
        "changed-lines",
        "--kind",
        "javascript",
        "--report",
        report,
        "--diff-file",
        combined,
    ) == (1, "uncovered changed lines: src/other.js:2\n", "")


def test_javascript_changed_test_source_is_skipped(tmp_path):
    report = lcov(tmp_path)
    combined = tmp_path / "combined.patch"
    combined.write_text(
        patch(tmp_path, "src/app.js").read_text()
        + patch(tmp_path, "src/app.test.js").read_text()
    )
    assert invoke(
        "changed-lines",
        "--kind",
        "javascript",
        "--report",
        report,
        "--diff-file",
        combined,
    ) == (
        0,
        "all changed lines covered; skipped changed source outside declared paths: src/app.test.js\n",
        "",
    )


def test_two_changed_sources_absent_from_coverage_are_named(tmp_path):
    report = lcov(tmp_path)
    combined = tmp_path / "combined.patch"
    combined.write_text(
        patch(tmp_path, "src/first.js").read_text()
        + patch(tmp_path, "src/second.js").read_text()
    )
    assert invoke(
        "changed-lines",
        "--kind",
        "javascript",
        "--report",
        report,
        "--diff-file",
        combined,
    ) == (
        2,
        "configuration error: changed source absent from coverage: src/first.js, src/second.js\n",
        "",
    )


def test_two_changed_sources_outside_declared_root_are_named(tmp_path):
    report = lcov(tmp_path)
    combined = tmp_path / "combined.patch"
    combined.write_text(
        "".join(
            patch(tmp_path, name).read_text()
            for name in ("src/app.js", "vendor/first.js", "vendor/second.js")
        )
    )
    assert invoke(
        "changed-lines",
        "--kind",
        "javascript",
        "--report",
        report,
        "--diff-file",
        combined,
        "--source-roots-json",
        '["src"]',
    ) == (
        0,
        "all changed lines covered; skipped changed source outside declared paths: vendor/first.js, vendor/second.js\n",
        "",
    )


def test_javascript_floor_without_branches(tmp_path):
    report = lcov(tmp_path)
    report.write_text(report.read_text().replace("BRF:2\nBRH:2", "BRF:0\nBRH:0"))
    assert invoke(
        "floor", "--kind", "javascript", "--report", report, "--head-floor", "100"
    ) == (0, "coverage 100.0% meets floor 100%\n", "")


def test_javascript_lcov_source_path_with_colon(tmp_path):
    report = lcov(tmp_path, hits=0)
    report.write_text(report.read_text().replace("SF:src/app.js", "SF:src:app.js"))
    assert invoke(
        "changed-lines",
        "--kind",
        "javascript",
        "--report",
        report,
        "--diff-file",
        patch(tmp_path, "src:app.js"),
    ) == (1, "uncovered changed lines: src:app.js:2\n", "")


def test_javascript_no_changed_lines(tmp_path):
    report = lcov(tmp_path)
    empty = tmp_path / "empty.patch"
    empty.write_text("")
    assert invoke(
        "changed-lines",
        "--kind",
        "javascript",
        "--report",
        report,
        "--diff-file",
        empty,
    ) == (0, "no changed JavaScript lines to check\n", "")


def test_javascript_partial_branch_without_line_record_is_uncovered(tmp_path):
    report = lcov(tmp_path)
    report.write_text(
        "TN:\nSF:src/app.js\nBRDA:2,0,0,1\nBRDA:2,0,1,0\nBRF:2\nBRH:1\nend_of_record\n"
    )
    assert invoke(
        "changed-lines",
        "--kind",
        "javascript",
        "--report",
        report,
        "--diff-file",
        patch(tmp_path, "src/app.js"),
    ) == (1, "uncovered changed lines: src/app.js:2\n", "")
    assert "BRDA:2,0,1,0\n" in report.read_text()


def test_javascript_complete_branch_remains_covered(tmp_path):
    assert invoke(
        "changed-lines",
        "--kind",
        "javascript",
        "--report",
        lcov(tmp_path),
        "--diff-file",
        patch(tmp_path, "src/app.js"),
    ) == (0, "all changed lines covered\n", "")


def test_javascript_partial_branch_preserves_checksum(tmp_path):
    report = lcov(tmp_path, hits=1, branches=(1, 0))
    report.write_text(report.read_text().replace("DA:2,1\n", "DA:2,1,checksum\n"))
    assert invoke(
        "changed-lines",
        "--kind",
        "javascript",
        "--report",
        report,
        "--diff-file",
        patch(tmp_path, "src/app.js"),
    ) == (1, "uncovered changed lines: src/app.js:2\n", "")
    assert "DA:2,1,checksum\n" in report.read_text()
    assert "BRDA:2,0,1,0\n" in report.read_text()
    assert invoke(
        "floor", "--kind", "javascript", "--report", report, "--head-floor", "100"
    ) == (1, "coverage 50.0% is below floor 100%\n", "")


def test_javascript_complete_branch_without_line_record_stays_covered(tmp_path):
    report = lcov(tmp_path)
    report.write_text(
        "TN:\nSF:src/app.js\nBRDA:2,0,0,1\nBRDA:2,0,1,1\nBRF:2\nBRH:2\nend_of_record\n"
    )
    assert invoke(
        "changed-lines",
        "--kind",
        "javascript",
        "--report",
        report,
        "--diff-file",
        patch(tmp_path, "src/app.js"),
    ) == (0, "all changed lines covered\n", "")
    assert "DA:2,0\n" not in report.read_text()


def test_coverage_gutters_nested_jacoco_paths_are_exact(tmp_path):
    report = jacoco(tmp_path, covered=0, missed=1, package="scripts/lib")
    report.write_text(report.read_text().replace('name="health.ps1"', 'name="b.ps1"'))
    assert invoke(
        "changed-lines",
        "--kind",
        "powershell",
        "--report",
        report,
        "--diff-file",
        patch(tmp_path, "scripts/lib/b.ps1"),
        "--source-root",
        "scripts",
    ) == (1, "uncovered changed lines: scripts/lib/b.ps1:2\n", "")
    assert ET.parse(report).find(".//package").attrib["name"] == "scripts/lib"


@pytest.mark.parametrize(
    ("kind", "filename", "source_arg", "excluded"),
    [
        (
            "javascript",
            "src/app.js",
            ("--source-roots-json", '["src"]'),
            "stryker.config.mjs",
        ),
        (
            "javascript",
            "site/js/app.js",
            ("--source-roots-json", '["site/js/app.js"]'),
            "site/js/altcha.js",
        ),
        ("shell", "scripts/task.sh", ("--source-root", "scripts"), "vendor/task.sh"),
        (
            "powershell",
            "scripts/health.ps1",
            ("--source-root", "scripts"),
            "tests/Health.Tests.ps1",
        ),
    ],
)
def test_changed_lines_skip_outside_declared_sources(
    tmp_path, kind, filename, source_arg, excluded
):
    report = {"javascript": lcov, "shell": cobertura, "powershell": jacoco}[kind](
        tmp_path
    )
    if filename == "site/js/app.js":
        report.write_text(
            report.read_text().replace("SF:src/app.js", "SF:site/js/app.js")
        )
    first = patch(tmp_path, filename).read_text()
    second = patch(tmp_path, excluded).read_text()
    combined = tmp_path / "combined.patch"
    combined.write_text(first + second)
    assert invoke(
        "changed-lines",
        "--kind",
        kind,
        "--report",
        report,
        "--diff-file",
        combined,
        *source_arg,
    ) == (
        0,
        f"all changed lines covered; skipped changed source outside declared paths: {excluded}\n",
        "",
    )


def test_changed_lines_skip_colocated_javascript_test(tmp_path):
    source = patch(tmp_path, "src/app.js").read_text()
    test = patch(tmp_path, "src/app.test.js").read_text()
    combined = tmp_path / "combined.patch"
    combined.write_text(source + test)
    assert invoke(
        "changed-lines",
        "--kind",
        "javascript",
        "--report",
        lcov(tmp_path),
        "--diff-file",
        combined,
        "--source-roots-json",
        '["src"]',
    ) == (
        0,
        "all changed lines covered; skipped changed source outside declared paths: src/app.test.js\n",
        "",
    )


def test_lcov_absolute_path_is_normalized_in_report(tmp_path):
    coverage = lcov(tmp_path, hits=0)
    coverage.write_text(
        coverage.read_text().replace("SF:src/app.js", f"SF:{Path.cwd() / 'src/app.js'}")
    )
    assert invoke(
        "changed-lines",
        "--kind",
        "javascript",
        "--report",
        coverage,
        "--diff-file",
        patch(tmp_path, "src/app.js"),
        "--source-roots-json",
        '["src"]',
    ) == (1, "uncovered changed lines: src/app.js:2\n", "")
    assert [
        row for row in coverage.read_text().splitlines() if row.startswith("SF:")
    ] == ["SF:src/app.js"]


def test_jacoco_prefixed_path_is_normalized_in_report(tmp_path):
    coverage = jacoco(tmp_path, covered=0, missed=1, package="project/src")
    assert invoke(
        "changed-lines",
        "--kind",
        "powershell",
        "--report",
        coverage,
        "--diff-file",
        patch(tmp_path, "project/src/health.ps1"),
        "--source-root",
        "project/src",
    ) == (1, "uncovered changed lines: project/src/health.ps1:2\n", "")
    assert ET.parse(coverage).find(".//package").attrib["name"] == "project/src"


def test_stryker_no_coverage_status_fails(tmp_path):
    assert invoke(
        "survivors",
        "--format",
        "stryker",
        "--results",
        stryker(tmp_path, {0: "NoCoverage"}),
        "--survivors-file",
        tmp_path / "missing-survivors.yml",
        "--selected-file",
        "src/app.js",
    ) == (
        1,
        "mutant ran without tests: src/app.js#ArithmeticOperator@2:0-2:1~cb23f6635a581786\n",
        "",
    )


@pytest.mark.parametrize(
    ("hits", "expected"),
    [
        (1, (0, "all changed lines covered\n", "")),
        (0, (1, "uncovered changed lines: scripts/task.sh:2\n", "")),
    ],
)
def test_shell_changed_lines(tmp_path, hits, expected):
    assert (
        invoke(
            "changed-lines",
            "--kind",
            "shell",
            "--report",
            cobertura(tmp_path, hits),
            "--diff-file",
            patch(tmp_path, "scripts/task.sh"),
        )
        == expected
    )


@pytest.mark.parametrize(
    ("kind", "report_factory", "message"),
    [
        ("shell", cobertura, "no changed shell lines to check\n"),
        ("powershell", "jacoco", "no changed PowerShell lines to check\n"),
    ],
)
def test_language_no_changed_lines(tmp_path, kind, report_factory, message):
    empty = tmp_path / "empty.patch"
    empty.write_text("")
    if report_factory == "jacoco":
        report_factory = jacoco
    assert invoke(
        "changed-lines",
        "--kind",
        kind,
        "--report",
        report_factory(tmp_path),
        "--diff-file",
        empty,
    ) == (0, message, "")


def test_kcov_absolute_source_path_names_uncovered_line(tmp_path):
    report = tmp_path / "coverage.xml"
    filename = Path.cwd() / "scripts/task.sh"
    report.write_text(
        f'<coverage line-rate="0.0"><packages><package><classes>'
        f'<class filename="{filename}"><lines><line number="2" hits="0"/>'
        "</lines></class></classes></package></packages></coverage>"
    )
    assert invoke(
        "changed-lines",
        "--kind",
        "shell",
        "--report",
        report,
        "--diff-file",
        patch(tmp_path, "scripts/task.sh"),
    ) == (1, "uncovered changed lines: scripts/task.sh:2\n", "")
    assert 'filename="scripts/task.sh"' in report.read_text()


@pytest.mark.parametrize(
    ("covered", "missed", "expected"),
    [
        (1, 0, (0, "all changed lines covered\n", "")),
        (0, 1, (1, "uncovered changed lines: scripts/health.ps1:2\n", "")),
    ],
)
def test_powershell_changed_lines(tmp_path, covered, missed, expected):
    assert (
        invoke(
            "changed-lines",
            "--kind",
            "powershell",
            "--report",
            jacoco(tmp_path, covered, missed),
            "--diff-file",
            patch(tmp_path, "scripts/health.ps1"),
        )
        == expected
    )


def test_powershell_zero_instructions_is_not_covered(tmp_path):
    assert invoke(
        "changed-lines",
        "--kind",
        "powershell",
        "--report",
        jacoco(tmp_path, covered=0, missed=0),
        "--diff-file",
        patch(tmp_path, "scripts/health.ps1"),
    ) == (1, "uncovered changed lines: scripts/health.ps1:2\n", "")


def test_pester_absolute_package_path_is_normalized(tmp_path):
    report = jacoco(
        tmp_path, covered=0, missed=1, package=str(Path.cwd() / "project/src")
    )
    assert invoke(
        "changed-lines",
        "--kind",
        "powershell",
        "--report",
        report,
        "--diff-file",
        patch(tmp_path, "project/src/health.ps1"),
        "--source-root",
        "project/src",
    ) == (1, "uncovered changed lines: project/src/health.ps1:2\n", "")
    assert ET.parse(report).find(".//package").attrib["name"] == "project/src"


@pytest.mark.parametrize(
    ("kind", "rate", "report_factory", "expected"),
    [
        ("javascript", "1.0", lcov, (0, "coverage 100.0% meets floor 80%\n", "")),
        ("shell", "0.8", cobertura, (0, "coverage 80.0% meets floor 80%\n", "")),
        ("powershell", None, jacoco, (1, "coverage 50.0% is below floor 80%\n", "")),
    ],
)
def test_language_floor(tmp_path, kind, rate, report_factory, expected):
    if kind == "javascript":
        report = report_factory(tmp_path)
    elif kind == "shell":
        report = report_factory(tmp_path, rate=rate)
    else:
        report = report_factory(tmp_path, covered=1, missed=1)
    assert (
        invoke("floor", "--kind", kind, "--report", report, "--head-floor", "80")
        == expected
    )


def test_pester_single_source_file_path_is_used_directly(tmp_path):
    assert invoke(
        "changed-lines",
        "--kind",
        "powershell",
        "--report",
        jacoco(tmp_path, covered=0, missed=1, package="src"),
        "--diff-file",
        patch(tmp_path, "src/health.ps1"),
        "--source-root",
        "src/health.ps1",
    ) == (1, "uncovered changed lines: src/health.ps1:2\n", "")


def test_powershell_source_root_excludes_changed_test_file(tmp_path):
    assert invoke(
        "changed-lines",
        "--kind",
        "powershell",
        "--report",
        jacoco(tmp_path),
        "--diff-file",
        patch(tmp_path, "tests/Health.Tests.ps1"),
        "--source-root",
        "scripts",
    ) == (
        0,
        "no changed PowerShell lines to check; skipped changed source outside declared paths: tests/Health.Tests.ps1\n",
        "",
    )


def test_powershell_floor_rejects_report_without_line_counter(tmp_path):
    report = tmp_path / "coverage.xml"
    report.write_text(
        '<report name="Pester"><counter type="METHOD" missed="0" covered="1"/></report>'
    )
    assert invoke(
        "floor", "--kind", "powershell", "--report", report, "--head-floor", "80"
    ) == (2, "configuration error: JaCoCo report has no LINE counter\n", "")


def test_powershell_floor_without_lines(tmp_path):
    assert invoke(
        "floor",
        "--kind",
        "powershell",
        "--report",
        jacoco(tmp_path, covered=0, missed=0),
        "--head-floor",
        "100",
    ) == (0, "coverage 100.0% meets floor 100%\n", "")


def stryker(tmp_path, survivors):
    path = tmp_path / "mutation.json"
    path.write_text(
        json.dumps(
            {
                "schemaVersion": "1",
                "files": {
                    "src/app.js": {
                        "language": "javascript",
                        "mutants": [
                            {
                                "id": str(number),
                                "mutatorName": "ArithmeticOperator",
                                "replacement": "a + b",
                                "status": status,
                                "location": {
                                    "start": {"line": 2, "column": 0},
                                    "end": {"line": 2, "column": 1},
                                },
                            }
                            for number, status in survivors.items()
                        ],
                    }
                },
            }
        )
    )
    return path


def explanations(tmp_path, content):
    path = tmp_path / ".mutation-survivors.yml"
    path.write_text(content)
    return path


@pytest.mark.parametrize(
    ("mutants", "entries", "expected"),
    [
        ({0: "Killed"}, "[]\n", (0, "mutation survivors accepted\n", "")),
        (
            {0: "Survived"},
            "[]\n",
            (
                1,
                "unexplained mutant: src/app.js#ArithmeticOperator@2:0-2:1~cb23f6635a581786\n",
                "",
            ),
        ),
        (
            {0: "Survived"},
            "- mutant: src/app.js#ArithmeticOperator@2:0-2:1~cb23f6635a581786\n  reason: Equivalent\n",
            (0, "mutation survivors accepted\n", ""),
        ),
        (
            {0: "Survived"},
            "- mutant: src/app.js#ArithmeticOperator@2:0-2:1~cb23f6635a581786\n  reason: '   '\n",
            (
                1,
                "blank reason: src/app.js#ArithmeticOperator@2:0-2:1~cb23f6635a581786\n",
                "",
            ),
        ),
        (
            {0: "Killed"},
            "- mutant: src/app.js#ArithmeticOperator@2:0-2:1~cb23f6635a581786\n  reason: Equivalent\n",
            (
                1,
                "stale mutant: src/app.js#ArithmeticOperator@2:0-2:1~cb23f6635a581786\n",
                "",
            ),
        ),
    ],
)
def test_stryker_survivors(tmp_path, mutants, entries, expected):
    assert (
        invoke(
            "survivors",
            "--format",
            "stryker",
            "--results",
            stryker(tmp_path, mutants),
            "--survivors-file",
            explanations(tmp_path, entries),
        )
        == expected
    )


def test_stryker_scope_preserves_unchanged_file_explanations(tmp_path):
    assert invoke(
        "survivors",
        "--format",
        "stryker",
        "--results",
        stryker(tmp_path, {0: "Survived"}),
        "--survivors-file",
        explanations(
            tmp_path,
            "- mutant: src/old.js#ArithmeticOperator@2:0-2:1~cb23f6635a581786\n  reason: Unchanged file\n"
            "- mutant: src/app.js#ArithmeticOperator@2:0-2:1~cb23f6635a581786\n  reason: Equivalent\n",
        ),
        "--selected-file",
        "src/app.js",
    ) == (
        1,
        "stale mutant: src/old.js#ArithmeticOperator@2:0-2:1~cb23f6635a581786\n",
        "",
    )


def test_stryker_unselected_file_with_hash_in_name_is_stale(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "src" / "app#part.js"
    source.parent.mkdir()
    source.write_text("export const value = 1;\n")
    key = "src/app#part.js#ArithmeticOperator@2:0-2:1~cb23f6635a581786"
    assert invoke(
        "survivors",
        "--format",
        "stryker",
        "--results",
        stryker(tmp_path, {0: "Killed"}),
        "--survivors-file",
        explanations(tmp_path, f"- mutant: {key}\n  reason: Unchanged file\n"),
        "--selected-file",
        "src/app.js",
    ) == (1, f"stale mutant: {key}\n", "")


def test_explanation_for_existing_unselected_stryker_file_is_retained(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    old = tmp_path / "src" / "old.js"
    old.parent.mkdir()
    old.write_text("export const value = 1;\n")
    result = invoke(
        "survivors",
        "--format",
        "stryker",
        "--results",
        stryker(tmp_path, {0: "Killed"}),
        "--survivors-file",
        explanations(
            tmp_path,
            "- mutant: src/old.js#ArithmeticOperator@2:0-2:1~cb23f6635a581786\n  reason: Previously explained\n",
        ),
        "--selected-file",
        "src/app.js",
    )
    assert result == (0, "mutation survivors accepted\n", "")


def test_stryker_survivor_key_is_stable_across_file_sets(tmp_path):
    path = stryker(tmp_path, {7: "Survived"})
    first = invoke(
        "survivors",
        "--format",
        "stryker",
        "--results",
        path,
        "--survivors-file",
        explanations(tmp_path, "[]\n"),
        "--selected-file",
        "src/app.js",
    )
    data = json.loads(path.read_text())
    data["files"]["src/other.js"] = {"language": "javascript", "mutants": []}
    data["files"]["src/app.js"]["mutants"][0]["id"] = "41"
    path.write_text(json.dumps(data))
    second = invoke(
        "survivors",
        "--format",
        "stryker",
        "--results",
        path,
        "--survivors-file",
        explanations(tmp_path, "[]\n"),
        "--selected-file",
        "src/app.js",
    )
    expected = (
        1,
        "unexplained mutant: src/app.js#ArithmeticOperator@2:0-2:1~cb23f6635a581786\n",
        "",
    )
    assert first == second == expected


def test_stryker_same_location_with_different_replacements_has_distinct_keys(tmp_path):
    path = stryker(tmp_path, {1: "Survived"})
    data = json.loads(path.read_text())
    other = dict(data["files"]["src/app.js"]["mutants"][0])
    other["id"] = "2"
    other["replacement"] = "a - b"
    data["files"]["src/app.js"]["mutants"].append(other)
    path.write_text(json.dumps(data))
    result = invoke(
        "survivors",
        "--format",
        "stryker",
        "--results",
        path,
        "--survivors-file",
        explanations(tmp_path, "[]\n"),
    )
    assert result == (
        1,
        "unexplained mutant: src/app.js#ArithmeticOperator@2:0-2:1~5a079debdfff12da, "
        "src/app.js#ArithmeticOperator@2:0-2:1~cb23f6635a581786\n",
        "",
    )


def test_stryker_duplicate_stable_key_is_configuration_error(tmp_path):
    path = stryker(tmp_path, {1: "Survived"})
    data = json.loads(path.read_text())
    duplicate = dict(data["files"]["src/app.js"]["mutants"][0])
    duplicate["id"] = "2"
    data["files"]["src/app.js"]["mutants"].append(duplicate)
    path.write_text(json.dumps(data))
    assert invoke(
        "survivors",
        "--format",
        "stryker",
        "--results",
        path,
        "--survivors-file",
        explanations(tmp_path, "[]\n"),
    ) == (
        2,
        "configuration error: duplicate Stryker survivor key: src/app.js#ArithmeticOperator@2:0-2:1~cb23f6635a581786\n",
        "",
    )


def test_selected_stryker_file_without_results_fails(tmp_path):
    assert invoke(
        "survivors",
        "--format",
        "stryker",
        "--results",
        stryker(tmp_path, {0: "Killed"}),
        "--survivors-file",
        explanations(tmp_path, "[]\n"),
        "--selected-file",
        "src/missing.js",
    ) == (1, "selected files have no mutation results: src/missing.js\n", "")


def test_each_selected_stryker_file_needs_results(tmp_path):
    assert invoke(
        "survivors",
        "--format",
        "stryker",
        "--results",
        stryker(tmp_path, {0: "Killed"}),
        "--survivors-file",
        explanations(tmp_path, "[]\n"),
        "--selected-file",
        "src/app.js",
        "--selected-file",
        "src/missing.js",
    ) == (1, "selected files have no mutation results: src/missing.js\n", "")


def test_stryker_unselected_file_survivor_is_out_of_scope(tmp_path):
    path = stryker(tmp_path, {0: "Killed"})
    data = json.loads(path.read_text())
    data["files"]["src/other.js"] = {
        "language": "javascript",
        "mutants": [
            {
                **data["files"]["src/app.js"]["mutants"][0],
                "id": "1",
                "status": "Survived",
            }
        ],
    }
    path.write_text(json.dumps(data))
    assert invoke(
        "survivors",
        "--format",
        "stryker",
        "--results",
        path,
        "--survivors-file",
        explanations(tmp_path, "[]\n"),
        "--selected-file",
        "src/app.js",
    ) == (
        0,
        "mutation survivors accepted\n",
        "",
    )


def test_stryker_blank_reason_outside_scope_fails(tmp_path):
    assert invoke(
        "survivors",
        "--format",
        "stryker",
        "--results",
        stryker(tmp_path, {0: "Killed"}),
        "--survivors-file",
        explanations(
            tmp_path,
            "- mutant: src/old.js#ArithmeticOperator@2:0-2:1~cb23f6635a581786\n  reason: '  '\n",
        ),
        "--selected-file",
        "src/app.js",
    ) == (
        1,
        "blank reason: src/old.js#ArithmeticOperator@2:0-2:1~cb23f6635a581786\n",
        "",
    )
