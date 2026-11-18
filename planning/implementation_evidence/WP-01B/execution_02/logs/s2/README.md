# S2 bounded schema/mapping/review-input delivery

Scope: PLAN-3-DAG CONTRACT_FIELDS sections 2.1, 2.2 and 3.1 only. D01 independent.
Code is delivered for Primary review; this is not whole-WP acceptance.

## Files and interfaces

Nine files are listed with byte hashes in after_sha256.json. No dependency/lock/shared-enum
changes by Sol-B. C-owned FactSelector/ProducerSelector are imported, not duplicated.

- schema_types.py: four descriptor branches, expected-kind SchemaRef views, FieldSelector,
  ResolvedFieldType (including ordered optional ancestors and payload-root absence).
- tool_mappings.py: five InputSource branches, InputBinding, current-owner context registrations
  and bounded MappingTypeContext.
- validation/schema_descriptors.py: SchemaCheckIssue/SchemaCheckError; resolve_descriptor,
  describe_field/resolve_field, validate_descriptor, require_type_containment,
  require_mapping_compatibility, validate_target_bindings.
- validation/review_inputs.py: four explicit ReviewScope branches, ReviewedDeployment,
  two assessment branches and ReviewMetadata; no quality/report authorization service.
- Three assigned test files and deterministic contracts.json fixture.

S2 pure checks report malformed shape through Pydantic ValidationError, finite semantic
violations through SchemaCheckError.issue(outcome/code/field_path/message). A missing exact
schema descriptor is incomplete; a known identity with different digest is invalid.
No project-wide ValidationReport framework or hidden global registry was introduced.

## Actual final validation

Environment: Python3.12.3, Pydantic2.12.5, pytest8.4.2, jsonschema4.25.1,
Ruff0.14.14, mypy1.18.2; see environment.json for actual version command/output.

TEST_PASSED: final_pytest.json/xml: 82 passed, zero fail/error/skip, exit0.
The test command was:

```bash
uv run --locked --no-sync python -m pytest tests/unit/test_b_schema_descriptors.py tests/contract/test_b_mapping_contracts.py tests/negative/test_b_mapping_invalid.py -q --junitxml=planning/implementation_evidence/WP-01B/execution_02/logs/s2/final_pytest.xml
```

TEST_PASSED: final_ruff.json, final_format.json, final_mypy.json each exit0.
These JSON logs preserve exact argument arrays, cwd, UTC start/end and full output.
Mypy checked six owned source files. Ruff/format checked owned source/test files only.
Generated JSON Schema Draft202012 structure and positive/negative fixture checks are
executed in the contract/negative pytest tests, including every descriptor, source,
review-scope and assessment branch. No schema-generation CLI or full A+B suite was run.

Initial missing-path pytest exit4 and all actual source-check failures remain preserved.
See execution_findings.json and cwd_correction.json for the execution path mistake,
rectification limits, intermediate format parse failure and review status. Do not claim
that the original tree was never touched or that pre-move hashes were collected.

## Boundaries and remaining work

RUNTIME_NOT_RUN / runtime acceptance N/A. All input data is synthetic. No API/model/GPU,
Tool execution, DB, migrations or credential access; no Git mutations or publication.

Model/Tool/Policy-aware source resolution, ancestor DAG checks, definition output matching,
full mapping source identity/producer authority and actual review coverage verification
remain M1/integration work, not silently PASS. require_mapping_compatibility checks the
explicitly supplied resolved types, not that their source actually exists or is authoritative.
validate_target_bindings verifies only target field coverage/requiredness, not source
availability or runtime partial-object construction. Actual result envelope/payload/state
validation remains later runtime-support work. Descriptor authenticity and full resource
closure remain WP-02. No Rule AST or replacement language is implemented.

Acceptance: local targeted code/tests PASS; Independent Review PENDING_PRIMARY_REVIEW;
whole WP NOT_EVALUATED. Recommended next work is Primary direct S2 review and dependent
O1/R1/M1 integration, within existing scope; no automatic WP-01C start.
