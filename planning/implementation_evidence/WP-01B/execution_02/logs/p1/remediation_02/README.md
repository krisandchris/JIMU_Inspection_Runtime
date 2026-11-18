# P1 finite catalogue lookup hash distinctions

A private typed generic ContentRef resolver now distinguishes known(kind,id,version)
with conflicting pinned digest from an absent identity. Preflight still rejects directory
duplicates before lookup. It is used for Prompt, nested Tool ModelBinding, explicit
additional proposal ModelBinding and retention Vocabulary; existing Policy resolver
already had this distinction. Review resolver keys registry/id/version separately from
SHA256, serving both local-exception and fallback report lookup. Different registry or
version is absent/incomplete; same key wrongSHA is invalid. No byte authenticity is
inferred, no report/body hash computed. The remaining next() resolves an already-local
validated deployment ID, not a ContentRef/report directory.

before_fix.json:6real failures and10control passes; covers wrongdigest vs empty catalogue
for each ContentRef path, both report consumers and absent/other-registry/other-version.
Final156targeted tests pass, no skips; mypy/Ruff/format exit0. Exact commands, cwd, UTC,
output/JUnit and new hashes are saved; previous logs and hashes remain untouched.
Only P1-owned validator/unit tests changed. Errors use fixed bounded structured codes/
messages, not raw exception/report serialization. FIXED_PENDING_INDEPENDENT_RECHECK.
M1 remediation_03 completed before this work; Schema-export task not started.
