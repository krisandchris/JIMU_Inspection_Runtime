# R1 integration type remediation

Only registry.py changed: the finite diagnostic `_fail` path annotation now imports/reuses IssuePath (string or nonnegative strict integer, max32), matching the shared SchemaCheckIssue contract. No path conversion, dropping, Any, ignore or semantic selector widening. Existing FieldPath remains max8.

Original full-integration mypy failure is referenced and preserved in baseline_failure.json. Targeted Registry pytest passed51, with zero failures/errors/skips; Ruff check, Ruff format check and Registry mypy all exited0. Exact commands/raw outputs are in commands.jsonl; pytest_final.xml and after_sha256.json cover this new remediation, without rewriting the historical R1 seal. The initial format check required reformatting after the shorter annotation; original exit1 is retained and final format check passed. Existing path tests exercised the unchanged resolution/error behavior; no implementation-mirroring test was added for an annotation-only correction.

Runtime Acceptance N/A; RUNTIME_NOT_RUN. Full integration re-verification belongs to Primary/Sol-B. Source write ownership released after these checks.
