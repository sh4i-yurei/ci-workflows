"""Copy the versioned permission and memory settings into a machine's Codex config."""

import json
import os
import re
import shutil
import tempfile
from datetime import date, datetime, time, timezone
from pathlib import Path

import tomllib

SECTION = re.compile(r"^\s*\[([^]]+)\]\s*(?:#.*)?$")
ARRAY_SECTION = re.compile(r"^\s*\[\[([^]]+)\]\]\s*(?:#.*)?$")


def key_parts(raw: str) -> tuple[str, ...]:
    """Use the TOML parser to distinguish dotted keys from quoted names."""
    node = tomllib.loads(f"{raw} = 1")
    parts = []
    while isinstance(node, dict):
        name, node = next(iter(node.items()))
        parts.append(name)
    return tuple(parts)


def assignment_key(line: str) -> tuple[str, ...] | None:
    quoted = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
        elif char == "\\" and quoted == '"':
            escaped = True
        elif char in ("'", '"'):
            if quoted is None:
                quoted = char
            elif quoted == char:
                quoted = None
        elif char == "=" and quoted is None:
            return key_parts(line[:index].strip())
    return None


def multiline_opening(line: str) -> str | None:
    match = re.search(r"=\s*(\"\"\"|''')", line)
    if match and line.count(match.group(1), match.start(1)) % 2:
        return match.group(1)
    return None


def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return (
            "{ "
            + ", ".join(
                f"{json.dumps(key)} = {toml_value(item)}" for key, item in value.items()
            )
            + " }"
        )
    raise TypeError(f"Cannot write TOML value of type {type(value).__name__}")


def merged_config(base_text: str, profile_text: str) -> str:
    profile = tomllib.loads(profile_text)
    if profile.get("default_permissions") != "mark":
        raise ValueError("mark.config.toml must select the mark permission profile")
    memories = profile.get("memories", {})
    if not all(
        isinstance(memories.get(key), bool)
        for key in ("generate_memories", "use_memories")
    ):
        raise ValueError(
            "mark.config.toml must set generate_memories and use_memories to true or false"
        )
    owned_memory_keys = {(key,) for key in memories}
    memory_lines = "".join(
        f"{key} = {toml_value(value)}\n" for key, value in memories.items()
    )
    base = tomllib.loads(base_text)

    kept = []
    section = ()
    has_memories = False
    inline_permissions = False
    multiline = None
    for line in base_text.splitlines(keepends=True):
        if multiline:
            if multiline in line:
                multiline = None
            if section[:2] != ("permissions", "mark"):
                kept.append(line)
            continue
        array_match = ARRAY_SECTION.match(line)
        if array_match:
            section = key_parts(array_match.group(1))
            kept.append(line)
            continue
        match = SECTION.match(line)
        if match:
            section = key_parts(match.group(1))
            if section == ("memories",):
                has_memories = True
                kept.append(line)
                kept.append(memory_lines)
                continue
            if section[:2] == ("permissions", "mark"):
                continue
        if "=" in line and not line.lstrip().startswith("#"):
            multiline = multiline_opening(line)
        if section[:2] == ("permissions", "mark"):
            continue
        key = (
            assignment_key(line)
            if "=" in line and not line.lstrip().startswith("#")
            else None
        )
        if section == () and key == ("default_permissions",):
            continue
        if (
            section == ()
            and key
            and (key[:1] == ("memories",) or key[:2] == ("permissions", "mark"))
        ):
            continue
        if section == () and key == ("permissions",):
            inline_permissions = True
            continue
        if section == ("permissions",) and key == ("mark",):
            continue
        if section == ("memories",) and key in owned_memory_keys:
            continue
        kept.append(line)

    other = "".join(kept).strip("\n")
    permissions_part = profile_text[profile_text.index("[permissions.mark]") :]
    permission_tables, memory_table = permissions_part.split("[memories]", 1)
    if not has_memories:
        extra_memories = "".join(
            f"\n{key} = {toml_value(value)}"
            for key, value in base.get("memories", {}).items()
            if key not in memories
        )
        other = f"{other}\n\n[memories]{memory_table.rstrip()}{extra_memories}"
    if inline_permissions:
        for name, value in base.get("permissions", {}).items():
            if name != "mark":
                other += f"\n\n[permissions.{json.dumps(name)}]\n"
                other += "\n".join(
                    f"{json.dumps(key)} = {toml_value(item)}"
                    for key, item in value.items()
                )
    result = f'default_permissions = ":danger-full-access"\n\n{other}\n\n{permission_tables.rstrip()}\n'
    tomllib.loads(result)
    return result


def install_profile_config(home: Path, profile_path: Path) -> None:
    config = home / ".codex/config.toml"
    if not profile_path.is_file():
        return
    if config.is_symlink():
        raise ValueError(f"Refusing to merge into symlinked config: {config}")
    old = config.read_text() if config.exists() else ""
    profile_text = profile_path.read_text()
    current = tomllib.loads(old)
    desired = tomllib.loads(profile_text)
    if (
        current.get("default_permissions") == ":danger-full-access"
        and current.get("permissions", {}).get("mark") == desired["permissions"]["mark"]
        and all(
            current.get("memories", {}).get(key) is value
            for key, value in desired["memories"].items()
        )
    ):
        print(f"OK: {config} already has the full-access default and mark profile")
        return
    new = merged_config(old, profile_text)
    if new == old:
        print(f"OK: {config} already has the full-access default and mark profile")
        return
    config.parent.mkdir(parents=True, exist_ok=True)
    if config.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        backup = config.with_name(f"config.toml.bak-{stamp}")
        shutil.copy2(config, backup)
        print(f"Backup: {backup}")
    mode = config.stat().st_mode if config.exists() else 0o600
    with tempfile.NamedTemporaryFile(
        mode="w", dir=config.parent, prefix="config.toml.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(new)
    try:
        os.chmod(temporary, mode & 0o777)
        temporary.replace(config)
    finally:
        temporary.unlink(missing_ok=True)
    print(
        f"Configured: {config} defaults to full access and keeps the mark profile and machine settings"
    )
