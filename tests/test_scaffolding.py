from pathlib import Path
import os
import subprocess
import sys
import tomllib

import pytest
import yaml
import ci_workflows


ROOT = Path(__file__).resolve().parents[1]


def test_package_version():
    assert ci_workflows.__version__ == "0.1.0"


def workflow(name):
    return yaml.load(
        (ROOT / ".github" / "workflows" / name).read_text(),
        Loader=yaml.BaseLoader,
    )


def test_own_ci_calls_shared_python_checks():
    ci = workflow("ci.yml")
    assert ci["on"] == {"push": {}, "pull_request": {}}
    assert ci["permissions"] == {"contents": "read"}
    assert (
        "actionlint@v1.7.12 .github/workflows/*.yml"
        in ci["jobs"]["actionlint"]["steps"][1]["run"]
    )
    assert ci["jobs"]["checks"] == {
        "uses": "./.github/workflows/python.yml",
        "with": {
            "coverage_floor": "100",
            "install_command": "python -m pip install -e '.[test]'",
            "caller_workflow": ".github/workflows/ci.yml",
            "caller_job": "checks",
        },
    }


def test_python_workflow_is_reusable_and_runs_pytest_with_branch_coverage():
    python = workflow("python.yml")
    assert python["on"] == {
        "workflow_call": {
            "inputs": {
                "coverage_floor": {
                    "required": "true",
                    "type": "number",
                },
                "install_command": {"required": "true", "type": "string"},
                "caller_workflow": {"required": "true", "type": "string"},
                "caller_job": {"required": "true", "type": "string"},
            }
        }
    }
    assert python["permissions"] == {"contents": "read"}
    runs = [step["run"] for step in python["jobs"]["checks"]["steps"] if "run" in step]
    assert "bash .ci-workflows/scripts/workflow-glue.sh test-python" in runs
    coverage_step = next(
        step
        for step in python["jobs"]["checks"]["steps"]
        if step.get("name") == "Run tests with branch coverage"
    )
    assert "env" not in coverage_step
    steps = python["jobs"]["checks"]["steps"]
    assert {step["name"] for step in steps} == {
        "Check out caller",
        "Check out pinned shared checks",
        "Set up Python",
        "Install shared checks",
        "Install caller",
        "Run tests with branch coverage",
        "Check floor ratchet",
        "Check changed lines",
        "Mutate changed functions",
    }
    shared = next(
        step for step in steps if step["name"] == "Check out pinned shared checks"
    )
    assert shared["with"] == {
        "repository": "${{ job.workflow_repository }}",
        "ref": "${{ job.workflow_sha }}",
        "path": ".ci-workflows",
    }


def test_readme_documents_workflow_contract():
    readme = (ROOT / "README.md").read_text()
    headings = {line for line in readme.splitlines() if line.startswith("## ")}
    assert headings == {
        "## Workflows",
        "## Inputs",
        "## Pinning a caller",
        "## Mutation survivors",
        "## Mutation tool gaps",
    }
    assert "mutant: pkg.f.x__mutmut_3" in readme
    assert "reason: Equivalent transformation" in readme
    assert "Shell and PowerShell have no mutation tool" in readme
    assert "coverage_package" not in readme
    assert "source_paths" in readme


@pytest.mark.parametrize(
    ("source_config", "mismatch", "expected_code", "expected_error"),
    [
        (
            '[tool.mutmut]\nsource_paths = ["pkg"]\n',
            True,
            1,
            "configuration error: mutmut 3.8.0 is required for Python function selection\n"
            "function selection failed for pkg/f.py\n",
        ),
        (
            "",
            False,
            2,
            "configuration error: mutmut source_paths must be a non-empty list\n",
        ),
    ],
)
def test_python_workflow_reports_real_cli_failures(
    tmp_path, source_config, mismatch, expected_code, expected_error
):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    source = repo / "pkg" / "f.py"
    source.parent.mkdir()
    source.write_text("def changed():\n    return 1\n")
    (repo / "pyproject.toml").write_text(source_config)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    commit = [
        "git",
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
    ]
    subprocess.run([*commit, "baseline"], cwd=repo, check=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    source.write_text("def changed():\n    return 2\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run([*commit, "changed"], cwd=repo, check=True)
    (repo / ".ci-workflows").symlink_to(ROOT, target_is_directory=True)

    tool_bin = tmp_path / "bin"
    tool_bin.mkdir()
    python_command = tool_bin / "python"
    python_command.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python_command.chmod(0o755)
    startup = tmp_path / "startup"
    startup.mkdir()
    if mismatch:
        (startup / "sitecustomize.py").write_text(
            "import importlib.metadata\n"
            "original_version = importlib.metadata.version\n"
            "def mismatched_version(name):\n"
            "    return '3.9.0' if name == 'mutmut' else original_version(name)\n"
            "importlib.metadata.version = mismatched_version\n"
        )
    mutation_step = next(
        step["run"]
        for step in workflow("python.yml")["jobs"]["checks"]["steps"]
        if step["name"] == "Mutate changed functions"
    )
    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", mutation_step],
        cwd=repo,
        env={
            **os.environ,
            "PATH": f"{tool_bin}:{os.environ['PATH']}",
            "PYTHONPATH": f"{startup}:{ROOT}",
            "BASE_SHA": base,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        expected_code,
        "",
        expected_error,
    )


def test_python_workflow_installs_verified_mutmut_version():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert project["project"]["optional-dependencies"]["mutation"] == ["mutmut==3.8.0"]
    install = next(
        step["run"]
        for step in workflow("python.yml")["jobs"]["checks"]["steps"]
        if step["name"] == "Install shared checks"
    )
    assert install == (
        "python -m pip install --constraint .ci-workflows/requirements/ci-lock.txt "
        "-e '.ci-workflows[mutation]'"
    )


def test_local_mutation_command_uses_ci_selector_and_gate():
    instructions = (ROOT / "AGENTS.md").read_text()
    workflow_step = next(
        step["run"]
        for step in workflow("python.yml")["jobs"]["checks"]["steps"]
        if step["name"] == "Mutate changed functions"
    )
    assert workflow_step == "bash .ci-workflows/scripts/workflow-glue.sh mutate-python"
    assert (
        "BASE_SHA=$(git merge-base origin/main HEAD) "
        "bash scripts/workflow-glue.sh mutate-python"
    ) in instructions
