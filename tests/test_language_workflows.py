from pathlib import Path
import shlex
import subprocess

import yaml


ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    return yaml.load(
        (ROOT / ".github" / "workflows" / name).read_text(), Loader=yaml.BaseLoader
    )


def test_javascript_workflow_runs_branch_coverage_and_changed_mutations():
    document = workflow("javascript.yml")
    assert document["on"] == {
        "workflow_call": {
            "inputs": {
                "coverage_floor": {"required": "true", "type": "number"},
                "install_command": {"required": "true", "type": "string"},
                "caller_workflow": {"required": "true", "type": "string"},
                "caller_job": {"required": "true", "type": "string"},
                "mutation_sources": {"required": "true", "type": "string"},
            }
        }
    }
    assert document["permissions"] == {"contents": "read"}
    steps = document["jobs"]["checks"]["steps"]
    assert {step["name"] for step in steps} == {
        "Check out caller",
        "Check out pinned shared checks",
        "Set up Node",
        "Set up Python",
        "Install shared checks",
        "Install caller",
        "Run Node tests with branch coverage",
        "Check floor ratchet",
        "Check changed lines",
        "Mutate changed JavaScript",
    }
    coverage = next(
        step for step in steps if step["name"] == "Run Node tests with branch coverage"
    )
    assert coverage["run"] == (
        "node --test --experimental-test-coverage "
        "--test-coverage-exclude='**/test/**' --test-coverage-exclude='**/tests/**' "
        "--test-coverage-exclude='**/*.test.*' --test-coverage-exclude='**/*.spec.*' "
        "--test-reporter=spec --test-reporter-destination=stdout "
        "--test-reporter=lcov --test-reporter-destination=coverage.lcov"
    )
    shared = next(
        step for step in steps if step["name"] == "Check out pinned shared checks"
    )
    assert shared["with"] == {
        "repository": "${{ job.workflow_repository }}",
        "ref": "${{ job.workflow_sha }}",
        "path": ".ci-workflows",
    }
    changed = next(step for step in steps if step["name"] == "Check changed lines")
    assert changed["env"]["MUTATION_SOURCES"] == "${{ inputs.mutation_sources }}"
    assert (
        changed["run"]
        == "bash .ci-workflows/scripts/workflow-glue.sh changed javascript coverage.lcov"
    )


def test_javascript_workflow_excludes_tests_and_prints_failure(tmp_path):
    steps = workflow("javascript.yml")["jobs"]["checks"]["steps"]
    command = next(
        step["run"]
        for step in steps
        if step["name"] == "Run Node tests with branch coverage"
    )
    source = tmp_path / "src"
    tests = tmp_path / "test"
    source.mkdir()
    tests.mkdir()
    (source / "app.js").write_text("export const answer = 42;\n")
    (tests / "app.test.js").write_text(
        "import { test } from 'node:test';\n"
        "import { strict as assert } from 'node:assert';\n"
        "import { answer } from '../src/app.js';\n"
        "test('visible failure detail', () => assert.equal(answer, 43));\n"
    )
    (tmp_path / "package.json").write_text('{"type":"module"}\n')
    result = subprocess.run(
        shlex.split(command), cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert result.returncode == 1
    assert "visible failure detail" in result.stdout
    assert "42 !== 43" in result.stdout
    report = (tmp_path / "coverage.lcov").read_text()
    assert "src/app.js" in report
    assert "test/app.test.js" not in report


def test_shell_workflow_runs_bats_under_kcov():
    document = workflow("shell.yml")
    assert document["on"] == {
        "workflow_call": {
            "inputs": {
                "coverage_floor": {"required": "true", "type": "number"},
                "caller_workflow": {"required": "true", "type": "string"},
                "caller_job": {"required": "true", "type": "string"},
                "source_path": {"required": "true", "type": "string"},
                "tests_path": {"required": "true", "type": "string"},
            }
        }
    }
    assert document["permissions"] == {"contents": "read"}
    steps = document["jobs"]["checks"]["steps"]
    assert {step["name"] for step in steps} == {
        "Check out caller",
        "Check out pinned shared checks",
        "Set up Python",
        "Install Bats and kcov",
        "Install shared checks",
        "Run Bats under kcov",
        "Check floor ratchet",
        "Check changed lines",
    }
    coverage = next(step for step in steps if step["name"] == "Run Bats under kcov")
    assert coverage["run"] == (
        'kcov --cobertura-only --include-path="$SOURCE_PATH" '
        '.coverage-shell /usr/bin/bats "$TESTS_PATH"'
    )
    changed = next(step for step in steps if step["name"] == "Check changed lines")
    assert changed["env"]["SOURCE_PATH"] == "${{ inputs.source_path }}"
    assert (
        changed["run"]
        == "bash .ci-workflows/scripts/workflow-glue.sh changed shell .coverage-shell/cov.xml"
    )


def test_powershell_workflow_runs_pester_with_jacoco():
    document = workflow("powershell.yml")
    assert document["on"] == {
        "workflow_call": {
            "inputs": {
                "coverage_floor": {"required": "true", "type": "number"},
                "caller_workflow": {"required": "true", "type": "string"},
                "caller_job": {"required": "true", "type": "string"},
                "source_path": {"required": "true", "type": "string"},
                "tests_path": {"required": "true", "type": "string"},
            }
        }
    }
    assert document["permissions"] == {"contents": "read"}
    assert set(document["jobs"]) == {"tests", "checks"}
    test_job = document["jobs"]["tests"]
    assert test_job["runs-on"] == "windows-latest"
    steps = test_job["steps"]
    assert {step["name"] for step in steps} == {
        "Check out caller",
        "Install Pester 5",
        "Run Pester with JaCoCo",
        "Upload coverage report",
    }
    assert all(step["shell"] == "powershell" for step in steps if "run" in step)
    install = next(step for step in steps if step["name"] == "Install Pester 5")
    assert install["run"] == (
        "Install-Module -Name Pester -RequiredVersion 5.7.1 "
        "-Scope CurrentUser -Force -SkipPublisherCheck"
    )
    pester = next(step for step in steps if step["name"] == "Run Pester with JaCoCo")
    assert pester["env"] == {
        "SOURCE_PATH": "${{ inputs.source_path }}",
        "TESTS_PATH": "${{ inputs.tests_path }}",
    }
    assert "Import-Module Pester -RequiredVersion 5.7.1" in pester["run"]
    assert "$PSVersionTable.PSEdition" in pester["run"]
    assert "$PSVersionTable.PSVersion" in pester["run"]
    assert "(Get-Module Pester).Version" in pester["run"]
    assert "$config.Run.Exit = $true" in pester["run"]
    assert "$config.CodeCoverage.OutputFormat = 'CoverageGutters'" in pester["run"]
    upload = next(step for step in steps if step["name"] == "Upload coverage report")
    assert (
        upload["uses"]
        == "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
    )
    assert upload["with"] == {
        "name": "powershell-coverage",
        "path": "coverage-jacoco.xml",
        "if-no-files-found": "error",
    }

    gate_job = document["jobs"]["checks"]
    assert gate_job["runs-on"] == "ubuntu-latest"
    assert gate_job["needs"] == "tests"
    gate_steps = gate_job["steps"]
    assert {step["name"] for step in gate_steps} == {
        "Check out caller",
        "Check out pinned shared checks",
        "Set up Python",
        "Install shared checks",
        "Download coverage report",
        "Check floor ratchet",
        "Check changed lines",
    }
    download = next(
        step for step in gate_steps if step["name"] == "Download coverage report"
    )
    assert (
        download["uses"]
        == "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093"
    )
    assert download["with"] == {"name": "powershell-coverage"}
    floor = next(step for step in gate_steps if step["name"] == "Check floor ratchet")
    assert floor["env"] == {
        "BASE_SHA": "${{ github.event.pull_request.base.sha || github.event.before }}",
        "COVERAGE_FLOOR": "${{ inputs.coverage_floor }}",
        "CALLER_WORKFLOW": "${{ inputs.caller_workflow }}",
        "CALLER_JOB": "${{ inputs.caller_job }}",
    }
    assert (
        floor["run"]
        == "bash .ci-workflows/scripts/workflow-glue.sh floor powershell coverage-jacoco.xml"
    )
    changed = next(step for step in gate_steps if step["name"] == "Check changed lines")
    assert (
        changed["env"]["BASE_SHA"]
        == "${{ github.event.pull_request.base.sha || github.event.before }}"
    )
    assert changed["env"]["SOURCE_PATH"] == "${{ inputs.source_path }}"
    assert (
        changed["run"]
        == "bash .ci-workflows/scripts/workflow-glue.sh changed powershell coverage-jacoco.xml"
    )


def test_ci_runs_covered_powershell_sample_on_push():
    document = workflow("ci.yml")
    assert document["on"]["push"] == {}
    assert "powershell" in document["jobs"]
    assert (
        document["jobs"]["powershell"]["uses"] == "./.github/workflows/powershell.yml"
    )
    assert document["jobs"]["powershell"]["with"] == {
        "coverage_floor": "100",
        "caller_workflow": ".github/workflows/ci.yml",
        "caller_job": "powershell",
        "source_path": "tests/fixtures/powershell/covered/src",
        "tests_path": "tests/fixtures/powershell/covered/test",
    }
