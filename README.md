# RuleSort

[![CI](https://github.com/Saddidly/rulesort/actions/workflows/ci.yml/badge.svg)](https://github.com/Saddidly/rulesort/actions/workflows/ci.yml)

RuleSort organizes files from explicit JSON rules. It separates deciding from changing: `plan` shows proposed moves and records preconditions, `apply` checks them again before moving anything, and each transaction has a journal for inspection, safe recovery, and undo.

## Install

    python -m pip install .

RuleSort has no runtime dependencies and supports Python 3.10 and newer. The module entry point is also available:

    python -m rulesort.cli --help

## Quick start

Copy `examples/rules.json` and set its root to the folder you want to organize. Then:

    rulesort plan examples/rules.json --out rulesort-plan.json
    rulesort apply rulesort-plan.json
    rulesort inspect /path/printed/by/apply/journal.jsonl
    rulesort undo /path/printed/by/apply/journal.jsonl

If a process stops during apply or undo, inspect the transaction before recovery:

    rulesort inspect /path/to/.rulesort-transactions/<transaction-id>/journal.jsonl
    rulesort recover /path/to/.rulesort-transactions/<transaction-id>/journal.jsonl

`inspect` reports the journal state, where each recorded file was found, and any reason recovery is unsafe. `recover` acts only on a validated interrupted v2 transaction. An interrupted apply is returned to original paths; an interrupted undo is completed to original paths. Both actions are idempotent and refuse ambiguity or unrelated files rather than overwriting or guessing. A second recovery after success is a no-op.

The plan command is a dry run and never changes managed files. It exits with code 2 when the plan contains conflicts, while still saving the report. Apply refuses a plan with conflicts unless `--allow-partial` is supplied. The `--json` option emits machine-readable reports for scripts.

## Rules

Rules are evaluated in order; the first matching rule wins. A rule can include any combination of:

- `extension` or `extensions`: case-insensitive extension match, with or without the leading dot.
- `name_glob`: case-insensitive filename glob.
- `name_regex`: regular expression searched against the filename.
- `size_min` and `size_max`: inclusive byte limits.
- `modified_after` and `modified_before`: ISO-8601 timestamps.
- `destination`: a directory under the root. Supported placeholders are `{name}`, `{stem}`, `{extension}`, `{year}`, `{month}`, `{day}`, and `{rule}`. The source filename is appended automatically.

A config can set `recursive` to true and choose `conflict_policy`: `error` reports collisions, `skip` records them as skipped, and `suffix` chooses an available numeric filename such as `report (1).pdf`. The scanner skips symlinked files and directories and ignores its own `.rulesort-transactions` directory.

## Safety and recovery

Plans record each source's relative path, byte size, nanosecond modification time, and SHA-256. Before apply, RuleSort validates the complete eligible batch, checks file identities and target availability, and rejects file/directory ancestor overlaps. Case-only renames are unsupported. Apply stages all inputs before committing any destination, so file cycles do not overwrite one another. Undo stages the complete applied batch before restoring original names.

Version 2 journals record each move intent before the filesystem change and completion after it, with each record flushed and passed to `fsync`. They bind the journal to its transaction directory and root and capture device/inode identity alongside size and hash. A hash match alone is not treated as proof that two paths name the same file. Recovery is offered only when journal structure, move evidence, file identity, content, and destination availability agree. Malformed or truncated journals are left untouched. Legacy v1 journals can be inspected and completed v1 applies can still be undone, but interrupted v1 operations lack enough evidence for automatic recovery.

Apply, undo, inspect, and recover take a native advisory lock for the root. The lock file at `.rulesort-transactions/.operation.lock` remains after use; the operating system releases the lock when a process exits, so no PID-based stale-lock deletion is needed. POSIX uses `flock`; Windows uses a byte-range lock. These guarantees depend on the filesystem correctly implementing those advisory locks. Network or unusual filesystems may not provide the required locking or hard-link behavior.

Moves use same-volume hard links and unlink the prior name. Filesystems without hard-link support are refused. Journals are local files and should be kept with backups for long-running workflows. Flushing journal records improves recovery evidence but does not promise that every filesystem, storage device, or operating system will preserve all directory and file changes after sudden power loss. RuleSort is not a substitute for a filesystem snapshot or backup.

RuleSort checks symlinks in existing destination path components. A hostile process changing directories between checks can still race an operation; run it against folders you control.

## Development

    python -m pip install -e .
    python -m unittest discover -s tests -v

The tests use real temporary files and subprocess termination to cover apply/undo, interrupted move boundaries, repeated recovery, cycles, content changes, unrelated collisions, malformed journals, hard links, root locking, and filesystem path conflicts.

## Architecture

- `models.py` validates configuration and evaluates metadata rules.
- `planning.py` scans without following symlinks, resolves destinations, and writes portable JSON plans.
- `transaction.py` validates plans and exposes the public apply/undo transaction operations.
- `journal.py` implements anchored journal parsing, write-ahead records, and per-root advisory locking.
- `filesystem.py` provides checked root paths, no-replace moves, file identity checks, and journaled directory changes.
- `recovery.py` inspects transaction state and safely resumes deterministic recovery.
- `cli.py` exposes plan, apply, undo, inspect, and recover with text or JSON output.

## License

MIT. See `LICENSE`.
