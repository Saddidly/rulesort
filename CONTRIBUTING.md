# Contributing

Thanks for helping improve RuleSort. Keep changes focused and explain any change to plan or journal compatibility.

## Before opening a change

- Use the standard library unless a dependency clearly improves safety or maintenance.
- Keep plan and journal formats versioned. Preserve safe refusal when a path, hash, or metadata precondition no longer matches.
- Add or update tests for user-visible behavior, especially filesystem failures and undo.
- Run python -m unittest discover -s tests -v and python -m pip install -e .
- Do not commit real user file paths, file contents, or transaction journals.

## Reporting issues

Include Python version, operating system, the command used, and a redacted example configuration. Never attach private files or credentials.
