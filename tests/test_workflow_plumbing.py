"""Exercise the shell commands used by the reusable workflows."""

import os
import subprocess
import sys
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
GLUE = ROOT / "scripts/workflow-glue.sh"


def run(repo, *args, base_sha="", input_text="", extra_env=None):
    env = {
        **os.environ,
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
        "configuration error: mutmut source_paths must be a non-empty list\n",
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


def test_floor_and_changed_gates_pass_base_and_exact_diff(commits, tmp_path):
    repo, base = commits
    wrapper = tmp_path / "python"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$@" >> "$CALL_LOG"\n'
        "if [[ $3 == floor ]]; then\n"
        "  while (( $# )); do\n"
        '    if [[ $1 == --base-workflow ]]; then cat "$2" >> "$CALL_LOG"; fi\n'
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
    calls = call_log.read_text()
    assert "floor\n--kind\npython\n--report\ncoverage.xml\n--head-floor\n90\n" in calls
    assert "--caller-job\nchecks\njobs: {}\n" in calls
    call_log.write_text("")
    no_base = run(repo, "floor", "python", "coverage.xml", extra_env=env)
    assert no_base.returncode == 0
    assert "--base-workflow" not in call_log.read_text()
    changed = run(
        repo, "changed", "python", "coverage.xml", base_sha=base, extra_env=env
    )
    assert (changed.returncode, changed.stdout, changed.stderr) == (0, "", "")
    assert (
        "changed-lines\n--kind\npython\n--report\ncoverage.xml\n"
        in call_log.read_text()
    )
    assert "--- a/src/app.py\n+++ b/src/app.py\n" in diff_log.read_text()


def test_mutation_commands_pass_exact_selectors_and_scope(commits, tmp_path):
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
select-functions) cat >/dev/null; printf '["pkg.f.x_one","pkg.f.x_two"]\\n' ;;
survivors) printf 'python %s\\n' "$*" >> "$CALL_LOG" ;;
esac""",
        "mutmut": 'printf \'mutmut %s\\n\' "$*" >> "$CALL_LOG"',
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
    py = run(repo, "mutate-python", base_sha=base, extra_env=env)
    assert (py.returncode, py.stdout, py.stderr) == (0, "", "")
    assert log.read_text() == (
        "mutmut run pkg.f.x_one__mutmut_* pkg.f.x_two__mutmut_*\n"
        "python -m ci_workflows survivors --format mutmut --results mutants "
        "--survivors-file .mutation-survivors.yml --selected-function pkg.f.x_one "
        "--selected-function pkg.f.x_two\n"
    )
    log.write_text("")
    js = run(repo, "mutate-javascript", base_sha=base, extra_env=env)
    assert (js.returncode, js.stdout, js.stderr) == (0, "", "")
    assert log.read_text() == (
        "npx argc=7 --no-install stryker run --mutate src/app.js,src/other file.js "
        "--reporters json,clear-text\n"
        "python -m ci_workflows survivors --format stryker --results "
        "reports/mutation/mutation.json --survivors-file .mutation-survivors.yml "
        "--selected-file src/app.js --selected-file src/other file.js\n"
    )


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


def test_readme_names_molecule_podman_and_python_mutmut_contract():
    readme = (ROOT / "README.md").read_text()
    assert "PODMAN_EXECUTABLE" in readme
    assert "mutmut" in readme and "Python workflow installs" in readme


def test_tailnet_readme_names_caller_identity_permission():
    from pathlib import Path

    instructions = Path("README.md").read_text()
    assert "caller must grant `id-token: write`" in instructions
