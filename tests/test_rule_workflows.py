import json
import os
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_rule_workflows_are_reusable_and_read_only():
    for name, expected_input in (
        ("prometheus.yml", "test_files"),
        ("tailscale.yml", "policy_file"),
    ):
        workflow = yaml.load(
            (ROOT / ".github/workflows" / name).read_text(),
            Loader=yaml.BaseLoader,
        )
        assert workflow["on"]["workflow_call"]["inputs"][expected_input] == {
            "required": "true",
            "type": "string",
        }
        assert workflow["permissions"] == (
            {"contents": "read", "id-token": "write"}
            if name == "tailscale.yml"
            else {"contents": "read"}
        )


def test_prometheus_workflow_runs_promtool_and_case_check():
    workflow = yaml.load(
        (ROOT / ".github/workflows/prometheus.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    steps = workflow["jobs"]["checks"]["steps"]
    assert {step["name"] for step in steps} == {
        "Check out caller",
        "Check out pinned shared checks",
        "Set up Python",
        "Install shared checks",
        "Run promtool rule tests",
        "Check alert cases",
    }
    promtool = next(step for step in steps if step["name"] == "Run promtool rule tests")
    assert "matrix" not in workflow["jobs"]["checks"]
    assert promtool["env"]["TEST_FILES_JSON"] == "${{ inputs.test_files }}"
    assert "test rules" in promtool["run"]
    assert "${test_files[@]}" in promtool["run"]
    cases = next(step for step in steps if step["name"] == "Check alert cases")
    assert 'for file in "${test_files[@]}"' in cases["run"]
    assert 'alerts "${case_args[@]}"' in cases["run"]


def test_prometheus_workflow_combines_two_test_files_in_one_job(tmp_path):
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
    workflow = yaml.load(
        (ROOT / ".github/workflows/prometheus.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    steps = workflow["jobs"]["checks"]["steps"]
    docker_step = next(
        step for step in steps if step["name"] == "Run promtool rule tests"
    )
    cases_step = next(step for step in steps if step["name"] == "Check alert cases")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$DOCKER_ARGS_FILE"\n')
    fake_docker.chmod(0o755)
    recorded = tmp_path / "docker-args"
    env = os.environ | {
        "PATH": f"{fake_bin}:{ROOT / '.venv/bin'}:{os.environ['PATH']}",
        "TEST_FILES_JSON": json.dumps([str(firing), str(quiet)]),
        "DOCKER_ARGS_FILE": str(recorded),
    }
    promtool = subprocess.run(
        ["bash", "-e", "-c", docker_step["run"]],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert (promtool.returncode, recorded.read_text().splitlines()[-4:]) == (
        0,
        ["test", "rules", str(firing), str(quiet)],
    )
    cases = subprocess.run(
        ["bash", "-e", "-c", cases_step["run"]],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert (cases.returncode, cases.stdout.splitlines()[-1], cases.stderr) == (
        0,
        "all alerts have firing and quiet cases",
        "",
    )


def test_tailscale_workflow_runs_remote_tests_without_apply():
    workflow = yaml.load(
        (ROOT / ".github/workflows/tailscale.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    steps = workflow["jobs"]["checks"]["steps"]
    assert {step["name"] for step in steps} == {
        "Check out caller",
        "Check out pinned shared checks",
        "Set up Python",
        "Install shared checks",
        "Check grant cases",
    }
    assert workflow["on"]["workflow_call"]["secrets"] == {
        "TS_OAUTH_ID": {"required": "true"},
        "TS_AUDIENCE": {"required": "true"},
        "TS_TAILNET": {"required": "true"},
    }
    check = next(step for step in steps if step["name"] == "Check grant cases")
    assert check["env"] == {
        "POLICY_FILE": "${{ inputs.policy_file }}",
        "TS_OAUTH_ID": "${{ secrets.TS_OAUTH_ID }}",
        "TS_AUDIENCE": "${{ secrets.TS_AUDIENCE }}",
        "TS_TAILNET": "${{ secrets.TS_TAILNET }}",
    }
    assert check["run"] == 'python -m ci_workflows policy --file "$POLICY_FILE"'
    assert "api-key" not in (ROOT / ".github/workflows/tailscale.yml").read_text()
