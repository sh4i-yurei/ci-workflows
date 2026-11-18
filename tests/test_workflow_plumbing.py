"""Exercise the shell commands used by the reusable workflows."""

import os
import json
import re
import subprocess
import sys
import time
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
GLUE = ROOT / "scripts/workflow-glue.sh"


def run(repo, *args, base_sha="", input_text="", extra_env=None):
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(repo),
        "BASE_SHA": base_sha,
        "PYTHONPATH": str(ROOT),
        "PYTHON": sys.executable,
        **(extra_env or {}),
    }
    return subprocess.run(
        ["bash", str(GLUE), *args],
        cwd=repo,
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        (
            '["hooks", "skills/buildlog/scripts"]',
            (0, "hooks,skills/buildlog/scripts\n", ""),
        ),
        (
            '["hooks", "odd,name"]',
            (2, "", "kcov source path contains a comma: odd,name\n"),
        ),
        ("[]", (2, "", "configuration error: source paths must be a non-empty list\n")),
        (
            '"hooks"',
            (2, "", "configuration error: source paths must be a non-empty list\n"),
        ),
        (
            '["hooks", 1]',
            (2, "", "configuration error: source paths must be a non-empty list\n"),
        ),
        (
            '[""]',
            (2, "", "configuration error: source paths must be a non-empty list\n"),
        ),
    ],
)
def test_kcov_paths_uses_validated_json_list(tmp_path, sources, expected):
    result = run(tmp_path, "kcov-paths", extra_env={"SOURCE_PATHS": sources})
    assert (result.returncode, result.stdout, result.stderr) == expected


def test_python_coverage_arguments_follow_two_declared_sources(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.mutmut]\nsource_paths = ["hooks", "scripts"]\n'
    )
    invocation = tmp_path / "invocation.txt"
    wrapper = tmp_path / "python-wrapper"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import os, subprocess, sys\n"
        "from pathlib import Path\n"
        "if sys.argv[1:4] == ['-m', 'ci_workflows', 'source-paths']:\n"
        "    raise SystemExit(subprocess.call([sys.executable, *sys.argv[1:]]))\n"
        "Path(os.environ['INVOCATION']).write_text('\\n'.join(sys.argv[1:]) + '\\n')\n"
    )
    wrapper.chmod(0o755)
    result = run(
        tmp_path,
        "test-python",
        extra_env={"PYTHON": str(wrapper), "INVOCATION": str(invocation)},
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert invocation.is_file()
    assert invocation.read_text() == (
        "-m\npytest\n--cov=hooks\n--cov=scripts\n--cov-branch\n--cov-report=xml\n"
    )


def test_python_coverage_stops_before_pytest_on_invalid_sources(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.mutmut]\n")
    result = run(tmp_path, "test-python")
    assert (result.returncode, result.stdout, result.stderr) == (
        2,
        "",
        "configuration error: source paths must be a non-empty list\n",
    )
    assert not (tmp_path / "coverage.xml").exists()


def test_python_coverage_reports_unimported_files_in_both_folders(tmp_path):
    fixtures = ROOT / "tests/fixtures/python"
    # Codex-config files copied from commit 09a0775 without changes.
    for relative in ("hooks/session_start.py", "scripts/merge_config.py"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True)
        target.write_bytes((fixtures / relative).read_bytes())
    (tmp_path / "pyproject.toml").write_text(
        '[tool.mutmut]\nsource_paths = ["hooks", "scripts"]\n'
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_only.py").write_text(
        "def test_nothing_imported():\n    assert True\n"
    )
    result = run(tmp_path, "test-python")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (tmp_path / "coverage.xml").is_file()
    report = ET.parse(tmp_path / "coverage.xml")
    sources = [Path(node.text) for node in report.findall("./sources/source")]
    classes = {}
    for node in report.findall(".//class"):
        matches = [
            source / node.attrib["filename"]
            for source in sources
            if (source / node.attrib["filename"]).is_file()
        ]
        assert len(matches) == 1
        classes[str(matches[0].relative_to(tmp_path))] = node.attrib["line-rate"]
    assert classes == {
        "hooks/session_start.py": "0",
        "scripts/merge_config.py": "0",
    }


@pytest.fixture
def commits(tmp_path):
    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("x = 1\n")
    (tmp_path / "src/app.js").write_text("export const x = 1;\n")
    (tmp_path / "ci.yml").write_text("jobs: {}\n")
    (tmp_path / "pyproject.toml").write_text('[tool.mutmut]\nsource_paths = ["src"]\n')
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (tmp_path / "src/app.py").write_text("x = 2\n")
    (tmp_path / "src/app.js").write_text("export const x = 2;\n")
    git("add", ".")
    git("commit", "-qm", "head")
    return tmp_path, base


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def feature_branch(repo, base):
    git(repo, "branch", "-M", "feature")
    git(repo, "update-ref", "refs/remotes/origin/main", base)


@pytest.mark.parametrize("base_sha", ["", "0" * 40])
def test_feature_push_uses_merge_base_for_diff(commits, base_sha):
    repo, base = commits
    feature_branch(repo, base)
    result = run(
        repo,
        "diff",
        "names",
        base_sha=base_sha,
        extra_env={"DEFAULT_BRANCH": "main", "PUSHED_REF": "feature"},
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "src/app.js\nsrc/app.py\n",
        f"base {base} is the merge base of HEAD and origin/main\n",
    )


def test_second_feature_push_still_compares_whole_branch(commits):
    repo, base = commits
    feature_branch(repo, base)
    previous = git(repo, "rev-parse", "HEAD")
    (repo / "src/first.py").write_text("first = True\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "third")
    result = run(
        repo,
        "diff",
        "names",
        base_sha=previous,
        extra_env={"DEFAULT_BRANCH": "main", "PUSHED_REF": "feature"},
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "src/app.js\nsrc/app.py\nsrc/first.py\n",
        f"base {base} is the merge base of HEAD and origin/main\n",
    )


def test_feature_push_reads_default_branch_workflow(commits):
    repo, base = commits
    git(repo, "reset", "--hard", base)
    feature_branch(repo, base)
    (repo / "ci.yml").write_text("jobs: {a: {}}\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "first feature")
    previous = git(repo, "rev-parse", "HEAD")
    (repo / "src/app.py").write_text("x = 3\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "second feature")
    result = run(
        repo,
        "base-workflow",
        "ci.yml",
        base_sha=previous,
        extra_env={"DEFAULT_BRANCH": "main", "PUSHED_REF": "feature"},
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "jobs: {}\n",
        f"base {base} is the merge base of HEAD and origin/main\n",
    )


@pytest.mark.parametrize(
    "base_sha, expected",
    [
        ("0" * 40, "ci.yml\npyproject.toml\nsrc/app.js\nsrc/app.py\n"),
        ("commit", "src/app.js\nsrc/app.py\n"),
    ],
)
def test_default_branch_push_keeps_before(commits, base_sha, expected):
    repo, base = commits
    result = run(
        repo,
        "diff",
        "names",
        base_sha=base if base_sha == "commit" else base_sha,
        extra_env={"DEFAULT_BRANCH": "main", "PUSHED_REF": "main"},
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, expected, "")


def test_pull_request_keeps_base_sha(commits):
    repo, base = commits
    result = run(
        repo,
        "diff",
        "names",
        base_sha=base,
        extra_env={"DEFAULT_BRANCH": "main", "PUSHED_REF": ""},
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "src/app.js\nsrc/app.py\n",
        "",
    )


def test_feature_push_requires_default_ref_for_diff_and_floor(commits, tmp_path):
    repo, base = commits
    git(repo, "branch", "-M", "feature")
    vars = {"DEFAULT_BRANCH": "main", "PUSHED_REF": "feature"}
    diff = run(repo, "diff", "names", base_sha=base, extra_env=vars)
    assert (diff.returncode, diff.stdout, diff.stderr) == (
        2,
        "",
        "default branch is not an available ref: origin/main\n",
    )
    wrapper = tmp_path / "python"
    wrapper.write_text("#!/usr/bin/env bash\nexit 0\n")
    wrapper.chmod(0o755)
    floor = run(
        repo,
        "floor",
        "python",
        "coverage.xml",
        base_sha=base,
        extra_env={
            **vars,
            "PYTHON": str(wrapper),
            "COVERAGE_FLOOR": "90",
            "CALLER_WORKFLOW": "ci.yml",
            "CALLER_JOB": "checks",
        },
    )
    assert (floor.returncode, floor.stdout, floor.stderr) == (
        2,
        "",
        "default branch is not an available ref: origin/main\n",
    )


def test_run_does_not_inherit_runner_branch_variables(commits, monkeypatch):
    repo, base = commits
    monkeypatch.setenv("PUSHED_REF", "feature")
    monkeypatch.setenv("DEFAULT_BRANCH", "main")
    result = run(repo, "diff", "names", base_sha=base)
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "src/app.js\nsrc/app.py\n",
        "",
    )


@pytest.mark.parametrize("base_kind", ["empty", "zero", "commit"])
def test_diff_and_source_selection_have_exact_output(commits, base_kind):
    repo, base = commits
    sha = {"empty": "", "zero": "0" * 40, "commit": base}[base_kind]
    expected = (
        ["src/app.js", "src/app.py"]
        if base_kind == "commit"
        else ["ci.yml", "pyproject.toml", "src/app.js", "src/app.py"]
    )
    result = run(repo, "diff", "names", base_sha=sha)
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "".join(f"{name}\n" for name in expected),
        "",
    )
    result = run(repo, "diff", "patch", base_sha=sha)
    assert result.returncode == 0
    assert "+++ b/src/app.py\n" in result.stdout
    result = run(
        repo,
        "sources",
        "python",
        base_sha=sha,
        input_text="src/app.py\nsrc/app.js\ntests/test_app.py\n",
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "src/app.py\n", "")


def test_base_workflow_is_read_only_when_present(commits):
    repo, base = commits
    present = run(repo, "base-workflow", "ci.yml", base_sha=base)
    absent = run(repo, "base-workflow", "missing.yml", base_sha=base)
    assert (present.returncode, present.stdout, present.stderr) == (0, "jobs: {}\n", "")
    assert (absent.returncode, absent.stdout, absent.stderr) == (1, "", "")


def test_missing_base_includes_every_commit(commits):
    repo, _ = commits
    (repo / "src/first.py").write_text("first = True\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "third"], cwd=repo, check=True)
    names = run(repo, "diff", "names")
    assert (names.returncode, names.stdout, names.stderr) == (
        0,
        "ci.yml\npyproject.toml\nsrc/app.js\nsrc/app.py\nsrc/first.py\n",
        "",
    )


def test_unreachable_base_fails_floor_and_diff(commits, tmp_path):
    repo, _ = commits
    bad_sha = "a" * 40
    diff = run(repo, "diff", "names", base_sha=bad_sha)
    assert diff.returncode != 0
    assert "base SHA" in diff.stderr
    wrapper = tmp_path / "python"
    wrapper.write_text("#!/usr/bin/env bash\nexit 0\n")
    wrapper.chmod(0o755)
    floor = run(
        repo,
        "floor",
        "python",
        "coverage.xml",
        base_sha=bad_sha,
        extra_env={
            "PYTHON": str(wrapper),
            "COVERAGE_FLOOR": "90",
            "CALLER_WORKFLOW": "ci.yml",
            "CALLER_JOB": "checks",
        },
    )
    assert floor.returncode != 0
    assert "base SHA" in floor.stderr


@pytest.mark.parametrize("kind", ["shell", "powershell"])
def test_changed_glue_passes_source_list_for_both_workflows(commits, tmp_path, kind):
    repo, base = commits
    wrapper = tmp_path / "python"
    wrapper.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" > "$CALL_LOG"\ncat >/dev/null\n'
    )
    wrapper.chmod(0o755)
    call_log = tmp_path / "calls"
    result = run(
        repo,
        "changed",
        kind,
        "coverage.xml",
        base_sha=base,
        extra_env={
            "PYTHON": str(wrapper),
            "CALL_LOG": str(call_log),
            "SOURCE_PATHS": '["hooks", "skills/buildlog/scripts"]',
        },
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert call_log.read_text().splitlines() == [
        "-m",
        "ci_workflows",
        "changed-lines",
        "--kind",
        kind,
        "--report",
        "coverage.xml",
        "--diff-file",
        "/dev/stdin",
        "--source-roots-json",
        '["hooks", "skills/buildlog/scripts"]',
    ]


def test_floor_and_changed_gates_pass_base_and_exact_diff(commits, tmp_path):
    repo, base = commits
    wrapper = tmp_path / "python"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$@" >> "$CALL_LOG"\n'
        "if [[ $3 == floor ]]; then\n"
        "  while (( $# )); do\n"
        '    if [[ $1 == --base-workflow || $1 == --head-workflow ]]; then cat "$2" >> "$CALL_LOG"; fi\n'
        "    shift\n"
        "  done\n"
        'elif [[ $3 == changed-lines ]]; then cat >> "$DIFF_LOG"; fi\n'
    )
    wrapper.chmod(0o755)
    call_log = tmp_path / "calls"
    diff_log = tmp_path / "diff"
    env = {
        "PYTHON": str(wrapper),
        "CALL_LOG": str(call_log),
        "DIFF_LOG": str(diff_log),
        "COVERAGE_FLOOR": "90",
        "CALLER_WORKFLOW": "ci.yml",
        "CALLER_JOB": "checks",
    }
    floor = run(repo, "floor", "python", "coverage.xml", base_sha=base, extra_env=env)
    assert (floor.returncode, floor.stdout, floor.stderr) == (0, "", "")
    assert call_log.read_text() == (
        "-m\nci_workflows\nfloor\n--kind\npython\n--report\ncoverage.xml\n"
        "--head-floor\n90\n--head-workflow\nci.yml\n--caller-job\nchecks\n"
        "--base-workflow\n/dev/stdin\njobs: {}\njobs: {}\n"
    )
    call_log.write_text("")
    no_base = run(repo, "floor", "python", "coverage.xml", extra_env=env)
    assert (no_base.returncode, no_base.stdout, no_base.stderr) == (0, "", "")
    assert call_log.read_text() == (
        "-m\nci_workflows\nfloor\n--kind\npython\n--report\ncoverage.xml\n"
        "--head-floor\n90\n--head-workflow\nci.yml\n--caller-job\nchecks\n"
        "jobs: {}\n"
    )
    changed = run(
        repo, "changed", "python", "coverage.xml", base_sha=base, extra_env=env
    )
    assert (changed.returncode, changed.stdout, changed.stderr) == (0, "", "")
    assert (
        "changed-lines\n--kind\npython\n--report\ncoverage.xml\n"
        in call_log.read_text()
    )
    assert "--- a/src/app.py\n+++ b/src/app.py\n" in diff_log.read_text()


@pytest.fixture
def mutation_stubs(commits, tmp_path):
    repo, base = commits
    (repo / "src/other file.js").write_text("export const y = 2;\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "space",
        ],
        cwd=repo,
        check=True,
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls"
    for name, body in {
        "python": """case $3 in
filter-sources) cat ;;
select-functions)
  cat >/dev/null
  source_file=$5
  if [[ -n ${STUB_SELECTION:-} ]]; then
    jq -c --arg source "$source_file" '.[$source] // []' <<< "$STUB_SELECTION"
  else
    printf '["pkg.f.x_one","pkg.f.x_two"]\\n'
  fi ;;
mutation-plan)
  printf 'python ' >> "$CALL_LOG"
  printf '%s|' "$@" >> "$CALL_LOG"
  printf '\\n' >> "$CALL_LOG"
  if [[ -n ${STUB_PLAN_EXIT:-} ]]; then
    echo 'configuration error: stub plan failure' >&2
    exit "$STUB_PLAN_EXIT"
  fi
  cat "$PLAN_JSON" ;;
mutation-config|mutation-checks)
  printf 'python ' >> "$CALL_LOG"
  printf '%s|' "$@" >> "$CALL_LOG"
  printf '\\n' >> "$CALL_LOG"
  exit "${STUB_EXIT:-0}" ;;
survivors) printf 'python %s\\n' "$*" >> "$CALL_LOG" ;;
esac""",
        "mutmut": """printf 'mutmut %s\\n' "$*" >> "$CALL_LOG"
if [[ -n ${STUB_MUTMUT_SHARD:-} ]]; then
  if [[ -z ${STUB_MUTMUT_QUIET:-} ]]; then
    printf 'Running mutation testing\\n    5.00 mutations/second\\n'
  fi
  mkdir -p mutants/sample mutants/.cache
  printf 'other meta\\n' > mutants/sample/other.py.meta
  printf 'init meta\\n' > mutants/sample/__init__.py.meta
  printf 'stray meta\\n' > mutants/.cache/stray.meta
  exit "${STUB_MUTMUT_EXIT:-0}"
fi""",
        "npx": 'printf \'npx argc=%s %s\\n\' "$#" "$*" >> "$CALL_LOG"',
    }.items():
        executable = bin_dir / name
        executable.write_text("#!/usr/bin/env bash\n" + body + "\n")
        executable.chmod(0o755)
    env = {
        "PYTHON": str(bin_dir / "python"),
        "CALL_LOG": str(log),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "MUTATION_SOURCES": '["src"]',
    }
    return repo, base, env, log


PYTHON_MUTATION_CALLS = (
    "mutmut run pkg.f.x_one__mutmut_* pkg.f.x_two__mutmut_*\n"
    "python -m ci_workflows survivors --format mutmut --results mutants "
    "--survivors-file .mutation-survivors.yml --selected-function pkg.f.x_one "
    "--selected-function pkg.f.x_two\n"
)
JAVASCRIPT_MUTATION_CALLS = (
    "npx argc=7 --no-install stryker run --mutate src/app.js,src/other file.js "
    "--reporters json,clear-text\n"
    "python -m ci_workflows survivors --format stryker --results "
    "reports/mutation/mutation.json --survivors-file .mutation-survivors.yml "
    "--selected-file src/app.js --selected-file src/other file.js\n"
)


def test_mutation_commands_pass_exact_selectors_and_scope(mutation_stubs):
    repo, base, env, log = mutation_stubs
    py = run(repo, "mutate-python", base_sha=base, extra_env=env)
    assert (py.returncode, py.stdout, py.stderr) == (0, "", "")
    assert log.read_text() == PYTHON_MUTATION_CALLS
    log.write_text("")
    js = run(repo, "mutate-javascript", base_sha=base, extra_env=env)
    assert (js.returncode, js.stdout, js.stderr) == (0, "", "")
    assert log.read_text() == JAVASCRIPT_MUTATION_CALLS


@pytest.mark.parametrize("command", ["mutate-python", "mutate-javascript"])
def test_mutation_default_branch_push_runs_locally(mutation_stubs, command):
    repo, base, env, log = mutation_stubs
    result = run(
        repo,
        command,
        base_sha=base,
        extra_env={**env, "DEFAULT_BRANCH": "main", "PUSHED_REF": "main"},
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert log.read_text() == (
        PYTHON_MUTATION_CALLS
        if command == "mutate-python"
        else JAVASCRIPT_MUTATION_CALLS
    )


@pytest.mark.parametrize("command", ["mutate-python", "mutate-javascript"])
def test_mutation_feature_push_runs_as_before(mutation_stubs, command):
    repo, base, env, log = mutation_stubs
    feature_branch(repo, base)
    result = run(
        repo,
        command,
        extra_env={**env, "DEFAULT_BRANCH": "main", "PUSHED_REF": "feature"},
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "",
        f"base {base} is the merge base of HEAD and origin/main\n"
        * (2 if command == "mutate-python" else 1),
    )
    assert log.read_text() == (
        PYTHON_MUTATION_CALLS
        if command == "mutate-python"
        else JAVASCRIPT_MUTATION_CALLS
    )


@pytest.mark.parametrize("command", ["mutate-python", "mutate-javascript"])
def test_mutation_pull_request_runs_as_before(mutation_stubs, command):
    repo, base, env, log = mutation_stubs
    result = run(
        repo,
        command,
        base_sha=base,
        extra_env={**env, "DEFAULT_BRANCH": "main", "PUSHED_REF": ""},
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert log.read_text() == (
        PYTHON_MUTATION_CALLS
        if command == "mutate-python"
        else JAVASCRIPT_MUTATION_CALLS
    )


def test_mutation_does_not_skip_without_default_branch(mutation_stubs):
    repo, base, env, log = mutation_stubs
    result = run(
        repo,
        "mutate-python",
        base_sha=base,
        extra_env={**env, "DEFAULT_BRANCH": "", "PUSHED_REF": "main"},
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert log.read_text() == PYTHON_MUTATION_CALLS


PLAN = {
    "functions": ["sample.other.x_twice", "sample.x_add"],
    "sources": ["sample/other.py", "sample/__init__.py"],
    "mutants": 6,
    "shards": [
        ["sample.other.x_twice__mutmut_1", "sample.other.x_twice__mutmut_2"],
        ["sample.other.x_twice__mutmut_3", "sample.other.x_twice__mutmut_4"],
        ["sample.other.x_twice__mutmut_5", "sample.x_add__mutmut_1"],
    ],
}
PLAN_CALL = "python -m|ci_workflows|mutation-plan|--source|src/app.py|--selected-function|pkg.f.x_one|--selected-function|pkg.f.x_two|\n"


def plan_setup(mutation_stubs):
    repo, base, env, log = mutation_stubs
    plan = repo / "stub-plan.json"
    plan.write_text(json.dumps(PLAN))
    output = repo / "github-output"
    output.write_text("")
    return (
        repo,
        base,
        {**env, "PLAN_JSON": str(plan), "GITHUB_OUTPUT": str(output)},
        log,
        output,
    )


def test_plan_python_default_branch_skips(mutation_stubs):
    repo, base, env, log, output = plan_setup(mutation_stubs)
    result = run(
        repo,
        "plan-python",
        base_sha=base,
        extra_env={**env, "DEFAULT_BRANCH": "main", "PUSHED_REF": "main"},
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "mutation skipped: push to the default branch main\n",
        "",
    )
    assert output.read_text() == "shards=[]\nskipped=push to the default branch main\n"
    assert not log.exists() and not (repo / "mutation-plan.json").exists()


def test_plan_python_no_functions_skips(mutation_stubs):
    repo, base, env, log, output = plan_setup(mutation_stubs)
    env["STUB_SELECTION"] = json.dumps({"src/app.py": []})
    result = run(repo, "plan-python", base_sha=base, extra_env=env)
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "mutation skipped: no Python function changed\n",
        "",
    )
    assert output.read_text() == "shards=[]\nskipped=no Python function changed\n"
    assert not log.exists() and not (repo / "mutation-plan.json").exists()


def test_plan_python_uses_selected_source_and_shard_numbers(mutation_stubs):
    repo, base, env, log, output = plan_setup(mutation_stubs)
    result = run(repo, "plan-python", base_sha=base, extra_env=env)
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "mutation plan: 6 mutants in 3 shards\n",
        "",
    )
    assert log.read_text() == PLAN_CALL
    assert (repo / "mutation-plan.json").read_text() == (
        repo / "stub-plan.json"
    ).read_text()
    assert output.read_text() == "shards=[0,1,2]\nskipped=\n"


def test_plan_python_passes_only_sources_with_functions(mutation_stubs):
    repo, base, env, log, _ = plan_setup(mutation_stubs)
    for name in ("a", "b", "c"):
        (repo / f"src/{name}.py").write_text("x = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "three files")
    env["STUB_SELECTION"] = json.dumps({"src/b.py": ["src.b.x_one"]})
    result = run(repo, "plan-python", base_sha=base, extra_env=env)
    assert result.returncode == 0
    assert (
        log.read_text()
        == "python -m|ci_workflows|mutation-plan|--source|src/b.py|--selected-function|src.b.x_one|\n"
    )


def test_plan_python_keeps_empty_plan(mutation_stubs):
    repo, base, env, _, output = plan_setup(mutation_stubs)
    empty = {"functions": [], "sources": ["src/app.py"], "mutants": 0, "shards": []}
    (repo / "stub-plan.json").write_text(json.dumps(empty))
    result = run(repo, "plan-python", base_sha=base, extra_env=env)
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "mutation plan: 0 mutants in 0 shards\n",
        "",
    )
    assert (repo / "mutation-plan.json").read_text() == json.dumps(empty)
    assert output.read_text() == "shards=[]\nskipped=\n"


@pytest.mark.parametrize(
    "branch,ref", [("feature", "feature"), (None, ""), (None, "main-fix")]
)
def test_plan_python_branch_inputs(mutation_stubs, branch, ref):
    repo, base, env, log, output = plan_setup(mutation_stubs)
    if branch or ref == "main-fix":
        feature_branch(repo, base)
    if ref == "main-fix":
        git(repo, "branch", "-M", "main-fix")
    result = run(
        repo,
        "plan-python",
        base_sha=base,
        extra_env={**env, "DEFAULT_BRANCH": "main", "PUSHED_REF": ref},
    )
    expected_stderr = (
        f"base {base} is the merge base of HEAD and origin/main\n" * 2
        if branch or ref == "main-fix"
        else ""
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "mutation plan: 6 mutants in 3 shards\n",
        expected_stderr,
    )
    assert log.read_text() == PLAN_CALL
    assert (repo / "mutation-plan.json").read_text() == (
        repo / "stub-plan.json"
    ).read_text()
    assert output.read_text() == "shards=[0,1,2]\nskipped=\n"


def test_plan_python_failure_does_not_publish_outputs(mutation_stubs):
    repo, base, env, log, output = plan_setup(mutation_stubs)
    result = run(
        repo, "plan-python", base_sha=base, extra_env={**env, "STUB_PLAN_EXIT": "2"}
    )
    assert (
        result.returncode == 2
        and "configuration error: stub plan failure" in result.stderr
    )
    assert log.read_text() == PLAN_CALL
    assert output.read_text() == ""


def test_plan_python_requires_github_output(mutation_stubs):
    repo, base, env, log, _ = plan_setup(mutation_stubs)
    del env["GITHUB_OUTPUT"]
    result = run(repo, "plan-python", base_sha=base, extra_env=env)
    assert (result.returncode, result.stdout, result.stderr) == (
        2,
        "",
        "GITHUB_OUTPUT is not set\n",
    )
    assert not log.exists()


def shard_setup(mutation_stubs):
    repo, base, env, log, _ = plan_setup(mutation_stubs)
    (repo / "mutation-plan.json").write_text(json.dumps(PLAN))
    summary = repo / "summary"
    summary.write_text("")
    return (
        repo,
        base,
        {
            **env,
            "JOB_STARTED": str(int(time.time()) - 30),
            "GITHUB_STEP_SUMMARY": str(summary),
            "STUB_MUTMUT_SHARD": "1",
        },
        log,
        summary,
    )


@pytest.mark.parametrize(
    "number,names",
    [(0, PLAN["shards"][0]), (1, PLAN["shards"][1]), (2, PLAN["shards"][2])],
)
def test_shard_python_runs_exact_names_and_copies_visible_meta(
    mutation_stubs, number, names
):
    repo, base, env, log, summary = shard_setup(mutation_stubs)
    t0 = int(time.time())
    env["JOB_STARTED"] = str(t0 - 30)
    result = run(repo, "shard-python", str(number), base_sha=base, extra_env=env)
    t1 = int(time.time())
    assert result.returncode == 0
    assert (
        "Running mutation testing" in result.stdout
        and "5.00 mutations/second" in result.stdout
    )
    assert log.read_text() == (
        "python -m|ci_workflows|mutation-config|--config|pyproject.toml|--source|sample/other.py|--source|sample/__init__.py|\n"
        + "mutmut run "
        + " ".join(names)
        + "\n"
    )
    for name, content in (
        ("other.py.meta", "other meta\n"),
        ("__init__.py.meta", "init meta\n"),
    ):
        assert (
            repo / "mutation-results" / str(number) / "sample" / name
        ).read_text() == content
    assert not (repo / "mutation-results" / str(number) / ".cache").exists()
    match = re.fullmatch(
        rf"shard {number + 1} of 3: 2 mutants, setup (\d+) s, 5\.00 mutations/second\n",
        summary.read_text(),
    )
    assert match and 30 <= int(match[1]) <= t1 - t0 + 30


def test_shard_python_quiet_fallback(mutation_stubs):
    repo, base, env, _, summary = shard_setup(mutation_stubs)
    result = run(
        repo,
        "shard-python",
        "1",
        base_sha=base,
        extra_env={**env, "STUB_MUTMUT_QUIET": "1"},
    )
    assert result.returncode == 0
    assert (
        summary.read_text()
        == "shard 2 of 3: 2 mutants, setup unknown, mutations/second unknown\n"
    )


@pytest.mark.parametrize(
    "failure,expected,code",
    [("STUB_MUTMUT_EXIT", "mutmut run", 1), ("STUB_EXIT", "mutation-config", 2)],
)
def test_shard_python_stops_on_failure(mutation_stubs, failure, expected, code):
    repo, base, env, log, summary = shard_setup(mutation_stubs)
    result = run(
        repo, "shard-python", "1", base_sha=base, extra_env={**env, failure: str(code)}
    )
    assert result.returncode == code and expected in log.read_text()
    if failure == "STUB_EXIT":
        assert log.read_text() == (
            "python -m|ci_workflows|mutation-config|--config|pyproject.toml|"
            "--source|sample/other.py|--source|sample/__init__.py|\n"
        )
    assert not (repo / "mutation-results").exists() and summary.read_text() == ""


@pytest.mark.parametrize(
    "argument,missing,message",
    [
        (None, None, "shard-python needs a shard number\n"),
        ("x", None, "shard number must be a whole number: x\n"),
        ("3", None, "no shard 3 in mutation-plan.json\n"),
        ("1", "JOB_STARTED", "JOB_STARTED is not set\n"),
        ("1", "GITHUB_STEP_SUMMARY", "GITHUB_STEP_SUMMARY is not set\n"),
        ("1", "plan", "mutation-plan.json not found\n"),
    ],
)
def test_shard_python_refuses_bad_inputs(mutation_stubs, argument, missing, message):
    repo, base, env, log, _ = shard_setup(mutation_stubs)
    if missing == "plan":
        (repo / "mutation-plan.json").unlink()
    elif missing:
        del env[missing]
    args = () if argument is None else (argument,)
    result = run(repo, "shard-python", *args, base_sha=base, extra_env=env)
    assert (result.returncode, result.stdout, result.stderr) == (2, "", message)
    assert not log.exists()


@pytest.mark.parametrize(
    "private,reason,expected",
    [
        ("true", "no Python function changed", "--private|"),
        (
            "false",
            "push to the default branch main",
            "--skipped|push to the default branch main|",
        ),
        (
            "false",
            "",
            "--plan-file|mutation-plan.json|--shards|mutation-shards|--survivors-file|.mutation-survivors.yml|--source-config|pyproject.toml|",
        ),
    ],
)
def test_checks_python_passes_exact_mode(mutation_stubs, private, reason, expected):
    repo, base, env, log = mutation_stubs
    results = {
        "TESTS_RESULT": "success",
        "PLAN_RESULT": "skipped" if private == "true" else "success",
        "MUTATE_RESULT": "skipped" if reason else "success",
        "REPOSITORY_PRIVATE": private,
        "PLAN_SKIPPED": reason,
    }
    result = run(repo, "checks-python", base_sha=base, extra_env={**env, **results})
    assert result.returncode == 0
    assert (
        log.read_text()
        == "python -m|ci_workflows|mutation-checks|--tests|success|--plan|"
        + results["PLAN_RESULT"]
        + "|--mutate|"
        + results["MUTATE_RESULT"]
        + "|"
        + expected
        + "\n"
    )


@pytest.mark.parametrize("code", [1, 2])
def test_checks_python_propagates_exit(mutation_stubs, code):
    repo, base, env, _ = mutation_stubs
    result = run(
        repo,
        "checks-python",
        base_sha=base,
        extra_env={
            **env,
            "TESTS_RESULT": "success",
            "PLAN_RESULT": "success",
            "MUTATE_RESULT": "success",
            "STUB_EXIT": str(code),
        },
    )
    assert result.returncode == code


def test_checks_python_real_private_cli(commits):
    repo, base = commits
    result = run(
        repo,
        "checks-python",
        base_sha=base,
        extra_env={
            "TESTS_RESULT": "success",
            "PLAN_RESULT": "skipped",
            "MUTATE_RESULT": "skipped",
            "REPOSITORY_PRIVATE": "true",
        },
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "mutation skipped in a private repository\n",
        "",
    )


@pytest.mark.parametrize("missing", ["TESTS_RESULT", "PLAN_RESULT", "MUTATE_RESULT"])
def test_checks_python_requires_results(mutation_stubs, missing):
    repo, base, env, log = mutation_stubs
    results = {
        "TESTS_RESULT": "success",
        "PLAN_RESULT": "success",
        "MUTATE_RESULT": "success",
    }
    del results[missing]
    result = run(repo, "checks-python", base_sha=base, extra_env={**env, **results})
    assert (result.returncode, result.stdout, result.stderr) == (
        2,
        "",
        f"{missing} is not set\n",
    )
    assert not log.exists()


def test_ci_runs_pinned_actionlint_and_shared_dependencies_are_exact():
    ci = yaml.load(
        (ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader
    )
    steps = ci["jobs"]["actionlint"]["steps"]
    assert any(
        "actionlint@v1.7.12 .github/workflows/*.yml" in step.get("run", "")
        for step in steps
    )
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert 'dependencies = ["PyYAML==' in pyproject
    assert "diff-cover==" in pyproject and "unidiff==" in pyproject


def test_readme_names_molecule_podman_and_local_mutmut_contract():
    readme = (ROOT / "README.md").read_text()
    assert "PODMAN_EXECUTABLE" in readme
    assert "mutmut" in readme and "local mutation needs" in readme


def test_tailnet_readme_names_caller_identity_permission():
    from pathlib import Path

    instructions = Path("README.md").read_text()
    assert "caller must grant `id-token: write`" in instructions
