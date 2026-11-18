# X4 finite condition bindings handoff

Authorized scope: CONTRACT_FIELDS §13/13.1/13.2, no Rule definitions, operand TypeChecker, evaluator, renderer, APIs or runtime calls.

Changed product files:
- src/jimu_runtime/domain/validation/condition_inputs.py (new strict use-site union and result)
- src/jimu_runtime/domain/validation/conditions.py (new finite binding helper)
- src/jimu_runtime/domain/validation/definitions.py (full16 Policy subtype and real use-site adaptation)
- tests/unit/test_b_condition_bindings.py
- tests/contract/test_b_condition_bindings_schema.py
- tests/negative/test_b_condition_bindings_invalid.py
- tests/negative/test_b_definition_conditions_invalid.py
- tests/fixtures/wp01b/x4/support.py
- tests/fixtures/wp01b/v1/contracts.json: supplied actual Applicability artifact required by existing Profile ref; previous positive fixture relied on absent expression-content exemption.
- tests/fixtures/wp01b/v1/support.py: with_step supplies actual ToolSkip artifact required by its existing skip ref; no prior assertion weakened.

DefinitionValidationInput and ProfileUseContext already carry the actual directories/Context views, so definition_inputs.py needed no change (before/after hashes equal). No shared Policy/Producer/ref/schema-exporter/lock/config file modified by A.

Public API: validation.condition_inputs.ConditionReferenceContext is the strict use_role union of ApplicabilityConditionContext, ToolStepGuardConditionContext and ProducerActivationConditionContext. ConditionReferenceResult has report and selectors_for_matching. validation.conditions.validate_condition_references has exactly the frozen required directories/registry/allowlist signature. Failed/incomplete bindings return no projection. Finite success includes condition.references passed and operand_types_and_truth deferred. Local refs resolve to temporary exact FactSelector with actual enclosing Plan; source AST stays untouched. C authenticates actual declaration/use association and System outputs plus unique Producer match.

TEST_PASSED: final_pytest_02.json/xml: 128 passed, 0 failed/errors/skipped. This comprises 54 X4 tests and 74 prior V1 relation tests. Explicit coverage includes strict/no-null/schema branches, wrong scope, extra/copy/constructed/subclass taint, exact pins and unused nested conflicts, complete unused allowlist, 1024 diagnostics, wrong paths, self/future/sibling/unmapped/output/key, missing consuming step, known foreign anchors despite missing content, positive strict ancestor, Plan reuse without reverse digest mutation, exact forward Tool refs in manifest activation, exact Tool guards forbidden, and complete Policy subtype/content consumers.

STATIC checks passed: final_lint_02.json (9 owned Python product/test/fixture files), final_format_check_02.json (9 already formatted), final_mypy_02.json (46 source files). Generated Schema validity and actual positive/negative validation run in the contract tests; whole exporter/index not run (owned by integration). recorder_final_lint.json covers the audit recorder itself after formatting.

Failures retained: old_v1_before_upgrade (11 failed), after_upgrade_01 (71 failed: newly written literal fixture had wrong ValueContract shape), after_upgrade_02 (12 failed: fixture mistakenly added ToolSkip input_domain field), targeted_01 (3 failed: two diagnostic expectations and a model_construct extra removed at construction), targeted_02 (1 failed: new test used nonexistent selection enum), lint_01/lint_02 and recorder_lint. Corrected tests preserve negative obligations; model_construct regression now injects actual retained taint before validation. Subsequent targeted_03 (124), targeted_04/final_pytest (126), final_pytest_02 (128) all passed.

Evidence limitation: initial development static commands occurred before the subprocess recorder; pre_recorder_attempts.json records exact command/outcome summaries and tool transcript identifiers, with no fabricated UTC bounds. All recorded pytest failures and final verification have exact cwd/argv, UTC start/end, exit, full stdout/stderr and JUnit. before.json captures the four owned source paths. fixture_previous_seal_hashes.json cites prior sealed V1 bytes instead of falsely claiming a new pre-edit snapshot. after_sha256.json records current product bytes.

Environment is in environment.json and uv_version.json. No install, dependency or Git mutation. RUNTIME_NOT_RUN. No migration/security credential changes. Conditional activation truth, semantic operand typing, scheduling, external authenticity, Rule/Bundle closure and runtime authority remain deferred. This is a component handoff for Primary review, not WP acceptance.
