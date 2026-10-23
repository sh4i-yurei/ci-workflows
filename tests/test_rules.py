import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from ci_workflows.checks import main


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"


def invoke(*args):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main([*map(str, args)])
    return code, stdout.getvalue(), stderr.getvalue()


def test_prometheus_incomplete_names_only_b(tmp_path):
    assert invoke("alerts", "--test-file", FIXTURES / "alerts/incomplete.yml") == (
        1,
        "alerts missing quiet case: B\n",
        "",
    )


def test_prometheus_complete_accepts_both_alerts():
    assert invoke("alerts", "--test-file", FIXTURES / "alerts/complete.yml") == (
        0,
        "all alerts have firing and quiet cases\n",
        "",
    )


def test_rule_file_names_must_be_nonempty_strings(tmp_path):
    invalid = tmp_path / "invalid.yml"
    invalid.write_text("rule_files: [1]\ntests: []\n")
    assert invoke("alerts", "--test-file", invalid) == (
        2,
        "configuration error: rule_files must be a list of strings\n",
        "",
    )


@pytest.mark.parametrize(
    "quiet_case", ["{alertname: A, exp_alerts:}", "{alertname: A}"]
)
def test_prometheus_empty_or_omitted_expected_alerts_are_quiet(tmp_path, quiet_case):
    (tmp_path / "rules.yml").write_text(
        "groups: [{name: sample, rules: [{alert: A, expr: up}]}]\n"
    )
    test_file = tmp_path / "test.yml"
    test_file.write_text(
        "rule_files: [rules.yml]\ntests: [{alert_rule_test: ["
        "{alertname: A, exp_alerts: [{}]}, " + quiet_case + "]}]\n"
    )
    assert invoke("alerts", "--test-file", test_file) == (
        0,
        "all alerts have firing and quiet cases\n",
        "",
    )


def test_prometheus_rule_files_glob_expands_all_alerts(tmp_path):
    rules = tmp_path / "rules"
    rules.mkdir()
    for name in ("A", "B"):
        (rules / f"{name}.yml").write_text(
            f"groups: [{{name: sample, rules: [{{alert: {name}, expr: up}}]}}]\n"
        )
    test_file = tmp_path / "test.yml"
    test_file.write_text(
        "rule_files: ['rules/*.yml']\ntests: [{alert_rule_test: ["
        "{alertname: A, exp_alerts: [{}]}, {alertname: A, exp_alerts: []}]}]\n"
    )
    assert invoke("alerts", "--test-file", test_file) == (
        1,
        "alerts missing firing case: B; alerts missing quiet case: B\n",
        "",
    )


def test_prometheus_unmatched_rule_files_glob_is_configuration_error(tmp_path):
    test_file = tmp_path / "test.yml"
    test_file.write_text("rule_files: ['rules/*.yml']\ntests: []\n")
    assert invoke("alerts", "--test-file", test_file) == (
        2,
        "configuration error: rule_files pattern has no matches: rules/*.yml\n",
        "",
    )


def test_prometheus_evidence_spans_test_files(tmp_path):
    (tmp_path / "rules.yml").write_text(
        "groups: [{name: sample, rules: [{alert: A, expr: up}]}]\n"
    )
    firing = tmp_path / "firing.yml"
    quiet = tmp_path / "quiet.yml"
    firing.write_text(
        "rule_files: [rules.yml]\ntests: [{alert_rule_test: [{alertname: A, exp_alerts: [{}]}]}]\n"
    )
    quiet.write_text(
        "rule_files: [rules.yml]\ntests: [{alert_rule_test: [{alertname: A, exp_alerts: []}]}]\n"
    )
    assert invoke("alerts", "--test-file", firing, "--test-file", quiet) == (
        0,
        "all alerts have firing and quiet cases\n",
        "",
    )


def test_prometheus_missing_firing_cases_and_ignores_recording_rule(tmp_path):
    (tmp_path / "rules.yml").write_text((FIXTURES / "alerts/rules.yml").read_text())
    test_file = tmp_path / "tests.yml"
    test_file.write_text(
        "rule_files: [rules.yml]\ntests:\n  - alert_rule_test:\n"
        "      - {alertname: A, exp_alerts: []}\n"
        "      - {alertname: B, exp_alerts: []}\n"
    )
    assert invoke("alerts", "--test-file", test_file) == (
        1,
        "alerts missing firing case: A, B\n",
        "",
    )


@pytest.mark.parametrize(
    ("test_text", "rule_text", "message"),
    [
        ("[]\n", None, "invalid rule test file: {test_file}"),
        ("rule_files: rules.yml\n", None, "rule_files must be a list of strings"),
        ("rule_files: [rules.yml]\n", "[]\n", "invalid rule file: {rule_file}"),
        (
            "rule_files: [rules.yml]\ntests: [{alert_rule_test: [{alertname: A, exp_alerts: 4}]}]\n",
            "groups: []\n",
            "exp_alerts must be a list",
        ),
    ],
)
def test_invalid_prometheus_inputs(tmp_path, test_text, rule_text, message):
    test_file = tmp_path / "tests.yml"
    rule_file = tmp_path / "rules.yml"
    test_file.write_text(test_text)
    if rule_text is not None:
        rule_file.write_text(rule_text)
    assert invoke("alerts", "--test-file", test_file) == (
        2,
        f"configuration error: {message.format(test_file=test_file, rule_file=rule_file)}\n",
        "",
    )


def test_prometheus_two_missing_quiet_cases_are_named_exactly(tmp_path):
    rule_file = tmp_path / "rules.yml"
    rule_file.write_text(
        "groups: [{name: sample, rules: [{alert: B, expr: up}, {alert: C, expr: up}]}]\n"
    )
    test_file = tmp_path / "tests.yml"
    test_file.write_text(
        "rule_files: [rules.yml]\ntests: [{alert_rule_test: ["
        "{alertname: B, exp_alerts: [{}]}, {alertname: C, exp_alerts: [{}]}]}]\n"
    )
    assert invoke("alerts", "--test-file", test_file) == (
        1,
        "alerts missing quiet case: B, C\n",
        "",
    )


def test_prometheus_suite_without_alert_cases_does_not_count_as_evidence(tmp_path):
    (tmp_path / "rules.yml").write_text(
        "groups: [{name: x, rules: [{alert: A, expr: up}]}]\n"
    )
    test_file = tmp_path / "tests.yml"
    test_file.write_text("rule_files: [rules.yml]\ntests: [{promql_expr_test: []}]\n")
    assert invoke("alerts", "--test-file", test_file) == (
        1,
        "alerts missing firing case: A; alerts missing quiet case: A\n",
        "",
    )


@pytest.mark.parametrize(
    ("rule_text", "test_text", "message"),
    [
        (
            "record: x\n",
            "rule_files: [rules.yml]\ntests: []\n",
            "rule file has no groups: {rule_file}",
        ),
        (
            "groups: [{name: x}]\n",
            "rule_files: [rules.yml]\ntests: []\n",
            "rule group has no rules: {rule_file}",
        ),
        (
            "groups: []\n",
            "rule_files: [rules.yml]\n",
            "rule test file has no tests: {test_file}",
        ),
    ],
)
def test_missing_required_prometheus_sections(tmp_path, rule_text, test_text, message):
    rule_file = tmp_path / "rules.yml"
    test_file = tmp_path / "tests.yml"
    rule_file.write_text(rule_text)
    test_file.write_text(test_text)
    assert invoke("alerts", "--test-file", test_file) == (
        2,
        f"configuration error: {message.format(rule_file=rule_file, test_file=test_file)}\n",
        "",
    )
