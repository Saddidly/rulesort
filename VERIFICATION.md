# Verification

Local evidence recorded on 2026-10-03 for the 1.1.0 working tree.

## Passed locally

The 32 unittest cases passed using Windows Python 3.12.14 from the bundled
workspace runtime. The suite uses temporary real files and subprocesses, and
covers plan/apply/undo, failure injection around link/unlink boundaries,
interrupted and repeated recovery, two- and three-file cycles, changed content, unrelated
destination collisions, malformed or truncated journals, hard-linked inputs,
ancestor conflicts, and competing root operations.

Commands run from the repository root:

```powershell
$env:PYTHONPATH = "src"
& "C:\Users\jinxa\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m compileall -q src/rulesort tests
& "C:\Users\jinxa\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m unittest discover -s tests -v
```

The suite completed with `Ran 32 tests` and `OK`; `compileall` also completed
without diagnostics.

The parent review also reported a separate installed-wheel CLI acceptance pass:
two-file plan/apply/inspect/undo preserved the expected bytes, and an occupied
target was refused without changing any file.

## Limits

This local run covers the current Windows environment and filesystem only. It
does not establish behavior on every POSIX or network filesystem, power-loss
durability, or hostile concurrent filesystem mutation. No runtime dependencies
were installed for this change. Release build/install and hosted workflow
results are left to the parent review.

## Hosted workflow

The [GitHub Actions CI workflow](https://github.com/Saddidly/rulesort/actions/workflows/ci.yml)
publishes live results per revision. This working-tree revision's hosted outcome
has not been verified here; use the workflow page to check a specific pushed
revision.
