# Architecture

`models.py` validates rules and matches file metadata. `planning.py` performs a
read-only inventory without following symlinks, resolves relative destinations,
and records source size, mtime, and SHA-256 preconditions in a versioned plan.
Plan artifacts use fresh paths and cannot replace scanned inputs.

`transaction.py` validates the complete eligible batch before mutation. Files
stage under a transaction-specific directory on the same filesystem. Each
no-replace move links a new name and removes its old name; hard-link support is
required. Existing targets are never overwritten. Journal events are JSONL and
fsynced. Ordinary I/O failures attempt rollback; partial rollback or process
termination requires inspecting journal/staging evidence before manual recovery.

Undo validates completed history and current content, stages all moved files,
then restores original paths. Its rollback handles restoration failures without
clobbering unrelated files. Interrupted undo is not automatically replayed.
The CLI owns arguments, exit codes, and report presentation. Tests inject failures
at staged commit/restore boundaries using real temporary files.

These checks assume trusted, stable directories. A hostile process can race
filesystem checks, and journals do not replace a backup or snapshot. No remote
service, watcher, cross-volume move, or automatic crash repair is implemented.
