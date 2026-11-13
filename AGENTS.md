# Working on Files2GDrive

- Read README.md and the relevant guides in docs/ before changing behavior.
- Use a working branch. Commit changes, push that branch, open a pull request to main, review the diff and applicable checks, then merge when authorized. Do not push changes directly to main or bypass repository protections.
- Update public documentation when behavior or deployment status changes. Keep private profiles, tokens, OAuth JSON files and local notes out of commits and release assets.
- Never read or write SQLite from upload workers: the controller owns persistent state. Preserve durable Drive IDs, queue/backoff, retention, manual pause and reconnect safeguards.
- Use focused checks for documentation-only changes and relevant tests for code changes. The full suite runs with `python -m unittest discover -s tests -q` in the configured environment.
- OAuth in production is distinct from software maturity and Google brand verification. The current public release is 0.1.1; the initial 0.x series does not promise a stable API. Use SemVer version strings and v-prefixed Git tags, and never replace released artifacts with different contents under the same version.
