# Security policy

Please report suspected vulnerabilities privately to the repository maintainer rather than posting sensitive file paths or exploit details publicly. Include the RuleSort version, operating system, relevant command, and a minimal reproduction using disposable files.

RuleSort is a local filesystem utility. It treats JSON configuration and plan files as untrusted input, constrains destinations to the configured root, refuses symlinked scan inputs, and hashes source files before apply and undo. These checks reduce accidental loss but do not protect against a hostile process racing filesystem changes. Keep backups of valuable data.
