from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
import hashlib
import json
import os
import re
import tempfile

from .models import Config, PlanError

PLAN_VERSION = 1
TRANSACTION_DIR = ".rulesort-transactions"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _safe_destination(root: Path, template: str, source: Path, rule_name: str, mtime: float) -> Path:
    moment = datetime.fromtimestamp(mtime, tz=timezone.utc)
    values = {
        "name": source.name,
        "stem": source.stem,
        "extension": source.suffix.lstrip(".").casefold() or "no-extension",
        "year": f"{moment.year:04d}",
        "month": f"{moment.month:02d}",
        "day": f"{moment.day:02d}",
        "rule": re.sub(r"[^A-Za-z0-9._-]+", "-", rule_name).strip("-") or "rule",
    }
    try:
        rendered = template.format_map(values)
    except (KeyError, ValueError) as exc:
        raise PlanError(f"invalid destination template {template!r}: {exc}") from exc
    relative = PurePosixPath(rendered.replace("\\", "/"))
    if ":" in rendered or relative.is_absolute() or any(part in {"..", ""} for part in relative.parts):
        raise PlanError(f"destination template must stay inside root: {rendered!r}")
    destination = (root / Path(*relative.parts) / source.name).resolve(strict=False)
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise PlanError(f"destination escapes root: {destination}") from exc
    return destination


def _iter_files(root: Path, recursive: bool) -> list[Path]:
    found: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
        except OSError as exc:
            raise PlanError(f"cannot scan {directory}: {exc}") from exc
        for entry in entries:
            if directory == root and entry.name == TRANSACTION_DIR:
                continue
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if recursive:
                        pending.append(Path(entry.path))
                    continue
                if entry.is_file(follow_symlinks=False):
                    found.append(Path(entry.path))
            except OSError as exc:
                raise PlanError(f"cannot inspect {entry.path}: {exc}") from exc
    return sorted(found, key=lambda path: _relative(path, root).casefold())


def _suffix_name(path: Path, number: int) -> Path:
    return path.with_name(f"{path.stem} ({number}){path.suffix}")


def build_plan(config: Config) -> dict[str, Any]:
    root = config.root.resolve()
    if not root.is_dir():
        raise PlanError(f"root is not an existing directory: {root}")
    actions: list[dict[str, Any]] = []
    planned_sources: set[str] = set()
    for source in _iter_files(root, config.recursive):
        try:
            stat = source.stat(follow_symlinks=False)
            if source.is_symlink() or not source.is_file():
                continue
            relative_source = _relative(source, root)
            for rule in config.rules:
                if not rule.matches(source, stat.st_size, stat.st_mtime):
                    continue
                destination = _safe_destination(root, rule.destination, source, rule.name, stat.st_mtime)
                relative_destination = _relative(destination, root)
                status = "unchanged" if relative_source.casefold() == relative_destination.casefold() else "planned"
                actions.append({
                    "source": relative_source,
                    "destination": relative_destination,
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "sha256": sha256_file(source),
                    "rule": rule.name,
                    "status": status,
                    "reason": None,
                })
                if status == "planned":
                    planned_sources.add(relative_source.casefold())
                break
        except OSError as exc:
            raise PlanError(f"cannot read file metadata for {source}: {exc}") from exc

    destinations: dict[str, list[dict[str, Any]]] = {}
    for action in actions:
        if action["status"] == "planned":
            destinations.setdefault(action["destination"].casefold(), []).append(action)
    reserved: set[str] = set()
    for action in actions:
        if action["status"] != "planned":
            continue
        destination = root / Path(action["destination"])
        key = action["destination"].casefold()
        duplicate = len(destinations[key]) > 1 or key in reserved
        external_occupant = destination.exists() and key not in planned_sources
        if duplicate or external_occupant:
            if config.conflict_policy == "suffix":
                candidate = destination
                index = 1
                while candidate.exists() or candidate.relative_to(root).as_posix().casefold() in reserved:
                    candidate = _suffix_name(destination, index)
                    index += 1
                action["destination"] = _relative(candidate, root)
                action["reason"] = "destination renamed with a numeric suffix to avoid a collision"
                reserved.add(action["destination"].casefold())
            else:
                action["status"] = "skipped" if config.conflict_policy == "skip" else "conflict"
                action["reason"] = "multiple sources target this path" if duplicate else "destination already exists"
        else:
            reserved.add(key)

    return {
        "version": PLAN_VERSION,
        "root": str(root),
        "recursive": config.recursive,
        "conflict_policy": config.conflict_policy,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "actions": actions,
    }


def save_plan(plan: dict[str, Any], path: Path) -> None:
    target = path.resolve()
    if any(target == (Path(plan['root']) / item['source']).resolve() for item in plan['actions']):
        raise PlanError('plan output cannot replace an input file')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, prefix='.rulesort-plan-', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(plan, indent=2, ensure_ascii=False) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    except OSError as error:
        raise PlanError(f'cannot publish plan (choose a new output path): {error}') from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_plan(path: Path) -> dict[str, Any]:
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PlanError(f"cannot read plan {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PlanError(f"invalid JSON in plan {path} at line {exc.lineno}: {exc.msg}") from exc
    if not isinstance(plan, dict) or plan.get("version") != PLAN_VERSION or not isinstance(plan.get("actions"), list):
        raise PlanError("unsupported or malformed plan")
    root = Path(plan.get("root", "")).resolve()
    if not root.is_dir():
        raise PlanError(f"plan root is unavailable: {root}")
    seen_sources: set[str] = set()
    for index, item in enumerate(plan["actions"]):
        if not isinstance(item, dict):
            raise PlanError(f"actions[{index}] must be an object")
        for key in ("source", "destination", "status", "sha256"):
            if not isinstance(item.get(key), str):
                raise PlanError(f"actions[{index}].{key} must be a string")
        source = PurePosixPath(item["source"])
        destination = PurePosixPath(item["destination"])
        if (
            "\\" in item["source"]
            or "\\" in item["destination"]
            or ":" in item["source"]
            or ":" in item["destination"]
            or source.is_absolute()
            or destination.is_absolute()
            or ".." in source.parts
            or ".." in destination.parts
        ):
            raise PlanError(f"actions[{index}] contains a path outside root")
        source_key = item["source"].casefold()
        if source_key in seen_sources:
            raise PlanError(f"duplicate source in plan: {item['source']}")
        seen_sources.add(source_key)
        if item["status"] not in {"planned", "unchanged", "conflict", "skipped"}:
            raise PlanError(f"actions[{index}] has unknown status")
        if item["status"] == "planned" and (not isinstance(item.get("size"), int) or not isinstance(item.get("mtime_ns"), int)):
            raise PlanError(f"actions[{index}] is missing precondition metadata")
    plan["root"] = str(root)
    return plan


def summary(plan: dict[str, Any]) -> dict[str, int]:
    counts = {"planned": 0, "unchanged": 0, "conflict": 0, "skipped": 0}
    for item in plan["actions"]:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return counts
