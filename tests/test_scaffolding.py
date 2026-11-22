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
    assert ci["on"] == {"push": {}}
    assert ci["permissions"] == {"contents": "read"}
    assert (
        "actionlint@v1.7.12 .github/workflows/*.yml"
        in ci["jobs"]["actionlint"]["steps"][1]["run"]
    )
    assert ci["jobs"]["checks"] == {
        "uses": "./.github/workflows/python.yml",
        "with": {
            "coverage_floor": "100",
            "install_command": "python -m pip install -e '.[test,mutation]'",
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
                "system_packages": {
                    "required": "false",
                    "type": "string",
                    "default": "[]",
                },
            }
        }
    }
    assert python["permissions"] == {"contents": "read"}
    assert list(python["jobs"]) == ["tests", "plan", "mutate", "checks"]
    assert python["concurrency"] == {
        "group": "ci-workflows-python-${{ github.workflow }}-${{ inputs.caller_job }}-${{ github.event_name }}-${{ github.ref }}",
        "cancel-in-progress": "${{ github.ref != format('refs/heads/{0}', github.event.repository.default_branch) }}",
    }
    assert all(job["runs-on"] == "ubuntu-latest" for job in python["jobs"].values())
    runs = [step["run"] for step in python["jobs"]["tests"]["steps"] if "run" in step]
    assert "bash .ci-workflows/scripts/workflow-glue.sh test-python" in runs
    coverage_step = next(
        step
        for step in python["jobs"]["tests"]["steps"]
        if step.get("name") == "Run tests with branch coverage"
    )
    assert "env" not in coverage_step
    steps = python["jobs"]["tests"]["steps"]
    assert [step["name"] for step in steps] == [
        "Check out caller",
        "Check out pinned shared checks",
        "Set up Python",
        "Install system packages",
        "Install shared checks",
        "Install caller",
        "Run tests with branch coverage",
        "Check floor ratchet",
        "Check changed lines",
    ]
    assert all(
        "mutate-python" not in step.get("run", "")
        and "mutmut" not in step.get("run", "")
        for step in steps
    )
    shared = next(
        step for step in steps if step["name"] == "Check out pinned shared checks"
    )
    assert shared["with"] == {
        "repository": "${{ job.workflow_repository }}",
        "ref": "${{ job.workflow_sha }}",
        "path": ".ci-workflows",
    }


def step(job, name):
    return next(item for item in job["steps"] if item["name"] == name)


def test_python_mutation_jobs_and_artifacts():
    jobs = workflow("python.yml")["jobs"]
    tests, plan, mutate, checks = (
        jobs[name] for name in ("tests", "plan", "mutate", "checks")
    )
    assert step(tests, "Run tests with branch coverage") == {
        "name": "Run tests with branch coverage",
        "run": "bash .ci-workflows/scripts/workflow-glue.sh test-python",
    }
    assert plan["if"] == "${{ !github.event.repository.private }}"
    assert plan["outputs"] == {
        "shards": "${{ steps.plan.outputs.shards }}",
        "skipped": "${{ steps.plan.outputs.skipped }}",
    }
    assert [s["name"] for s in plan["steps"]] == [
        "Check out caller",
        "Check out pinned shared checks",
        "Set up Python",
        "Install shared checks",
        "Plan mutation",
        "Upload plan",
    ]
    assert step(plan, "Check out caller")["with"] == {"fetch-depth": "0"}
    assert step(plan, "Plan mutation") == {
        "name": "Plan mutation",
        "id": "plan",
        "shell": "bash",
        "env": {
            "BASE_SHA": "${{ github.event.pull_request.base.sha || github.event.before }}",
            "DEFAULT_BRANCH": "${{ github.event.repository.default_branch }}",
            "PUSHED_REF": "${{ github.event_name == 'push' && github.ref_name || '' }}",
        },
        "run": "bash .ci-workflows/scripts/workflow-glue.sh plan-python",
    }
    assert step(plan, "Upload plan") == {
        "name": "Upload plan",
        "if": "steps.plan.outputs.skipped == ''",
        "uses": "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9",
        "with": {
            "name": "mutation.${{ inputs.caller_job }}.plan",
            "path": "mutation-plan.json",
            "if-no-files-found": "error",
            "overwrite": "true",
        },
    }
    assert mutate["needs"] == "plan"
    assert (
        mutate["if"]
        == "needs.plan.result == 'success' && needs.plan.outputs.shards != '[]'"
    )
    assert mutate["timeout-minutes"] == "60"
    assert mutate["strategy"] == {
        "fail-fast": "false",
        "max-parallel": "10",
        "matrix": {"shard": "${{ fromJSON(needs.plan.outputs.shards) }}"},
    }
    assert [s["name"] for s in mutate["steps"]] == [
        "Record job start",
        "Check out caller",
        "Check out pinned shared checks",
        "Set up Python",
        "Install system packages",
        "Install shared checks",
        "Install caller",
        "Download plan",
        "Run mutation shard",
        "Upload shard results",
    ]
    assert mutate["steps"][0] == {
        "name": "Record job start",
        "run": 'echo "JOB_STARTED=$(date +%s)" >> "$GITHUB_ENV"',
    }
    assert "with" not in step(mutate, "Check out caller")
    assert step(mutate, "Install caller") == step(tests, "Install caller")
    download = {
        "name": "Download plan",
        "uses": "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333",
        "with": {"name": "mutation.${{ inputs.caller_job }}.plan", "path": "."},
    }
    assert step(mutate, "Download plan") == download
    assert step(mutate, "Run mutation shard") == {
        "name": "Run mutation shard",
        "env": {"SHARD": "${{ matrix.shard }}"},
        "run": 'bash .ci-workflows/scripts/workflow-glue.sh shard-python "$SHARD"',
    }
    assert step(mutate, "Upload shard results") == {
        "name": "Upload shard results",
        "uses": "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9",
        "with": {
            "name": "mutation.${{ inputs.caller_job }}.shard.${{ matrix.shard }}",
            "path": "mutation-results/",
            "if-no-files-found": "error",
            "overwrite": "true",
        },
    }
    assert checks["needs"] == ["tests", "plan", "mutate"]
    assert checks["if"] == "${{ !cancelled() }}"
    assert [s["name"] for s in checks["steps"]] == [
        "Check out caller",
        "Check out pinned shared checks",
        "Set up Python",
        "Install shared checks",
        "Download plan",
        "Download shard results",
        "Judge mutation",
    ]
    assert "with" not in step(checks, "Check out caller")
    assert step(checks, "Download plan") == {
        **download,
        "if": "needs.plan.result == 'success' && needs.plan.outputs.skipped == ''",
    }
    assert step(checks, "Download shard results") == {
        "name": "Download shard results",
        "if": "needs.mutate.result == 'success'",
        "uses": "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333",
        "with": {
            "pattern": "mutation.${{ inputs.caller_job }}.shard.*",
            "path": "mutation-shards",
            "merge-multiple": "true",
        },
    }
    assert step(checks, "Judge mutation") == {
        "name": "Judge mutation",
        "env": {
            "TESTS_RESULT": "${{ needs.tests.result }}",
            "PLAN_RESULT": "${{ needs.plan.result }}",
            "MUTATE_RESULT": "${{ needs.mutate.result }}",
            "REPOSITORY_PRIVATE": "${{ github.event.repository.private }}",
            "PLAN_SKIPPED": "${{ needs.plan.outputs.skipped }}",
        },
        "run": "bash .ci-workflows/scripts/workflow-glue.sh checks-python",
    }


def test_python_system_packages_steps_and_execution(tmp_path):
    jobs = workflow("python.yml")["jobs"]
    expected = {
        "name": "Install system packages",
        "if": "inputs.system_packages != '[]'",
        "env": {"SYSTEM_PACKAGES": "${{ inputs.system_packages }}"},
        "run": 'packages=$(jq -r \'.[]\' <<< "$SYSTEM_PACKAGES")\nmapfile -t names <<< "$packages"\nsudo apt-get update -qq\nsudo apt-get install -y -qq "${names[@]}"\n',
    }
    assert step(jobs["tests"], "Install system packages") == expected
    assert step(jobs["mutate"], "Install system packages") == expected
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sudo = bin_dir / "sudo"
    sudo.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$SUDO_LOG"\n')
    sudo.chmod(0o755)
    log = tmp_path / "sudo.log"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "SUDO_LOG": str(log),
        "SYSTEM_PACKAGES": '["podman","bats"]',
    }
    result = subprocess.run(
        ["bash", "-e", "-c", expected["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text() == "apt-get update -qq\napt-get install -y -qq podman bats\n"
    log.unlink()
    env["SYSTEM_PACKAGES"] = '"podman"'
    result = subprocess.run(
        ["bash", "-e", "-c", expected["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert not log.exists()


def test_python_shared_installs_and_pinned_actions():
    jobs = workflow("python.yml")["jobs"]
    expected = "python -m pip install --constraint .ci-workflows/requirements/ci-lock.txt -e '.ci-workflows[mutation]'"
    assert (
        step(jobs["tests"], "Install shared checks")["run"]
        == "python -m pip install --constraint .ci-workflows/requirements/ci-lock.txt -e .ci-workflows"
    )
    for name in ("plan", "mutate", "checks"):
        assert step(jobs[name], "Install shared checks")["run"] == expected
        for common in ("Check out pinned shared checks", "Set up Python"):
            assert step(jobs[name], common) == step(jobs["tests"], common)
    allowed = {
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0",
        "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9",
        "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333",
    }
    assert {
        s["uses"] for job in jobs.values() for s in job["steps"] if "uses" in s
    } <= allowed


def test_readme_documents_workflow_contract():
    readme = (ROOT / "README.md").read_text()
    headings = [line for line in readme.splitlines() if line.startswith("## ")]
    assert headings == [
        "## Workflows",
        "## Inputs",
        "## Python mutation",
        "## Pinning a caller",
        "## Mutation survivors",
        "## Mutation tool gaps",
    ]
    assert "mutant: pkg.f.x__mutmut_3" in readme
    assert "reason: Equivalent transformation" in readme
    assert "Shell and PowerShell have no mutation tool" in readme
    assert "coverage_package" not in readme
    assert "source_paths" in readme
    assert (
        "On a push to a branch other than the default branch, the changed-line and floor steps "
        "compare with the merge base of that branch and the default branch."
    ) in readme
    assert "Mutation is skipped on a push to the default branch" in readme
    assert (
        "| `python.yml` | pytest branch coverage, changed lines, floor, and mutation of the changed functions |"
        in readme
    )
    assert (
        "| `javascript.yml` | Node test runner branch coverage, changed lines, and floor |"
        in readme
    )
    assert (
        "The Python workflow mutates the functions a push changes, as the Python mutation section describes."
        in readme
    )
    assert "The Python and JavaScript workflows do not mutate." not in readme
    assert (
        "The JavaScript workflow does not mutate; a caller runs `scripts/workflow-glue.sh mutate-javascript` locally before pushing. A caller can also run Python mutation locally with `scripts/workflow-glue.sh mutate-python`, which uses the same selection and survivors gate as CI."
        in readme
    )
    assert (
        'The Python workflow also accepts `system_packages`, a JSON list of apt packages such as `["podman"]`, which defaults to `[]`. Its tests and mutate jobs install them in a step of their own before the caller\'s `install_command` runs, so an install command never needs `apt-get`.'
        in readme
    )
    assert (
        "The Python workflow runs four jobs. `tests` runs the caller's tests with branch coverage, then the floor and changed-line checks. `plan` selects the functions the push changed and divides their mutants evenly into shards of at most 200. `mutate` runs one job for each shard, at most 10 at a time, and each shard adds its setup time and its speed to the run's summary. `checks` merges the shards' results and runs the survivors gate."
        in readme
    )
    assert (
        "Require the `checks` job in the caller's ruleset. It runs even after another job fails, and passes only when every job before it passed or was skipped for a reason it prints, because GitHub counts a skipped job as a pass."
        in readme
    )
    assert (
        "Mutation is skipped on a push to the default branch, because the branch's own push already ran it, when no Python function changed, and in a private repository. A newer push to the same branch cancels the older run, except on the default branch, where a newer run waits for the running one instead of cancelling it."
        in readme
    )
    assert (
        "The workflow's mutate job installs that extra before the caller's install command, so the install command must not install another mutmut version."
        in readme
    )
    assert (
        "A mutmut entry is stale when its mutant ran and did not survive, or when its mutant is missing from a file the run mutated or from a file that no longer exists. CI mutates only the changed files, so it keeps an entry for a file that exists but wasn't mutated, because the run can't judge it."
        in readme
    )
    assert (
        "| `python.yml` | pytest branch coverage, changed lines, and floor |"
        not in readme
    )
    assert (
        "A mutmut entry is stale when its mutant is absent from the generated inventory"
        not in readme
    )
    assert "scripts/workflow-glue.sh mutate-python" in readme
    assert "scripts/workflow-glue.sh mutate-javascript" in readme
    assert "same selection and survivors gate" in readme
    assert (
        "local mutation needs the shared checks' `mutation` extra, which pins mutmut 3.8.0"
        in readme
    )
    assert (
        "Trigger the caller workflow on push only. A push run's checks on a pull "
        "request's head commit count toward the pull request's required checks, so a "
        "pull_request trigger would run the same checks a second time."
    ) in readme


def test_architecture_documents_local_mutation_and_push_only_ci():
    architecture = (ROOT / "ARCHITECTURE.md").read_text()
    rows = {
        line.split(" | ", 1)[0]: line
        for line in architecture.splitlines()
        if line.startswith("| `")
    }
    assert "runs on push only" in rows["| `.github/workflows/ci.yml`"]
    assert "mutation commands are run locally" in rows["| `scripts/workflow-glue.sh`"]
    assert "skipping both mutation commands" not in rows["| `scripts/workflow-glue.sh`"]
    assert (
        "In a public repository, CI mutates the changed Python functions on every push except to the default branch, in shards whose results the Python workflow's `checks` job merges before judging them; JavaScript mutation runs only locally."
        in architecture
    )
    assert (
        "`plan-python`, `shard-python` and `checks-python` prepare the inputs of the Python workflow's `plan`, `mutate` and `checks` jobs."
        in rows["| `scripts/workflow-glue.sh`"]
    )
    assert "CI does not mutate" not in architecture
    assert "will run" not in architecture
    assert "CI always runs mutation fresh" not in architecture


def test_architecture_assigns_mutation_ownership():
    architecture = (ROOT / "ARCHITECTURE.md").read_text()
    rows = [line for line in architecture.splitlines() if line.startswith("| `")]
    checks = "| `ci_workflows/checks.py` | The CLI entry point and the code-coverage gates: `changed-lines`, `floor`, `select-functions`, `survivors`, `filter-sources` and `source-paths`; it also parses `rehearse`, `mutation-plan`, `mutation-merge`, `mutation-config` and `mutation-checks`, hands them to their owning modules, and runs the survivors gate on the results `mutation-checks` merges. | Grow another kind of check; a new kind gets its own module, as `rules.py`, `tailnet.py` and `mutation.py` did |"
    mutation = "| `ci_workflows/mutation.py` | Loading mutmut 3.8.0 for every command that needs it, listing a file's mutant names, planning a run's shards (`mutation-plan`), merging shards' results (`mutation-merge`), writing `only_mutate` (`mutation-config`), and judging the merging job's needed jobs before its survivors are checked (`mutation-checks`). | Judge survivors; the survivors gate in `checks.py` does |"
    assert rows[rows.index(checks) + 1] == mutation


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
            "configuration error: source paths must be a non-empty list\n",
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
    result = subprocess.run(
        ["bash", ".ci-workflows/scripts/workflow-glue.sh", "mutate-python"],
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
    assert project["project"]["optional-dependencies"]["mutation"] == [
        "mutmut==3.8.0",
        "tomlkit==0.15.2",
    ]
    install = next(
        step["run"]
        for step in workflow("python.yml")["jobs"]["tests"]["steps"]
        if step["name"] == "Install shared checks"
    )
    assert install == (
        "python -m pip install --constraint .ci-workflows/requirements/ci-lock.txt "
        "-e .ci-workflows"
    )


def test_local_mutation_command_uses_ci_selector_and_gate():
    instructions = (ROOT / "AGENTS.md").read_text()
    assert (
        "BASE_SHA=$(git merge-base origin/main HEAD) "
        "bash scripts/workflow-glue.sh mutate-python"
    ) in instructions
    assert (
        "CI mutates this repository's changed functions on every push to a branch other than main"
        in instructions
    )
    assert "only mutation run" not in instructions
    assert "CI does not mutate" not in instructions
