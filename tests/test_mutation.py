"""Public behavior of mutation planning, merging and scoped survivors."""

import io
import builtins
import difflib
import json
import math
import subprocess
import sys
import tempfile
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


@pytest.mark.parametrize(
    "arguments, missing",
    [
        (["mutation-plan", "--selected-function", "x"], "--source"),
        (["mutation-plan", "--source", "a.py"], "--selected-function"),
        (["mutation-merge", "--shard", "s"], "--into"),
        (["mutation-merge", "--into", "t"], "--shard"),
    ],
)
def test_mutation_commands_require_arguments(arguments, missing, capsys):
    with pytest.raises(SystemExit) as error:
        main(arguments)
    assert error.value.code == 2
    assert capsys.readouterr().err.splitlines()[-1] == (
        f"ci-checks {arguments[0]}: error: the following arguments are required: {missing}"
    )


@pytest.mark.parametrize(
    "lines, extra, expected_sizes",
    [
        (67, (), [101, 100]),
        (3, ("--per-shard", "2"), [2, 2, 2, 2, 1]),
    ],
)
def test_plan_large_and_uneven_shards(
    tmp_path, monkeypatch, lines, extra, expected_sizes
):
    source = "def big(v):\n" + "    v = v + 1\n" * lines + "    return v\n"
    path = tmp_path / "pkg/big.py"
    path.parent.mkdir()
    path.write_text(source)
    (tmp_path / "pyproject.toml").write_text('[tool.mutmut]\nsource_paths = ["pkg"]\n')
    monkeypatch.chdir(tmp_path)
    expected = [
        get_mutant_name(Path("pkg/big.py"), name)
        for name in mutate_file_contents("pkg/big.py", source).mutant_names
        if name.startswith("x_big__mutmut_")
    ]
    assert len(expected) == lines * 3
    result = invoke(
        "mutation-plan",
        "--source",
        "pkg/big.py",
        "--selected-function",
        "pkg.big.x_big",
        *extra,
    )
    assert (result.returncode, result.stderr) == (0, "")
    data = json.loads(result.stdout)
    assert data["mutants"] == len(expected)
    assert [len(shard) for shard in data["shards"]] == expected_sizes
    assert names_from(result) == expected


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
    assert list(data) == ["functions", "sources", "mutants", "shards"]
    assert data["functions"] == list(SELECTED)
    assert data["sources"] == ["sample/other.py", "sample/__init__.py"]
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


def test_plan_keeps_given_scope_order_and_omits_unmutated_functions(
    tmp_path, monkeypatch
):
    fixture_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = invoke(
        "mutation-plan",
        "--source",
        "sample/__init__.py",
        "--source",
        "sample/other.py",
        "--selected-function",
        "sample.x_add",
        "--selected-function",
        "sample.x_unknown",
        "--selected-function",
        "sample.other.x_twice",
        "--per-shard",
        2,
    )
    assert (result.returncode, result.stderr) == (0, "")
    data = json.loads(result.stdout)
    assert list(data) == ["functions", "sources", "mutants", "shards"]
    assert data["functions"] == ["sample.x_add", "sample.other.x_twice"]
    assert data["sources"] == ["sample/__init__.py", "sample/other.py"]
    expected = [
        get_mutant_name(path, name)
        for path, source, function in (
            (Path("sample/__init__.py"), ADD_SOURCE, "x_add"),
            (Path("sample/other.py"), OTHER_SOURCE, "x_twice"),
        )
        for name in mutate_file_contents(str(path), source).mutant_names
        if name.startswith(function + "__mutmut_")
    ]
    assert names_from(result) == expected
    assert data["mutants"] == len(expected)


@pytest.mark.parametrize(
    "count, size, code, shards", [(256, 1, 0, 256), (257, 1, 2, None), (257, 2, 0, 129)]
)
def test_plan_matrix_limit(tmp_path, monkeypatch, count, size, code, shards):
    source = "".join(f"def f{index:03}(a):\n    return -a\n" for index in range(count))
    (tmp_path / "many.py").write_text(source)
    monkeypatch.chdir(tmp_path)
    listed = mutate_file_contents("many.py", source).mutant_names
    assert len(listed) == count
    result = invoke(
        "mutation-plan",
        "--source",
        "many.py",
        *(
            part
            for index in range(count)
            for part in ("--selected-function", f"many.x_f{index:03}")
        ),
        "--per-shard",
        size,
    )
    assert result.returncode == code
    if code:
        assert (result.stdout, result.stderr) == (
            "",
            f"configuration error: {count} mutants need {count} shards, more than the 256 jobs GitHub runs from one matrix\n",
        )
    else:
        data = json.loads(result.stdout)
        assert data["mutants"] == len(listed)
        assert len(data["shards"]) == shards


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
        '{"functions":[],"sources":["sample/other.py"],"mutants":0,"shards":[]}\n',
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


def test_merge_preserves_mutants_without_results(tmp_path):
    first, second, target = (tmp_path / name for name in ("one", "two", "merged"))
    write_meta(
        first / "pkg/f.py.meta",
        {"pkg.f.x__mutmut_1": 1, "pkg.f.x__mutmut_2": None},
    )
    write_meta(
        second / "pkg/f.py.meta",
        {"pkg.f.x__mutmut_1": None, "pkg.f.x__mutmut_2": None},
    )
    result = invoke(
        "mutation-merge", "--into", target, "--shard", first, "--shard", second
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "merged 2 shards: 1 mutants with results\n",
        "",
    )
    assert json.loads((target / "pkg/f.py.meta").read_text()) == {
        "exit_code_by_key": {"pkg.f.x__mutmut_1": 1, "pkg.f.x__mutmut_2": None}
    }


def test_merge_two_result_files_per_shard(tmp_path):
    first, second, target = (tmp_path / name for name in ("one", "two", "merged"))
    for shard, status in ((first, 1), (second, None)):
        write_meta(shard / "pkg/f.py.meta", {"pkg.f.x__mutmut_1": status})
        write_meta(shard / "pkg/g.py.meta", {"pkg.g.y__mutmut_1": status})
    result = invoke(
        "mutation-merge", "--into", target, "--shard", first, "--shard", second
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "merged 2 shards: 2 mutants with results\n",
        "",
    )
    assert sorted(
        path.relative_to(target).as_posix() for path in target.rglob("*.meta")
    ) == ["pkg/f.py.meta", "pkg/g.py.meta"]
    assert json.loads((target / "pkg/f.py.meta").read_text()) == {
        "exit_code_by_key": {"pkg.f.x__mutmut_1": 1}
    }
    assert json.loads((target / "pkg/g.py.meta").read_text()) == {
        "exit_code_by_key": {"pkg.g.y__mutmut_1": 1}
    }


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


def test_survivors_non_python_source_is_stale(tmp_path, monkeypatch):
    result = survivors_case(
        tmp_path,
        monkeypatch,
        ["pkg/f.py", "pkg/g.txt"],
        ["pkg/f.py", "pkg/g.txt"],
        "pkg.g.y__mutmut_1",
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        1,
        "stale mutant: pkg.g.y__mutmut_1\n",
        "",
    )


def test_survivors_name_without_mutmut_suffix_is_stale(tmp_path, monkeypatch):
    result = survivors_case(
        tmp_path, monkeypatch, ["pkg"], ["pkg/f.py", "pkg/g.py"], "pkg.g.y"
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        1,
        "stale mutant: pkg.g.y\n",
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


def test_survivors_source_config_checks_version_with_existing_sources(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("importlib.metadata.version", lambda name: "3.9.0")
    result = survivors_case(
        tmp_path, monkeypatch, ["pkg"], ["pkg/f.py", "pkg/g.py"], "pkg.g.y__mutmut_1"
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


def test_mutation_config_preserves_fixture_and_source_order(tmp_path, monkeypatch):
    fixture_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    path = Path("pyproject.toml")
    before = path.read_text()
    result = invoke(
        "mutation-config",
        "--config",
        path,
        "--source",
        "sample/other.py",
        "--source",
        "sample/__init__.py",
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        'wrote only_mutate = ["sample/other.py", "sample/__init__.py"] to pyproject.toml\n',
        "",
    )
    expected = tomllib.loads(before)
    expected["tool"]["mutmut"]["only_mutate"] = [
        "sample/other.py",
        "sample/__init__.py",
    ]
    assert tomllib.loads(path.read_text()) == expected
    assert [
        line
        for line in difflib.ndiff(before.splitlines(), path.read_text().splitlines())
        if line.startswith(("+ ", "- "))
    ] == ['+ only_mutate = ["sample/other.py", "sample/__init__.py"]']


def test_mutation_config_preserves_repository_project(tmp_path, monkeypatch):
    root = Path(__file__).parent.parent
    project = tmp_path / "pyproject.toml"
    project.write_bytes((root / "pyproject.toml").read_bytes())
    (tmp_path / "ci_workflows").mkdir()
    (tmp_path / "ci_workflows/mutation.py").write_bytes(
        (root / "ci_workflows/mutation.py").read_bytes()
    )
    monkeypatch.chdir(tmp_path)
    before = project.read_text()
    result = invoke(
        "mutation-config",
        "--config",
        "pyproject.toml",
        "--source",
        "ci_workflows/mutation.py",
    )
    assert (result.returncode, result.stderr) == (0, "")
    expected = tomllib.loads(before)
    expected["tool"]["mutmut"]["only_mutate"] = ["ci_workflows/mutation.py"]
    assert tomllib.loads(project.read_text()) == expected
    assert [
        line
        for line in difflib.ndiff(before.splitlines(), project.read_text().splitlines())
        if line.startswith(("+ ", "- "))
    ] == ['+ only_mutate = ["ci_workflows/mutation.py"]']


@pytest.mark.parametrize(
    "kind, expected",
    [
        ("already", "pyproject.toml already sets only_mutate"),
        ("no-table", "pyproject.toml has no [tool.mutmut] table"),
        ("missing", "source not found: sample/missing.py"),
        ("non-python", "only_mutate takes Python files: sample/notes.txt"),
    ],
)
def test_mutation_config_refuses_without_writing(tmp_path, monkeypatch, kind, expected):
    fixture_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = Path("pyproject.toml")
    if kind == "already":
        config.write_text(config.read_text() + 'only_mutate = ["sample/other.py"]\n')
    elif kind == "no-table":
        config.write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    elif kind == "non-python":
        Path("sample/notes.txt").write_text("notes")
    source = {"missing": "sample/missing.py", "non-python": "sample/notes.txt"}.get(
        kind, "sample/__init__.py"
    )
    before = config.read_bytes()
    result = invoke("mutation-config", "--config", config, "--source", source)
    assert (result.returncode, result.stdout, result.stderr, config.read_bytes()) == (
        2,
        "",
        f"configuration error: {expected}\n",
        before,
    )


def test_mutation_config_missing_tomlkit(tmp_path, monkeypatch):
    fixture_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    original = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "tomlkit":
            raise ModuleNotFoundError("tomlkit")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    before = Path("pyproject.toml").read_bytes()
    result = invoke(
        "mutation-config", "--config", "pyproject.toml", "--source", "sample/other.py"
    )
    assert (
        result.returncode,
        result.stdout,
        result.stderr,
        Path("pyproject.toml").read_bytes(),
    ) == (
        2,
        "",
        "configuration error: tomlkit is required to write only_mutate\n",
        before,
    )


def test_source_paths_import_without_tomlkit(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.modules['tomlkit'] = None; from ci_workflows.checks import main; raise SystemExit(main(['source-paths', '--roots-json', '[\"pkg\"]']))",
        ],
        cwd=tmp_path,
        env={"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin", "HOME": str(home)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "pkg\n", "")


@pytest.mark.parametrize("missing", ["--config", "--source"])
def test_mutation_config_requires_arguments(missing, capsys):
    args = (
        ["mutation-config", "--source", "f.py"]
        if missing == "--config"
        else ["mutation-config", "--config", "p.toml"]
    )
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2
    assert f"the following arguments are required: {missing}" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_merge_refuses_missing_or_file_shard(tmp_path, kind):
    first, second, target = (tmp_path / name for name in ("one", "two", "merged"))
    write_meta(first / "pkg/f.py.meta", {"pkg.f.x__mutmut_1": 1})
    if kind == "file":
        second.write_text("not a folder")
    result = invoke(
        "mutation-merge", "--into", target, "--shard", first, "--shard", second
    )
    assert (result.returncode, result.stdout, result.stderr, target.exists()) == (
        2,
        "",
        f"configuration error: shard results not found: {second}\n",
        False,
    )


def checks(*extra, tests="success", plan_result="success", mutate="success"):
    return invoke(
        "mutation-checks",
        "--tests",
        tests,
        "--plan",
        plan_result,
        "--mutate",
        mutate,
        *extra,
    )


@pytest.mark.parametrize(
    "tests,code", [("failure", 1), ("cancelled", 2), ("skipped", 2)]
)
def test_checks_judges_tests_first(tests, code):
    result = checks(tests=tests)
    assert (result.returncode, result.stdout, result.stderr) == (
        code,
        f"tests job ended {tests}\n",
        "",
    )


def test_checks_tests_precede_private():
    result = checks(
        "--private", tests="failure", plan_result="skipped", mutate="skipped"
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        1,
        "tests job ended failure\n",
        "",
    )


@pytest.mark.parametrize(
    "plan_result,mutate,code,message",
    [
        ("skipped", "skipped", 0, "mutation skipped in a private repository"),
        (
            "success",
            "skipped",
            2,
            "configuration error: a private repository's plan and shards must be skipped, not plan success and shards skipped",
        ),
    ],
)
def test_checks_private(plan_result, mutate, code, message):
    result = checks("--private", plan_result=plan_result, mutate=mutate)
    assert (result.returncode, result.stdout, result.stderr) == (
        code,
        message + "\n",
        "",
    )


@pytest.mark.parametrize("plan_result,code", [("failure", 1), ("cancelled", 2)])
def test_checks_plan_result(plan_result, code):
    result = checks(plan_result=plan_result, mutate="skipped")
    assert (result.returncode, result.stdout, result.stderr) == (
        code,
        f"mutation plan job ended {plan_result}\n",
        "",
    )


def test_checks_plan_precedes_skip():
    result = checks(
        "--skipped",
        "push to the default branch main",
        plan_result="failure",
        mutate="skipped",
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        1,
        "mutation plan job ended failure\n",
        "",
    )


@pytest.mark.parametrize(
    "mutate,code,message",
    [
        ("skipped", 0, "mutation skipped: push to the default branch main"),
        (
            "success",
            2,
            "configuration error: the plan skipped mutation, but the shards ended success",
        ),
    ],
)
def test_checks_explicit_skip(mutate, code, message):
    result = checks("--skipped", "push to the default branch main", mutate=mutate)
    assert (result.returncode, result.stdout, result.stderr) == (
        code,
        message + "\n",
        "",
    )


def test_checks_private_and_skipped_are_exclusive(capsys):
    with pytest.raises(SystemExit) as error:
        main(
            [
                "mutation-checks",
                "--tests",
                "success",
                "--plan",
                "success",
                "--mutate",
                "skipped",
                "--private",
                "--skipped",
                "reason",
            ]
        )
    assert error.value.code == 2
    assert (
        "argument --skipped: not allowed with argument --private"
        in capsys.readouterr().err
    )


def test_checks_requires_result_files():
    result = checks(
        "--plan-file",
        "plan.json",
        "--shards",
        "shards",
        "--source-config",
        "pyproject.toml",
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        2,
        "configuration error: --plan-file, --shards, --survivors-file and --source-config are required unless mutation was skipped\n",
        "",
    )


def checks_project(tmp_path, monkeypatch, empty=False):
    source = tmp_path / "source"
    source.mkdir()
    fixture_project(source)
    monkeypatch.chdir(source)
    result = (
        invoke(
            "mutation-plan",
            "--source",
            "sample/other.py",
            "--selected-function",
            "sample.x_unknown",
        )
        if empty
        else plan("--per-shard", 2)
    )
    assert result.returncode == 0
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(result.stdout)
    shards = tmp_path / "shards"
    survivors = tmp_path / "survivors.yml"
    survivors.write_text("[]\n")
    options = (
        "--plan-file",
        plan_file,
        "--shards",
        shards,
        "--survivors-file",
        survivors,
        "--source-config",
        "pyproject.toml",
    )
    return source, json.loads(result.stdout), shards, survivors, options


@pytest.mark.parametrize(
    "mutate,code,message",
    [
        ("skipped", 0, "mutation skipped: the selected functions have no mutants"),
        (
            "success",
            2,
            "configuration error: the plan has no shards, but the shards ended success",
        ),
    ],
)
def test_checks_empty_real_plan(tmp_path, monkeypatch, mutate, code, message):
    _, data, _, _, options = checks_project(tmp_path, monkeypatch, empty=True)
    assert data["shards"] == []
    result = checks(*options, mutate=mutate)
    assert (result.returncode, result.stdout, result.stderr) == (
        code,
        message + "\n",
        "",
    )


@pytest.mark.parametrize("mutate,code", [("failure", 1), ("cancelled", 2)])
def test_checks_shard_result(tmp_path, monkeypatch, mutate, code):
    _, data, _, _, options = checks_project(tmp_path, monkeypatch)
    assert len(data["shards"]) == 3
    result = checks(*options, mutate=mutate)
    assert (result.returncode, result.stdout, result.stderr) == (
        code,
        f"mutation shards ended {mutate}\n",
        "",
    )


def write_check_shards(shards, data, survivor=None, unselected=False):
    selected = {name for group in data["shards"] for name in group}
    for index, group in enumerate(data["shards"]):
        for path, source in (
            (Path("sample/__init__.py"), ADD_SOURCE),
            (Path("sample/other.py"), OTHER_SOURCE),
        ):
            names = [
                get_mutant_name(path, name)
                for name in mutate_file_contents(str(path), source).mutant_names
            ]
            statuses = {
                name: (0 if name == survivor else 1)
                if name in group or (unselected and index == 0 and name == survivor)
                else None
                for name in names
            }
            write_meta(shards / str(index) / (str(path) + ".meta"), statuses)
    assert len(selected) == data["mutants"]


@pytest.mark.parametrize(
    "case",
    [
        "accepted",
        "selected-survivor",
        "unselected-survivor",
        "unmutated-file",
        "missing-middle",
    ],
)
def test_checks_merges_and_judges_survivors(tmp_path, monkeypatch, case):
    source, data, shards, survivors, options = checks_project(tmp_path, monkeypatch)
    assert (data["mutants"], len(data["shards"])) == (6, 3)
    selected_survivor = next(
        name
        for group in data["shards"]
        for name in group
        if name.startswith("sample.x_add__mutmut_")
    )
    unselected_survivor = next(
        get_mutant_name(Path("sample/__init__.py"), name)
        for name in mutate_file_contents("sample/__init__.py", ADD_SOURCE).mutant_names
        if name.startswith("x_unchanged__mutmut_")
    )
    survivor = (
        selected_survivor
        if case == "selected-survivor"
        else unselected_survivor
        if case == "unselected-survivor"
        else None
    )
    write_check_shards(shards, data, survivor, case == "unselected-survivor")
    if case == "missing-middle":
        import shutil

        shutil.rmtree(shards / "1")
    if case == "unmutated-file":
        (source / "sample/extra.py").write_text("def spare(a):\n    return -a\n")
        survivors.write_text(
            "- mutant: sample.extra.x_spare__mutmut_1\n  reason: Kept for this case.\n"
        )
    temp = tmp_path / "temporary"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    before = sorted(p.relative_to(source) for p in source.rglob("*"))
    result = checks(*options)
    if case == "missing-middle":
        expected = (
            2,
            f"configuration error: shard results not found: {shards / '1'}\n",
            "",
        )
    elif case == "selected-survivor":
        expected = (
            1,
            f"merged 3 shards: 6 mutants with results\nunexplained mutant: {survivor}\n",
            "",
        )
    elif case == "unselected-survivor":
        expected = (
            0,
            "merged 3 shards: 7 mutants with results\nmutation survivors accepted\n",
            "",
        )
    else:
        expected = (
            0,
            "merged 3 shards: 6 mutants with results\nmutation survivors accepted\n",
            "",
        )
    assert (result.returncode, result.stdout, result.stderr) == expected
    assert list(temp.iterdir()) == []
    assert sorted(p.relative_to(source) for p in source.rglob("*")) == before


@pytest.mark.parametrize("missing", ["--tests", "--plan", "--mutate"])
def test_checks_requires_job_results(missing, capsys):
    args = ["mutation-checks"]
    for option in ("--tests", "--plan", "--mutate"):
        if option != missing:
            args.extend((option, "success"))
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2
    assert f"the following arguments are required: {missing}" in capsys.readouterr().err


def test_checks_rejects_unknown_result(capsys):
    with pytest.raises(SystemExit) as error:
        main(
            [
                "mutation-checks",
                "--tests",
                "neutral",
                "--plan",
                "success",
                "--mutate",
                "success",
            ]
        )
    assert error.value.code == 2
    assert "argument --tests: invalid choice: 'neutral'" in capsys.readouterr().err
