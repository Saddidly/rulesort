from __future__ import annotations

from pathlib import Path, PurePosixPath
import os
import stat

from .models import ConflictError, PlanError
from .journal import Journal
from .planning import TRANSACTION_DIR, sha256_file


def root_path(root: Path, relative: str) -> Path:
    rel = PurePosixPath(relative)
    if (
        not relative
        or relative == "."
        or "\\" in relative
        or ":" in relative
        or rel.is_absolute()
        or rel.as_posix() != relative
        or ".." in rel.parts
        or not rel.parts
    ):
        raise PlanError(f"unsafe relative path: {relative!r}")
    if rel.parts[0].casefold() == TRANSACTION_DIR.casefold():
        raise PlanError("RuleSort transaction storage cannot be a source or destination")
    root = root.resolve()
    path = root.joinpath(*rel.parts)
    resolved_parent = path.parent.resolve(strict=False)
    try:
        resolved_parent.relative_to(root)
    except ValueError as exc:
        raise PlanError(f"path parent escapes root through a symlink: {relative}") from exc
    cursor = root
    for part in rel.parts[:-1]:
        cursor = cursor / part
        if os.path.lexists(cursor) and cursor.is_symlink():
            raise PlanError(f"path crosses a symlink: {relative}")
    return path


def require_regular_file(path: Path, description: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ConflictError(f"cannot inspect {description} {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ConflictError(f"{description} is not a regular file: {path}")
    return info


def move_no_replace(source: Path, destination: Path) -> None:
    """Move a regular file on one filesystem without replacing any name.

    The caller must append and fsync a write-ahead intent before calling this.
    A failure after linking deliberately leaves both names for evidence-based
    recovery; deleting the extra name here would add an unjournaled boundary.
    """
    source_info = require_regular_file(source, "move source")
    if os.path.lexists(destination):
        raise FileExistsError(f"move destination already exists: {destination}")
    os.link(source, destination, follow_symlinks=False)
    try:
        linked_source = require_regular_file(source, "move source")
        linked_destination = require_regular_file(destination, "move destination")
        if (linked_source.st_dev, linked_source.st_ino) != (linked_destination.st_dev, linked_destination.st_ino):
            raise ConflictError(f"source changed while publishing destination: {source}")
        if (source_info.st_dev, source_info.st_ino) != (linked_source.st_dev, linked_source.st_ino):
            raise ConflictError(f"source identity changed while publishing destination: {source}")
        source.unlink()
    except Exception:
        # Preserve both names when unlink fails or the filesystem changes under
        # us. The journal intent lets inspection identify this exact boundary.
        raise


def file_matches(path: Path, action: dict[str, object]) -> bool:
    try:
        info = require_regular_file(path, "transaction file")
        return (
            info.st_size == action["size"]
            and info.st_dev == action["device"]
            and info.st_ino == action["inode"]
            and sha256_file(path) == action["sha256"]
        )
    except (ConflictError, OSError):
        return False


def ensure_root_directory(directory: Path, root: Path, journal: Journal) -> None:
    """Create missing root-relative parent directories with write-ahead records."""
    root = root.resolve()
    try:
        directory.relative_to(root)
    except ValueError as exc:
        raise PlanError(f"directory is outside root: {directory}") from exc
    missing: list[Path] = []
    cursor = directory
    while cursor != root and not os.path.lexists(cursor):
        missing.append(cursor)
        if cursor.parent == cursor:
            raise PlanError(f"directory is outside root: {directory}")
        cursor = cursor.parent
    if cursor != root:
        if cursor.is_symlink() or not cursor.is_dir():
            raise ConflictError(f"parent path is not a safe directory: {cursor}")
    for item in reversed(missing):
        relative = item.relative_to(root).as_posix()
        root_path(root, relative)
        intent_id = journal.intent("directory_intent", operation="create", scope="root", path=relative)
        try:
            item.mkdir()
        except OSError:
            journal.write("directory_skipped", id=intent_id)
            raise
        info = item.lstat()
        journal.write("directory_complete", id=intent_id, device=info.st_dev, inode=info.st_ino)


def ensure_transaction_directory(path: Path, scope: str, journal: Journal) -> None:
    if scope not in {"apply_stage", "undo_stage"}:
        raise ValueError(f"unsupported transaction directory scope: {scope}")
    if os.path.lexists(path):
        if path.is_symlink() or not path.is_dir():
            raise ConflictError(f"transaction staging path is not a directory: {path}")
        return
    intent_id = journal.intent("directory_intent", operation="create", scope=scope)
    try:
        path.mkdir()
    except OSError:
        journal.write("directory_skipped", id=intent_id)
        raise
    journal.write("directory_complete", id=intent_id)


def remove_created_root_directories(root: Path, rows: list[dict[str, object]], journal: Journal) -> None:
    created_by_id: dict[int, str] = {}
    completed_identity: dict[int, tuple[int, int]] = {}
    for row in rows:
        event = row.get("event")
        intent_id = row.get("id")
        if event == "directory_intent" and row.get("scope") == "root" and row.get("operation") == "create":
            path = row.get("path")
            if type(intent_id) is int and isinstance(path, str):
                created_by_id[intent_id] = path
        elif event == "directory_complete" and type(intent_id) is int and type(row.get("device")) is int and type(row.get("inode")) is int:
            completed_identity[intent_id] = (row["device"], row["inode"])
    created = [(created_by_id[intent_id], identity) for intent_id, identity in completed_identity.items()
               if intent_id in created_by_id]
    for relative, expected_identity in reversed(created):
        directory = root_path(root, relative)
        if not os.path.lexists(directory):
            continue
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            info = directory.lstat()
        except OSError:
            continue
        if (info.st_dev, info.st_ino) != expected_identity:
            continue
        try:
            with os.scandir(directory) as entries:
                if next(entries, None) is not None:
                    continue
        except OSError:
            continue
        intent_id = journal.intent("directory_intent", operation="remove", scope="root", path=relative)
        try:
            directory.rmdir()
        except OSError:
            journal.write("directory_skipped", id=intent_id)
        else:
            journal.write("directory_complete", id=intent_id)
