"""Plan a caller's CI rehearsal from its workflows and the pinned shared workflows."""

import re
import subprocess
from pathlib import Path

import pytest
import yaml

from ci_workflows.errors import ConfigurationError
from ci_workflows.rehearsal import plan

ROOT = Path(__file__).parent.parent
SOURCE_ROOT = ROOT.parent if ROOT.name == "mutants" else ROOT
FIXTURES = ROOT / "tests" / "fixtures" / "rehearsal"
SHARED_PYTHON = (FIXTURES / "shared" / "python.yml").read_text()
SHARED_SHELL = (FIXTURES / "shared" / "shell.yml").read_text()
ZEROS = "0" * 40
SHORT = ["bash", "-e", "{0}"]
LONG = ["bash", "--noprofile", "--norc", "-eo", "pipefail", "{0}"]
PIN_AFTER = re.compile(r"((?:python|shell)\.yml@)[0-9a-f]{40}")


def git(folder, *args):
    return subprocess.run(
        ["git", "-C", str(folder), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def new_repo(folder, branch=None):
    folder.mkdir()
    git(folder, "init", "-q")
    git(folder, "config", "user.name", "Rehearsal Test")
    git(folder, "config", "user.email", "rehearsal@example.invalid")
    if branch:
        git(folder, "checkout", "-q", "-b", branch)
    return folder


def commit_files(folder, files):
    for name, text in files.items():
        path = folder / ".github" / "workflows" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", "workflows")
    return git(folder, "rev-parse", "HEAD").strip()


def shared_repo(tmp_path):
    folder = new_repo(tmp_path / "shared")
    pin = commit_files(folder, {"python.yml": SHARED_PYTHON, "shell.yml": SHARED_SHELL})
    return folder, pin


def changed_pin(shared, files):
    """Commit changed shared workflows on a side branch and return to the first commit."""
    git(shared, "checkout", "-q", "-b", "changed")
    pin = commit_files(shared, files)
    git(shared, "checkout", "-q", "-")
    return pin


def caller_text(fixture, pin):
    return PIN_AFTER.sub(
        lambda match: match.group(1) + pin, (FIXTURES / "callers" / fixture).read_text()
    )


def caller_repo(tmp_path, files):
    folder = new_repo(tmp_path / "caller", branch="agent/sample")
    commit_files(folder, files)
    return folder


def edit(text, old, new):
    assert old in text
    return text.replace(old, new, 1)


def fixture_steps(text, job):
    return yaml.safe_load(text)["jobs"][job]["steps"]


def claude_config(tmp_path, change=lambda text, pin: text, extra=None):
    shared, pin = shared_repo(tmp_path)
    files = {"ci.yml": change(caller_text("claude-config.yml", pin), pin)}
    files.update(extra or {})
    return caller_repo(tmp_path, files), shared, pin


def refusal(caller, shared):
    with pytest.raises(ConfigurationError) as raised:
        plan(caller, shared)
    return str(raised.value)


def floor_env(floor, job):
    return {
        "BASE_SHA": ZEROS,
        "DEFAULT_BRANCH": "main",
        "PUSHED_REF": "agent/sample",
        "COVERAGE_FLOOR": floor,
        "CALLER_WORKFLOW": ".github/workflows/ci.yml",
        "CALLER_JOB": job,
    }


CHANGED_ENV = {
    "BASE_SHA": ZEROS,
    "DEFAULT_BRANCH": "main",
    "PUSHED_REF": "agent/sample",
}
CHECKOUTS_AND_SETUP = [
    {"name": "Check out caller", "kind": "checkout-caller"},
    {
        "name": "Check out pinned shared checks",
        "kind": "checkout-shared",
        "path": ".ci-workflows",
    },
    {"name": "Set up Python", "kind": "setup-python", "version": "3.12"},
]
INSTALL_SHARED = {
    "name": "Install shared checks",
    "kind": "run",
    "shell": SHORT,
    "run": "python -m pip install --constraint .ci-workflows/requirements/ci-lock.txt -e .ci-workflows",
    "env": {},
}


def test_claude_config_caller(tmp_path):
    caller, shared, pin = claude_config(tmp_path)
    install_command = yaml.safe_load(
        (FIXTURES / "callers" / "claude-config.yml").read_text()
    )["jobs"]["python"]["with"]["install_command"]
    source_paths = '["hooks", "skills/buildlog/scripts", "tools"]'
    python_runs = {
        step["name"]: step.get("run") for step in fixture_steps(SHARED_PYTHON, "tests")
    }
    shell_runs = {
        step["name"]: step.get("run") for step in fixture_steps(SHARED_SHELL, "checks")
    }

    result = plan(caller, shared)

    python_steps = CHECKOUTS_AND_SETUP + [
        {
            "name": "Install system packages",
            "kind": "skip",
            "reason": "no runner packages",
        },
        INSTALL_SHARED,
        {
            "name": "Install caller",
            "kind": "run",
            "shell": SHORT,
            "run": 'bash -e -o pipefail -c "$INSTALL_COMMAND"',
            "env": {"INSTALL_COMMAND": install_command},
        },
        {
            "name": "Run tests with branch coverage",
            "kind": "run",
            "shell": SHORT,
            "run": "bash .ci-workflows/scripts/workflow-glue.sh test-python",
            "env": {},
        },
        {
            "name": "Check floor ratchet",
            "kind": "run",
            "shell": LONG,
            "run": "bash .ci-workflows/scripts/workflow-glue.sh floor python coverage.xml",
            "env": floor_env("69", "python"),
        },
        {
            "name": "Check changed lines",
            "kind": "run",
            "shell": LONG,
            "run": "bash .ci-workflows/scripts/workflow-glue.sh changed python coverage.xml",
            "env": CHANGED_ENV,
        },
    ]
    shell_steps = CHECKOUTS_AND_SETUP + [
        {
            "name": "Install Bats and kcov",
            "kind": "packages",
            "packages": ["bats", "kcov"],
        },
        INSTALL_SHARED,
        {
            "name": "Run Bats under kcov",
            "kind": "run",
            "shell": SHORT,
            "run": shell_runs["Run Bats under kcov"],
            "env": {"SOURCE_PATHS": source_paths, "TESTS_PATH": "tests/shell"},
        },
        {
            "name": "Check floor ratchet",
            "kind": "run",
            "shell": LONG,
            "run": "bash .ci-workflows/scripts/workflow-glue.sh floor shell .coverage-shell/cov.xml",
            "env": floor_env("73", "shell"),
        },
        {
            "name": "Check changed lines",
            "kind": "run",
            "shell": LONG,
            "run": "bash .ci-workflows/scripts/workflow-glue.sh changed shell .coverage-shell/cov.xml",
            "env": CHANGED_ENV | {"SOURCE_PATHS": source_paths},
        },
    ]
    assert result == {
        "branch": "agent/sample",
        "event": {
            "BASE_SHA": ZEROS,
            "DEFAULT_BRANCH": "main",
            "PUSHED_REF": "agent/sample",
        },
        "jobs": [
            {
                "workflow": ".github/workflows/ci.yml",
                "job": "python",
                "rehearsed": True,
                "shared": "python.yml",
                "pin": pin,
                "inputs": {
                    "caller_workflow": ".github/workflows/ci.yml",
                    "caller_job": "python",
                    "coverage_floor": "69",
                    "install_command": install_command,
                    "system_packages": "[]",
                },
                "steps": python_steps,
            },
            {
                "workflow": ".github/workflows/ci.yml",
                "job": "shell",
                "rehearsed": True,
                "shared": "shell.yml",
                "pin": pin,
                "inputs": {
                    "caller_workflow": ".github/workflows/ci.yml",
                    "caller_job": "shell",
                    "source_paths": source_paths,
                    "tests_path": "tests/shell",
                    "coverage_floor": "73",
                },
                "steps": shell_steps,
            },
        ],
    }
    assert "\n" in install_command.rstrip("\n")
    for job, runs in (
        (result["jobs"][0], python_runs),
        (result["jobs"][1], shell_runs),
    ):
        for step in job["steps"]:
            if step["kind"] == "run":
                assert step["run"] == runs[step["name"]]


def test_keepbreathing_infra_caller(tmp_path):
    shared, pin = shared_repo(tmp_path)
    text = caller_text("keepbreathing-infra.yml", pin)
    caller = caller_repo(tmp_path, {"ci.yml": text})
    keys = list(yaml.safe_load(text)["jobs"])

    jobs = plan(caller, shared)["jobs"]

    assert len(jobs) == 21 and keys[17] == "python"
    python = jobs[17]
    assert (
        python["job"],
        python["rehearsed"],
        python["inputs"]["system_packages"],
    ) == ("python", True, '["podman"]')
    assert python["steps"][3] == {
        "name": "Install system packages",
        "kind": "packages",
        "packages": ["podman"],
    }
    assert jobs[:17] + jobs[18:] == [
        {
            "workflow": ".github/workflows/ci.yml",
            "job": key,
            "rehearsed": False,
            "reason": "no shared workflow",
        }
        for key in keys[:17] + keys[18:]
    ]


def test_ci_workflows_caller(tmp_path):
    shared, _ = shared_repo(tmp_path)
    caller = caller_repo(
        tmp_path,
        {
            "ci.yml": (FIXTURES / "callers" / "ci-workflows.yml").read_text(),
            "python.yml": SHARED_PYTHON,
            "shell.yml": SHARED_SHELL,
        },
    )

    jobs = plan(caller, shared)["jobs"]

    assert [job["job"] for job in jobs] == [
        "actionlint",
        "checks",
        "powershell",
        "shell",
    ]
    assert jobs[0] == {
        "workflow": ".github/workflows/ci.yml",
        "job": "actionlint",
        "rehearsed": False,
        "reason": "no shared workflow",
    }
    assert jobs[2] == {
        "workflow": ".github/workflows/ci.yml",
        "job": "powershell",
        "rehearsed": False,
        "reason": "powershell.yml runs on Windows",
    }
    checks, shell = jobs[1], jobs[3]
    assert {
        key: checks[key] for key in ("workflow", "rehearsed", "shared", "pin", "inputs")
    } == {
        "workflow": ".github/workflows/ci.yml",
        "rehearsed": True,
        "shared": "python.yml",
        "pin": None,
        "inputs": {
            "coverage_floor": "100",
            "install_command": "python -m pip install -e '.[test,mutation]'",
            "caller_workflow": ".github/workflows/ci.yml",
            "caller_job": "checks",
            "system_packages": "[]",
        },
    }
    assert (
        shell["rehearsed"],
        shell["shared"],
        shell["pin"],
        shell["inputs"]["coverage_floor"],
    ) == (True, "shell.yml", None, "100")
    assert (len(checks["steps"]), len(shell["steps"])) == (9, 8)


def test_codex_config_caller(tmp_path):
    shared, pin = shared_repo(tmp_path)
    caller = caller_repo(tmp_path, {"ci.yml": caller_text("codex-config.yml", pin)})

    jobs = plan(caller, shared)["jobs"]

    assert [
        (job["job"], job["rehearsed"], job["pin"], job["inputs"]["coverage_floor"])
        for job in jobs
    ] == [("python", True, pin, "1")]


def test_files_in_sorted_order_and_other_workflows(tmp_path):
    shared, pin = shared_repo(tmp_path)
    lint = (
        "on:\n  push: {}\njobs:\n"
        f"  lint:\n    uses: sh4i-yurei/ci-workflows/.github/workflows/molecule.yml@{pin}\n"
        "  other:\n    uses: octo/other/.github/workflows/x.yml@v1\n"
    )
    caller = caller_repo(
        tmp_path, {"lint.yaml": lint, "ci.yml": caller_text("claude-config.yml", pin)}
    )

    jobs = plan(caller, shared)["jobs"]

    assert [(job["workflow"], job["job"]) for job in jobs[:2]] == [
        (".github/workflows/ci.yml", "python"),
        (".github/workflows/ci.yml", "shell"),
    ]
    assert jobs[2:] == [
        {
            "workflow": ".github/workflows/lint.yaml",
            "job": "lint",
            "rehearsed": False,
            "reason": "molecule.yml isn't rehearsed",
        },
        {
            "workflow": ".github/workflows/lint.yaml",
            "job": "other",
            "rehearsed": False,
            "reason": "not a ci-workflows workflow",
        },
    ]


@pytest.mark.parametrize(
    "uses_end", ["molecule.yml", "molecule.yml@", "molecule.yml@main"]
)
def test_other_ci_workflows_workflow_is_listed_whatever_its_pin(tmp_path, uses_end):
    lint = (
        "on:\n  push: {}\njobs:\n"
        f"  lint:\n    uses: sh4i-yurei/ci-workflows/.github/workflows/{uses_end}\n"
    )
    caller, shared, _ = claude_config(tmp_path, extra={"lint.yaml": lint})

    assert plan(caller, shared)["jobs"][2:] == [
        {
            "workflow": ".github/workflows/lint.yaml",
            "job": "lint",
            "rehearsed": False,
            "reason": "molecule.yml isn't rehearsed",
        }
    ]


def test_local_shared_workflow_that_does_not_exist(tmp_path):
    shared, _ = shared_repo(tmp_path)
    ci = "on:\n  push: {}\njobs:\n  checks:\n    uses: ./.github/workflows/python.yml\n"
    caller = caller_repo(tmp_path, {"ci.yml": ci})

    assert (
        refusal(caller, shared)
        == ".github/workflows/ci.yml job checks: .github/workflows/python.yml not found"
    )


def test_only_yml_and_yaml_files_are_read(tmp_path):
    caller, shared, _ = claude_config(
        tmp_path, extra={"README.md": "just: text\n", "ci.yml.orig": "just: text\n"}
    )

    jobs = plan(caller, shared)["jobs"]

    assert [(job["workflow"], job["job"]) for job in jobs] == [
        (".github/workflows/ci.yml", "python"),
        (".github/workflows/ci.yml", "shell"),
    ]


def test_local_workflow_that_is_not_rehearsed(tmp_path):
    shared, _ = shared_repo(tmp_path)
    ci = "on:\n  push: {}\njobs:\n  deploy:\n    uses: ./.github/workflows/deploy.yml\n"
    caller = caller_repo(tmp_path, {"ci.yml": ci})

    assert plan(caller, shared)["jobs"] == [
        {
            "workflow": ".github/workflows/ci.yml",
            "job": "deploy",
            "rehearsed": False,
            "reason": "deploy.yml isn't rehearsed",
        }
    ]


@pytest.mark.parametrize(
    "pin_text, message",
    [
        ("main", ".github/workflows/ci.yml job python: pin main is not a commit"),
        (
            "a" * 40,
            ".github/workflows/ci.yml job python: {a} is not in {shared}; fetch ci-workflows",
        ),
        ("{upper}", ".github/workflows/ci.yml job python: pin {upper} is not a commit"),
    ],
)
def test_bad_pins(tmp_path, pin_text, message):
    def change(text, pin):
        return edit(
            text,
            f"python.yml@{pin}",
            "python.yml@" + pin_text.format(upper=pin.upper()),
        )

    caller, shared, pin = claude_config(tmp_path, change)

    assert refusal(caller, shared) == message.format(
        a="a" * 40, shared=shared, upper=pin.upper()
    )


@pytest.mark.parametrize("uses_end", ["python.yml", "python.yml@"])
def test_missing_pin(tmp_path, uses_end):
    caller, shared, _ = claude_config(
        tmp_path, lambda text, pin: edit(text, f"python.yml@{pin}", uses_end)
    )

    assert (
        refusal(caller, shared) == ".github/workflows/ci.yml job python: pin is missing"
    )


@pytest.mark.parametrize(
    "old, new, message",
    [
        (
            "      coverage_floor: 69\n",
            "      coverage_floor: 69\n      frobnicate: 1\n",
            "unknown input frobnicate",
        ),
        (
            "      caller_job: python\n",
            "      caller_job: ${{ github.job }}\n",
            "input caller_job holds an expression",
        ),
        (
            "      coverage_floor: 69\n",
            "      coverage_floor: 69\n      system_packages: podman\n",
            "input system_packages is not a JSON list",
        ),
        (
            "      coverage_floor: 69\n",
            "      coverage_floor: 69\n      system_packages: '[1]'\n",
            "input system_packages is not a JSON list",
        ),
        (
            "      coverage_floor: 69\n",
            '      coverage_floor: 69\n      system_packages: \'{"a": "b"}\'\n',
            "input system_packages is not a JSON list",
        ),
    ],
)
def test_bad_inputs(tmp_path, old, new, message):
    caller, shared, _ = claude_config(tmp_path, lambda text, pin: edit(text, old, new))

    assert refusal(caller, shared) == ".github/workflows/ci.yml job python: " + message


def test_missing_input(tmp_path):
    def change(text, pin):
        changed = re.sub(r"      install_command: \|\n(?:        .*\n)+", "", text)
        assert "install_command" not in changed
        return changed

    caller, shared, _ = claude_config(tmp_path, change)

    assert (
        refusal(caller, shared)
        == ".github/workflows/ci.yml job python: missing input install_command"
    )


def test_detached_caller(tmp_path):
    caller, shared, _ = claude_config(tmp_path)
    git(caller, "checkout", "-q", "--detach")

    assert refusal(caller, shared) == f"not on a branch: {caller}"


@pytest.mark.parametrize("state", ["no repository", "no commit"])
def test_caller_without_a_commit_is_refused(tmp_path, state):
    shared, pin = shared_repo(tmp_path)
    caller = tmp_path / "caller"
    if state == "no repository":
        caller.mkdir()
    else:
        new_repo(caller, branch="agent/sample")
    workflows = caller / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(caller_text("claude-config.yml", pin))

    assert refusal(caller, shared) == f"not a git repository with a commit: {caller}"


def test_job_with_neither_uses_nor_steps(tmp_path):
    caller, shared, _ = claude_config(
        tmp_path, lambda text, pin: text + "  bare:\n    runs-on: ubuntu-latest\n"
    )

    assert plan(caller, shared)["jobs"][-1:] == [
        {
            "workflow": ".github/workflows/ci.yml",
            "job": "bare",
            "rehearsed": False,
            "reason": "neither uses nor steps",
        }
    ]


PYTHON_CHANGES = {
    "unknown action": (
        "      - name: Install system packages\n",
        "      - name: Cache\n        uses: actions/cache@v4\n      - name: Install system packages\n",
        "python.yml job tests: step 'Cache' unknown action actions/cache",
    ),
    "unknown step key": (
        "      - name: Install shared checks\n",
        "      - name: Install shared checks\n        timeout-minutes: 5\n",
        "python.yml job tests: step 'Install shared checks' unknown key timeout-minutes",
    ),
    "unknown run expression": (
        "workflow-glue.sh test-python\n",
        "workflow-glue.sh test-python ${{ github.sha }}\n",
        "python.yml job tests: step 'Run tests with branch coverage' unknown expression ${{ github.sha }}",
    ),
    "unknown if": (
        "      - name: Install shared checks\n",
        "      - name: Install shared checks\n        if: always()\n",
        "python.yml job tests: step 'Install shared checks' unknown expression always()",
    ),
    "unknown shell": (
        "      - name: Check floor ratchet\n        shell: bash\n",
        "      - name: Check floor ratchet\n        shell: sh\n",
        "python.yml job tests: step 'Check floor ratchet' unknown shell sh",
    ),
    "needs root": (
        "        run: python -m pip install --constraint",
        "        run: sudo python -m pip install --constraint",
        "python.yml job tests: step 'Install shared checks' needs root",
    ),
    "unknown job key": (
        "  tests:\n    runs-on: ubuntu-latest\n",
        "  tests:\n    env: {A: b}\n    runs-on: ubuntu-latest\n",
        "python.yml job tests: unknown key env",
    ),
    "unknown top-level key": (
        "jobs:\n",
        "defaults: {run: {shell: bash}}\njobs:\n",
        "python.yml: unknown key defaults",
    ),
    "missing job": (
        "  tests:\n",
        "  test:\n",
        "python.yml: missing job tests",
    ),
    "unknown checkout": (
        "          ref: ${{ job.workflow_sha }}\n",
        "          ref: main\n",
        "python.yml job tests: step 'Check out pinned shared checks' unknown checkout",
    ),
    "unknown setup": (
        "        with:\n          python-version: '3.12'\n",
        "",
        "python.yml job tests: step 'Set up Python' unknown setup",
    ),
    "job as a list": (
        "  tests:\n    runs-on: ubuntu-latest\n",
        "  tests:\n  - runs-on: ubuntu-latest\n",
        "python.yml job tests: not a job",
    ),
    "unknown expression in a checkout's with": (
        "          fetch-depth: 0\n",
        "          fetch-depth: 0\n          ref: ${{ github.sha }}\n",
        "python.yml job tests: step 'Check out caller' unknown expression ${{ github.sha }}",
    ),
    "workflow_sha outside the shared checkout": (
        "          fetch-depth: 0\n",
        "          fetch-depth: 0\n          ref: ${{ job.workflow_sha }}\n",
        "python.yml job tests: step 'Check out caller' unknown expression ${{ job.workflow_sha }}",
    ),
    "unknown expression in an action's env": (
        "          fetch-depth: 0\n      - name: Check out pinned shared checks\n",
        "          fetch-depth: 0\n        env:\n          TOKEN: ${{ github.token }}\n"
        "      - name: Check out pinned shared checks\n",
        "python.yml job tests: step 'Check out caller' unknown expression ${{ github.token }}",
    ),
    "step with neither uses nor run": (
        "      - name: Install system packages\n",
        "      - name: Empty\n      - name: Install system packages\n",
        "python.yml job tests: step 'Empty' neither uses nor run",
    ),
    "step that is not a mapping": (
        "      - name: Install system packages\n",
        "      - just text\n      - name: Install system packages\n",
        "python.yml job tests: not a job",
    ),
}


@pytest.mark.parametrize(
    "old, new, message", PYTHON_CHANGES.values(), ids=PYTHON_CHANGES.keys()
)
def test_changed_shared_python_at_pin(tmp_path, old, new, message):
    shared, pin = shared_repo(tmp_path)
    second = changed_pin(shared, {"python.yml": edit(SHARED_PYTHON, old, new)})
    text = edit(
        caller_text("claude-config.yml", pin),
        f"python.yml@{pin}",
        f"python.yml@{second}",
    )
    caller = caller_repo(tmp_path, {"ci.yml": text})

    assert refusal(caller, shared) == message


def caller_at_second_python_pin(tmp_path, python):
    """A claude-config caller whose python job pins a second commit holding python."""
    shared, pin = shared_repo(tmp_path)
    second = changed_pin(shared, {"python.yml": python})
    text = edit(
        caller_text("claude-config.yml", pin),
        f"python.yml@{pin}",
        f"python.yml@{second}",
    )
    return caller_repo(tmp_path, {"ci.yml": text}), shared


ROUND1_CHANGES = {
    "action with a second @": (
        "      - name: Install system packages\n",
        "      - name: Cache\n        uses: actions/cache@v4@extra\n"
        "      - name: Install system packages\n",
        "python.yml job tests: step 'Cache' unknown action actions/cache",
    ),
    "unknown expression in the shared checkout's path": (
        "          path: .ci-workflows\n",
        "          path: ${{ github.sha }}\n",
        "python.yml job tests: step 'Check out pinned shared checks' unknown expression ${{ github.sha }}",
    ),
    "unknown expression in python-version": (
        "          python-version: '3.12'\n",
        "          python-version: ${{ github.sha }}\n",
        "python.yml job tests: step 'Set up Python' unknown expression ${{ github.sha }}",
    ),
    "unknown expression in a run step's env": (
        "          INSTALL_COMMAND: ${{ inputs.install_command }}\n",
        "          INSTALL_COMMAND: ${{ github.sha }}\n",
        "python.yml job tests: step 'Install caller' unknown expression ${{ github.sha }}",
    ),
    "the package condition on another step": (
        "      - name: Install shared checks\n",
        "      - name: Install shared checks\n        if: inputs.system_packages != '[]'\n",
        "python.yml job tests: step 'Install shared checks' unknown expression inputs.system_packages != '[]'",
    ),
}


@pytest.mark.parametrize(
    "old, new, message", ROUND1_CHANGES.values(), ids=ROUND1_CHANGES.keys()
)
def test_round1_refusals_at_pin(tmp_path, old, new, message):
    caller, shared = caller_at_second_python_pin(
        tmp_path, edit(SHARED_PYTHON, old, new)
    )

    assert refusal(caller, shared) == message


def test_known_expressions_in_checkout_path_and_python_version(tmp_path):
    default_branch = "${{ github.event.repository.default_branch }}"
    python = edit(
        edit(
            SHARED_PYTHON,
            "          path: .ci-workflows\n",
            f"          path: {default_branch}\n",
        ),
        "          python-version: '3.12'\n",
        f"          python-version: {default_branch}\n",
    )
    caller, shared = caller_at_second_python_pin(tmp_path, python)

    steps = plan(caller, shared)["jobs"][0]["steps"]

    assert steps[1:3] == [
        {
            "name": "Check out pinned shared checks",
            "kind": "checkout-shared",
            "path": "main",
        },
        {"name": "Set up Python", "kind": "setup-python", "version": "main"},
    ]


def test_shared_workflow_that_is_not_a_workflow(tmp_path):
    caller, shared = caller_at_second_python_pin(tmp_path, "just: text\n")

    assert refusal(caller, shared) == "python.yml: not a workflow"


@pytest.mark.parametrize("steps", ["run tests", {"name": "Check out caller"}])
def test_shared_steps_that_are_not_a_list(tmp_path, steps):
    document = yaml.safe_load(SHARED_PYTHON)
    document["jobs"]["tests"]["steps"] = steps
    shared, pin = shared_repo(tmp_path)
    second = changed_pin(
        shared, {"python.yml": yaml.safe_dump(document, sort_keys=False)}
    )
    text = edit(
        caller_text("claude-config.yml", pin),
        f"python.yml@{pin}",
        f"python.yml@{second}",
    )
    caller = caller_repo(tmp_path, {"ci.yml": text})

    assert refusal(caller, shared) == "python.yml job tests: not a job"


def test_changed_shared_shell_at_pin(tmp_path):
    shared, pin = shared_repo(tmp_path)
    second = changed_pin(
        shared,
        {"shell.yml": edit(SHARED_SHELL, "-qq bats kcov\n", "-qq bats kcov jq\n")},
    )
    text = edit(
        caller_text("claude-config.yml", pin), f"shell.yml@{pin}", f"shell.yml@{second}"
    )
    caller = caller_repo(tmp_path, {"ci.yml": text})

    assert (
        refusal(caller, shared)
        == "shell.yml job checks: step 'Install Bats and kcov' needs root"
    )


def test_package_step_without_its_condition_installs_an_empty_list(tmp_path):
    shared, pin = shared_repo(tmp_path)
    second = changed_pin(
        shared,
        {
            "python.yml": edit(
                SHARED_PYTHON, "        if: inputs.system_packages != '[]'\n", ""
            )
        },
    )
    text = edit(
        caller_text("claude-config.yml", pin),
        f"python.yml@{pin}",
        f"python.yml@{second}",
    )
    caller = caller_repo(tmp_path, {"ci.yml": text})

    jobs = plan(caller, shared)["jobs"]

    assert [job["steps"][3] for job in jobs[:1]] == [
        {"name": "Install system packages", "kind": "packages", "packages": []}
    ]


@pytest.mark.parametrize(
    "text, message",
    [
        ("just: text\n", ".github/workflows/ci.yml: not a workflow"),
        ("jobs: [a]\n", ".github/workflows/ci.yml: not a workflow"),
        ("jobs: {a: b\n", ".github/workflows/ci.yml: not a workflow"),
        (
            "on:\n  push: {}\njobs:\n  python: [a]\n",
            ".github/workflows/ci.yml job python: not a job",
        ),
    ],
)
def test_caller_that_is_not_a_workflow(tmp_path, text, message):
    shared, _ = shared_repo(tmp_path)
    caller = caller_repo(tmp_path, {"ci.yml": text})

    assert refusal(caller, shared) == message


REUSE_JOB = "jobs:\n  reused:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo reused\n"


@pytest.mark.parametrize(
    "trigger, listed",
    [
        ("on:\n  workflow_call: {}\n", []),
        ("on: workflow_call\n", []),
        ("on: [workflow_call]\n", []),
        (
            "on:\n  workflow_call: {}\n  push: {}\n",
            [
                {
                    "workflow": ".github/workflows/reuse.yml",
                    "job": "reused",
                    "rehearsed": False,
                    "reason": "no shared workflow",
                }
            ],
        ),
    ],
)
def test_reusable_workflow_files_are_not_listed(tmp_path, trigger, listed):
    caller, shared, _ = claude_config(
        tmp_path, extra={"reuse.yml": trigger + REUSE_JOB}
    )

    jobs = plan(caller, shared)["jobs"]

    assert [job["job"] for job in jobs[:2]] == ["python", "shell"]
    assert jobs[2:] == listed


def test_reusable_file_sorted_before_ci_is_skipped(tmp_path):
    caller, shared, _ = claude_config(
        tmp_path, extra={"a-reuse.yml": "on:\n  workflow_call: {}\n" + REUSE_JOB}
    )

    jobs = plan(caller, shared)["jobs"]

    assert [(job["workflow"], job["job"]) for job in jobs] == [
        (".github/workflows/ci.yml", "python"),
        (".github/workflows/ci.yml", "shell"),
    ]


def test_drift_against_this_repository_workflows(tmp_path):
    shared, _ = shared_repo(tmp_path)
    workflows = SOURCE_ROOT / ".github" / "workflows"
    own_jobs = yaml.safe_load((workflows / "ci.yml").read_text())["jobs"]
    ci = "name: Drift\non:\n  push: {}\n" + yaml.safe_dump(
        {"jobs": {name: own_jobs[name] for name in ("checks", "shell")}},
        sort_keys=False,
    )
    caller = caller_repo(
        tmp_path,
        {
            "ci.yml": ci,
            "python.yml": (workflows / "python.yml").read_text(),
            "shell.yml": (workflows / "shell.yml").read_text(),
        },
    )

    jobs = plan(caller, shared)["jobs"]

    assert [
        (job["workflow"], job["job"], job["rehearsed"], len(job["steps"]))
        for job in jobs
    ] == [
        (".github/workflows/ci.yml", "checks", True, 9),
        (".github/workflows/ci.yml", "shell", True, 8),
    ]


def snapshot(folder):
    files = sorted(
        path.relative_to(folder).as_posix()
        for path in folder.rglob("*")
        if ".git" not in path.relative_to(folder).parts
    )
    return files, git(folder, "status", "--porcelain", "--ignored")


@pytest.mark.parametrize(
    "fixture",
    [
        "claude-config.yml",
        "codex-config.yml",
        "keepbreathing-infra.yml",
        "ci-workflows.yml",
    ],
)
def test_plan_changes_neither_repository(tmp_path, fixture):
    shared, pin = shared_repo(tmp_path)
    files = {"ci.yml": caller_text(fixture, pin)}
    if fixture == "ci-workflows.yml":
        files.update({"python.yml": SHARED_PYTHON, "shell.yml": SHARED_SHELL})
    caller = caller_repo(tmp_path, files)
    before = snapshot(caller), snapshot(shared)

    plan(caller, shared)

    assert (snapshot(caller), snapshot(shared)) == before
