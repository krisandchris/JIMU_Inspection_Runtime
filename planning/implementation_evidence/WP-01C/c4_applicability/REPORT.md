# WP-01C C4 Applicability

## Scope and deliverables

This slice implements the six Applicability contracts in Domain Model §3.8.1 and WP-01 plan §15. The authorized local CPU contract work uses the reviewed WP-01C plan and the platform authorization register. The detached baseline was `2552affeb69fbc2594fe70724f895b0db0967d47`. The worktree also contained concurrent WP-01C work. The exact observed state is in [git_status.log](git_status.log).

- `domain/runtime/applicability.py` provides versioned roots `ProfileEligibilityEvaluation`, `RuleSetApplicabilityEvaluation`, `RuleApplicabilityEvaluation`, `ApplicabilityTrace`, and `UnresolvedObligation`. `ApplicabilityLevelTrace` is nested and discriminates evaluated levels from ancestor projections.
- `domain/validation/applicability.py` provides `validate_applicability_records(scope: ApplicabilityScope, records: ApplicabilityRecordSet) -> None` over explicitly selected records and finite corroboration directories. It raises `ValueError` on association errors.
- `tests/unit/test_c_applicability_records.py`, `tests/contract/test_c_applicability_schemas.py`, `tests/negative/test_c_applicability_invalid.py`, and `tests/fixtures/wp01c/applicability/` contain deterministic in-memory test data and checks.

Trace roots include Release and Bundle pins alongside the Snapshot digest. A diagnostic ref identifies a specific issue by trace, level and zero-based issue index. The directly evaluated UNKNOWN owns the shared obligation ID. A projected obligation cites that same ID and the decisive ancestor's diagnostics. Coverage and Decision can use the typed trace and evaluation refs to pin selected records and use the obligation ID for ledger linkage.

The association inputs represent the caller's trusted view of Bundle membership, Snapshot association and NodeRun, Attempt and Object records. The verifier checks that the records agree with that view, including N01 status, the pinned scope, metadata allowlists, forward membership, fixed parent prefixes, N06 invocation ownership and diagnostic resolution. Bundle admission, source-digest computation, gate evaluation, persistence and replay remain work for their owning packages. No existing wire contract or database migration is introduced. The new versioned roots use the repository's existing `1.0.0` schema version policy.

## Evidence

All recorded commands exited with code 0 on Python 3.12.3 using the existing lock, uv 0.9.15 and offline execution. Exact argv, working directory, timestamp and exit code are in the JSON files beside the corresponding logs.

| Check | Result | Exact argv and output |
| --- | --- | --- |
| Targeted contract, unit and negative tests | `TEST_PASSED`, 86 passed, 0 skipped | [argv](pytest.json), [stdout](pytest.log), [JUnit](pytest.xml) |
| Ruff lint | Pass | [argv](ruff_check.json), [stdout](ruff_check.log) |
| Ruff format | Pass | [argv](ruff_format.json), [stdout](ruff_format.log) |
| mypy | Pass, two source modules explicitly targeted | [argv](mypy.json), [stdout](mypy.log) |
| Locked environment | Captured | [argv](environment.json), [stdout](environment.log), [input hashes](INPUT_SHA256SUMS) |

The targeted fixtures cover H01–H05 and both H07 exclusions, including a surviving sibling branch. Family-owned A01–A03 and A07–A09 associations, A12 immutability and retry selection, and A13 rejection of a fabricated gate or unjustified ancestor projection are covered. Applicability absence of model-call shape and separation from predicate truth is verified through its schema and negative fixtures. H06, the Required Evidence Guard in A10, and orchestration or aggregation aspects of A04–A06, A09, A11 and A13 require the corresponding Decision, Coverage or node contracts and later runtime evidence.

## Acceptance and handoff

Applicability model and finite association criteria are **PASS** for the targeted slice. Root schema registration and byte reproducibility are C5 integration gates. Combined G12 and overall WP-01 acceptance remain **CONDITIONAL** on consumer integration, generated artifacts and the package-level review.

`RUNTIME_NOT_RUN`. No service, model, provider, GPU, database or trusted external registry was contacted. The next local gate is C5 schema registration and package verification.
