"""Coverage and mutation gates used by reusable workflows."""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import yaml
from unidiff import PatchSet

from ci_workflows.errors import ConfigurationError
from ci_workflows.rules import alert_cases
from ci_workflows.tailnet import policy_cases
from ci_workflows.survivors import load_explanations


def _changed_lines(patch_text: str) -> dict[str, set[int]]:
    patches = PatchSet(io.StringIO(patch_text))
    return {
        patched.path: {
            line.target_line_no
            for hunk in patched
            for line in hunk
            if line.is_added and line.target_line_no is not None
        }
        for patched in patches
    }


def _changed_function_lines(patch_text: str) -> dict[str, set[int]]:
    changed: dict[str, set[int]] = {}
    for patched in PatchSet(io.StringIO(patch_text)):
        lines = changed.setdefault(patched.path, set())
        for hunk in patched:
            target_line = hunk.target_start + (hunk.target_length == 0)
            for line in hunk:
                if line.is_added:
                    lines.add(target_line)
                elif line.is_removed:
                    lines.update((target_line - 1, target_line))
                if not line.is_removed:
                    target_line += 1
    return changed


def _source_path(value: str) -> str:
    path = Path(value)
    return (
        str(path.relative_to(Path.cwd()))
        if path.is_absolute() and path.is_relative_to(Path.cwd())
        else value
    )


def _repository_root() -> Path:
    current = Path.cwd()
    return next(
        (path for path in (current, *current.parents) if (path / ".git").exists()),
        current,
    )


def _javascript_test_path(value: str) -> bool:
    path = Path(value)
    return (
        any(part in {"test", "tests"} for part in path.parts[:-1])
        or ".test." in path.name
        or ".spec." in path.name
    )


def _normalize_lcov_report(report: Path) -> tuple[set[str], str]:
    """Keep the standard report and prepare one line verdict per measured source line."""
    paths: set[str] = set()
    original: list[str] = []
    gate: list[str] = []
    source: str | None = None
    hits: dict[int, int] = {}
    branches: dict[int, list[bool]] = {}
    for row in report.read_text().splitlines():
        tag, _, value = row.partition(":")
        if tag == "SF":
            source = _source_path(value)
            paths.add(source)
            row = f"SF:{source}"
            hits = {}
            branches = {}
        elif tag == "DA" and source is not None:
            number, count, *_ = value.split(",")
            hits[int(number)] = int(count)
        elif tag == "BRDA" and source is not None:
            number, _, _, taken = value.split(",")
            branches.setdefault(int(number), []).append(taken != "-" and int(taken) > 0)
        elif row == "end_of_record" and source is not None:
            gate.append(f"SF:{source}")
            for number in sorted(hits.keys() | branches.keys()):
                outcomes = branches.get(number, [])
                ran = hits[number] > 0 if number in hits else any(outcomes)
                gate.append(f"DA:{number},{int(ran and all(outcomes))}")
            gate.append("end_of_record")
            source = None
        original.append(row)
    report.write_text("\n".join(original) + "\n")
    return paths, "\n".join(gate) + "\n"


def _normalize_coverage_report(report: Path, kind: str) -> set[str]:
    """Put XML source names in the same repository coordinate system as git."""
    paths: set[str] = set()

    tree = ET.parse(report)
    root = tree.getroot()
    if kind == "powershell":
        for package in root.findall("package"):
            directory = Path(_source_path(package.attrib.get("name", "")))
            package.set("name", directory.as_posix())
            paths.update(
                (directory / item.attrib["name"]).as_posix()
                for item in package.findall("sourcefile")
            )
    else:
        roots = [
            Path(item.text) for item in root.findall("./sources/source") if item.text
        ]
        for cls in root.findall(".//class"):
            filename = Path(cls.attrib["filename"])
            if not filename.is_absolute():
                filename = next(
                    (base / filename for base in roots if (base / filename).is_file()),
                    filename,
                )
            name = _source_path(str(filename))
            cls.set("filename", name)
            paths.add(name)
            if kind == "python":
                for line in cls.findall("./lines/line"):
                    branch = line.attrib.get("condition-coverage")
                    if branch is not None and not branch.startswith("100% "):
                        line.set("hits", "0")
        sources = root.find("sources")
        if sources is not None:
            root.remove(sources)
    tree.write(report, encoding="unicode")
    return paths


def changed_lines(
    report: Path,
    patch_file: Path,
    kind: str,
    source_root: Path | None = None,
    source_config: Path | None = None,
    source_roots_json: str | None = None,
) -> tuple[int, str]:
    patch_text = patch_file.read_text()
    changed = _changed_lines(patch_text)
    suffixes = {
        "python": (".py",),
        "javascript": (".js", ".mjs", ".cjs"),
        "shell": (".sh", ".bash"),
        "powershell": (".ps1", ".psm1"),
    }
    names = {
        "python": "Python",
        "javascript": "JavaScript",
        "shell": "shell",
        "powershell": "PowerShell",
    }
    language_changes = {
        path: lines
        for path, lines in changed.items()
        if path.endswith(suffixes[kind]) and lines
    }
    allowed = set(language_changes)
    if source_config is not None or source_roots_json is not None:
        allowed = set(
            filter_sources(
                source_config,
                list(language_changes),
                source_roots_json,
                list(suffixes[kind]),
            )
        )
    elif source_root is not None:
        root_path = Path(_source_path(str(source_root)))
        allowed = {
            path for path in language_changes if Path(path).is_relative_to(root_path)
        }
    if kind == "javascript":
        allowed = {path for path in allowed if not _javascript_test_path(path)}
    skipped = sorted(set(language_changes) - allowed)
    language_changes = {
        path: lines for path, lines in language_changes.items() if path in allowed
    }
    suffix = (
        "; skipped changed source outside declared paths: " + ", ".join(skipped)
        if skipped
        else ""
    )
    if not language_changes:
        return 0, f"no changed {names[kind]} lines to check{suffix}"
    if kind == "javascript":
        report_paths, gate_lcov = _normalize_lcov_report(report)
    else:
        report_paths = _normalize_coverage_report(report, kind)
        gate_lcov = None
    absent = sorted(set(language_changes) - report_paths)
    if absent:
        raise ConfigurationError(
            "changed source absent from coverage: " + ", ".join(absent)
        )
    selected = "".join(
        str(item)
        for item in PatchSet(io.StringIO(patch_text))
        if item.path in language_changes
    )
    with tempfile.TemporaryDirectory() as directory:
        gate_report = report.resolve()
        if gate_lcov is not None:
            gate_report = Path(directory) / "changed.lcov"
            gate_report.write_text(gate_lcov)
        repository_root = _repository_root()
        diff_cover = subprocess.run(
            [
                str(Path(sys.executable).with_name("diff-cover")),
                str(gate_report),
                "--diff-file",
                "/dev/stdin",
                "--fail-under",
                "100",
                "--src-roots",
                ".",
                "--format",
                "json:/dev/stdout",
                "-q",
            ],
            input=selected,
            capture_output=True,
            text=True,
            check=False,
            **({"cwd": repository_root} if repository_root != Path.cwd() else {}),
        )
    if diff_cover.returncode not in (0, 1):
        raise ConfigurationError(f"diff-cover failed: {diff_cover.stderr.strip()}")
    try:
        summary = json.loads(diff_cover.stdout)
    except (json.JSONDecodeError, AttributeError) as exc:
        raise ConfigurationError("diff-cover did not return a JSON report") from exc
    missing = sorted(
        f"{path}:{number}"
        for path, stats in summary["src_stats"].items()
        for number in stats["violation_lines"]
    )
    if missing:
        return diff_cover.returncode, "uncovered changed lines: " + ", ".join(
            missing
        ) + suffix
    return diff_cover.returncode, "all changed lines covered" + suffix


def _floor(value: str) -> Decimal:
    try:
        floor = Decimal(value)
    except InvalidOperation as exc:
        raise ConfigurationError(f"invalid coverage floor {value!r}") from exc
    if not floor.is_finite() or floor < 0 or floor > 100:
        raise ConfigurationError(f"invalid coverage floor {value!r}")
    return floor


def _base_floor(workflow: Path, caller_job: str | None) -> Decimal | None:
    document = yaml.load(workflow.read_text(), Loader=yaml.BaseLoader)
    if not isinstance(document, dict):
        raise ConfigurationError("base workflow must be a YAML mapping")
    jobs = document.get("jobs", {})
    if caller_job is not None:
        if not isinstance(jobs, dict) or caller_job not in jobs:
            raise ConfigurationError(f"base workflow has no job {caller_job!r}")
        job = jobs[caller_job]
        if not isinstance(job, dict) or not isinstance(job.get("with"), dict):
            return None
        value = job["with"].get("coverage_floor")
        return _floor(value) if value is not None else None
    values = {
        job["with"]["coverage_floor"]
        for job in jobs.values()
        if isinstance(job, dict)
        and isinstance(job.get("with"), dict)
        and "coverage_floor" in job["with"]
    }
    if not values:
        return None
    if len(values) != 1:
        raise ConfigurationError("base workflow has multiple coverage floors")
    return _floor(values.pop())


def floor_check(
    report: Path,
    head_floor: str,
    base_workflow: Path | None,
    kind: str,
    caller_job: str | None = None,
) -> tuple[int, str]:
    head = _floor(head_floor)
    base = _base_floor(base_workflow, caller_job) if base_workflow else None
    if base is not None and head < base:
        return 1, f"coverage floor fell from {base:g}% to {head:g}%"
    if kind == "javascript":
        branch_total = branch_hits = 0
        for raw in report.read_text().splitlines():
            tag, separator, value = raw.partition(":")
            if separator and tag == "BRF":
                branch_total += int(value)
            elif separator and tag == "BRH":
                branch_hits += int(value)
        total = (
            Decimal(100)
            if branch_total == 0
            else Decimal(branch_hits) * 100 / branch_total
        )
    elif kind == "powershell":
        root = ET.parse(report).getroot()
        counter = next(
            (
                item
                for item in root.findall("counter")
                if item.attrib.get("type") == "LINE"
            ),
            None,
        )
        if counter is None:
            raise ConfigurationError("JaCoCo report has no LINE counter")
        covered, missed = int(counter.attrib["covered"]), int(counter.attrib["missed"])
        total = (
            Decimal(100)
            if covered + missed == 0
            else Decimal(covered) * 100 / (covered + missed)
        )
    else:
        root = ET.parse(report).getroot()
        metric = "branch-rate" if kind == "python" else "line-rate"
        try:
            total = Decimal(root.attrib[metric]) * 100
        except (KeyError, InvalidOperation) as exc:
            raise ConfigurationError(f"coverage report has no valid {metric}") from exc
    shown_total = f"{total:.1f}"
    if total < head:
        return 1, f"coverage {shown_total}% is below floor {head:g}%"
    return 0, f"coverage {shown_total}% meets floor {head:g}%"


def select_functions(
    source: Path, source_name: str, patch_file: Path, *, mutmut_names: bool
) -> tuple[list[str], list[str]]:
    changed = _changed_function_lines(patch_file.read_text()).get(source_name, set())
    module_name = ".".join(Path(source_name).with_suffix("").parts)
    source_text = source.read_text()
    tree = ast.parse(source_text, filename=source_name)
    selected: set[str] = set()
    gaps: set[str] = set()
    generated: set[str] = set()
    if mutmut_names:
        error = "mutmut 3.8.0 is required for Python function selection"
        try:
            if version("mutmut") != "3.8.0":
                raise ConfigurationError(error)
            from mutmut.mutation.file_mutation import mutate_file_contents
            from mutmut.mutation.pragma_handling import PragmaParseError
            from mutmut.utils.format_utils import get_mutant_name, make_mutant_key
        except (ImportError, PackageNotFoundError) as exc:
            raise ConfigurationError(error) from exc

        try:
            generated = {
                get_mutant_name(Path(source_name), name.partition("__mutmut_")[0])
                for name in mutate_file_contents(source_name, source_text).mutant_names
            }
        except PragmaParseError as exc:
            raise ConfigurationError(str(exc)) from exc

    def visit(
        body: list[ast.stmt],
        parent: str,
        owner: str | None = None,
        class_name: str | None = None,
    ) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = min(
                    [node.lineno, *(item.lineno for item in node.decorator_list)]
                )
                dotted_name = f"{parent}.{node.name}"
                candidate = (
                    owner
                    or get_mutant_name(
                        Path(source_name), make_mutant_key(node.name, class_name)
                    )
                    if mutmut_names
                    else dotted_name
                )
                if any(start <= number <= node.end_lineno for number in changed):
                    if mutmut_names:
                        if candidate in generated:
                            selected.add(candidate)
                        else:
                            gaps.add(dotted_name)
                    else:
                        selected.add(dotted_name)
                visit(node.body, dotted_name, candidate if mutmut_names else None)
            elif isinstance(node, ast.ClassDef):
                visit(node.body, f"{parent}.{node.name}", owner, node.name)

    visit(tree.body, module_name)
    return sorted(selected), sorted(gaps)


def survivor_check(
    results: Path,
    survivors_file: Path,
    result_format: str,
    selected_functions: list[str] | None = None,
    selected_files: list[str] | None = None,
) -> tuple[int, str]:
    statuses: dict[str, int | str | None] = {}
    if result_format == "mutmut":
        files = (
            sorted(
                file
                for file in results.rglob("*.meta")
                if not any(
                    part.startswith(".") for part in file.relative_to(results).parts
                )
            )
            if results.is_dir()
            else [results]
        )
        for file in files:
            data = json.loads(file.read_text())
            found = data["exit_code_by_key"]
            if not isinstance(found, dict) or statuses.keys() & found.keys():
                raise ConfigurationError(
                    "mutmut result statuses must be unique mappings"
                )
            statuses.update(found)
    else:
        data = json.loads(results.read_text())
        for filename, file_result in data["files"].items():
            for mutant in file_result["mutants"]:
                location = mutant["location"]
                start, end = location["start"], location["end"]
                replacement = hashlib.sha256(
                    mutant["replacement"].encode()
                ).hexdigest()[:16]
                name = (
                    f"{filename}#{mutant['mutatorName']}@"
                    f"{start['line']}:{start['column']}-{end['line']}:{end['column']}"
                    f"~{replacement}"
                )
                if name in statuses:
                    raise ConfigurationError(f"duplicate Stryker survivor key: {name}")
                statuses[name] = mutant["status"]
    if selected_functions is None and selected_files is None:

        def in_scope(name: str) -> bool:
            return True

    else:
        prefixes = (
            tuple(f"{file}#" for file in selected_files)
            if result_format == "stryker"
            else tuple(f"{function}__mutmut_" for function in selected_functions)
        )

        def in_scope(name: str) -> bool:
            return name.startswith(prefixes)

    surviving_status = 0 if result_format == "mutmut" else "Survived"
    all_explanations = load_explanations(
        survivors_file,
        duplicate_error="survivor names must be unique non-empty strings",
        invalid_name_error="survivor names must be unique non-empty strings",
    )
    for name, reason in all_explanations.items():
        if not reason:
            return 1, f"blank reason: {name}"
    explanations = {
        name for name in all_explanations if not name.startswith("tailscale:")
    }
    if selected_functions is not None or selected_files is not None:
        selected = selected_files if result_format == "stryker" else selected_functions
        marker = "#" if result_format == "stryker" else "__mutmut_"
        missing = [
            name
            for name in selected or []
            if not any(result.startswith(f"{name}{marker}") for result in statuses)
        ]
        if missing:
            label = "files" if result_format == "stryker" else "functions"
            return 1, f"selected {label} have no mutation results: {', '.join(missing)}"
    no_tests_statuses = {None, 5, 33} if result_format == "mutmut" else {"NoCoverage"}
    no_tests = sorted(
        name
        for name, status in statuses.items()
        if status in no_tests_statuses and in_scope(name)
    )
    if no_tests:
        return 1, "mutant ran without tests: " + ", ".join(no_tests)
    survivors = {
        name
        for name, status in statuses.items()
        if status == surviving_status and in_scope(name)
    }

    def still_valid(name: str) -> bool:
        if name in statuses:
            return statuses[name] == surviving_status or (
                result_format == "mutmut"
                and statuses[name] is None
                and not in_scope(name)
            )
        return (
            result_format == "stryker"
            and not in_scope(name)
            and "#" in name
            and Path(name.partition("#")[0]).is_file()
        )

    stale = sorted(name for name in explanations if not still_valid(name))
    if stale:
        return 1, "stale mutant: " + ", ".join(stale)
    unexplained = sorted(survivors - explanations)
    if unexplained:
        return 1, "unexplained mutant: " + ", ".join(unexplained)
    return 0, "mutation survivors accepted"


def filter_sources(
    config: Path | None,
    changed_files: list[str],
    roots_json: str | None = None,
    extensions: list[str] | None = None,
) -> list[str]:
    if config is not None:
        document = tomllib.loads(config.read_text())
        roots = document.get("tool", {}).get("mutmut", {}).get("source_paths")
        error = "mutmut source_paths must be a non-empty list"
    else:
        roots = json.loads(roots_json)
        error = "mutation source paths must be a non-empty list"
    if (
        not isinstance(roots, list)
        or not roots
        or not all(isinstance(root, str) and root for root in roots)
    ):
        raise ConfigurationError(error)
    source_paths = [Path(root) for root in roots]
    allowed_extensions = extensions or [".py"]
    return [
        name
        for name in changed_files
        if name
        and Path(name).suffix in allowed_extensions
        and not (
            Path(name).suffix in {".js", ".mjs", ".cjs"} and _javascript_test_path(name)
        )
        and any(Path(name).is_relative_to(root) for root in source_paths)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ci-checks")
    commands = parser.add_subparsers(dest="command", required=True)
    changed = commands.add_parser("changed-lines")
    changed.add_argument("--report", type=Path, required=True)
    changed.add_argument("--diff-file", type=Path, required=True)
    changed.add_argument("--source-root", type=Path)
    changed.add_argument("--source-config", type=Path)
    changed.add_argument("--source-roots-json")
    changed.add_argument(
        "--kind",
        choices=["python", "javascript", "shell", "powershell"],
        default="python",
    )
    floor = commands.add_parser("floor")
    floor.add_argument("--report", type=Path, required=True)
    floor.add_argument("--head-floor", required=True)
    floor.add_argument("--base-workflow", type=Path)
    floor.add_argument("--caller-job")
    floor.add_argument(
        "--kind",
        choices=["python", "javascript", "shell", "powershell"],
        default="python",
    )
    functions = commands.add_parser("select-functions")
    functions.add_argument("--source", type=Path, required=True)
    functions.add_argument("--source-name", required=True)
    functions.add_argument("--diff-file", type=Path, required=True)
    functions.add_argument("--mutmut", action="store_true")
    survivors = commands.add_parser("survivors")
    survivors.add_argument("--format", choices=["mutmut", "stryker"], required=True)
    survivors.add_argument("--results", type=Path, required=True)
    survivors.add_argument("--survivors-file", type=Path, required=True)
    survivors.add_argument("--selected-function", action="append")
    survivors.add_argument("--selected-file", action="append")
    source_filter = commands.add_parser("filter-sources")
    roots = source_filter.add_mutually_exclusive_group(required=True)
    roots.add_argument("--config", type=Path)
    roots.add_argument("--roots-json")
    source_filter.add_argument("--extension", action="append")
    alerts = commands.add_parser("alerts")
    alerts.add_argument("--test-file", action="append", type=Path, required=True)
    policy = commands.add_parser("policy")
    policy.add_argument("--file", type=Path, required=True)
    policy.add_argument("--tailnet", default=os.environ.get("TS_TAILNET"))
    policy.add_argument(
        "--survivors-file", type=Path, default=Path(".mutation-survivors.yml")
    )
    policy.add_argument("--api-base-url", default="https://api.tailscale.com")
    policy.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args(argv)
    error_stream = (
        sys.stderr
        if args.command in {"select-functions", "filter-sources"}
        else sys.stdout
    )
    try:
        if args.command == "changed-lines":
            code, message = changed_lines(
                args.report,
                args.diff_file,
                args.kind,
                args.source_root,
                args.source_config,
                args.source_roots_json,
            )
        elif args.command == "floor":
            code, message = floor_check(
                args.report,
                args.head_floor,
                args.base_workflow,
                args.kind,
                args.caller_job,
            )
        elif args.command == "select-functions":
            selected, gaps = select_functions(
                args.source,
                args.source_name,
                args.diff_file,
                mutmut_names=args.mutmut,
            )
            for name in gaps:
                print(
                    f"mutation tool gap: {name} (mutmut generated no mutants)",
                    file=sys.stderr,
                )
            print(json.dumps(selected))
            return 0
        elif args.command == "filter-sources":
            for name in filter_sources(
                args.config,
                sys.stdin.read().splitlines(),
                args.roots_json,
                args.extension,
            ):
                print(name)
            return 0
        elif args.command == "alerts":
            code, message = alert_cases(args.test_file)
        elif args.command == "policy":
            code, message = policy_cases(
                args.file,
                args.tailnet,
                os.environ.get("TS_ACCESS_TOKEN", ""),
                args.survivors_file,
                args.api_base_url,
                args.timeout,
            )
        else:
            code, message = survivor_check(
                args.results,
                args.survivors_file,
                args.format,
                args.selected_function,
                args.selected_file,
            )
    except SyntaxError as exc:
        print(
            f"configuration error: invalid Python source {exc.filename}:{exc.lineno}",
            file=error_stream,
        )
        return 2
    except (
        ConfigurationError,
        ValueError,
        ET.ParseError,
        yaml.YAMLError,
        json.JSONDecodeError,
        OSError,
        tomllib.TOMLDecodeError,
    ) as exc:
        print(f"configuration error: {exc}", file=error_stream)
        return 2
    print(message)
    return code
