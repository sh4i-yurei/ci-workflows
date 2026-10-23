"""Verify that alert rules have meaningful test cases."""

from __future__ import annotations

from glob import glob
from pathlib import Path

import yaml

from ci_workflows.errors import ConfigurationError


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ConfigurationError(f"{label} must be a list of strings")
    return tuple(value)


def alert_cases(test_files: list[Path]) -> tuple[int, str]:
    alerts: set[str] = set()
    firing: set[str] = set()
    quiet: set[str] = set()
    for test_file in test_files:
        suite = yaml.safe_load(test_file.read_text())
        if not isinstance(suite, dict):
            raise ConfigurationError(f"invalid rule test file: {test_file}")
        for rule_name in _strings(suite.get("rule_files"), "rule_files"):
            matches = sorted(glob(str(test_file.parent / rule_name)))
            if not matches:
                raise ConfigurationError(
                    f"rule_files pattern has no matches: {rule_name}"
                )
            for match in matches:
                rules_path = Path(match)
                rules = yaml.safe_load(rules_path.read_text())
                if not isinstance(rules, dict):
                    raise ConfigurationError(f"invalid rule file: {rules_path}")
                groups = rules.get("groups")
                if not isinstance(groups, list):
                    raise ConfigurationError(f"rule file has no groups: {rules_path}")
                for group in groups:
                    entries = group.get("rules")
                    if not isinstance(entries, list):
                        raise ConfigurationError(
                            f"rule group has no rules: {rules_path}"
                        )
                    for rule in entries:
                        if "alert" in rule:
                            alerts.add(rule["alert"])
        tests = suite.get("tests")
        if not isinstance(tests, list):
            raise ConfigurationError(f"rule test file has no tests: {test_file}")
        for test in tests:
            for case in test.get("alert_rule_test", []):
                name = case["alertname"]
                expected = case.get("exp_alerts")
                if expected is None:
                    expected = []
                if not isinstance(expected, list):
                    raise ConfigurationError("exp_alerts must be a list")
                (firing if expected else quiet).add(name)
    missing_firing = sorted(alerts - firing)
    missing_quiet = sorted(alerts - quiet)
    messages = []
    if missing_firing:
        messages.append("alerts missing firing case: " + ", ".join(missing_firing))
    if missing_quiet:
        messages.append("alerts missing quiet case: " + ", ".join(missing_quiet))
    return (
        (1, "; ".join(messages))
        if messages
        else (0, "all alerts have firing and quiet cases")
    )
