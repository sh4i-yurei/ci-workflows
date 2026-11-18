"""Configure, plan and judge mutmut shards, and combine their result files."""

from __future__ import annotations

import ast
import json
from importlib.metadata import PackageNotFoundError
from pathlib import Path

from ci_workflows.errors import ConfigurationError


def configure(config: Path, sources: list[str]) -> str:
    for source in sources:
        if not Path(source).is_file():
            raise ConfigurationError(f"source not found: {source}")
        if not source.endswith(".py"):
            raise ConfigurationError(f"only_mutate takes Python files: {source}")
    try:
        import tomlkit
    except ImportError as exc:
        raise ConfigurationError("tomlkit is required to write only_mutate") from exc
    document = tomlkit.parse(config.read_text())
    table = document.get("tool", {}).get("mutmut")
    if table is None:
        raise ConfigurationError(f"{config} has no [tool.mutmut] table")
    if "only_mutate" in table:
        raise ConfigurationError(f"{config} already sets only_mutate")
    table["only_mutate"] = sources
    config.write_text(tomlkit.dumps(document))
    return f"wrote only_mutate = {json.dumps(sources)} to {config}"


def load_mutmut(error: str):
    """Load the pinned mutmut API only for commands that require it."""
    from importlib.metadata import version

    try:
        if version("mutmut") != "3.8.0":
            raise ConfigurationError(error)
        from mutmut.mutation.file_mutation import mutate_file_contents
        from mutmut.mutation.pragma_handling import PragmaParseError
        from mutmut.utils.format_utils import get_mutant_name, make_mutant_key
    except (ImportError, PackageNotFoundError) as exc:
        raise ConfigurationError(error) from exc
    return mutate_file_contents, PragmaParseError, get_mutant_name, make_mutant_key


def plan(sources: list[str], selected_functions: list[str], per_shard: int) -> dict:
    mutate_file_contents, PragmaParseError, get_mutant_name, _ = load_mutmut(
        "mutmut 3.8.0 is required for mutation planning"
    )
    if per_shard <= 0:
        raise ConfigurationError("--per-shard must be a positive whole number")
    prefixes = tuple(f"{function}__mutmut_" for function in selected_functions)
    names: list[str] = []
    for source in sources:
        path = Path(source)
        if not path.is_file():
            raise ConfigurationError(f"source not found: {source}")
        text = path.read_text()
        ast.parse(text, filename=source)
        try:
            generated = mutate_file_contents(source, text).mutant_names
        except PragmaParseError as exc:
            raise ConfigurationError(str(exc)) from exc
        names.extend(
            name
            for mutant in generated
            if (name := get_mutant_name(path, mutant)).startswith(prefixes)
        )
    count = len(names)
    shard_count = (count + per_shard - 1) // per_shard
    if shard_count > 256:
        raise ConfigurationError(
            f"{count} mutants need {shard_count} shards, more than the 256 jobs GitHub runs from one matrix"
        )
    base, extra = divmod(count, shard_count) if shard_count else (0, 0)
    shards = []
    start = 0
    for index in range(shard_count):
        end = start + base + (index < extra)
        shards.append(names[start:end])
        start = end
    functions = [
        function
        for function in selected_functions
        if any(name.startswith(f"{function}__mutmut_") for name in names)
    ]
    return {
        "functions": functions,
        "sources": sources,
        "mutants": count,
        "shards": shards,
    }


def checks_verdict(
    tests: str,
    plan_result: str,
    mutate: str,
    private: bool,
    skipped: str | None,
    plan_file: Path | None,
    shards: Path | None,
    survivors_file: Path | None,
    source_config: Path | None,
) -> tuple[int, str] | dict:
    if tests != "success":
        return (1 if tests == "failure" else 2), f"tests job ended {tests}"
    if private:
        if plan_result == mutate == "skipped":
            return 0, "mutation skipped in a private repository"
        return 2, (
            "configuration error: a private repository's plan and shards must be skipped, "
            f"not plan {plan_result} and shards {mutate}"
        )
    if plan_result != "success":
        return (
            1 if plan_result == "failure" else 2
        ), f"mutation plan job ended {plan_result}"
    if skipped is not None:
        if mutate == "skipped":
            return 0, f"mutation skipped: {skipped}"
        return (
            2,
            f"configuration error: the plan skipped mutation, but the shards ended {mutate}",
        )
    if None in (plan_file, shards, survivors_file, source_config):
        return 2, (
            "configuration error: --plan-file, --shards, --survivors-file and "
            "--source-config are required unless mutation was skipped"
        )
    data = json.loads(plan_file.read_text())
    if not data["shards"]:
        if mutate == "skipped":
            return 0, "mutation skipped: the selected functions have no mutants"
        return (
            2,
            f"configuration error: the plan has no shards, but the shards ended {mutate}",
        )
    if mutate != "success":
        return (1 if mutate == "failure" else 2), f"mutation shards ended {mutate}"
    return data


def merge(into: Path, shards: list[Path]) -> str:
    if into.exists():
        raise ConfigurationError(f"merge target already exists: {into}")
    for shard in shards:
        if not shard.is_dir():
            raise ConfigurationError(f"shard results not found: {shard}")

    def files(shard: Path) -> dict[Path, Path]:
        return {
            path.relative_to(shard): path
            for path in shard.rglob("*.meta")
            if not any(part.startswith(".") for part in path.relative_to(shard).parts)
        }

    shard_files = [files(shard) for shard in shards]
    paths = set(shard_files[0])
    missing = sorted(
        set.union(*(set(group) for group in shard_files))
        - set.intersection(*(set(group) for group in shard_files))
    )
    if missing:
        raise ConfigurationError(
            "shards hold different result files: " + ", ".join(map(str, missing))
        )
    output = {}
    completed = 0
    for path in sorted(paths):
        documents = [json.loads(group[path].read_text()) for group in shard_files]
        statuses = []
        for document in documents:
            if not isinstance(document, dict) or not isinstance(
                document.get("exit_code_by_key"), dict
            ):
                raise ConfigurationError(
                    "mutmut result statuses must be unique mappings"
                )
            statuses.append(document["exit_code_by_key"])
        keys = statuses[0].keys()
        if any(group.keys() != keys for group in statuses[1:]):
            raise ConfigurationError(f"shards list different mutants in {path}")
        merged = {}
        for mutant in keys:
            found = [group[mutant] for group in statuses if group[mutant] is not None]
            if len(found) > 1:
                raise ConfigurationError(
                    f"mutant has results from more than one shard: {mutant}"
                )
            merged[mutant] = found[0] if found else None
            completed += bool(found)
        output[path] = {**documents[0], "exit_code_by_key": merged}
    for path, document in output.items():
        destination = into / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(document))
    return f"merged {len(shards)} shards: {completed} mutants with results"
