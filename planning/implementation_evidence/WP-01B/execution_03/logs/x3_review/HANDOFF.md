# Independent X-I06 / X-I07 regression handoff

Authorized owned product change: tests/negative/test_b_producer_authority_review_regressions.py only. No fixture files needed or changed. No checker, model, contract, X3 fixture, dependency, exporter or Git changes. All evidence is exclusive execution_03/logs/x3_review/.

The suite calls actual validate_producer_authority_contracts with actual typed ProducerAuthorityValidationInput, local A2 history/candidate byte snapshot, full X2 Producer context, real FactSchemaRegistry and explicit parser/snapshot limits. It reuses existing X3 selected histories, adds unselected failed/late results, and preserves valid selected output completeness. No mock checker, runtime substitute, condition evaluator or invented truth certificate.

Frozen cases (63):
- 18 positive Question/Tool/System combinations: unselected late failed, on-time failed and late succeeded attempts, with zero or emitted output. A result is not rejected merely because it is unselected/failed/late.
- Three emitted-reference→missing-candidate cases require incomplete; three borrowed-Fact cases require invalid. In borrowed cases the review Fact is removed, leaving only independently valid original Fact→original Result links; the unselected Result points at that other Result's Fact. This exercises the reverse direction specifically.
- Exact Execution/Attempt/source Result/output/status contradictions across N04/N05/System, plus Object contradiction cases, require invalid. N04 has Question references rather than output/status metadata, so those nonexistent combinations are not manufactured.
- Zero-Fact unselected N04/N05 static Node kind, Attempt owner, Object/Profile links; actual Tool Plan/step/Tool; System Attempt requirement; missing Attempt/Node incomplete; N04 effective Question anchor contradiction.
- mandatory_plus_optional with optional_source=all cannot omit a configured optional Question; a complete two-Question sequence with both Facts remains legal.
- Nonempty partial success: complete selected N04 two-Question or N05 two-required-output batch remains valid while failed/late unselected result has only one corresponding output. No selected completeness requirement leaks into unselected partial history.
- Unselected System object-scoped zero/emitted positives, zero-Fact wrong Profile negative, emitted wrong Object negative.

Development evidence is explicitly separate: development_01 passed47/skipped2 while expanding the matrix, with skip reasons retained; unsupported N04 output/status combinations were replaced by collection of only applicable cases. development_02 passed57/0skip, development_03 passed63/0skip. These preceded D's final writer release and are not the final seal. Initial Ruff fixed two import issues with exit0; raw output retained in lint_01.json.

D released checker SHA256 4179223f05cc4a226936a6c83ff832919a119b5754495bc3715c34856c4349af. Final actual run after release: final_pytest.json/xml, 63 passed, zero failures/errors/skips. final_lint.json PASS and final_format_check.json PASS for the one owned test plus two own audit scripts. No full-suite or source mypy rerun was requested/performed by this subtask.

Test SHA256 178cf0b369e43b85735671103fe26eede7d5922eaddf35ec48110178c81dc3a6. final_before_sha256.json and final_after_sha256.json contain 60 source/dependency/fixture/test/config files with identical before/after bytes. final_verification.json independently checks this equality and JUnit totals. Each command record preserves exact argv/cwd, UTC start/end, exit/stdout/stderr and checker hash before/after; pytest records JUnit. environment.json and uv_version.json record actual CPU tooling versions.

TEST_PASSED for this independent frozen regression suite, STATIC checks passed as above. RUNTIME_NOT_RUN. No runtime or full WP acceptance claim. All owned files are released to Primary for review and generation barrier.
