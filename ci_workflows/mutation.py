"""Plan mutmut shards and combine their result files."""

from __future__ import annotations

import ast
import json
from importlib.metadata import PackageNotFoundError
from pathlib import Path

from ci_workflows.errors import ConfigurationError


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
    base, extra = divmod(count, shard_count) if shard_count else (0, 0)
    shards = []
    start = 0
    for index in range(shard_count):
        end = start + base + (index < extra)
        shards.append(names[start:end])
        start = end
    return {"mutants": count, "shards": shards}


def merge(into: Path, shards: list[Path]) -> str:
    if into.exists():
        raise ConfigurationError(f"merge target already exists: {into}")

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
