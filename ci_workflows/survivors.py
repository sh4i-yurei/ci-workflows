"""Read explanations shared by code and policy mutation gates."""

from pathlib import Path

import yaml

from ci_workflows.errors import ConfigurationError


def load_explanations(
    path: Path,
    *,
    duplicate_error: str,
    invalid_name_error: str = "each survivor needs mutant and reason",
) -> dict[str, str]:
    entries = yaml.safe_load(path.read_text()) if path.exists() else []
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise ConfigurationError("survivors file must be a list")
    explanations = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"mutant", "reason"}:
            raise ConfigurationError("each survivor needs mutant and reason")
        name, reason = entry["mutant"], entry["reason"]
        if not isinstance(name, str) or not name:
            raise ConfigurationError(invalid_name_error)
        if not isinstance(reason, str):
            raise ConfigurationError("each survivor needs mutant and reason")
        if name in explanations:
            raise ConfigurationError(duplicate_error.format(name=name))
        explanations[name] = reason.strip()
    return explanations
