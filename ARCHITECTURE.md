# Architecture

`models.py` validates configuration and matches files against metadata rules.
`planning.py` builds a read-only inventory without following symlinks, resolves
relative destinations, and records source size, mtime, and SHA-256 preconditions
in a versioned plan. Plan artifacts use fresh paths and cannot replace scanned
inputs.

`transaction.py` validates the complete eligible batch while holding the root
operation lock, then stages inputs before committing any destination. It rejects
duplicate identities and file/directory ancestor overlaps before mutation.
Undo stages the complete applied batch before restoring original paths. Both
paths use same-volume, no-replace hard-link moves; there is no cross-volume
fallback.

`journal.py` owns JSONL v2 parsing and writing, strict transaction/container
anchoring, event-order validation, and the process-released advisory root lock.
Each move has a flushed write-ahead intent and a completion record. The v2
manifest captures relative source/destination paths, size, mtime, content hash,
device, and inode. Legacy v1 journals remain readable for inspection and
completed-apply undo, but they do not have sufficient evidence for crash
recovery.

`filesystem.py` centralizes checked root-relative paths, no-replace moves,
identity/content verification, and journaled directory creation/cleanup.
`recovery.py` inventories managed locations once under the root lock, refuses
unrelated or modified files, and performs deterministic recovery. Interrupted
apply returns all files to their original paths. Interrupted undo completes the
requested undo to original paths. Recovery first stages the entire batch, so
cycles do not overwrite; dual names are accepted only with matching inode
identity and a pending move intent. Recovery is repeatable after another process
interruption. Ambiguous, malformed, or truncated journal state is never repaired
automatically.

`cli.py` exposes `plan`, `apply`, `undo`, `inspect`, and `recover`, including
machine-readable JSON reports. Inspect and recover acquire the same root lock as
mutating operations so their reports and decisions are based on a coherent
single-RuleSort-operation view.

The advisory lock uses `flock` on POSIX and a byte-range lock on Windows. The
lock file remains in `.rulesort-transactions`; the operating system releases the
lock after process exit. Lock correctness depends on the filesystem's advisory
locking implementation. The move primitive similarly requires hard-link
support. Journal records are flushed and passed to `fsync`, but that alone does
not guarantee directory-entry or device-cache persistence after power loss.

These checks assume trusted, stable directories. A hostile process can race
filesystem checks, and journals do not replace a backup or snapshot. Case-only
renames are unsupported.
