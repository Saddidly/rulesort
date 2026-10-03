# RuleSort

RuleSort organizes files from explicit JSON rules. It separates deciding from changing: the plan command shows every proposed move and saves its preconditions; apply checks those preconditions again, performs a staged batch, and records an fsynced journal that can undo a completed transaction.

## Install

    python -m pip install .

The project has no runtime dependencies. After installation, the module entry point is also available:

    python -m rulesort.cli --help

## Quick start

Copy examples/rules.json and set its root to the folder you want to organize. Then:

    rulesort plan examples/rules.json --out rulesort-plan.json
    rulesort apply rulesort-plan.json
    rulesort undo /path/printed/by/apply/journal.jsonl

The plan command is a dry run and never changes files. It exits with code 2 when the plan contains conflicts, while still saving the report. Apply refuses a plan with conflicts unless --allow-partial is supplied. The --json option emits machine-readable reports for scripts.

## Rules

Rules are evaluated in order; the first matching rule wins. A rule can include any combination of:

- extension or extensions: case-insensitive extension match, with or without the leading dot.
- name_glob: case-insensitive filename glob.
- name_regex: regular expression searched against the filename.
- size_min and size_max: inclusive byte limits.
- modified_after and modified_before: ISO-8601 timestamps.
- destination: a directory under the root. Supported placeholders are {name}, {stem}, {extension}, {year}, {month}, {day}, and {rule}. The source filename is appended automatically.

A config can set recursive to true and choose conflict_policy: error reports collisions, skip records them as skipped, and suffix chooses an available numeric filename such as report (1).pdf. The scanner skips symlinked files and directories and ignores its own .rulesort-transactions journal directory.

## Safety model

Plans store each source's relative path, byte size, nanosecond modification time, and SHA-256. Apply checks all eligible inputs and destination collisions before moving anything. It then moves every source into a private same-volume staging directory before creating destinations, on a filesystem supporting hard links. Cross-volume moves and filesystems without hard-link support are refused. Each state change is appended and flushed to the transaction journal.

Undo verifies the moved content hashes and checks that original names are available before staging the whole batch back. If a precondition fails, the operation stops before mutation. If a filesystem error occurs mid-transaction, RuleSort attempts rollback and records whether rollback completed; inspect the journal before retrying after an incomplete rollback. Plans and journals are local files and should be kept with backups for long-running workflows.

RuleSort checks symlinks in existing destination path components. A hostile process changing directories between checks can still race the operation; run against folders you control. The journal provides recovery evidence, not a substitute for a filesystem snapshot or backup.

## Development

    python -m pip install -e .
    python -m unittest discover -s tests -v

The tests cover apply/undo, stale-plan refusal, destination conflicts, case-insensitive collision detection, and recursive symlink handling.

## Architecture

- models.py validates configuration and evaluates metadata rules.
- planning.py scans without following symlinks, resolves destinations, and writes portable JSON plans.
- transaction.py owns precondition checks, staging, durable JSONL journal events, rollback, and undo.
- cli.py exposes plan, apply, and undo with text or JSON output.

## License

MIT. See LICENSE.

Plan output must be a fresh path and cannot replace an input file. Ordinary I/O failures trigger rollback. After process termination, power loss, or an interrupted undo, inspect the journal and staging directory before manual recovery; automatic crash recovery is not implemented.
