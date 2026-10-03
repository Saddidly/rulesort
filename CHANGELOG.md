# Changelog

## 1.1.0

- Added strict v2 write-ahead journals, `inspect` and `recover` commands, and deterministic recovery for interrupted operations. Recovery rolls interrupted applies back to original names and completes interrupted undo to original names; it refuses changed files, unrelated collisions, malformed journals, and ambiguous inode evidence.
- Added a per-root native advisory lock for apply, undo, inspect, and recover. The persistent lock file is released by the operating system when its process exits.
- Added preflight checks for hard-linked planned inputs, unusable file identity, existing non-directory destination parents, and file/directory ancestor overlaps.
- Kept legacy v1 journals available for safe inspection and completed-apply undo; interrupted v1 operations remain manual-recovery cases.
- Added real-filesystem and subprocess interruption coverage and documented filesystem, locking, and power-loss limits.

## 1.0.0

Initial implemented release with rule-based plans, staged apply, journals, and undo.
