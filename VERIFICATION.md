# Verification

Local evidence recorded on 2026-10-03.

## Passed

10 unittest cases passed using real temporary files, including successful apply/undo, stale content, collisions, symlink scanning, no-replace behavior, protected plan output, and injected partial apply/undo failures.

## Environment and limits

Windows Python 3.12.14 on a hard-link-capable filesystem. Automatic crash recovery and cross-volume moves are not implemented.

The checked-in CI workflow is ready to run when published. It is configuration,
not evidence of a hosted pass. Re-run README commands after changing dependencies
or moving to another platform. Screenshots, where included, use synthetic data.
