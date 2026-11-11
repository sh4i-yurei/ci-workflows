"""Public behavior of mutation planning, merging and scoped survivors."""

import io
import json
import math
import subprocess
import sys
import tomllib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest
from mutmut.mutation.file_mutation import mutate_file_contents
from mutmut.utils.format_utils import get_mutant_name

from ci_workflows.checks import main


ADD_SOURCE = (
    "def add(a, b):\n    return a + b\n\n\ndef unchanged(a):\n    return a * 2\n"
)
OTHER_SOURCE = "def twice(a):\n    if a > 10:\n        return a * 2\n    return a + a\n"
TEST_SOURCE = (
    "from sample import add, unchanged\nfrom sample.other import twice\n\n\n"
    "def test_add():\n    assert add(2, 3) == 5\n\n\n"
    "def test_unchanged():\n    assert unchanged(4) == 8\n\n\n"
    "def test_twice():\n    assert twice(3) == 6\n"
)
SELECTED = ("sample.other.x_twice", "sample.x_add")


def invoke(*args):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main([*map(str, args)])
    return SimpleNamespace(
        returncode=code, stdout=stdout.getvalue(), stderr=stderr.getvalue()
    )


def fixture_project(path):
    (path / "sample").mkdir()
    (path / "sample" / "__init__.py").write_text(ADD_SOURCE)
    (path / "sample" / "other.py").write_text(OTHER_SOURCE)
    (path / "tests").mkdir()
    (path / "tests" / "test_sample.py").write_text(TEST_SOURCE)
    (path / "pyproject.toml").write_text(
        '[tool.mutmut]\nsource_paths = ["sample"]\n'
        'pytest_add_cli_args_test_selection = ["tests/test_sample.py"]\n'
    )


def plan(*extra):
    return invoke(
        "mutation-plan",
        "--source",
        "sample/other.py",
        "--source",
        "sample/__init__.py",
        "--selected-function",
        SELECTED[0],
        "--selected-function",
        SELECTED[1],
        *extra,
    )


def names_from(result):
    return [name for shard in json.loads(result.stdout)["shards"] for name in shard]


def test_plan_names_and_balanced_shards(tmp_path, monkeypatch):
    fixture_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = plan("--per-shard", 3)
    assert (result.returncode, result.stderr) == (0, "")
    assert (
        result.stdout
        == json.dumps(json.loads(result.stdout), separators=(",", ":")) + "\n"
    )
    data = json.loads(result.stdout)
    names = names_from(result)
    expected = []
    for path, source, function in (
        (Path("sample/other.py"), OTHER_SOURCE, "x_twice"),
        (Path("sample/__init__.py"), ADD_SOURCE, "x_add"),
    ):
        expected.extend(
            get_mutant_name(path, name)
            for name in mutate_file_contents(str(path), source).mutant_names
            if name.startswith(function + "__mutmut_")
        )
    assert names == expected
    assert len(names) == len(set(names)) == data["mutants"]
    assert all("x_unchanged__mutmut_" not in name for name in names)
    sizes = [len(shard) for shard in data["shards"]]
    assert len(sizes) == math.ceil(len(names) / 3)
    assert sizes == sorted(sizes, reverse=True)
    assert max(sizes) - min(sizes) <= 1


def test_plan_empty_selection_and_sizes(tmp_path, monkeypatch):
    fixture_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    empty = invoke(
        "mutation-plan",
        "--source",
        "sample/other.py",
        "--selected-function",
        "sample.x_add",
    )
    assert (empty.returncode, empty.stdout, empty.stderr) == (
        0,
        '{"mutants":0,"shards":[]}\n',
        "",
    )
    count = json.loads(plan("--per-shard", 3).stdout)["mutants"]
    reference = names_from(plan("--per-shard", count + 1))
    for size in (1, 2, 4, count, count + 1):
        result = plan("--per-shard", size)
        data = json.loads(result.stdout)
        sizes = [len(shard) for shard in data["shards"]]
        assert (result.returncode, result.stderr, data["mutants"]) == (0, "", count)
        assert names_from(result) == reference
        assert len(sizes) == math.ceil(count / size)
        assert sizes == sorted(sizes, reverse=True)
        assert max(sizes) - min(sizes) <= 1


@pytest.mark.parametrize("size", ["0", "-1"])
def test_plan_rejects_nonpositive_size(tmp_path, monkeypatch, size):
    fixture_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = plan("--per-shard", size)
    assert (result.returncode, result.stdout, result.stderr) == (
        2,
        "",
        "configuration error: --per-shard must be a positive whole number\n",
    )


@pytest.mark.parametrize(
    "name, source, expected",
    [
        ("absent.py", None, "source not found: absent.py"),
        ("bad.py", "def broken(:\n    return 1\n", "invalid Python source bad.py:1"),
        (
            "mod.py",
            "def changed():\n    # pragma: no mutate end\n    return 1\n",
            "# pragma: no mutate end at mod.py:2 without a # pragma: no mutate start",
        ),
    ],
)
def test_plan_source_errors(tmp_path, monkeypatch, name, source, expected):
    monkeypatch.chdir(tmp_path)
    if source is not None:
        Path(name).write_text(source)
    result = invoke("mutation-plan", "--source", name, "--selected-function", "x")
    assert (result.returncode, result.stdout, result.stderr) == (
        2,
        "",
        f"configuration error: {expected}\n",
    )


def test_plan_version_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("importlib.metadata.version", lambda name: "3.9.0")
    result = invoke(
        "mutation-plan", "--source", "missing.py", "--selected-function", "x"
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        2,
        "",
        "configuration error: mutmut 3.8.0 is required for mutation planning\n",
    )


@pytest.mark.parametrize(
    "command, expected",
    [
        (
            ["mutation-plan", "--source", "missing.py", "--selected-function", "x"],
            (
                2,
                "",
                "configuration error: mutmut 3.8.0 is required for mutation planning\n",
            ),
        ),
        (["source-paths", "--roots-json", '["pkg"]'], (0, "pkg\n", "")),
    ],
)
def test_import_without_mutmut(tmp_path, command, expected):
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.modules['mutmut'] = None; from ci_workflows.checks import main; raise SystemExit(main(sys.argv[1:]))",
            *command,
        ],
        cwd=tmp_path,
        env={"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin", "HOME": str(home)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert (result.returncode, result.stdout, result.stderr) == expected


def write_meta(path, statuses, extra=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"exit_code_by_key": statuses, **(extra or {})}))


def test_merge_results_and_hidden_files(tmp_path):
    first, second, target = (tmp_path / name for name in ("one", "two", "merged"))
    write_meta(
        first / "pkg/f.py.meta",
        {"pkg.f.x__mutmut_1": 1, "pkg.f.x__mutmut_2": None},
        {"source": "first"},
    )
    write_meta(
        second / "pkg/f.py.meta", {"pkg.f.x__mutmut_1": None, "pkg.f.x__mutmut_2": 0}
    )
    (first / ".cache").mkdir()
    (first / ".cache" / "stray.meta").write_text("not json")
    result = invoke(
        "mutation-merge", "--into", target, "--shard", first, "--shard", second
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "merged 2 shards: 2 mutants with results\n",
        "",
    )
    assert json.loads((target / "pkg/f.py.meta").read_text()) == {
        "exit_code_by_key": {"pkg.f.x__mutmut_1": 1, "pkg.f.x__mutmut_2": 0},
        "source": "first",
    }
    assert not (target / ".cache").exists()


@pytest.mark.parametrize(
    "kind, expected",
    [
        ("files", "shards hold different result files: pkg/f.py.meta, pkg/g.py.meta"),
        ("keys", "shards list different mutants in pkg/f.py.meta"),
        ("duplicate", "mutant has results from more than one shard: pkg.f.x__mutmut_1"),
    ],
)
def test_merge_rejects_inconsistent_shards(tmp_path, kind, expected):
    first, second, target = (tmp_path / name for name in ("one", "two", "merged"))
    key = "pkg.f.x__mutmut_1"
    write_meta(first / "pkg/f.py.meta", {key: 1})
    if kind == "files":
        write_meta(second / "pkg/g.py.meta", {key: None})
    elif kind == "keys":
        write_meta(second / "pkg/f.py.meta", {"pkg.f.x__mutmut_2": None})
    else:
        write_meta(second / "pkg/f.py.meta", {key: 0})
    result = invoke(
        "mutation-merge", "--into", target, "--shard", first, "--shard", second
    )
    assert (result.returncode, result.stdout, result.stderr, target.exists()) == (
        2,
        "",
        f"configuration error: {expected}\n",
        False,
    )


@pytest.mark.parametrize(
    "content", ["[]", "{}", '{"exit_code_by_key": []}', "not json"]
)
def test_merge_rejects_bad_meta(tmp_path, content):
    shard, target = tmp_path / "shard", tmp_path / "merged"
    shard.mkdir()
    (shard / "f.py.meta").write_text(content)
    result = invoke("mutation-merge", "--into", target, "--shard", shard)
    message = (
        "Expecting value: line 1 column 1 (char 0)"
        if content == "not json"
        else "mutmut result statuses must be unique mappings"
    )
    assert (result.returncode, result.stdout, result.stderr, target.exists()) == (
        2,
        "",
        f"configuration error: {message}\n",
        False,
    )


def test_merge_rejects_existing_target(tmp_path):
    target = tmp_path / "merged"
    target.mkdir()
    result = invoke("mutation-merge", "--into", target, "--shard", tmp_path / "shard")
    assert (result.returncode, result.stdout, result.stderr) == (
        2,
        "",
        f"configuration error: merge target already exists: {target}\n",
    )


def survivors_case(tmp_path, monkeypatch, roots, files, extra, source_config=True):
    monkeypatch.chdir(tmp_path)
    for file in files:
        path = Path(file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("pass\n")
    Path("pyproject.toml").write_text(
        "[tool.mutmut]\nsource_paths = " + json.dumps(roots) + "\n"
    )
    write_meta(Path("results/f.py.meta"), {"pkg.f.x__mutmut_3": 0})
    Path("survivors.yml").write_text(
        "- mutant: pkg.f.x__mutmut_3\n  reason: equivalent\n"
        f"- mutant: {extra}\n  reason: equivalent\n"
    )
    args = [
        "survivors",
        "--format",
        "mutmut",
        "--results",
        "results",
        "--survivors-file",
        "survivors.yml",
        "--selected-function",
        "pkg.f.x",
    ]
    if source_config:
        args.extend(("--source-config", "pyproject.toml"))
    return invoke(*args)


@pytest.mark.parametrize(
    "files, configured, expected",
    [
        (("pkg/f.py", "pkg/g.py"), True, (0, "mutation survivors accepted\n")),
        (("pkg/f.py",), True, (1, "stale mutant: pkg.g.y__mutmut_1\n")),
        (("pkg/f.py", "pkg/g.py"), False, (1, "stale mutant: pkg.g.y__mutmut_1\n")),
        (("pkg/f.py",), False, (1, "stale mutant: pkg.g.y__mutmut_1\n")),
    ],
)
def test_survivors_source_scope(tmp_path, monkeypatch, files, configured, expected):
    result = survivors_case(
        tmp_path, monkeypatch, ["pkg"], files, "pkg.g.y__mutmut_1", configured
    )
    assert (result.returncode, result.stdout, result.stderr) == (*expected, "")


def test_survivors_deleted_function_is_stale(tmp_path, monkeypatch):
    result = survivors_case(
        tmp_path, monkeypatch, ["pkg"], ["pkg/f.py"], "pkg.f.deleted__mutmut_7"
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        1,
        "stale mutant: pkg.f.deleted__mutmut_7\n",
        "",
    )


@pytest.mark.parametrize(
    "roots, files, extra",
    [
        (["pkg"], ["pkg/f.py", "pkg/__init__.py"], "pkg.z__mutmut_1"),
        (["src"], ["src/pkg/f.py", "src/pkg/h.py"], "pkg.h.w__mutmut_2"),
        (["pkg/f.py", "pkg/g.py"], ["pkg/f.py", "pkg/g.py"], "pkg.g.y__mutmut_1"),
    ],
)
def test_survivors_source_path_forms(tmp_path, monkeypatch, roots, files, extra):
    result = survivors_case(tmp_path, monkeypatch, roots, files, extra)
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "mutation survivors accepted\n",
        "",
    )


def test_survivors_source_config_rejects_stryker(tmp_path):
    result = invoke(
        "survivors",
        "--format",
        "stryker",
        "--results",
        tmp_path / "none",
        "--survivors-file",
        tmp_path / "none",
        "--source-config",
        tmp_path / "none",
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        2,
        "configuration error: --source-config applies only to mutmut results\n",
        "",
    )


def test_survivors_source_config_checks_version_first(tmp_path, monkeypatch):
    monkeypatch.setattr("importlib.metadata.version", lambda name: "3.9.0")
    result = invoke(
        "survivors",
        "--format",
        "mutmut",
        "--results",
        tmp_path / "none",
        "--survivors-file",
        tmp_path / "none",
        "--source-config",
        tmp_path / "none",
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        2,
        "configuration error: mutmut 3.8.0 is required for the survivors source scope\n",
        "",
    )


def test_mutmut_test_selection():
    document = tomllib.loads(
        (Path(__file__).parent.parent / "pyproject.toml").read_text()
    )
    assert document["tool"]["mutmut"]["pytest_add_cli_args_test_selection"] == [
        "tests/test_python_checks.py",
        "tests/test_python_edges.py",
        "tests/test_other_languages.py",
        "tests/test_rules.py",
        "tests/test_tailnet_mutation.py",
        "tests/test_mutation.py",
    ]
