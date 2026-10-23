from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_molecule_workflow_runs_caller_scenarios_with_rootless_podman():
    workflow = yaml.load(
        (ROOT / ".github/workflows/molecule.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert workflow["on"] == {
        "workflow_call": {
            "inputs": {
                "scenarios": {"required": "true", "type": "string"},
            }
        }
    }
    assert workflow["permissions"] == {"contents": "read"}
    steps = workflow["jobs"]["checks"]["steps"]
    assert {step["name"] for step in steps} == {
        "Check out caller",
        "Check out pinned shared checks",
        "Set up Python",
        "Install Molecule and Podman",
        "Verify rootless Podman",
        "Run caller scenario",
    }
    assert workflow["jobs"]["checks"]["strategy"]["matrix"] == {
        "scenario": "${{ fromJSON(inputs.scenarios) }}"
    }
    rootless = next(step for step in steps if step["name"] == "Verify rootless Podman")
    assert (
        rootless["run"]
        == 'test "$(podman info --format "{{.Host.Security.Rootless}}")" = true'
    )
    run = next(step for step in steps if step["name"] == "Run caller scenario")
    assert run["run"] == 'molecule test -s "$SCENARIO"'


def test_molecule_installs_exact_locked_versions():
    workflow = yaml.load(
        (ROOT / ".github/workflows/molecule.yml").read_text(), Loader=yaml.BaseLoader
    )
    install = next(
        step
        for step in workflow["jobs"]["checks"]["steps"]
        if step.get("name") == "Install Molecule and Podman"
    )["run"]
    assert "--constraint .ci-workflows/requirements/molecule-lock.txt" in install
    assert "containers.podman:1.21.0" in install
    lock = (ROOT / "requirements/molecule-lock.txt").read_text()
    requirements = [
        line
        for line in lock.splitlines()
        if line and not line.startswith("#") and not line.startswith(" ")
    ]
    assert requirements
    assert all(
        re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.!+~-]+", line)
        for line in requirements
    )
    assert "molecule==26.10.0" in requirements
    assert "ansible-core==2.21.4" in requirements


def test_molecule_fixture_verifies_live_process_owner():
    verify = yaml.safe_load(
        (ROOT / "tests/fixtures/molecule/molecule/default/verify.yml").read_text()
    )
    tasks = verify[0]["tasks"]
    assert tasks[0]["ansible.builtin.command"] == (
        "systemctl show sample.service -p ActiveState -p UnitFileState -p User -p MainPID"
    )
    assert tasks[2]["ansible.builtin.command"] == (
        "stat -c %U /proc/{{ sample_props.MainPID }}"
    )
    assert tasks[3]["ansible.builtin.assert"]["that"] == [
        "sample_props.ActiveState == 'active'",
        "sample_props.UnitFileState == 'enabled'",
        "sample_props.User == 'samplesvc'",
        "sample_props.MainPID | int > 0",
        "sample_owner.stdout == 'samplesvc'",
    ]
