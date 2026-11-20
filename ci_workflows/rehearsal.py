"""Plan a caller's CI rehearsal from its workflows and the pinned shared workflows.

The plan imitates a branch's first push. It lists every job in the caller's own
workflows, and turns each step of the shared Python and shell jobs into a
checkout, a Python setup, a package step, a skipped step or a step to run with
its shell and environment. Anything it doesn't know is refused with a
ConfigurationError that names it. It never writes a file.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import yaml

from ci_workflows.errors import ConfigurationError

ZEROS = "0" * 40
DEFAULT_BRANCH = "main"
SHARED_PREFIX = "sh4i-yurei/ci-workflows/.github/workflows/"
LOCAL_PREFIX = "./.github/workflows/"
PLANNED_JOBS = {"python.yml": "tests", "shell.yml": "checks"}
COMMIT = re.compile(r"[0-9a-f]{40}")
EXPRESSION = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)
SUDO = re.compile(r"\bsudo\b")

# PyYAML reads a workflow's on: key as the boolean True.
WORKFLOW_KEYS = {"name", True, "permissions", "concurrency", "jobs"}
JOB_KEYS = {"runs-on", "steps"}
STEP_KEYS = {"name", "uses", "with", "run", "shell", "env", "if"}

# How GitHub runs a run: step with no shell: and with shell: bash.
SHELLS = {
    None: ("bash", "-e", "{0}"),
    "bash": ("bash", "--noprofile", "--norc", "-eo", "pipefail", "{0}"),
}

SHARED_CHECKOUT = {
    "repository": "${{ job.workflow_repository }}",
    "ref": "${{ job.workflow_sha }}",
}

# The two runner-package steps, each name and whole run: text copied from the
# shared workflows at abf0315.
PYTHON_PACKAGES = (
    "Install system packages",
    "packages=$(jq -r '.[]' <<< \"$SYSTEM_PACKAGES\")\n"
    'mapfile -t names <<< "$packages"\n'
    "sudo apt-get update -qq\n"
    'sudo apt-get install -y -qq "${names[@]}"\n',
)
SHELL_PACKAGES = (
    "Install Bats and kcov",
    "sudo apt-get update -qq && sudo apt-get install -y -qq bats kcov",
)
SYSTEM_PACKAGES_IF = "inputs.system_packages != '[]'"


def plan(caller: Path, shared_repo: Path) -> dict:
    """Return the rehearsal plan for the repository at caller."""
    branch = _git(
        caller, "rev-parse", "--abbrev-ref", "HEAD", check=True
    ).stdout.strip()
    if branch == "HEAD":
        raise ConfigurationError(f"not on a branch: {caller}")
    event = {"BASE_SHA": ZEROS, "DEFAULT_BRANCH": DEFAULT_BRANCH, "PUSHED_REF": branch}
    jobs = []
    for path in sorted((caller / ".github" / "workflows").iterdir()):
        if not path.name.endswith((".yml", ".yaml")):
            continue
        workflow = path.relative_to(caller).as_posix()
        document = _workflow(workflow, path.read_text())
        if _reusable(document.get(True)):
            continue
        for name, job in document["jobs"].items():
            job = _job(workflow, name, job)
            jobs.append(_caller_job(caller, shared_repo, event, workflow, name, job))
    return {"branch": branch, "event": event, "jobs": jobs}


def _git(folder: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(folder), *args], capture_output=True, text=True, check=check
    )


def _workflow(label: str, text: str) -> dict:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        document = None
    if not isinstance(document, dict) or not isinstance(document.get("jobs"), dict):
        raise ConfigurationError(f"{label}: not a workflow")
    return document


def _job(label: str, name: str, job: object) -> dict:
    if not isinstance(job, dict):
        raise ConfigurationError(f"{label} job {name}: not a job")
    return job


def _reusable(trigger: object) -> bool:
    """A file whose only trigger is workflow_call runs only when another calls it."""
    if isinstance(trigger, (dict, list)):
        return list(trigger) == ["workflow_call"]
    return trigger == "workflow_call"


def _not_rehearsed(entry: dict, reason: str) -> dict:
    return entry | {"rehearsed": False, "reason": reason}


def _caller_job(
    caller: Path, shared_repo: Path, event: dict, workflow: str, name: str, job: dict
) -> dict:
    entry = {"workflow": workflow, "job": name}
    if "uses" not in job:
        if "steps" in job:
            return _not_rehearsed(entry, "no shared workflow")
        return _not_rehearsed(entry, "neither uses nor steps")
    label = f"{workflow} job {name}"
    uses = job["uses"]
    if uses.startswith(SHARED_PREFIX):
        # The pin is checked only for a workflow that is rehearsed.
        file, _, ref = uses.removeprefix(SHARED_PREFIX).partition("@")
    elif uses.startswith(LOCAL_PREFIX):
        file, ref = uses.removeprefix(LOCAL_PREFIX), None
    else:
        return _not_rehearsed(entry, "not a ci-workflows workflow")
    if file == "powershell.yml":
        return _not_rehearsed(entry, "powershell.yml runs on Windows")
    if file not in PLANNED_JOBS:
        return _not_rehearsed(entry, f"{file} isn't rehearsed")
    if ref is None:
        text = _local_text(label, caller, file)
    else:
        text = _shared_text(label, shared_repo, ref, file)
    shared = _workflow(file, text)
    job_name, shared_job = _planned_job(file, shared)
    inputs = _inputs(label, job.get("with") or {}, shared)
    table = _expressions(inputs, event)
    steps = [
        _step(
            f"{file} job {job_name}: step '{step.get('name')}'",
            step,
            table,
            inputs,
            label,
        )
        for step in shared_job["steps"]
    ]
    return entry | {
        "rehearsed": True,
        "shared": file,
        "pin": ref,
        "inputs": inputs,
        "steps": steps,
    }


def _local_text(label: str, caller: Path, file: str) -> str:
    path = caller / ".github" / "workflows" / file
    if not path.is_file():
        raise ConfigurationError(f"{label}: .github/workflows/{file} not found")
    return path.read_text()


def _shared_text(label: str, shared_repo: Path, ref: str, file: str) -> str:
    if not ref:
        raise ConfigurationError(f"{label}: pin is missing")
    if not COMMIT.fullmatch(ref):
        raise ConfigurationError(f"{label}: pin {ref} is not a commit")
    shown = _git(shared_repo, "show", f"{ref}:.github/workflows/{file}")
    if shown.returncode != 0:
        raise ConfigurationError(
            f"{label}: {ref} is not in {shared_repo}; fetch ci-workflows"
        )
    return shown.stdout


def _planned_job(file: str, shared: dict) -> tuple[str, dict]:
    for key in shared:
        if key not in WORKFLOW_KEYS:
            raise ConfigurationError(f"{file}: unknown key {key}")
    name = PLANNED_JOBS[file]
    if name not in shared["jobs"]:
        raise ConfigurationError(f"{file}: missing job {name}")
    job = _job(file, name, shared["jobs"][name])
    for key in job:
        if key not in JOB_KEYS:
            raise ConfigurationError(f"{file} job {name}: unknown key {key}")
    steps = job.get("steps")
    if not isinstance(steps, list) or not all(isinstance(step, dict) for step in steps):
        raise ConfigurationError(f"{file} job {name}: not a job")
    return name, job


def _inputs(label: str, given: dict, shared: dict) -> dict:
    """Each input as the text GitHub writes into a step."""
    declared = shared[True]["workflow_call"]["inputs"]
    for key, value in given.items():
        if key not in declared:
            raise ConfigurationError(f"{label}: unknown input {key}")
        if "${{" in str(value):
            raise ConfigurationError(f"{label}: input {key} holds an expression")
    inputs = {}
    for key, spec in declared.items():
        if key in given:
            inputs[key] = str(given[key])
        elif spec.get("required"):
            raise ConfigurationError(f"{label}: missing input {key}")
        else:
            inputs[key] = str(spec["default"])
    return inputs


def _expressions(inputs: dict, event: dict) -> dict:
    """The fixed table of expressions a planned step may hold, by exact text."""
    table = {f"${{{{ inputs.{key} }}}}": value for key, value in inputs.items()}
    table["${{ github.event.pull_request.base.sha || github.event.before }}"] = event[
        "BASE_SHA"
    ]
    table["${{ github.event.repository.default_branch }}"] = event["DEFAULT_BRANCH"]
    table["${{ github.event_name == 'push' && github.ref_name || '' }}"] = event[
        "PUSHED_REF"
    ]
    return table


def _expand(where: str, text: str, table: dict) -> str:
    def replace(match: re.Match) -> str:
        expression = match.group(0)
        if expression not in table:
            raise ConfigurationError(f"{where} unknown expression {expression}")
        return table[expression]

    return EXPRESSION.sub(replace, text)


def _expand_all(where: str, values: dict, table: dict) -> dict:
    return {key: _expand(where, str(value), table) for key, value in values.items()}


def _step(where: str, step: dict, table: dict, inputs: dict, label: str) -> dict:
    for key in step:
        if key not in STEP_KEYS:
            raise ConfigurationError(f"{where} unknown key {key}")
    name, uses, run = step.get("name"), step.get("uses"), step.get("run")
    if uses is None and run is None:
        raise ConfigurationError(f"{where} neither uses nor run")
    if "if" in step and not (
        (name, run) == PYTHON_PACKAGES and step["if"] == SYSTEM_PACKAGES_IF
    ):
        raise ConfigurationError(f"{where} unknown expression {step['if']}")
    if uses is not None:
        # An action step's env: isn't carried, but its expressions must be known.
        _expand_all(where, step.get("env") or {}, table)
        return {"name": name} | _action(where, step, table)
    if SUDO.search(run):
        return {"name": name} | _packages(where, step, inputs, label)
    shell = step.get("shell")
    if shell not in SHELLS:
        raise ConfigurationError(f"{where} unknown shell {shell}")
    return {
        "name": name,
        "kind": "run",
        "shell": list(SHELLS[shell]),
        "run": _expand(where, run, table),
        "env": _expand_all(where, step.get("env") or {}, table),
    }


def _action(where: str, step: dict, table: dict) -> dict:
    action = step["uses"].partition("@")[0]
    given = step.get("with") or {}
    if action == "actions/checkout":
        if "repository" not in given:
            _expand_all(where, given, table)
            return {"kind": "checkout-caller"}
        if set(given) == {*SHARED_CHECKOUT, "path"} and all(
            given[key] == value for key, value in SHARED_CHECKOUT.items()
        ):
            return {
                "kind": "checkout-shared",
                "path": _expand(where, str(given["path"]), table),
            }
        raise ConfigurationError(f"{where} unknown checkout")
    if action == "actions/setup-python":
        values = _expand_all(where, given, table)
        if "python-version" not in values:
            raise ConfigurationError(f"{where} unknown setup")
        return {"kind": "setup-python", "version": values["python-version"]}
    raise ConfigurationError(f"{where} unknown action {action}")


def _packages(where: str, step: dict, inputs: dict, label: str) -> dict:
    """A runner-package step is planned, never run; anything else with sudo is refused."""
    found = (step.get("name"), step["run"])
    if found == SHELL_PACKAGES:
        return {"kind": "packages", "packages": ["bats", "kcov"]}
    if found != PYTHON_PACKAGES:
        raise ConfigurationError(f"{where} needs root")
    text = inputs["system_packages"]
    try:
        packages = json.loads(text)
    except json.JSONDecodeError:
        packages = None
    if not isinstance(packages, list) or not all(
        isinstance(item, str) for item in packages
    ):
        raise ConfigurationError(f"{label}: input system_packages is not a JSON list")
    if step.get("if") == SYSTEM_PACKAGES_IF and text == "[]":
        return {"kind": "skip", "reason": "no runner packages"}
    return {"kind": "packages", "packages": packages}
