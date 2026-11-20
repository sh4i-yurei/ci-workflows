"""Run a CI rehearsal plan in a scratch folder."""

import contextlib
import io
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from ci_workflows.errors import ConfigurationError
from ci_workflows.rehearse import run

ZEROS = "0" * 40
SHORT = ["bash", "-e", "{0}"]
LONG = ["bash", "--noprofile", "--norc", "-eo", "pipefail", "{0}"]
CI = ".github/workflows/ci.yml"
HEADING = f"== {CI} python: "
CHECKOUT_CALLER = {"name": "Check out caller", "kind": "checkout-caller"}
SKIP = {
    "name": "Install system packages",
    "kind": "skip",
    "reason": "no runner packages",
}
MISSING_PIN = "0123456789abcdef0123456789abcdef01234567"


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


def commit(folder, message="commit"):
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", message)
    return git(folder, "rev-parse", "HEAD").strip()


def make_caller(tmp_path, origin_main=True):
    folder = new_repo(tmp_path / "caller", branch="agent/sample")
    (folder / "tracked.txt").write_text("committed\n")
    commit(folder, "first")
    if origin_main:
        git(folder, "update-ref", "refs/remotes/origin/main", "HEAD")
    return folder


@pytest.fixture
def repos(tmp_path):
    caller = make_caller(tmp_path)
    shared = new_repo(tmp_path / "shared")
    (shared / "marker.txt").write_text("shared marker\n")
    return caller, shared, commit(shared, "marker")


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    folder = tmp_path / "tmp"
    folder.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    return folder


def folders(scratch):
    return sorted(scratch.glob("ci-rehearse-*"))


def kept(scratch, out):
    last = out.splitlines()[-1]
    assert last.startswith("kept ")
    path = Path(last.removeprefix("kept "))
    assert folders(scratch) == [path]
    return path


def summary(out):
    lines = out.splitlines()
    return lines[lines.index("== summary") + 1 :]


def job(*steps, pin=None, name="python"):
    return {
        "workflow": CI,
        "job": name,
        "rehearsed": True,
        "shared": "python.yml",
        "pin": pin,
        "inputs": {},
        "steps": list(steps),
    }


def not_rehearsed(workflow, name, reason):
    return {"workflow": workflow, "job": name, "rehearsed": False, "reason": reason}


def plan(*jobs):
    return {
        "branch": "agent/sample",
        "event": {
            "BASE_SHA": ZEROS,
            "DEFAULT_BRANCH": "main",
            "PUSHED_REF": "agent/sample",
        },
        "jobs": list(jobs),
    }


def run_step(name, text, env=None, shell=SHORT):
    return {"name": name, "kind": "run", "shell": shell, "run": text, "env": env or {}}


def checkout_shared(path=".ci-workflows"):
    return {
        "name": "Check out pinned shared checks",
        "kind": "checkout-shared",
        "path": path,
    }


def setup_python(version="3.12"):
    return {"name": "Set up Python", "kind": "setup-python", "version": version}


def packages(names):
    return {"name": "Install system packages", "kind": "packages", "packages": names}


def stand_in_uv(tmp_path, monkeypatch, code=0):
    """Put a uv first on PATH that records its arguments and makes the venv folder."""
    folder = tmp_path / "uv-bin"
    folder.mkdir()
    record = tmp_path / "uv-args.txt"
    uv = folder / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" >> '{record}'\n"
        'mkdir -p "$5/bin"\n'
        f"exit {code}\n"
    )
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")
    return record


def test_skip_step_prints_its_reason(repos, scratch, capsys):
    caller, shared, _ = repos
    assert run(plan(job(SKIP)), caller, shared) == 0
    assert capsys.readouterr().out == (
        f"{HEADING}Install system packages\n"
        "skipped: no runner packages\n"
        "== summary\n"
        f"{CI} python:\n"
        "  Install system packages: skipped: no runner packages\n"
        "no runner packages missing\n"
    )
    assert folders(scratch) == []


def test_heading_comes_before_the_step_output(repos, scratch, tmp_path):
    """A step's heading reaches standard output before the step's own output.

    Standard output here is a buffered text layer over descriptor 1, as when
    run's output goes to a pipe, so a heading left in the buffer would land
    after the output the step's command writes straight to descriptor 1.
    """
    caller, shared, _ = repos
    output = tmp_path / "stdout.txt"
    saved = os.dup(1)
    with output.open("wb") as file:
        os.dup2(file.fileno(), 1)
    try:
        buffered = io.TextIOWrapper(
            io.FileIO(1, "w", closefd=False), write_through=False
        )
        with contextlib.redirect_stdout(buffered):
            code = run(plan(job(run_step("Echo", "echo from-step\n"))), caller, shared)
        buffered.flush()
    finally:
        os.dup2(saved, 1)
        os.close(saved)
    assert code == 0
    assert output.read_text().splitlines()[:2] == [f"{HEADING}Echo", "from-step"]


def test_snapshot_keeps_file_modes_and_links(repos, scratch, capsys):
    caller, shared, _ = repos
    script = caller / "run.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    commit(caller, "script")
    assert git(caller, "ls-files", "-s", "run.sh").startswith("100755 ")
    (caller / "link.txt").symlink_to("tracked.txt")
    (caller / "files" / "deeper").mkdir(parents=True)
    (caller / "files" / "deeper" / "nested.txt").write_text("nested\n")
    (caller / "links" / "deeper").mkdir(parents=True)
    (caller / "links" / "deeper" / "link.txt").symlink_to("../../tracked.txt")
    assert run(plan(job(SKIP)), caller, shared, keep=True) == 0
    repo = kept(scratch, capsys.readouterr().out) / "job-1" / "repo"
    assert os.access(repo / "run.sh", os.X_OK)
    assert (repo / "link.txt").is_symlink()
    assert os.readlink(repo / "link.txt") == "tracked.txt"
    assert git(repo, "ls-files", "-s", "link.txt").startswith("120000 ")
    assert (repo / "files" / "deeper" / "nested.txt").read_text() == "nested\n"
    assert os.readlink(repo / "links" / "deeper" / "link.txt") == "../../tracked.txt"


def test_checkout_shared_cannot_make_its_folder(repos, scratch, capsys):
    caller, shared, pin = repos
    steps = job(checkout_shared("tracked.txt"), pin=pin)
    assert run(plan(steps), caller, shared) == 2
    assert summary(capsys.readouterr().out) == [
        f"{CI} python:",
        "  Check out pinned shared checks: failed: can't make tracked.txt",
        "no runner packages missing",
    ]


def test_checkout_shared_fails_when_tar_cannot_write(repos, scratch, capsys):
    caller, shared, pin = repos
    lock = run_step("Lock", "mkdir -m 500 .ci-workflows\n")
    assert run(plan(job(lock, checkout_shared(), pin=pin)), caller, shared) == 2
    assert summary(capsys.readouterr().out) == [
        f"{CI} python:",
        "  Lock: ok",
        "  Check out pinned shared checks: failed (exit 2)",
        "no runner packages missing",
    ]
    assert folders(scratch) == []


def test_snapshot_holds_the_worktree_as_it_stands(repos, scratch, capsys):
    caller, shared, _ = repos
    (caller / "deleted.txt").write_text("deleted\n")
    (caller / ".gitignore").write_text("ignored.txt\n")
    commit(caller, "more")
    (caller / "tracked.txt").write_text("changed\n")
    (caller / "deleted.txt").unlink()
    (caller / "staged-new.txt").write_text("staged\n")
    git(caller, "add", "staged-new.txt")
    (caller / "untracked.txt").write_text("untracked\n")
    (caller / "ignored.txt").write_text("ignored\n")
    before = git(caller, "status", "--porcelain")
    listing = run_step(
        "List",
        'git ls-files > "$RUNNER_TEMP/files.txt"\ncat tracked.txt >> "$RUNNER_TEMP/files.txt"\n',
    )
    assert run(plan(job(CHECKOUT_CALLER, listing)), caller, shared, keep=True) == 0
    out = capsys.readouterr().out
    folder = kept(scratch, out) / "job-1"
    assert (folder / "runner-temp" / "files.txt").read_text() == (
        ".gitignore\nstaged-new.txt\ntracked.txt\nuntracked.txt\nchanged\n"
    )
    assert not (folder / "repo" / "ignored.txt").exists()
    assert not (folder / "repo" / "deleted.txt").exists()
    assert git(caller, "status", "--porcelain") == before
    short = git(folder / "repo", "rev-parse", "--short", "HEAD").strip()
    assert out.splitlines()[:2] == [
        f"{HEADING}Check out caller",
        f"checked out the worktree as {short}",
    ]
    assert summary(out)[:3] == [f"{CI} python:", "  Check out caller: ok", "  List: ok"]


def test_snapshot_takes_origin_main_from_the_caller(repos, scratch, capsys):
    caller, shared, _ = repos
    origin_main = git(caller, "rev-parse", "refs/remotes/origin/main").strip()
    git(caller, "checkout", "-q", "-b", "main")
    (caller / "main.txt").write_text("local main\n")
    commit(caller, "local main")
    git(caller, "checkout", "-q", "agent/sample")
    show = run_step(
        "Show", 'git rev-parse refs/remotes/origin/main > "$RUNNER_TEMP/main.txt"\n'
    )
    assert run(plan(job(show)), caller, shared, keep=True) == 0
    folder = kept(scratch, capsys.readouterr().out) / "job-1"
    assert (folder / "runner-temp" / "main.txt").read_text() == f"{origin_main}\n"


def test_snapshot_commit_is_named_rehearsal(repos, scratch, capsys):
    caller, shared, _ = repos
    assert run(plan(job(run_step("Step", "true\n"))), caller, shared, keep=True) == 0
    repo = kept(scratch, capsys.readouterr().out) / "job-1" / "repo"
    assert git(repo, "log", "-1", "--format=%s") == "rehearsal\n"


def test_run_step_gets_only_its_own_and_runner_variables(
    repos, scratch, capsys, monkeypatch
):
    caller, shared, _ = repos
    monkeypatch.setenv("GH_TOKEN", "token-value")
    monkeypatch.setenv("SECRET_TEST", "secret-value")
    step = run_step("Env", 'env | sort > "$RUNNER_TEMP/env.txt"\n', env={"FLOOR": "69"})
    assert run(plan(job(step)), caller, shared, keep=True) == 0
    folder = kept(scratch, capsys.readouterr().out) / "job-1"
    lines = (folder / "runner-temp" / "env.txt").read_text().splitlines()
    values = dict(line.split("=", 1) for line in lines)
    assert set(values) == {
        "CI",
        "FLOOR",
        "GITHUB_ACTIONS",
        "GITHUB_ENV",
        "GITHUB_OUTPUT",
        "GITHUB_PATH",
        "GITHUB_STEP_SUMMARY",
        "GITHUB_WORKSPACE",
        "HOME",
        "PATH",
        "RUNNER_TEMP",
        "PWD",
        "SHLVL",
        "_",
    }
    expected = {
        "CI": "true",
        "FLOOR": "69",
        "GITHUB_ACTIONS": "true",
        "GITHUB_ENV": str(folder / "github_env"),
        "GITHUB_OUTPUT": str(folder / "github_output"),
        "GITHUB_PATH": str(folder / "github_path"),
        "GITHUB_STEP_SUMMARY": str(folder / "github_step_summary"),
        "GITHUB_WORKSPACE": str(folder / "repo"),
        "HOME": str(folder / "home"),
        "PATH": f"{folder / 'venv' / 'bin'}:/usr/bin:/bin",
        "RUNNER_TEMP": str(folder / "runner-temp"),
        "PWD": str(folder / "repo"),
    }
    assert {name: values[name] for name in expected} == expected


def test_github_path_reaches_later_steps(repos, scratch, capsys, tmp_path):
    caller, shared, _ = repos
    tools = tmp_path / "tools"
    tools.mkdir()
    tool = tools / "tool"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    steps = [
        run_step("Add tools", f'echo "{tools}" >> "$GITHUB_PATH"\n'),
        run_step("Use tool", "tool\n"),
        run_step("Show path", 'echo "$PATH" > "$RUNNER_TEMP/path.txt"\n'),
    ]
    assert run(plan(job(*steps)), caller, shared, keep=True) == 0
    out = capsys.readouterr().out
    folder = kept(scratch, out) / "job-1"
    assert summary(out)[1:4] == ["  Add tools: ok", "  Use tool: ok", "  Show path: ok"]
    assert (folder / "runner-temp" / "path.txt").read_text() == (
        f"{tools}:{folder / 'venv' / 'bin'}:/usr/bin:/bin\n"
    )


def test_later_github_path_folders_come_first(repos, scratch, capsys, tmp_path):
    caller, shared, _ = repos
    steps = [
        run_step("First", f'echo "{tmp_path / "a"}" >> "$GITHUB_PATH"\n'),
        run_step(
            "Second",
            f'echo "{tmp_path / "b"}" >> "$GITHUB_PATH"\n'
            f'echo "{tmp_path / "c"}" >> "$GITHUB_PATH"\n',
        ),
        run_step("Show path", 'echo "$PATH" > "$RUNNER_TEMP/path.txt"\n'),
    ]
    assert run(plan(job(*steps)), caller, shared, keep=True) == 0
    folder = kept(scratch, capsys.readouterr().out) / "job-1"
    assert (folder / "runner-temp" / "path.txt").read_text() == (
        f"{tmp_path / 'c'}:{tmp_path / 'b'}:{tmp_path / 'a'}:"
        f"{folder / 'venv' / 'bin'}:/usr/bin:/bin\n"
    )


def test_every_step_runs_after_a_failure(repos, scratch, capsys):
    caller, shared, _ = repos
    steps = [
        run_step("first", "exit 3\n"),
        run_step("second", 'touch "$RUNNER_TEMP/after.txt"\n'),
    ]
    assert run(plan(job(*steps)), caller, shared, keep=True) == 1
    out = capsys.readouterr().out
    folder = kept(scratch, out) / "job-1"
    assert (folder / "runner-temp" / "after.txt").exists()
    assert summary(out)[1:3] == ["  first: failed (exit 3)", "  second: ok"]


def test_a_step_writing_github_env_fails(repos, scratch, capsys):
    caller, shared, _ = repos
    steps = [
        run_step("Write env", 'echo A=b >> "$GITHUB_ENV"\n'),
        run_step("After", "true\n"),
    ]
    assert run(plan(job(*steps)), caller, shared) == 2
    assert summary(capsys.readouterr().out)[1:3] == [
        "  Write env: failed: step wrote GITHUB_ENV, which the rehearsal doesn't carry",
        "  After: ok",
    ]


@pytest.mark.parametrize(
    ("shell", "text", "result", "reached"),
    [
        (SHORT, "false\necho reached > $RUNNER_TEMP/r.txt\n", "failed (exit 1)", False),
        (
            LONG,
            "false | true\necho reached > $RUNNER_TEMP/r.txt\n",
            "failed (exit 1)",
            False,
        ),
        (SHORT, "false | true\necho reached > $RUNNER_TEMP/r.txt\n", "ok", True),
    ],
    ids=["short stops on false", "long stops on a failed pipe", "short runs on"],
)
def test_run_step_uses_its_planned_shell(
    repos, scratch, capsys, shell, text, result, reached
):
    caller, shared, _ = repos
    step = run_step("Shell", text, shell=shell)
    assert run(plan(job(step)), caller, shared, keep=True) == (0 if reached else 1)
    out = capsys.readouterr().out
    folder = kept(scratch, out) / "job-1"
    assert summary(out)[1] == f"  Shell: {result}"
    assert (folder / "runner-temp" / "r.txt").exists() is reached


def test_run_steps_are_numbered_from_one(repos, scratch, capsys):
    caller, shared, _ = repos
    steps = job(run_step("One", "echo one\n"), run_step("Two", "echo two\n"))
    assert run(plan(steps), caller, shared, keep=True) == 0
    folder = kept(scratch, capsys.readouterr().out) / "job-1"
    assert (folder / "step-1.sh").read_text() == "echo one\n"
    assert (folder / "step-2.sh").read_text() == "echo two\n"


@pytest.mark.parametrize("path", [".ci-workflows", "nested/shared"])
def test_checkout_shared_at_the_pin(repos, scratch, capsys, path):
    caller, shared, pin = repos
    assert (
        run(plan(job(checkout_shared(path), pin=pin)), caller, shared, keep=True) == 0
    )
    out = capsys.readouterr().out
    checkout = kept(scratch, out) / "job-1" / "repo" / path
    assert (checkout / "marker.txt").read_text() == "shared marker\n"
    assert not (checkout / "tracked.txt").exists()
    assert summary(out)[1] == "  Check out pinned shared checks: ok"


def test_checkout_shared_without_a_pin_takes_the_snapshot(repos, scratch, capsys):
    caller, shared, _ = repos
    (caller / "untracked.txt").write_text("untracked\n")
    assert run(plan(job(checkout_shared())), caller, shared, keep=True) == 0
    checkout = (
        kept(scratch, capsys.readouterr().out) / "job-1" / "repo" / ".ci-workflows"
    )
    assert (checkout / "tracked.txt").read_text() == "committed\n"
    assert (checkout / "untracked.txt").read_text() == "untracked\n"
    assert not (checkout / "marker.txt").exists()


def test_checkout_shared_at_a_missing_pin_fails(repos, scratch, capsys):
    caller, shared, _ = repos
    steps = job(checkout_shared(), run_step("After", "true\n"), pin=MISSING_PIN)
    assert run(plan(steps), caller, shared) == 2
    assert summary(capsys.readouterr().out)[1:3] == [
        "  Check out pinned shared checks: failed (exit 128)",
        "  After: ok",
    ]


def test_setup_python_runs_uv(repos, scratch, capsys, tmp_path, monkeypatch):
    caller, shared, _ = repos
    record = stand_in_uv(tmp_path, monkeypatch)
    assert run(plan(job(setup_python("3.12"))), caller, shared, keep=True) == 0
    out = capsys.readouterr().out
    folder = kept(scratch, out) / "job-1"
    assert record.read_text().splitlines() == [
        "venv",
        "--seed",
        "--python",
        "3.12",
        str(folder / "venv"),
    ]
    assert summary(out)[1] == "  Set up Python: ok"


def test_setup_python_fails_when_uv_fails(
    repos, scratch, capsys, tmp_path, monkeypatch
):
    caller, shared, _ = repos
    stand_in_uv(tmp_path, monkeypatch, code=4)
    assert run(plan(job(setup_python())), caller, shared) == 2
    assert summary(capsys.readouterr().out)[1] == "  Set up Python: failed (exit 4)"


def test_setup_python_without_uv(repos, scratch, capsys, tmp_path, monkeypatch):
    caller, shared, _ = repos
    only = tmp_path / "only"
    only.mkdir()
    for name in ("git", "tar"):
        (only / name).symlink_to(shutil.which(name))
    monkeypatch.setenv("PATH", str(only))
    assert run(plan(job(setup_python())), caller, shared) == 2
    assert (
        summary(capsys.readouterr().out)[1] == "  Set up Python: failed: uv not found"
    )


@pytest.fixture
def no_installs(tmp_path, monkeypatch):
    """Put a sudo and an apt-get first on PATH that record any call."""
    folder = tmp_path / "installers"
    folder.mkdir()
    record = tmp_path / "installed.txt"
    for name in ("sudo", "apt-get"):
        tool = folder / name
        tool.write_text(f"#!/bin/sh\necho {name} \"$@\" >> '{record}'\n")
        tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")
    return record


def test_packages_are_checked_and_never_installed(repos, scratch, capsys, no_installs):
    caller, shared, _ = repos
    steps = job(
        packages(["sh", "no-such-package-zz", "bash"]), packages(["no-such-package-zz"])
    )
    assert run(plan(steps), caller, shared) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[:4] == [
        f"{HEADING}Install system packages",
        "runner packages: present sh, bash and missing no-such-package-zz",
        f"{HEADING}Install system packages",
        "runner packages: present none and missing no-such-package-zz",
    ]
    assert out.splitlines()[-1] == "missing runner packages: no-such-package-zz"
    assert not no_installs.exists()


def test_missing_packages_are_named_once(repos, scratch, capsys, no_installs):
    caller, shared, _ = repos
    first = job(packages(["no-such-package-zz"]))
    second = job(packages(["other-missing-zz", "no-such-package-zz"]), name="other")
    assert run(plan(first, second), caller, shared) == 0
    assert capsys.readouterr().out.splitlines()[-1] == (
        "missing runner packages: no-such-package-zz, other-missing-zz"
    )


def test_no_packages_missing(repos, scratch, capsys, no_installs):
    caller, shared, _ = repos
    assert run(plan(job(packages(["sh", "bash"]))), caller, shared) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[1] == "runner packages: present sh, bash and missing none"
    assert summary(out) == [
        f"{CI} python:",
        "  Install system packages: runner packages: present sh, bash and missing none",
        "no runner packages missing",
    ]


def test_summary_lists_every_job_in_plan_order(repos, scratch, capsys):
    caller, shared, _ = repos
    jobs = [
        not_rehearsed(".github/workflows/lint.yml", "actionlint", "no shared workflow"),
        job(SKIP),
        not_rehearsed(
            ".github/workflows/windows.yml", "pester", "powershell.yml runs on Windows"
        ),
    ]
    assert run(plan(*jobs), caller, shared, keep=True) == 0
    out = capsys.readouterr().out
    folder = kept(scratch, out)
    assert summary(out) == [
        ".github/workflows/lint.yml actionlint: not rehearsed: no shared workflow",
        f"{CI} python:",
        "  Install system packages: skipped: no runner packages",
        ".github/workflows/windows.yml pester: not rehearsed: powershell.yml runs on Windows",
        "no runner packages missing",
        f"kept {folder}",
    ]
    assert [path.name for path in folder.iterdir()] == ["job-1"]


def test_each_job_has_its_own_folder(repos, scratch, capsys, tmp_path, monkeypatch):
    caller, shared, _ = repos
    stand_in_uv(tmp_path, monkeypatch)
    first = job(setup_python(), run_step("Write", "echo first > first.txt\n"))
    seen = 'echo "$HOME" > "$RUNNER_TEMP/seen.txt"\necho "$PATH" >> "$RUNNER_TEMP/seen.txt"\n'
    second = job(setup_python(), run_step("Look", seen), name="other")
    assert run(plan(first, second), caller, shared, keep=True) == 0
    scratch_folder = kept(scratch, capsys.readouterr().out)
    one, two = scratch_folder / "job-1", scratch_folder / "job-2"
    for folder in (one, two):
        for name in ("repo", "home", "venv"):
            assert (folder / name).is_dir()
    assert (one / "repo" / "first.txt").read_text() == "first\n"
    assert not (two / "repo" / "first.txt").exists()
    assert (two / "runner-temp" / "seen.txt").read_text() == (
        f"{two / 'home'}\n{two / 'venv' / 'bin'}:/usr/bin:/bin\n"
    )


def test_scratch_folder_goes_when_a_step_failed(repos, scratch, capsys):
    caller, shared, _ = repos
    assert run(plan(job(run_step("Fail", "exit 1\n"))), caller, shared) == 1
    assert folders(scratch) == []


def test_scratch_folder_is_kept_when_asked(repos, scratch, capsys):
    caller, shared, _ = repos
    assert run(plan(job(SKIP)), caller, shared, keep=True) == 0
    assert kept(scratch, capsys.readouterr().out).is_dir()


def test_scratch_folder_goes_when_no_step_ran_even_if_kept(repos, scratch, capsys):
    caller, shared, _ = repos
    jobs = [
        not_rehearsed(".github/workflows/lint.yml", "actionlint", "no shared workflow")
    ]
    assert run(plan(*jobs), caller, shared, keep=True) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "no runner packages missing"
    assert folders(scratch) == []


@pytest.mark.parametrize("keep", [False, True], ids=["not kept", "kept"])
def test_caller_without_origin_main_is_refused(tmp_path, scratch, capsys, keep):
    caller = make_caller(tmp_path, origin_main=False)
    shared = new_repo(tmp_path / "shared")
    with pytest.raises(ConfigurationError) as raised:
        run(plan(job(SKIP)), caller, shared, keep=keep)
    assert str(raised.value) == f"{caller} has no origin/main; fetch it first"
    assert capsys.readouterr().out == ""
    assert folders(scratch) == []
