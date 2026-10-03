# Verification

Local evidence recorded on 2026-10-03.

## Passed

10 unittest cases passed using real temporary files, including successful apply/undo, stale content, collisions, symlink scanning, no-replace behavior, protected plan output, and injected partial apply/undo failures.

## Environment and limits

Windows Python 3.12.14 on a hard-link-capable filesystem. Automatic crash recovery and cross-volume moves are not implemented.

## Hosted evidence

[GitHub Actions run](https://github.com/Saddidly/rulesort/actions/runs/37111981732) passed on 2026-10-03 for code revision `0f621715fbb3c157dafcb00fc1eb58571664a6db`.

Ubuntu and Windows, Python 3.10/3.12; filesystem tests.

These checks cover the named environments and cases, not every possible input or platform. Re-run README commands after changing dependencies or moving to another platform. Screenshots and acceptance data are synthetic.

