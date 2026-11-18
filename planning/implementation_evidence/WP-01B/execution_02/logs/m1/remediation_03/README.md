# M1 Registry cross-catalogue diagnostics

No R1 changes. A shared local adapter maps only registry.unknown_identity to incomplete
at Tool definition/plan cross-catalogue boundaries. Digest/path/type and other errors
remain invalid. Original bounded ValidationIssue code/path/message/related_refs are
preserved, never str(exc) JSON embedded into Description4096. Unexpected report scope,
empty errors or issue count>64 yield explicit invalid diagnostic, never silent truncation.

Initial before_fix.json records a fixture setup failure (omitted generation spec), not
an actual logic reproduction. After fixture correction, before_logic_fix.json records
five real failures: unknown reported invalid twice; wrongdigest/path original diagnostics
lost. Final50tests pass, no skip; mypy/Ruff/format exit0. A4096-character typed message is
preserved in both consumers using a synthetic Registry exception seam. Code/test hashes
are newly recorded; old evidence not overwritten. FIXED_PENDING_INDEPENDENT_RECHECK.
No models/GPU/DB/Git/Rule/schema-export work performed.
