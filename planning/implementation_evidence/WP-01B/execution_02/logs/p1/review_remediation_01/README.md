# Independent P1 review remediation

C1: prompt directory is size-checked before traversal, every registration revalidated,
and every duplicate kind/id/version rejected, including identical refs/body, different
purpose/body or conflicting digest. Lookup occurs only after successful full preflight;
copy-injected unknown data returns structured invalid, not an uncaught error.

C2: internal scope matching returns applicable/excluded/owner-unresolved. Only applicable
assessments contribute certain version coverage. Unknown-owner possibilities are retained
separately. Fully covered binding-wide versions are not blocked by unrelated narrower
scope. If complete coverage could be established only with unknown owners, outcome is
incomplete; impossible coverage remains invalid. Unknown owner never grants approval.
Known Profile exclusion overrides a missing Region requirement in that same assessment.

Before fix:18 newly added behavior cases failed (before_fix.json), including both prompt
orders and local/fallback×profile/region scope orders. Added an extra explicit-owner test
for matching/missing/wrong digest owner combinations. After fix140targeted tests passed,
no skip/fail/error. Mypy/Ruff/format exit0. Exact commands/cwd/UTC/output/JUnit preserved.
Only owned validator and unit tests changed; old phase hashes/logs preserved. No schema,
business Policy/Rule AST, report authenticity or Runtime claims changed.
Status FIXED_PENDING_INDEPENDENT_RECHECK; wholeWP not accepted here.
