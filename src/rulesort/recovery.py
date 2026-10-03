from __future__ import annotations

from pathlib import Path
from typing import Any
import os
import stat

from .journal import Journal, JournalDocument, JOURNAL_VERSION, anchor_journal, read_journal, root_operation_lock
from .filesystem import (
    ensure_root_directory,
    ensure_transaction_directory,
    file_matches,
    move_no_replace,
    remove_created_root_directories,
    root_path,
)
from .models import ConflictError, PlanError


def _state(document: JournalDocument) -> str:
    if document.version == 1:
        if document.undo_complete:
            return "legacy_undone"
        if document.undo_started:
            return "legacy_undo_interrupted"
        if document.applied:
            return "legacy_applied"
        return "legacy_apply_interrupted"
    if document.recovery_complete is not None:
        return "recovered_apply" if document.recovery_complete[0] == "apply" else "recovered_undo"
    if document.undo_complete:
        return "undone"
    if document.undo_started:
        return "undo_interrupted"
    if document.applied:
        return "applied"
    return "apply_interrupted"


def _identity(item: dict[str, Any]) -> tuple[int, int]:
    return item["device"], item["inode"]


def _role_paths(document: JournalDocument, index: int, operation: str) -> dict[str, Path]:
    item = document.actions[index]
    paths = {
        "source": root_path(document.root, item["source"]),
        "destination": root_path(document.root, item["destination"]),
    }
    if operation == "apply":
        paths["apply_stage"] = document.transaction_dir / "staged" / item["stage_name"]
    else:
        if not document.undo_stage:
            raise PlanError("journal has no undo staging directory")
        paths["undo_stage"] = document.transaction_dir / document.undo_stage / item["stage_name"]
    return paths


def _same_path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path)).casefold()


def _pending_pair_evidence(document: JournalDocument, index: int, roles: set[str]) -> bool:
    pending = document.pending_intents or {}
    valid_ops: dict[frozenset[str], set[str]] = {
        frozenset({"source", "apply_stage"}): {"apply_stage", "recovery_apply_stage", "recovery_apply_restore"},
        frozenset({"apply_stage", "destination"}): {"apply_commit", "recovery_apply_stage"},
        frozenset({"destination", "undo_stage"}): {"undo_stage", "recovery_undo_stage"},
        frozenset({"source", "undo_stage"}): {"undo_restore", "recovery_undo_stage", "recovery_undo_restore"},
    }
    operation_set = valid_ops.get(frozenset(roles), set())
    for record in pending.values():
        if record.get("event") == "move_intent" and record.get("index") == index and record.get("operation") in operation_set:
            return True
        if record.get("event") == "unlink_intent" and record.get("index") == index:
            location = record.get("location")
            operation = record.get("operation")
            expected = "recovery_apply_dedupe" if operation == "recovery_apply_dedupe" else "recovery_undo_dedupe"
            if operation == expected and location in roles:
                return True
    return False


def _inventory(document: JournalDocument, operation: str) -> tuple[dict[int, dict[str, Path]], list[str], dict[str, tuple[Path, tuple[int, int] | None]]]:
    """Locate each recorded inode without treating matching hashes as identity."""
    issues: list[str] = []
    locations: dict[int, dict[str, Path]] = {index: {} for index in range(len(document.actions))}
    owner_by_identity = {_identity(item): index for index, item in enumerate(document.actions)}
    candidates: dict[str, tuple[Path, list[tuple[int, str]]]] = {}
    for index in range(len(document.actions)):
        try:
            for role, path in _role_paths(document, index, operation).items():
                key = _same_path_key(path)
                if key not in candidates:
                    candidates[key] = (path, [])
                candidates[key][1].append((index, role))
        except (OSError, PlanError) as exc:
            issues.append(str(exc))

    stage_dir = document.transaction_dir / ("staged" if operation == "apply" else (document.undo_stage or ""))
    unsafe_stage = os.path.lexists(stage_dir) and (stage_dir.is_symlink() or not stage_dir.is_dir())
    if unsafe_stage:
        issues.append(f"staging path is not a real directory: {stage_dir}")

    foreign: dict[str, tuple[Path, tuple[int, int] | None]] = {}
    for key, (path, roles) in candidates.items():
        if unsafe_stage and any(role in {"apply_stage", "undo_stage"} for _, role in roles):
            continue
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            issues.append(f"cannot inspect {path}: {exc}")
            continue
        if not stat.S_ISREG(info.st_mode):
            issues.append(f"managed path is not a regular file: {path}")
            continue
        identity = (info.st_dev, info.st_ino)
        owner = owner_by_identity.get(identity)
        if owner is None:
            foreign[key] = (path, identity)
            continue
        item = document.actions[owner]
        if not file_matches(path, item):
            issues.append(f"recorded file identity has changed content or size: {path}")
            continue
        owner_roles = [role for action_index, role in roles if action_index == owner]
        if not owner_roles:
            issues.append(f"recorded file identity is at an unauthorized path: {path}")
            continue
        for role in owner_roles:
            locations[owner][role] = path

    for index, item_locations in locations.items():
        if not item_locations:
            issues.append(f"recorded file is missing or has been replaced: {document.actions[index]['source']}")
            continue
        if len(item_locations) > 1 and not _pending_pair_evidence(document, index, set(item_locations)):
            issues.append(f"multiple names for one file lack matching write-ahead evidence: {document.actions[index]['source']}")
        if len(item_locations) > 2:
            issues.append(f"file appears at too many transaction locations: {document.actions[index]['source']}")

    # Every requested final source slot must be empty or occupied by an
    # expected transaction file that will be staged before restoration.
    for index, item in enumerate(document.actions):
        try:
            target = root_path(document.root, item["source"])
        except PlanError as exc:
            issues.append(str(exc))
            continue
        key = _same_path_key(target)
        if key in foreign:
            issues.append(f"unrelated file occupies a recovery target: {target}")
    # An unrelated file inside RuleSort staging would otherwise be at risk of
    # being mistaken for transaction-owned state.
    for key, (path, _) in foreign.items():
        for _, role in candidates[key][1]:
            if role in {"apply_stage", "undo_stage"}:
                issues.append(f"unrelated file occupies transaction staging: {path}")
                break
    return locations, _unique(issues), foreign


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _report(document: JournalDocument) -> dict[str, Any]:
    state = _state(document)
    report: dict[str, Any] = {
        "journal": str(document.path),
        "root": str(document.root),
        "version": document.version,
        "state": state,
        "recoverable": False,
        "recovery_target": None,
        "issues": [],
        "actions": [],
    }
    if document.version == 1:
        report["issues"] = ["legacy journals do not contain complete write-ahead recovery evidence"]
        report["recoverable"] = False
        report["actions"] = [
            {"source": item["source"], "destination": item["destination"], "locations": []}
            for item in document.actions
        ]
        return report

    operation = "apply" if state == "apply_interrupted" else "undo" if state == "undo_interrupted" else None
    if operation:
        report["recovery_target"] = "originals"
        locations, issues, _ = _inventory(document, operation)
        report["issues"] = issues
        report["recoverable"] = not issues
        report["actions"] = [
            {"source": item["source"], "destination": item["destination"],
             "locations": sorted(locations[index])}
            for index, item in enumerate(document.actions)
        ]
        return report

    operation = "apply" if state in {"applied", "recovered_apply"} else "undo"
    locations, issues, _ = _inventory(document, operation)
    expected_role = "destination" if state == "applied" else "source"
    for index, item_locations in locations.items():
        if set(item_locations) != {expected_role}:
            issues.append(f"completed transaction file is not at its expected {expected_role}: {document.actions[index]['source']}")
    report["issues"] = _unique(issues)
    report["actions"] = [
        {"source": item["source"], "destination": item["destination"],
         "locations": sorted(locations[index])}
        for index, item in enumerate(document.actions)
    ]
    report["recoverable"] = False
    return report


def inspect_journal(path: Path) -> dict[str, Any]:
    _, root, _, _ = anchor_journal(path)
    with root_operation_lock(root):
        return _report(read_journal(path))


def _assert_same_owned_file(first: Path, second: Path, item: dict[str, Any]) -> None:
    if not file_matches(first, item) or not file_matches(second, item):
        raise ConflictError(f"file changed before recovery: {first}")
    first_info, second_info = first.stat(), second.stat()
    if (first_info.st_dev, first_info.st_ino) != (second_info.st_dev, second_info.st_ino):
        raise ConflictError(f"matching content does not prove both names are the same transaction file: {first}")


def _unlink_alias(journal: Journal, document: JournalDocument, operation: str, index: int,
                  location: str, alias: Path, canonical: Path) -> None:
    item = document.actions[index]
    _assert_same_owned_file(alias, canonical, item)
    intent_operation = "recovery_apply_dedupe" if operation == "apply" else "recovery_undo_dedupe"
    intent_id = journal.intent("unlink_intent", operation=intent_operation, index=index, location=location)
    alias.unlink()
    journal.write("unlink_complete", id=intent_id)


def _move_recorded(journal: Journal, operation_name: str, index: int,
                   source: Path, destination: Path, item: dict[str, Any]) -> None:
    if not file_matches(source, item):
        raise ConflictError(f"recorded file changed before recovery: {source}")
    intent_id = journal.intent("move_intent", operation=operation_name, index=index)
    move_no_replace(source, destination)
    if not file_matches(destination, item):
        raise ConflictError(f"recorded file changed during recovery: {destination}")
    journal.write("move_complete", id=intent_id)


def _resolve_pending_intents(journal: Journal, document: JournalDocument) -> None:
    for intent_id in sorted((document.pending_intents or {}).keys()):
        journal.write("intent_resolved", id=intent_id, reason="recovery")


def _run_recovery(document: JournalDocument, journal: Journal, operation: str,
                  locations: dict[int, dict[str, Path]]) -> None:
    stage_scope = "apply_stage" if operation == "apply" else "undo_stage"
    stage_name = "staged" if operation == "apply" else document.undo_stage
    if not stage_name:
        raise PlanError("journal does not identify the undo staging directory")
    stage_dir = document.transaction_dir / stage_name
    ensure_transaction_directory(stage_dir, stage_scope, journal)

    # Normalize every item into private staging before restoring any original
    # name. This is what makes cycles and case-folded source/destination sets
    # safe without overwrites.
    for index, item in enumerate(document.actions):
        current = locations[index]
        stage = current.get(stage_scope)
        if stage is not None:
            for location, alias in list(current.items()):
                if location != stage_scope:
                    _unlink_alias(journal, document, operation, index, location, alias, stage)
            locations[index] = {stage_scope: stage}
            continue
        if len(current) != 1:
            raise ConflictError(f"recovery cannot identify one location for {item['source']}")
        location, source = next(iter(current.items()))
        if location not in {"source", "destination"}:
            raise ConflictError(f"recovery found the file at an unsupported location: {source}")
        destination = stage_dir / item["stage_name"]
        operation_name = "recovery_apply_stage" if operation == "apply" else "recovery_undo_stage"
        _move_recorded(journal, operation_name, index, source, destination, item)
        locations[index] = {stage_scope: destination}

    # Restore from the private batch only after every transaction file has
    # left its source and destination slots.
    for index, item in enumerate(document.actions):
        current = locations[index]
        source = current.get(stage_scope)
        if source is None:
            raise ConflictError(f"recovery staging file is missing: {item['stage_name']}")
        target = root_path(document.root, item["source"])
        if os.path.lexists(target):
            info = target.lstat()
            if (info.st_dev, info.st_ino) == _identity(item):
                _unlink_alias(journal, document, operation, index, "source", source, target)
                locations[index] = {"source": target}
                continue
            raise ConflictError(f"unrelated file occupies recovery target: {target}")
        ensure_root_directory(target.parent, document.root, journal)
        operation_name = "recovery_apply_restore" if operation == "apply" else "recovery_undo_restore"
        _move_recorded(journal, operation_name, index, source, target, item)
        locations[index] = {"source": target}

    remove_created_root_directories(document.root, document.rows, journal)
    final_locations, issues, _ = _inventory(document, operation)
    if issues or any(set(found) != {"source"} for found in final_locations.values()):
        detail = "; ".join(issues) or "not every file reached its original path"
        raise ConflictError(f"recovery could not verify its final state: {detail}")
    _resolve_pending_intents(journal, document)


def recover_locked(path: Path, document: JournalDocument | None = None) -> dict[str, Any]:
    """Recover while the caller already holds the root operation lock."""
    document = document or read_journal(path)
    if document.version != JOURNAL_VERSION:
        raise ConflictError("legacy journal lacks write-ahead evidence; automatic recovery is unavailable")
    state = _state(document)
    if state in {"recovered_apply", "recovered_undo", "applied", "undone"}:
        return _report(document)
    if state == "apply_interrupted":
        operation = "apply"
    elif state == "undo_interrupted":
        operation = "undo"
    else:
        raise ConflictError(f"journal state {state} cannot be recovered")

    locations, issues, _ = _inventory(document, operation)
    if issues:
        raise ConflictError("recovery refused without changing files: " + "; ".join(issues))
    journal = Journal(document.path, next_id=document.next_intent_id)
    try:
        journal.write("recovery_started", operation=operation, target="originals")
        _run_recovery(document, journal, operation, locations)
        journal.write("recovery_complete", operation=operation, target="originals")
    finally:
        journal.close()
    return _report(read_journal(document.path))


def recover_journal(path: Path) -> dict[str, Any]:
    _, root, _, _ = anchor_journal(path)
    with root_operation_lock(root):
        return recover_locked(path)
