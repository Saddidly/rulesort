from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any
import os
import re
import uuid

from .filesystem import (
    ensure_root_directory,
    ensure_transaction_directory,
    file_matches,
    move_no_replace,
    remove_created_root_directories,
    root_path,
)
from .journal import Journal, JournalDocument, JOURNAL_VERSION, anchor_journal, read_journal, root_operation_lock
from .models import ConflictError, PlanError
from .planning import TRANSACTION_DIR
from .recovery import _report, recover_locked


def _root_path(root: Path, relative: str) -> Path:
    """Compatibility alias for the shared checked root-path resolver."""
    return root_path(root, relative)


def _move_no_replace(source: Path, destination: Path) -> None:
    """Compatibility seam used by filesystem failure-injection tests."""
    move_no_replace(source, destination)


def _verify_planned_source(root: Path, item: dict[str, Any]) -> tuple[Path, os.stat_result]:
    source = root_path(root, item["source"])
    try:
        info = source.lstat()
    except OSError as exc:
        raise ConflictError(f"cannot inspect source {item['source']}: {exc}") from exc
    if source.is_symlink() or not source.is_file():
        raise ConflictError(f"source is missing or is not a regular file: {item['source']}")
    if info.st_size != item["size"] or info.st_mtime_ns != item["mtime_ns"]:
        raise ConflictError(f"source metadata changed since planning: {item['source']}")
    expected_device = item.get("device", info.st_dev)
    expected_inode = item.get("inode", info.st_ino)
    if (info.st_dev, info.st_ino) != (expected_device, expected_inode):
        raise ConflictError(f"source filesystem identity changed since planning: {item['source']}")
    if not file_matches(source, {**item, "device": expected_device, "inode": expected_inode}):
        raise ConflictError(f"source content changed since planning: {item['source']}")
    if info.st_ino <= 0:
        raise PlanError(f"filesystem does not expose a usable file identity for recovery: {item['source']}")
    return source, info


def _validate_apply_plan(plan: dict[str, Any], allow_partial: bool) -> tuple[Path, list[dict[str, Any]]]:
    if not isinstance(plan, dict) or type(plan.get("version")) is not int or plan.get("version") != 1 or not isinstance(plan.get("root"), str) or not isinstance(plan.get("actions"), list):
        raise PlanError("plan is malformed")
    root = Path(plan["root"]).resolve()
    if not root.is_dir():
        raise PlanError(f"plan root is unavailable: {root}")
    for index, item in enumerate(plan["actions"]):
        if not isinstance(item, dict) or item.get("status") not in {"planned", "unchanged", "conflict", "skipped"}:
            raise PlanError(f"actions[{index}] is malformed")
    conflicts = [item for item in plan["actions"] if item.get("status") == "conflict"]
    if conflicts and not allow_partial:
        raise ConflictError(f"plan has {len(conflicts)} conflict(s); use --allow-partial to apply eligible entries")
    raw_items = [item for item in plan["actions"] if isinstance(item, dict) and item.get("status") == "planned"]
    if not raw_items:
        raise PlanError("plan contains no eligible moves")

    items: list[dict[str, Any]] = []
    sources: dict[str, str] = {}
    destinations: dict[str, str] = {}
    spellings: dict[str, str] = {}
    identities: set[tuple[int, int]] = set()
    for index, raw in enumerate(raw_items):
        for field in ("source", "destination", "sha256"):
            if not isinstance(raw.get(field), str):
                raise PlanError(f"planned action {index} is missing {field}")
        if type(raw.get("size")) is not int or raw["size"] < 0:
            raise PlanError(f"planned action {index} has invalid size metadata")
        if type(raw.get("mtime_ns")) is not int:
            raise PlanError(f"planned action {index} has invalid modification-time metadata")
        if not isinstance(raw.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", raw["sha256"]):
            raise PlanError(f"planned action {index} has an invalid SHA-256 value")
        item = {key: raw[key] for key in ("source", "destination", "size", "mtime_ns", "sha256")}
        source = root_path(root, item["source"])
        target = root_path(root, item["destination"])
        if item["source"].casefold() == item["destination"].casefold():
            raise PlanError(f"case-only or identical source and destination is unsupported: {item['source']}")
        for value in (item["source"], item["destination"]):
            key = value.casefold()
            previous = spellings.get(key)
            if previous is not None and previous != value:
                raise PlanError("plan paths that differ only by case are ambiguous across platforms")
            spellings[key] = value
        source_key = item["source"].casefold()
        destination_key = item["destination"].casefold()
        if source_key in sources or destination_key in destinations:
            raise PlanError("plan contains duplicate sources or destinations")
        sources[source_key] = item["source"]
        destinations[destination_key] = item["destination"]
        verified_source, info = _verify_planned_source(root, item)
        identity = (info.st_dev, info.st_ino)
        if identity in identities:
            raise PlanError("multiple planned paths identify the same hard-linked file; split them into separate operations")
        identities.add(identity)
        item.update({"device": info.st_dev, "inode": info.st_ino, "stage_name": f"{index:08d}"})
        # Resolve parent components before creating transaction state.
        if verified_source == target:
            raise PlanError(f"identical source and destination: {item['source']}")
        items.append(item)

    managed_paths: dict[tuple[str, ...], str] = {}
    for item in items:
        for field in ("source", "destination"):
            relative = item[field]
            parts = tuple(part.casefold() for part in PurePosixPath(relative).parts)
            previous = managed_paths.get(parts)
            if previous is not None and previous != relative:
                raise PlanError("plan paths that differ only by case are ambiguous across platforms")
            managed_paths[parts] = relative
    for parts, relative in managed_paths.items():
        for depth in range(1, len(parts)):
            parent = parts[:depth]
            if parent in managed_paths:
                raise PlanError(f"managed file paths overlap as a file and directory: {managed_paths[parent]} and {relative}")

    # A final file cannot be created below an existing non-directory, even if
    # that ancestor is itself one of the sources scheduled for staging.
    for item in items:
        parent = root
        for segment in PurePosixPath(item["destination"]).parts[:-1]:
            parent = parent / segment
            if os.path.lexists(parent) and (parent.is_symlink() or not parent.is_dir()):
                raise ConflictError(f"destination parent is not a directory: {parent}")

    source_keys = set(sources)
    for item in items:
        target = root_path(root, item["destination"])
        if os.path.lexists(target) and item["destination"].casefold() not in source_keys:
            raise ConflictError(f"destination already exists: {item['destination']}")
    return root, items


def _journaled_move(journal: Journal, operation: str, index: int, source: Path,
                    destination: Path, item: dict[str, Any]) -> None:
    if not file_matches(source, item):
        raise ConflictError(f"recorded file changed before move: {source}")
    intent_id = journal.intent("move_intent", operation=operation, index=index)
    _move_no_replace(source, destination)
    if not file_matches(destination, item):
        raise ConflictError(f"recorded file changed during move: {destination}")
    journal.write("move_complete", id=intent_id)


def _ensure_legacy_root_directory(directory: Path, root: Path, journal: Journal) -> None:
    root = root.resolve()
    missing: list[Path] = []
    cursor = directory
    while cursor != root and not os.path.lexists(cursor):
        missing.append(cursor)
        cursor = cursor.parent
    if cursor != root and (cursor.is_symlink() or not cursor.is_dir()):
        raise ConflictError(f"parent path is not a safe directory: {cursor}")
    for item in reversed(missing):
        relative = item.relative_to(root).as_posix()
        root_path(root, relative)
        item.mkdir()
        journal.write("directory_created", path=relative)


def apply_plan(plan: dict[str, Any], allow_partial: bool = False) -> Path:
    if not isinstance(plan, dict) or not isinstance(plan.get("root"), str):
        raise PlanError("plan is malformed")
    root_hint = Path(plan["root"]).resolve()
    with root_operation_lock(root_hint):
        root, items = _validate_apply_plan(plan, allow_partial)
        tx_id = uuid.uuid4().hex
        transaction_root = root / TRANSACTION_DIR
        if transaction_root.is_symlink():
            raise PlanError(f"transaction directory must not be a symlink: {transaction_root}")
        transaction_root.mkdir(exist_ok=True)
        tx_dir = transaction_root / tx_id
        tx_dir.mkdir(exist_ok=False)
        journal_path = tx_dir / "journal.jsonl"
        journal = Journal(journal_path, create=True)
        manifest = [{key: item[key] for key in (
            "source", "destination", "stage_name", "size", "mtime_ns", "sha256", "device", "inode"
        )} for item in items]
        try:
            journal.write("transaction_started", version=JOURNAL_VERSION, id=tx_id, root=str(root), actions=manifest)
        except Exception:
            journal.close()
            raise
        try:
            ensure_transaction_directory(tx_dir / "staged", "apply_stage", journal)
            for index, item in enumerate(items):
                source = root_path(root, item["source"])
                _verify_planned_source(root, {**item, "mtime_ns": item["mtime_ns"]})
                _journaled_move(journal, "apply_stage", index, source, tx_dir / "staged" / item["stage_name"], item)
            for index, item in enumerate(items):
                target = root_path(root, item["destination"])
                ensure_root_directory(target.parent, root, journal)
                _journaled_move(journal, "apply_commit", index, tx_dir / "staged" / item["stage_name"], target, item)
            journal.write("transaction_applied")
        except Exception as exc:
            journal.close()
            try:
                observed = read_journal(journal_path)
            except Exception as journal_error:
                raise PlanError(
                    f"apply failed and its journal is ambiguous; no recovery was attempted; inspect {journal_path}: {journal_error}"
                ) from exc
            if observed.applied:
                raise PlanError(
                    f"apply reached its committed journal state, but a final journal write reported an error; "
                    f"inspect or undo {journal_path}: {exc}"
                ) from exc
            failure_journal = Journal(journal_path, next_id=observed.next_intent_id)
            try:
                failure_journal.write("apply_failed", error=str(exc))
            except OSError:
                pass
            finally:
                failure_journal.close()
            try:
                report = recover_locked(journal_path)
            except Exception as recovery_error:
                raise PlanError(f"apply failed and automatic recovery could not finish; inspect {journal_path}: {recovery_error}") from exc
            raise PlanError(f"apply failed and was recovered to original paths; inspect {journal_path}: {exc}") from exc
        else:
            journal.close()
    return journal_path


def _verify_legacy_applied(document: JournalDocument) -> None:
    if not document.applied:
        raise PlanError("legacy journal does not describe a completed apply transaction")
    for item in document.actions:
        destination = root_path(document.root, item["destination"])
        if destination.is_symlink() or not destination.is_file():
            raise ConflictError(f"applied file is missing or is not regular: {item['destination']}")
        from .planning import sha256_file

        if sha256_file(destination) != item["sha256"]:
            raise ConflictError(f"applied file changed: {item['destination']}")


def _undo_legacy(document: JournalDocument) -> None:
    if document.undo_complete:
        raise PlanError("legacy undo already completed")
    if document.undo_started:
        raise PlanError("legacy undo was interrupted; this journal has no automatic crash-recovery evidence")
    _verify_legacy_applied(document)
    destinations = {item["destination"].casefold() for item in document.actions}
    for item in document.actions:
        source = root_path(document.root, item["source"])
        if os.path.lexists(source) and item["source"].casefold() not in destinations:
            raise ConflictError(f"original path is occupied: {item['source']}")
    journal = Journal(document.path, next_id=document.next_intent_id)
    stage_dir = document.transaction_dir / f"undo-staged-{uuid.uuid4().hex}"
    stage_dir.mkdir(exist_ok=False)
    staged: set[str] = set()
    restored: set[str] = set()
    try:
        journal.write("undo_started", actions=len(document.actions))
        for index, item in enumerate(document.actions):
            current = root_path(document.root, item["destination"])
            _move_no_replace(current, stage_dir / f"{index:08d}")
            staged.add(item["source"])
            journal.write("undo_file_staged", destination=item["destination"], stage=f"{index:08d}")
        for index, item in enumerate(document.actions):
            original = root_path(document.root, item["source"])
            _ensure_legacy_root_directory(original.parent, document.root, journal)
            _move_no_replace(stage_dir / f"{index:08d}", original)
            restored.add(item["source"])
            journal.write("original_restored", source=item["source"])
        for row in reversed(document.rows):
            if row.get("event") == "directory_created" and isinstance(row.get("path"), str):
                try:
                    root_path(document.root, row["path"]).rmdir()
                except OSError:
                    pass
        journal.write("undo_complete")
    except Exception as exc:
        rollback_ok = True
        for index, item in reversed(list(enumerate(document.actions))):
            if item["source"] not in restored:
                continue
            try:
                _move_no_replace(root_path(document.root, item["source"]), stage_dir / f"{index:08d}")
            except (OSError, PlanError):
                rollback_ok = False
        for index, item in reversed(list(enumerate(document.actions))):
            if item["source"] not in staged:
                continue
            try:
                _move_no_replace(stage_dir / f"{index:08d}", root_path(document.root, item["destination"]))
            except (OSError, PlanError):
                rollback_ok = False
        journal.write("undo_rollback_complete" if rollback_ok else "undo_rollback_incomplete", error=str(exc))
        raise PlanError(f"legacy undo failed; inspect {document.path}: {exc}") from exc
    finally:
        journal.close()


def undo_journal(path: Path) -> None:
    _, root, _, _ = anchor_journal(path)
    with root_operation_lock(root):
        document = read_journal(path)
        if document.version == 1:
            _undo_legacy(document)
            return
        if document.recovery_complete is not None:
            raise PlanError("transaction has already been recovered; inspect the journal for its final state")
        if document.undo_complete:
            raise PlanError("undo already completed")
        if not document.applied:
            raise PlanError("apply is incomplete; inspect and recover the journal before undo")
        if document.undo_started:
            raise PlanError("undo was interrupted; inspect and recover the journal before retrying")
        report = _report(document)
        if report["issues"]:
            raise ConflictError("applied transaction is not safe to undo: " + "; ".join(report["issues"]))

        undo_stage = f"undo-staged-{uuid.uuid4().hex}"
        journal = Journal(document.path, next_id=document.next_intent_id)
        try:
            journal.write("undo_started", stage=undo_stage)
            stage_dir = document.transaction_dir / undo_stage
            ensure_transaction_directory(stage_dir, "undo_stage", journal)
            for index, item in enumerate(document.actions):
                current = root_path(root, item["destination"])
                _journaled_move(journal, "undo_stage", index, current, stage_dir / item["stage_name"], item)
            for index, item in enumerate(document.actions):
                original = root_path(root, item["source"])
                ensure_root_directory(original.parent, root, journal)
                _journaled_move(journal, "undo_restore", index, stage_dir / item["stage_name"], original, item)
            remove_created_root_directories(root, document.rows, journal)
            journal.write("undo_complete")
        except Exception as exc:
            journal.close()
            try:
                observed = read_journal(document.path)
            except Exception as journal_error:
                raise PlanError(
                    f"undo failed and its journal is ambiguous; no recovery was attempted; inspect {document.path}: {journal_error}"
                ) from exc
            if observed.undo_complete:
                raise PlanError(
                    f"undo reached its completed journal state, but a final journal write reported an error; "
                    f"inspect {document.path}: {exc}"
                ) from exc
            failure_journal = Journal(document.path, next_id=observed.next_intent_id)
            try:
                failure_journal.write("undo_failed", error=str(exc))
            except OSError:
                pass
            finally:
                failure_journal.close()
            try:
                recover_locked(document.path)
            except Exception as recovery_error:
                raise PlanError(f"undo failed and recovery could not finish; inspect {document.path}: {recovery_error}") from exc
            raise PlanError(f"undo failed and recovery completed the undo to original paths: {exc}") from exc
        else:
            journal.close()


def _read_journal(path: Path) -> list[dict[str, Any]]:
    """Compatibility helper retained for callers of the original module."""
    return read_journal(path).rows
