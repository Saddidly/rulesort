from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any
import json
import os
import uuid

from .models import ConflictError, PlanError
from .planning import TRANSACTION_DIR, sha256_file


class Journal:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("a", encoding="utf-8")

    def write(self, event: str, **data: Any) -> None:
        record = {"event": event, **data}
        self._handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def close(self) -> None:
        self._handle.close()


def _move_no_replace(source: Path, destination: Path) -> None:
    """Publish a same-filesystem regular file without replacing an existing name."""
    if source.is_symlink() or not source.is_file():
        raise ConflictError(f"move source is not a regular file: {source}")
    os.link(source, destination, follow_symlinks=False)
    try:
        source.unlink()
    except OSError:
        destination.unlink()
        raise


def _root_path(root: Path, relative: str) -> Path:
    rel = PurePosixPath(relative)
    if not relative or relative == "." or "\\" in relative or ":" in relative or rel.is_absolute() or ".." in rel.parts:
        raise PlanError(f"unsafe relative path: {relative!r}")
    path = root.joinpath(*rel.parts)
    resolved_parent = path.parent.resolve(strict=False)
    try:
        resolved_parent.relative_to(root.resolve())
    except ValueError as exc:
        raise PlanError(f"path parent escapes root through a symlink: {relative}") from exc
    cursor = root.resolve()
    for part in rel.parts[:-1]:
        cursor = cursor / part
        if cursor.exists() and cursor.is_symlink():
            raise PlanError(f"path crosses a symlink: {relative}")
    return path


def _verify_source(root: Path, item: dict[str, Any]) -> Path:
    source = _root_path(root, item["source"])
    try:
        if source.is_symlink() or not source.is_file():
            raise ConflictError(f"source is missing or is not a regular file: {item['source']}")
        stat = source.stat(follow_symlinks=False)
        if stat.st_size != item["size"] or stat.st_mtime_ns != item["mtime_ns"]:
            raise ConflictError(f"source metadata changed since planning: {item['source']}")
        if sha256_file(source) != item["sha256"]:
            raise ConflictError(f"source content changed since planning: {item['source']}")
    except OSError as exc:
        raise ConflictError(f"cannot verify source {item['source']}: {exc}") from exc
    return source


def _mkdir_tracked(directory: Path, root: Path, journal: Journal) -> None:
    missing: list[Path] = []
    cursor = directory
    while not cursor.exists():
        missing.append(cursor)
        if cursor == root or cursor.parent == cursor:
            break
        cursor = cursor.parent
    for item in reversed(missing):
        _root_path(root, item.relative_to(root).as_posix())
        item.mkdir(exist_ok=True)
        journal.write("directory_created", path=item.relative_to(root).as_posix())


def _rollback_apply(root: Path, stage_dir: Path, items: list[dict[str, Any]], moved: set[str], staged: set[str], journal: Journal) -> bool:
    failed = False
    # Put committed destinations back into staging first, so every original slot is free.
    for item in reversed(items):
        key = item["source"]
        if key not in moved:
            continue
        destination = _root_path(root, item["destination"])
        temporary = stage_dir / item["stage_name"]
        try:
            if destination.exists() and not temporary.exists():
                if destination.is_symlink() or sha256_file(destination) != item["sha256"]:
                    raise ConflictError(f"destination changed during rollback: {item['destination']}")
                _move_no_replace(destination, temporary)
            else:
                raise ConflictError(f"cannot recover destination: {item['destination']}")
                journal.write("rollback_destination_staged", source=item["source"], destination=item["destination"])
        except (OSError, PlanError) as exc:
            failed = True
            journal.write("rollback_error", source=item["source"], error=str(exc))
    for item in reversed(items):
        key = item["source"]
        temporary = stage_dir / item["stage_name"]
        if key not in staged and not temporary.exists():
            continue
        source = _root_path(root, item["source"])
        try:
            if source.exists():
                raise FileExistsError(f"original path is occupied: {item['source']}")
            source.parent.mkdir(parents=True, exist_ok=True)
            _move_no_replace(temporary, source)
            journal.write("rollback_source_restored", source=item["source"])
        except (OSError, PlanError) as exc:
            failed = True
            journal.write("rollback_error", source=item["source"], error=str(exc))
    return not failed


def apply_plan(plan: dict[str, Any], allow_partial: bool = False) -> Path:
    root = Path(plan["root"]).resolve()
    conflicts = [item for item in plan["actions"] if item["status"] == "conflict"]
    if conflicts and not allow_partial:
        raise ConflictError(f"plan has {len(conflicts)} conflict(s); use --allow-partial to apply eligible entries")
    items = [dict(item) for item in plan["actions"] if item["status"] == "planned"]
    if not items:
        raise PlanError("plan contains no eligible moves")
    tx_id = uuid.uuid4().hex
    transaction_root = root / TRANSACTION_DIR
    if transaction_root.is_symlink():
        raise PlanError(f"transaction directory must not be a symlink: {transaction_root}")
    transaction_root.mkdir(exist_ok=True)
    tx_dir = transaction_root / tx_id
    stage_dir = tx_dir / "staged"
    stage_dir.mkdir(parents=True, exist_ok=False)
    journal_path = tx_dir / "journal.jsonl"
    journal = Journal(journal_path)
    moved: set[str] = set()
    staged: set[str] = set()
    try:
        source_keys = {item["source"].casefold() for item in items}
        if len(source_keys) != len(items):
            raise PlanError("plan contains duplicate sources")
        destination_keys: set[str] = set()
        for index, item in enumerate(items):
            source = _verify_source(root, item)
            target = _root_path(root, item["destination"])
            if target == source:
                raise PlanError(f"identical source and destination: {item['source']}")
            if item["source"].split('/')[0] == TRANSACTION_DIR or item["destination"].split('/')[0] == TRANSACTION_DIR:
                raise PlanError("transaction storage cannot be a source or destination")
            target_key = item["destination"].casefold()
            if target_key in destination_keys:
                raise ConflictError(f"multiple actions target {item['destination']}")
            destination_keys.add(target_key)
            if os.path.lexists(target) and target_key not in source_keys:
                raise ConflictError(f"destination already exists: {item['destination']}")
            item["stage_name"] = f"{index:08d}"
        journal.write("transaction_started", id=tx_id, root=str(root), actions=items)
        for item in items:
            source = _verify_source(root, item)
            _move_no_replace(source, stage_dir / item["stage_name"])
            staged.add(item["source"])
            journal.write("source_staged", source=item["source"], stage=item["stage_name"])
        for item in items:
            target = _root_path(root, item["destination"])
            _mkdir_tracked(target.parent, root, journal)
            _move_no_replace(stage_dir / item["stage_name"], target)
            moved.add(item["source"])
            journal.write("destination_written", source=item["source"], destination=item["destination"])
        journal.write("transaction_applied")
    except Exception as exc:
        if staged:
            journal.write("apply_failed", error=str(exc))
            ok = _rollback_apply(root, stage_dir, items, moved, staged, journal)
            journal.write("rollback_complete" if ok else "rollback_incomplete", error=str(exc))
            outcome = "original files were restored" if ok else "rollback was incomplete"
            raise PlanError(f"apply failed; {outcome}; inspect {journal_path}: {exc}") from exc
        raise
    finally:
        journal.close()
    return journal_path


def _read_journal(path: Path) -> list[dict[str, Any]]:
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError(f"cannot read transaction journal {path}: {exc}") from exc
    if not rows:
        raise PlanError("transaction journal is empty")
    return rows


def undo_journal(path: Path) -> None:
    rows = _read_journal(path)
    started = next((row for row in rows if row.get("event") == "transaction_started"), None)
    if started is None or not any(row.get("event") == "transaction_applied" for row in rows):
        raise PlanError("journal does not describe a completed apply transaction")
    if any(row.get("event") in {"undo_complete", "undo_started"} for row in rows):
        raise PlanError("undo already completed or was interrupted; inspect journal before recovery")
    root = Path(started["root"]).resolve()
    if not root.is_dir():
        raise PlanError(f"transaction root is unavailable: {root}")
    items = started["actions"]
    if not isinstance(items, list) or not items:
        raise PlanError("journal contains no actions")
    destinations = {item["destination"].casefold() for item in items}
    sources = {item["source"].casefold() for item in items}
    if len(destinations) != len(items) or len(sources) != len(items):
        raise PlanError("journal contains duplicate sources or destinations")
    for item in items:
        current = _root_path(root, item["destination"])
        if current.is_symlink() or not current.is_file() or sha256_file(current) != item["sha256"]:
            raise ConflictError(f"applied file is missing or changed: {item['destination']}")
        original = _root_path(root, item["source"])
        if os.path.lexists(original) and item["source"].casefold() not in destinations:
            raise ConflictError(f"original path is occupied: {item['source']}")
    journal = Journal(path)
    stage_dir = path.parent / f"undo-staged-{uuid.uuid4().hex}"
    stage_dir.mkdir(exist_ok=False)
    staged: set[str] = set()
    restored: set[str] = set()
    try:
        journal.write("undo_started", actions=len(items))
        for index, item in enumerate(items):
            current = _root_path(root, item["destination"])
            if sha256_file(current) != item["sha256"]:
                raise ConflictError(f"file changed during undo: {item['destination']}")
            _move_no_replace(current, stage_dir / f"{index:08d}")
            staged.add(item["source"])
            journal.write("undo_file_staged", destination=item["destination"], stage=f"{index:08d}")
        for index, item in enumerate(items):
            original = _root_path(root, item["source"])
            original.parent.mkdir(parents=True, exist_ok=True)
            _move_no_replace(stage_dir / f"{index:08d}", original)
            restored.add(item["source"])
            journal.write("original_restored", source=item["source"])
        for relative in reversed([row["path"] for row in rows if row.get("event") == "directory_created"]):
            try:
                _root_path(root, relative).rmdir()
            except OSError:
                pass  # A non-empty directory may contain unrelated user files.
        journal.write("undo_complete")
    except Exception as exc:
        rollback_ok = True
        # Restage originals first so cycles never overwrite another restored file.
        for index, item in reversed(list(enumerate(items))):
            if item["source"] not in restored:
                continue
            try:
                original = _root_path(root, item["source"])
                if sha256_file(original) != item["sha256"]:
                    raise ConflictError(f"restored file changed: {item['source']}")
                _move_no_replace(original, stage_dir / f"{index:08d}")
            except (OSError, PlanError) as error:
                rollback_ok = False
                journal.write("undo_rollback_error", source=item["source"], error=str(error))
        for index, item in reversed(list(enumerate(items))):
            if item["source"] not in staged:
                continue
            try:
                temporary = stage_dir / f"{index:08d}"
                _move_no_replace(temporary, _root_path(root, item["destination"]))
                journal.write("undo_rollback_destination_restored", destination=item["destination"])
            except (OSError, PlanError) as error:
                rollback_ok = False
                journal.write("undo_rollback_error", destination=item["destination"], error=str(error))
        journal.write("undo_rollback_complete" if rollback_ok else "undo_rollback_incomplete", error=str(exc))
        outcome = "applied files were restored" if rollback_ok else "rollback was incomplete"
        raise PlanError(f"undo failed; {outcome}; inspect {path}: {exc}") from exc
    finally:
        journal.close()
