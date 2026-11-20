"""Run a CI rehearsal plan in a scratch folder.

Each rehearsed job gets its own folder in one scratch folder, holding a snapshot
of the caller's worktree as a commit, and runs its planned steps there in plan
order, every step whatever happened to the steps before it. A run step gets a
clean environment holding only its own variables and the runner variables the
shared steps use. Runner packages are checked, never installed. The caller is
only read. The scratch folder is removed when the run ends unless it is kept.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from ci_workflows.errors import ConfigurationError

# How much a step's result counts toward run's exit code, by ARCHITECTURE.md's
# exit codes: a failed run step is a check that ran and failed, and anything
# else that fails is a configuration or infrastructure error.
OK = 0
CHECK_FAILED = 1
CONFIGURATION_ERROR = 2

# The files a runner gives each step, by the variable that names each one.
STEP_FILES = {
    "GITHUB_PATH": "github_path",
    "GITHUB_ENV": "github_env",
    "GITHUB_OUTPUT": "github_output",
    "GITHUB_STEP_SUMMARY": "github_step_summary",
}
SYSTEM_PATH = ("/usr/bin", "/bin")
IDENTITY = ("-c", "user.name=Rehearsal", "-c", "user.email=rehearsal@example.invalid")
# Forced, because a clone of a caller with a local main already holds
# refs/remotes/origin/main, and the caller's own origin/main need not be a
# fast-forward of it.
ORIGIN_MAIN = "+refs/remotes/origin/main:refs/remotes/origin/main"
GITHUB_ENV_WRITTEN = "failed: step wrote GITHUB_ENV, which the rehearsal doesn't carry"


def run(plan: dict, caller: Path, shared_repo: Path, *, keep: bool = False) -> int:
    """Run plan's rehearsed jobs, print each step and a summary, and return the exit code."""
    scratch = Path(tempfile.mkdtemp(prefix="ci-rehearse-"))
    rehearsal = _Rehearsal(Path(caller), Path(shared_repo), scratch)
    try:
        rehearsed = [entry for entry in plan["jobs"] if entry["rehearsed"]]
        results = [
            rehearsal.job(entry, number) for number, entry in enumerate(rehearsed, 1)
        ]
        _summary(plan["jobs"], results, rehearsal.missing)
    finally:
        if keep and rehearsal.ran:
            _say(f"kept {scratch}")
        else:
            shutil.rmtree(scratch)
    return rehearsal.code


def _say(line: str) -> None:
    # Flushed, so each line comes before the output of the next command run.
    print(line, flush=True)


def _failed(returncode: int) -> str:
    return f"failed (exit {returncode})"


def _outcome(returncode: int, code: int) -> tuple[str, int]:
    if returncode != 0:
        return _failed(returncode), code
    return "ok", OK


def _names(names: list[str]) -> str:
    return ", ".join(names) or "none"


def _git(folder: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(folder), *args], check=True, capture_output=True
    )


def _listed(folder: Path, *args: str) -> list[str]:
    """The paths git ls-files prints with -z, each ended by a NUL."""
    out = _git(folder, "ls-files", *args, "-z").stdout
    return [os.fsdecode(name) for name in out.split(b"\0")[:-1]]


class _Rehearsal:
    """What the whole run shares: the caller, the pins and what the steps found."""

    def __init__(self, caller: Path, shared_repo: Path, scratch: Path):
        self.caller = caller
        self.shared_repo = shared_repo
        self.scratch = scratch
        self.ran = False
        self.code = OK
        self.missing: list[str] = []

    def job(self, entry: dict, number: int) -> list[tuple[str, str]]:
        """Run one rehearsed job in job-<number> and return each step's result."""
        folder = self.scratch / f"job-{number}"
        folder.mkdir()
        (folder / "home").mkdir()
        (folder / "runner-temp").mkdir()
        for name in STEP_FILES.values():
            (folder / name).touch()
        short = self._snapshot(folder / "repo")
        job = _Job(self, folder, entry["pin"], short)
        heading = f"== {entry['workflow']} {entry['job']}: "
        results = []
        for position, step in enumerate(entry["steps"], 1):
            self.ran = True
            _say(f"{heading}{step['name']}")
            result, code = STEPS[step["kind"]](job, step, position)
            self.code = max(self.code, code)
            results.append((step["name"], result))
        return results

    def _snapshot(self, repo: Path) -> str:
        """Commit the caller's worktree as it stands in a clone at repo."""
        _git(repo.parent, "clone", str(self.caller), repo.name)
        for name in _listed(repo):
            (repo / name).unlink()
        for name in _listed(self.caller, "-co", "--exclude-standard"):
            source, target = self.caller / name, repo / name
            if source.is_symlink():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(source.readlink())
            elif source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(source, target)
        _git(repo, "add", "-A")
        _git(repo, *IDENTITY, "commit", "--allow-empty", "-m", "rehearsal")
        try:
            _git(repo, "fetch", str(self.caller), ORIGIN_MAIN)
        except subprocess.CalledProcessError:
            raise ConfigurationError(
                f"{self.caller} has no origin/main; fetch it first"
            ) from None
        return _git(repo, "rev-parse", "--short", "HEAD").stdout.decode().strip()


class _Job:
    """One rehearsed job's folder and the PATH its run steps build up."""

    def __init__(
        self, rehearsal: _Rehearsal, folder: Path, pin: str | None, short: str
    ):
        self.rehearsal = rehearsal
        self.folder = folder
        self.repo = folder / "repo"
        self.venv = folder / "venv"
        self.pin = pin
        self.short = short
        # The folders the job's steps added to GITHUB_PATH, the latest first.
        self.path: list[str] = []

    def checkout_caller(self, step: dict, position: int) -> tuple[str, int]:
        _say(f"checked out the worktree as {self.short}")
        return "ok", OK

    def checkout_shared(self, step: dict, position: int) -> tuple[str, int]:
        path = step["path"]
        target = self.repo / path
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            return f"failed: can't make {path}", CONFIGURATION_ERROR
        # A ./ call checks out the caller's own commit, which is the snapshot.
        if self.pin is None:
            source, ref = self.repo, "HEAD"
        else:
            source, ref = self.rehearsal.shared_repo, self.pin
        archive = subprocess.run(
            ["git", "-C", str(source), "archive", ref], stdout=subprocess.PIPE
        )
        if archive.returncode != 0:
            return _failed(archive.returncode), CONFIGURATION_ERROR
        unpacked = subprocess.run(
            ["tar", "-x", "-C", str(target)], input=archive.stdout
        )
        return _outcome(unpacked.returncode, CONFIGURATION_ERROR)

    def setup_python(self, step: dict, position: int) -> tuple[str, int]:
        uv = shutil.which("uv")
        if uv is None:
            return "failed: uv not found", CONFIGURATION_ERROR
        made = subprocess.run(
            [uv, "venv", "--seed", "--python", step["version"], str(self.venv)]
        )
        return _outcome(made.returncode, CONFIGURATION_ERROR)

    def packages(self, step: dict, position: int) -> tuple[str, int]:
        present, missing = [], []
        for name in step["packages"]:
            (present if shutil.which(name) else missing).append(name)
        for name in missing:
            if name not in self.rehearsal.missing:
                self.rehearsal.missing.append(name)
        line = (
            f"runner packages: present {_names(present)} and missing {_names(missing)}"
        )
        _say(line)
        return line, OK

    def skip(self, step: dict, position: int) -> tuple[str, int]:
        line = f"skipped: {step['reason']}"
        _say(line)
        return line, OK

    def run(self, step: dict, position: int) -> tuple[str, int]:
        files = {name: self.folder / file for name, file in STEP_FILES.items()}
        for file in files.values():
            file.write_text("")
        script = self.folder / f"step-{position}.sh"
        script.write_text(step["run"])
        env = {
            "HOME": str(self.folder / "home"),
            "PATH": os.pathsep.join([*self.path, str(self.venv / "bin"), *SYSTEM_PATH]),
            "RUNNER_TEMP": str(self.folder / "runner-temp"),
            **{name: str(file) for name, file in files.items()},
            "GITHUB_WORKSPACE": str(self.repo),
            "CI": "true",
            "GITHUB_ACTIONS": "true",
            **step["env"],
        }
        command = [part.replace("{0}", str(script)) for part in step["shell"]]
        finished = subprocess.run(command, cwd=self.repo, env=env)
        self.path[:0] = reversed(files["GITHUB_PATH"].read_text().splitlines())
        if files["GITHUB_ENV"].read_text():
            return GITHUB_ENV_WRITTEN, CONFIGURATION_ERROR
        return _outcome(finished.returncode, CHECK_FAILED)


STEPS = {
    "checkout-caller": _Job.checkout_caller,
    "checkout-shared": _Job.checkout_shared,
    "setup-python": _Job.setup_python,
    "packages": _Job.packages,
    "skip": _Job.skip,
    "run": _Job.run,
}


def _summary(
    jobs: list[dict], results: list[list[tuple[str, str]]], missing: list[str]
):
    _say("== summary")
    rehearsed = iter(results)
    for entry in jobs:
        title = f"{entry['workflow']} {entry['job']}:"
        if not entry["rehearsed"]:
            _say(f"{title} not rehearsed: {entry['reason']}")
            continue
        _say(title)
        for name, result in next(rehearsed):
            _say(f"  {name}: {result}")
    if missing:
        _say(f"missing runner packages: {', '.join(missing)}")
    else:
        _say("no runner packages missing")
