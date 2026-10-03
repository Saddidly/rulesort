from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator
import errno
import json
import os
import re
import stat

from .models import ConflictError, PlanError
from .planning import TRANSACTION_DIR


JOURNAL_VERSION = 2
_TX_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STAGE_NAME = re.compile(r"^[0-9]{8}$")
_UNDO_STAGE = re.compile(r"^undo-staged-[0-9a-f]{32}$")


class Journal:
    """Append-only journal whose records are flushed before control returns."""

    def __init__(self, path: Path, next_id: int = 1, create: bool = False):
        self.path = path
        self._next_id = next_id
        mode = "x" if create else "a"
        self._handle = path.open(mode, encoding="utf-8", newline="\n")

    def write(self, event: str, **data: Any) -> None:
        record = {"event": event, **data}
        self._handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def intent(self, event: str, **data: Any) -> int:
        intent_id = self._next_id
        self._next_id += 1
        self.write(event, id=intent_id, **data)
        return intent_id

    def close(self) -> None:
        self._handle.close()


@dataclass(frozen=True)
class JournalDocument:
    path: Path
    root: Path
    transaction_dir: Path
    version: int
    rows: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    tx_id: str
    applied: bool = False
    undo_started: bool = False
    undo_stage: str | None = None
    undo_complete: bool = False
    recovery_complete: tuple[str, str] | None = None
    recovery_active: tuple[str, str] | None = None
    pending_intents: dict[int, dict[str, Any]] | None = None
    next_intent_id: int = 1


@contextmanager
def root_operation_lock(root: Path) -> Iterator[None]:
    """Serialize RuleSort operations using a native process-released file lock."""
    root = root.resolve()
    if not root.is_dir():
        raise PlanError(f"transaction root is unavailable: {root}")
    transaction_root = root / TRANSACTION_DIR
    if transaction_root.is_symlink():
        raise PlanError(f"transaction directory must not be a symlink: {transaction_root}")
    transaction_root.mkdir(exist_ok=True)
    lock_path = transaction_root / ".operation.lock"
    if lock_path.is_symlink():
        raise PlanError(f"operation lock must not be a symlink: {lock_path}")

    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise PlanError(f"cannot open the RuleSort operation lock {lock_path}: {exc}") from exc

    locked = False
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise PlanError(f"operation lock is not a regular file: {lock_path}")
        if os.name == "nt":
            import msvcrt

            if info.st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EDEADLK, errno.EAGAIN}:
                    raise ConflictError(f"another RuleSort operation holds the root lock for {root}") from exc
                raise
            locked = True
        else:
            import fcntl

            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise ConflictError(f"another RuleSort operation holds the root lock for {root}") from exc
                raise
            locked = True
        yield
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def anchor_journal(path: Path) -> tuple[Path, Path, Path, str]:
    """Validate the journal's container without trusting journal contents."""
    candidate = Path(os.path.abspath(path))
    if candidate.name != "journal.jsonl":
        raise PlanError("journal must be named journal.jsonl inside RuleSort transaction storage")
    transaction_dir = candidate.parent
    transaction_root = transaction_dir.parent
    tx_id = transaction_dir.name
    if not _TX_ID.fullmatch(tx_id):
        raise PlanError("journal is not inside a valid RuleSort transaction directory")
    if transaction_dir.is_symlink() or transaction_root.is_symlink():
        raise PlanError("journal transaction path must not contain a symlink")
    if transaction_root.name != TRANSACTION_DIR or not transaction_root.is_dir():
        raise PlanError("journal is not inside the root's RuleSort transaction directory")
    if candidate.is_symlink():
        raise PlanError("journal file must not be a symlink")
    root = transaction_root.parent.resolve()
    canonical_transaction_root = root / TRANSACTION_DIR
    if transaction_root.resolve() != canonical_transaction_root.resolve():
        raise PlanError("journal transaction directory does not belong to its resolved root")
    if transaction_dir.resolve().parent != canonical_transaction_root.resolve():
        raise PlanError("journal transaction path is not canonical")
    if not candidate.is_file():
        raise PlanError(f"journal file is unavailable: {candidate}")
    return candidate.resolve(), root, transaction_dir.resolve(), tx_id


def _relative_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value == ".":
        raise PlanError(f"journal {field} must be a non-empty relative path")
    relative = PurePosixPath(value)
    if (
        "\\" in value
        or ":" in value
        or relative.is_absolute()
        or relative.as_posix() != value
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise PlanError(f"journal contains an unsafe {field}: {value!r}")
    if relative.parts[0].casefold() == TRANSACTION_DIR.casefold():
        raise PlanError(f"journal {field} cannot use RuleSort transaction storage")
    return value


def _strict_int(value: Any, field: str, minimum: int | None = None) -> int:
    if type(value) is not int or (minimum is not None and value < minimum):
        raise PlanError(f"journal {field} must be an integer" + (f" >= {minimum}" if minimum is not None else ""))
    return value


def _validate_v2_action(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PlanError(f"journal actions[{index}] must be an object")
    expected = {"source", "destination", "stage_name", "size", "mtime_ns", "sha256", "device", "inode"}
    if set(raw) != expected:
        raise PlanError(f"journal actions[{index}] has missing or unexpected fields")
    item = dict(raw)
    item["source"] = _relative_path(item["source"], f"actions[{index}].source")
    item["destination"] = _relative_path(item["destination"], f"actions[{index}].destination")
    if item["source"].casefold() == item["destination"].casefold():
        raise PlanError(f"journal actions[{index}] has the same source and destination")
    if not isinstance(item["stage_name"], str) or not _STAGE_NAME.fullmatch(item["stage_name"]):
        raise PlanError(f"journal actions[{index}].stage_name is invalid")
    _strict_int(item["size"], f"actions[{index}].size", 0)
    _strict_int(item["mtime_ns"], f"actions[{index}].mtime_ns")
    _strict_int(item["device"], f"actions[{index}].device", 0)
    _strict_int(item["inode"], f"actions[{index}].inode", 0)
    if not isinstance(item["sha256"], str) or not _SHA256.fullmatch(item["sha256"]):
        raise PlanError(f"journal actions[{index}].sha256 is invalid")
    return item


def _validate_actions(raw: Any, version: int) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise PlanError("journal contains no actions")
    actions: list[dict[str, Any]] = []
    sources: dict[str, str] = {}
    destinations: dict[str, str] = {}
    path_spellings: dict[str, str] = {}
    identities: set[tuple[int, int]] = set()
    for index, entry in enumerate(raw):
        if version == JOURNAL_VERSION:
            item = _validate_v2_action(entry, index)
            if item["stage_name"] != f"{index:08d}":
                raise PlanError("journal stage names do not match action order")
            if item["inode"] <= 0:
                raise PlanError("journal filesystem identity is unavailable; automatic recovery is unsafe")
            identity = (item["device"], item["inode"])
            if item["inode"] > 0 and identity in identities:
                raise PlanError("journal contains multiple actions for one filesystem object")
            identities.add(identity)
        else:
            if not isinstance(entry, dict):
                raise PlanError(f"journal actions[{index}] must be an object")
            item = dict(entry)
            item["source"] = _relative_path(item.get("source"), f"actions[{index}].source")
            item["destination"] = _relative_path(item.get("destination"), f"actions[{index}].destination")
            if item["source"].casefold() == item["destination"].casefold():
                raise PlanError(f"legacy journal actions[{index}] has the same source and destination")
            if not isinstance(item.get("sha256"), str) or not _SHA256.fullmatch(item["sha256"]):
                raise PlanError(f"journal actions[{index}].sha256 is invalid")
            if "stage_name" in item and (not isinstance(item["stage_name"], str) or not _STAGE_NAME.fullmatch(item["stage_name"])):
                raise PlanError(f"journal actions[{index}].stage_name is invalid")
            if "size" in item:
                _strict_int(item["size"], f"legacy actions[{index}].size", 0)
        for label in ("source", "destination"):
            value = item[label]
            key = value.casefold()
            previous = path_spellings.get(key)
            if previous is not None and previous != value:
                raise PlanError("journal paths differ only by case and are ambiguous across platforms")
            path_spellings[key] = value
        source_key = item["source"].casefold()
        destination_key = item["destination"].casefold()
        if source_key in sources or destination_key in destinations:
            raise PlanError("journal contains duplicate sources or destinations")
        sources[source_key] = item["source"]
        destinations[destination_key] = item["destination"]
        actions.append(item)
    return actions


def _validate_v1_rows(rows: list[dict[str, Any]], tx_id: str, root: Path, transaction_dir: Path) -> JournalDocument:
    start = rows[0]
    if start.get("event") != "transaction_started" or set(start) != {"event", "id", "root", "actions"}:
        raise PlanError("legacy journal has a malformed transaction header")
    if start["id"] != tx_id or not isinstance(start["root"], str) or Path(start["root"]).resolve() != root:
        raise PlanError("legacy journal root or transaction id does not match its container")
    actions = _validate_actions(start["actions"], 1)
    allowed_events = {
        "transaction_started", "source_staged", "directory_created", "destination_written",
        "transaction_applied", "apply_failed", "rollback_destination_staged", "rollback_error",
        "rollback_source_restored", "rollback_complete", "rollback_incomplete", "undo_started",
        "undo_file_staged", "original_restored", "undo_complete", "undo_rollback_error",
        "undo_rollback_destination_restored", "undo_rollback_complete", "undo_rollback_incomplete",
    }
    for row in rows[1:]:
        if set(row) - {"event", "source", "destination", "stage", "path", "error", "actions"}:
            raise PlanError("legacy journal contains unexpected fields")
        if row.get("event") not in allowed_events - {"transaction_started"}:
            raise PlanError("legacy journal contains an unknown event")
        for field in ("source", "destination", "path"):
            if field in row:
                _relative_path(row[field], f"legacy event {field}")
        for field in ("error",):
            if field in row and not isinstance(row[field], str):
                raise PlanError(f"legacy event {field} must be text")
    applied = any(row.get("event") == "transaction_applied" for row in rows)
    undo_started = any(row.get("event") == "undo_started" for row in rows)
    undo_complete = any(row.get("event") == "undo_complete" for row in rows)
    return JournalDocument(transaction_dir / "journal.jsonl", root, transaction_dir, 1, rows, actions, tx_id,
                           applied=applied, undo_started=undo_started, undo_complete=undo_complete)


def _validate_v2_rows(rows: list[dict[str, Any]], tx_id: str, root: Path, transaction_dir: Path) -> JournalDocument:
    start = rows[0]
    expected_start_fields = {"event", "version", "id", "root", "actions"}
    if start.get("event") != "transaction_started" or set(start) != expected_start_fields:
        raise PlanError("journal has a malformed transaction header")
    if start.get("version") != JOURNAL_VERSION or type(start.get("version")) is not int:
        raise PlanError("journal version is unsupported")
    if start.get("id") != tx_id or not isinstance(start.get("root"), str) or Path(start["root"]).resolve() != root:
        raise PlanError("journal root or transaction id does not match its container")
    actions = _validate_actions(start["actions"], JOURNAL_VERSION)
    pending: dict[int, dict[str, Any]] = {}
    max_id = 0
    applied = undo_started = undo_complete = False
    undo_stage: str | None = None
    recovery_active: tuple[str, str] | None = None
    recovery_complete: tuple[str, str] | None = None
    operation_indices: dict[str, set[int]] = {}
    apply_failed = undo_failed = False

    for row in rows[1:]:
        if not isinstance(row.get("event"), str):
            raise PlanError("journal event name is missing")
        event = row["event"]
        if recovery_complete is not None:
            raise PlanError("journal contains an event after terminal recovery completion")
        if recovery_active is not None and event in {
            "transaction_applied", "undo_started", "undo_complete", "apply_failed", "undo_failed",
        }:
            raise PlanError(f"journal {event} is not allowed during active recovery")
        if event in {"move_intent", "unlink_intent", "directory_intent"}:
            if recovery_complete is not None or undo_complete:
                raise PlanError("journal contains an operation after its terminal event")
            intent_id = _strict_int(row.get("id"), "intent id", 1)
            if intent_id <= max_id or intent_id in pending:
                raise PlanError("journal intent ids are not strictly increasing")
            max_id = intent_id
            if event == "move_intent":
                if set(row) != {"event", "id", "operation", "index"}:
                    raise PlanError("journal move intent has unexpected fields")
                operation = row.get("operation")
                index = _strict_int(row.get("index"), "move intent index", 0)
                if index >= len(actions):
                    raise PlanError("journal move intent references an unknown action")
                if operation not in {"apply_stage", "apply_commit", "undo_stage", "undo_restore",
                                     "recovery_apply_stage", "recovery_apply_restore",
                                     "recovery_undo_stage", "recovery_undo_restore"}:
                    raise PlanError("journal move intent has an unknown operation")
                if operation.startswith("undo_") and (not applied or not undo_started or undo_complete):
                    raise PlanError("journal undo move is out of sequence")
                if operation == "apply_stage":
                    if applied or undo_started or apply_failed or recovery_active is not None or operation_indices.get("apply_commit"):
                        raise PlanError("journal apply staging move is out of sequence")
                    if index != len(operation_indices.get(operation, set())):
                        raise PlanError("journal apply staging actions are out of order")
                elif operation == "apply_commit":
                    if applied or undo_started or apply_failed or recovery_active is not None:
                        raise PlanError("journal apply commit is out of sequence")
                    if operation_indices.get("apply_stage", set()) != set(range(len(actions))):
                        raise PlanError("journal commits begin before every source was staged")
                    if index != len(operation_indices.get(operation, set())):
                        raise PlanError("journal apply commits are out of order")
                elif operation == "undo_stage":
                    if undo_failed or recovery_active is not None or operation_indices.get("undo_restore"):
                        raise PlanError("journal undo staging move is out of sequence")
                    if index != len(operation_indices.get(operation, set())):
                        raise PlanError("journal undo staging actions are out of order")
                elif operation == "undo_restore":
                    if undo_failed or recovery_active is not None:
                        raise PlanError("journal undo restore is out of sequence")
                    if operation_indices.get("undo_stage", set()) != set(range(len(actions))):
                        raise PlanError("undo restoration begins before every applied file was staged")
                    if index != len(operation_indices.get(operation, set())):
                        raise PlanError("journal undo restorations are out of order")
                if operation.startswith("recovery_apply_") and recovery_active != ("apply", "originals"):
                    raise PlanError("journal apply recovery move is out of sequence")
                if operation.startswith("recovery_undo_") and recovery_active != ("undo", "originals"):
                    raise PlanError("journal undo recovery move is out of sequence")
                if operation in {"apply_stage", "apply_commit", "undo_stage", "undo_restore"}:
                    already_pending = any(item.get("operation") == operation and item.get("index") == index
                                          for item in pending.values() if item.get("event") == "move_intent")
                    completed = index in operation_indices.get(operation, set())
                    if already_pending or completed:
                        raise PlanError(f"journal repeats {operation} for one action")
                pending[intent_id] = dict(row)
            elif event == "unlink_intent":
                if set(row) != {"event", "id", "operation", "index", "location"}:
                    raise PlanError("journal unlink intent has unexpected fields")
                operation = row.get("operation")
                index = _strict_int(row.get("index"), "unlink intent index", 0)
                if index >= len(actions) or operation not in {"recovery_apply_dedupe", "recovery_undo_dedupe"}:
                    raise PlanError("journal unlink intent is invalid")
                expected = ("apply", "originals") if operation == "recovery_apply_dedupe" else ("undo", "originals")
                if recovery_active != expected:
                    raise PlanError("journal unlink intent is out of sequence")
                allowed_locations = {"source", "destination", "apply_stage", "undo_stage"}
                if row.get("location") not in allowed_locations:
                    raise PlanError("journal unlink intent has an invalid location")
                pending[intent_id] = dict(row)
            else:
                scope = row.get("scope")
                expected_fields = {"event", "id", "operation", "scope"} | ({"path"} if scope == "root" else set())
                if set(row) != expected_fields or row.get("operation") not in {"create", "remove"} or scope not in {"root", "apply_stage", "undo_stage"}:
                    raise PlanError("journal directory intent is malformed")
                if scope == "root":
                    _relative_path(row.get("path"), "directory intent path")
                elif row.get("operation") == "remove":
                    raise PlanError("transaction staging directories cannot be removed by journal intent")
                pending[intent_id] = dict(row)
        elif event in {"move_complete", "unlink_complete", "directory_complete", "directory_skipped", "intent_resolved"}:
            expected_fields = {"event", "id"} if event != "intent_resolved" else {"event", "id", "reason"}
            intent_id = _strict_int(row.get("id"), f"{event} id", 1)
            intent = pending.get(intent_id)
            if intent is None:
                raise PlanError(f"journal {event} refers to no pending intent")
            expected_event = {"move_intent": "move_complete", "unlink_intent": "unlink_complete", "directory_intent": "directory_complete"}[intent["event"]]
            if event == "directory_complete" and intent["event"] == "directory_intent" and intent.get("scope") == "root" and intent.get("operation") == "create":
                expected_fields |= {"device", "inode"}
                _strict_int(row.get("device"), "created directory device", 0)
                _strict_int(row.get("inode"), "created directory inode", 0)
            if set(row) != expected_fields:
                raise PlanError(f"journal {event} has unexpected fields")
            if event == "intent_resolved":
                if row.get("reason") != "recovery" or recovery_active is None:
                    raise PlanError("journal intent resolution is invalid")
            elif event not in ({expected_event, "directory_skipped"} if intent["event"] == "directory_intent" else {expected_event}):
                raise PlanError(f"journal {event} does not match its intent")
            del pending[intent_id]
            if intent["event"] == "move_intent" and event == "move_complete":
                operation = intent["operation"]
                operation_indices.setdefault(operation, set()).add(intent["index"])
        elif event == "transaction_applied":
            if set(row) != {"event"} or applied or undo_started or apply_failed or pending or recovery_active is not None:
                raise PlanError("journal apply completion is out of sequence")
            expected_count = len(actions)
            expected_indices = set(range(expected_count))
            if operation_indices.get("apply_stage", set()) != expected_indices or operation_indices.get("apply_commit", set()) != expected_indices:
                raise PlanError("journal apply completion is missing move evidence")
            applied = True
        elif event == "undo_started":
            if set(row) != {"event", "stage"} or not applied or undo_started or pending or recovery_active is not None:
                raise PlanError("journal undo start is out of sequence")
            stage = row.get("stage")
            if not isinstance(stage, str) or not _UNDO_STAGE.fullmatch(stage):
                raise PlanError("journal undo stage is invalid")
            undo_started, undo_stage = True, stage
        elif event == "undo_complete":
            if set(row) != {"event"} or not undo_started or undo_complete or pending or recovery_active is not None:
                raise PlanError("journal undo completion is out of sequence")
            expected_count = len(actions)
            expected_indices = set(range(expected_count))
            if operation_indices.get("undo_stage", set()) != expected_indices or operation_indices.get("undo_restore", set()) != expected_indices:
                raise PlanError("journal undo completion is missing move evidence")
            undo_complete = True
        elif event == "recovery_started":
            if set(row) != {"event", "operation", "target"}:
                raise PlanError("journal recovery start is out of sequence")
            operation, target = row.get("operation"), row.get("target")
            candidate = (operation, target)
            if candidate == ("apply", "originals"):
                if applied or undo_started or recovery_complete is not None:
                    raise PlanError("apply recovery is out of sequence")
            elif candidate == ("undo", "originals"):
                if not applied or not undo_started or undo_complete or recovery_complete is not None:
                    raise PlanError("undo recovery is out of sequence")
            else:
                raise PlanError("journal recovery target is invalid")
            if recovery_active is not None and recovery_active != candidate:
                raise PlanError("journal recovery target changed mid-transaction")
            recovery_active = candidate
        elif event == "recovery_complete":
            if set(row) != {"event", "operation", "target"} or pending or recovery_active is None:
                raise PlanError("journal recovery completion is out of sequence")
            candidate = (row.get("operation"), row.get("target"))
            if candidate != recovery_active:
                raise PlanError("journal recovery completion does not match its start")
            recovery_complete = candidate
            recovery_active = None
        elif event in {"apply_failed", "undo_failed"}:
            if set(row) != {"event", "error"} or not isinstance(row.get("error"), str):
                raise PlanError(f"journal {event} record is malformed")
            if event == "apply_failed":
                if applied or undo_started or recovery_active is not None:
                    raise PlanError("journal apply failure is out of sequence")
                apply_failed = True
            else:
                if not undo_started or undo_complete or recovery_active is not None:
                    raise PlanError("journal undo failure is out of sequence")
                undo_failed = True
        else:
            raise PlanError(f"journal contains an unknown event: {event}")

    return JournalDocument(transaction_dir / "journal.jsonl", root, transaction_dir, JOURNAL_VERSION,
                           rows, actions, tx_id, applied, undo_started, undo_stage, undo_complete,
                           recovery_complete, recovery_active, pending, max_id + 1)


def read_journal(path: Path) -> JournalDocument:
    journal_path, root, transaction_dir, tx_id = anchor_journal(path)
    try:
        raw = journal_path.read_bytes()
    except OSError as exc:
        raise PlanError(f"cannot read transaction journal {journal_path}: {exc}") from exc
    if not raw or not raw.endswith(b"\n"):
        raise PlanError("transaction journal is empty or has a truncated final record; it is ambiguous and was not changed")
    try:
        text = raw.decode("utf-8")
        rows = [json.loads(line) for line in text.splitlines()]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanError(f"cannot parse transaction journal {journal_path}: {exc}") from exc
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise PlanError("transaction journal contains malformed records")
    first = rows[0]
    version = first.get("version", 1) if isinstance(first, dict) else None
    if version == 1:
        return _validate_v1_rows(rows, tx_id, root, transaction_dir)
    if version == JOURNAL_VERSION:
        return _validate_v2_rows(rows, tx_id, root, transaction_dir)
    raise PlanError("transaction journal has an unsupported version")
