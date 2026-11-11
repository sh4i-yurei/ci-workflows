"""Compare the planner and merger with completed mutmut 3.8.0 runs."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

from test_mutation import SELECTED, fixture_project, invoke, names_from, plan


def run_mutmut(project, names, home):
    result = subprocess.run(
        [sys.executable, "-m", "mutmut", "run", "--max-children", "1", *names],
        cwd=project,
        env={"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin", "HOME": str(home)},
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    return project / "mutants"


def statuses(folder):
    return {
        file.relative_to(folder): json.loads(file.read_text())["exit_code_by_key"]
        for file in folder.rglob("*.meta")
        if not any(part.startswith(".") for part in file.relative_to(folder).parts)
    }


def planned_fixture(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    fixture_project(source)
    monkeypatch.chdir(source)
    result = plan("--per-shard", 3)
    assert (result.returncode, result.stderr) == (0, "")
    return source, json.loads(result.stdout)["shards"], names_from(result)


def test_plan_names_are_exactly_the_names_mutmut_runs(tmp_path, monkeypatch):
    source, _, names = planned_fixture(tmp_path, monkeypatch)
    project = tmp_path / "all"
    shutil.copytree(source, project)
    home = tmp_path / "home"
    home.mkdir()
    actual = statuses(run_mutmut(project, names, home))
    completed = {
        name
        for group in actual.values()
        for name, status in group.items()
        if status is not None
    }
    assert completed == set(names)


def test_merge_shards_matches_one_complete_mutmut_run(tmp_path, monkeypatch):
    source, shards, names = planned_fixture(tmp_path, monkeypatch)
    assert len(shards) >= 2
    home = tmp_path / "home"
    home.mkdir()
    all_project = tmp_path / "all"
    shutil.copytree(source, all_project)
    all_results = run_mutmut(all_project, names, home)
    shard_results = []
    for index, shard in enumerate(shards):
        project = tmp_path / f"shard-{index}"
        shutil.copytree(source, project)
        shard_results.append(run_mutmut(project, shard, home))
    merged = tmp_path / "merged"
    arguments = ["mutation-merge", "--into", merged]
    for folder in shard_results:
        arguments.extend(("--shard", folder))
    result = invoke(*arguments)
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        f"merged {len(shards)} shards: {len(names)} mutants with results\n",
        "",
    )
    assert statuses(merged) == statuses(all_results)
    empty = tmp_path / "empty.yml"
    empty.write_text("[]\n")

    def check(results):
        return invoke(
            "survivors",
            "--format",
            "mutmut",
            "--results",
            results,
            "--survivors-file",
            empty,
            "--selected-function",
            SELECTED[0],
            "--selected-function",
            SELECTED[1],
        )

    merged_check, full_check = check(merged), check(all_results)
    assert (merged_check.returncode, merged_check.stdout) == (
        full_check.returncode,
        full_check.stdout,
    )
