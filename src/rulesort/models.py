from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import fnmatch
import re


class RuleSortError(Exception):
    """Base class for expected RuleSort errors."""


class ConfigurationError(RuleSortError):
    """The rule configuration is invalid."""


class PlanError(RuleSortError):
    """A plan is invalid or cannot safely be used."""


class ConflictError(PlanError):
    """One or more planned moves conflict with current filesystem state."""


def _parse_time(value: str, field: str) -> float:
    try:
        text = value.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (AttributeError, ValueError) as exc:
        raise ConfigurationError(f"{field} must be an ISO-8601 timestamp: {value!r}") from exc


@dataclass(frozen=True)
class Rule:
    name: str
    destination: str
    extensions: tuple[str, ...] = ()
    name_glob: str | None = None
    name_regex: str | None = None
    size_min: int | None = None
    size_max: int | None = None
    modified_after: float | None = None
    modified_before: float | None = None

    @classmethod
    def from_dict(cls, raw: Any, index: int) -> "Rule":
        if not isinstance(raw, dict):
            raise ConfigurationError(f"rules[{index}] must be an object")
        name = raw.get("name", f"rule-{index + 1}")
        destination = raw.get("destination")
        if not isinstance(name, str) or not name.strip():
            raise ConfigurationError(f"rules[{index}].name must be a non-empty string")
        if not isinstance(destination, str) or not destination.strip():
            raise ConfigurationError(f"rules[{index}].destination must be a non-empty string")
        ext_value = raw.get("extensions", raw.get("extension", []))
        if isinstance(ext_value, str):
            ext_value = [ext_value]
        if not isinstance(ext_value, list) or any(not isinstance(x, str) for x in ext_value):
            raise ConfigurationError(f"rules[{index}].extensions must be a string or list of strings")
        extensions = tuple((x if x.startswith(".") else "." + x).casefold() for x in ext_value)
        name_glob = raw.get("name_glob")
        name_regex = raw.get("name_regex")
        for field, value in (("name_glob", name_glob), ("name_regex", name_regex)):
            if value is not None and not isinstance(value, str):
                raise ConfigurationError(f"rules[{index}].{field} must be a string")
        if name_regex:
            try:
                re.compile(name_regex)
            except re.error as exc:
                raise ConfigurationError(f"rules[{index}].name_regex is invalid: {exc}") from exc
        size_min = raw.get("size_min")
        size_max = raw.get("size_max")
        for field, value in (("size_min", size_min), ("size_max", size_max)):
            if value is not None and (not isinstance(value, int) or value < 0):
                raise ConfigurationError(f"rules[{index}].{field} must be a non-negative integer")
        if size_min is not None and size_max is not None and size_min > size_max:
            raise ConfigurationError(f"rules[{index}].size_min cannot exceed size_max")
        after = raw.get("modified_after")
        before = raw.get("modified_before")
        return cls(
            name=name.strip(),
            destination=destination,
            extensions=extensions,
            name_glob=name_glob,
            name_regex=name_regex,
            size_min=size_min,
            size_max=size_max,
            modified_after=_parse_time(after, f"rules[{index}].modified_after") if after is not None else None,
            modified_before=_parse_time(before, f"rules[{index}].modified_before") if before is not None else None,
        )

    def matches(self, path: Path, size: int, modified: float) -> bool:
        if self.extensions and path.suffix.casefold() not in self.extensions:
            return False
        if self.name_glob and not fnmatch.fnmatchcase(path.name.casefold(), self.name_glob.casefold()):
            return False
        if self.name_regex and re.search(self.name_regex, path.name) is None:
            return False
        if self.size_min is not None and size < self.size_min:
            return False
        if self.size_max is not None and size > self.size_max:
            return False
        if self.modified_after is not None and modified <= self.modified_after:
            return False
        if self.modified_before is not None and modified >= self.modified_before:
            return False
        return True


@dataclass(frozen=True)
class Config:
    root: Path
    recursive: bool
    conflict_policy: str
    rules: tuple[Rule, ...]

    @classmethod
    def from_dict(cls, raw: Any, base_dir: Path) -> "Config":
        if not isinstance(raw, dict):
            raise ConfigurationError("configuration root must be an object")
        root_value = raw.get("root", ".")
        if not isinstance(root_value, str) or not root_value.strip():
            raise ConfigurationError("root must be a non-empty path string")
        root = Path(root_value).expanduser()
        if not root.is_absolute():
            root = base_dir / root
        recursive = raw.get("recursive", False)
        if not isinstance(recursive, bool):
            raise ConfigurationError("recursive must be true or false")
        policy = raw.get("conflict_policy", "error")
        if policy not in {"error", "skip", "suffix"}:
            raise ConfigurationError("conflict_policy must be error, skip, or suffix")
        rules_raw = raw.get("rules")
        if not isinstance(rules_raw, list) or not rules_raw:
            raise ConfigurationError("rules must be a non-empty list")
        rules = tuple(Rule.from_dict(item, i) for i, item in enumerate(rules_raw))
        return cls(root=root.resolve(), recursive=recursive, conflict_policy=policy, rules=rules)
