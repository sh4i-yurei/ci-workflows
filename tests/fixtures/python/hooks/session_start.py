"""Give each Codex session a short orientation from its current folder."""

import json
import subprocess
import sys
from pathlib import Path


def repository_line(cwd: Path) -> tuple[str, Path | None]:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        root = Path(result.stdout.strip())
        return f"Repository: {root.name}.", root
    except (OSError, subprocess.SubprocessError, ValueError):
        return "Repository: not in a git repository.", None


def environment_line() -> str:
    try:
        marker = Path("/run/.containerenv").read_text()
        if any(line.strip() == 'name="dev"' for line in marker.splitlines()):
            return "Environment: inside the dev container."
    except OSError:
        pass
    return "Environment: on the host; development tools, bw and Node live in dev."


def folder_instructions(cwd: Path, root: Path | None) -> list[Path]:
    if root is None:
        candidate = cwd / "AGENTS.md"
        return [candidate] if candidate.exists() else []
    found = []
    for folder in (cwd, *cwd.parents):
        candidate = folder / "AGENTS.md"
        if candidate.exists():
            found.append(candidate)
        if folder == root:
            break
    return found


def project_line(instructions: list[Path]) -> str:
    if not instructions:
        return "Project note: none named. Buildlog: none named."
    values = {}
    for instruction in instructions:
        try:
            content = instruction.read_text()
        except OSError:
            continue
        for line in content.splitlines():
            for label in ("Project note", "Buildlog"):
                if label not in values and line.startswith(f"{label}:"):
                    values[label] = line.partition(":")[2].strip() or "none named"
    return f"Project note: {values.get('Project note', 'none named')}. Buildlog: {values.get('Buildlog', 'none named')}."


def context(cwd: Path) -> str:
    repository, root = repository_line(cwd)
    return "\n".join(
        (
            repository,
            environment_line(),
            "Role: Build from the project spec; Claude plans and researches; vault writes stay within this project's note and buildlog.",
            project_line(folder_instructions(cwd, root)),
        )
    )


def main() -> None:
    try:
        event = json.load(sys.stdin)
        cwd = Path(event.get("cwd") or Path.cwd())
        output = context(cwd)
    except Exception as exc:  # noqa: BLE001
        output = f"Orientation unavailable: {type(exc).__name__}."
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": output,
                }
            }
        )
    )


if __name__ == "__main__":
    main()
