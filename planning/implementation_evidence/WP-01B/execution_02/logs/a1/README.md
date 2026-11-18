# A1 models-first targeted evidence

TEST_PASSED only for the recorded final targeted checks. RUNTIME_NOT_RUN. Primary review pending; no overall-WP authority/producer admission claim.

Owned source: runtime/{__init__,facts,authority}.py and validation/authority_inputs.py; the three assigned test files and a1/contracts.json fixture. No F1/shared/definition-enum files or dependency locks changed.

01..08 preserve initial mypy, lint/import and datetime failures. 09 is the exact field-mode diagnostic showing JSON mode remained available. 10 is the first 120-test green result (deprecated test-only model_fields access warnings subsequently fixed). 11..16 record the next 131 tests/checks. 17..22 are final Ruff/format/mypy and 135 passed, no warnings/skips; prior JUnit remains and targeted_results_v2.xml is final. 23 records environment versions. owned_sha256.json pins delivered files; evidence_sha256.json seals this envelope.

RuntimeUTCDateTime locally decodes a string only when ValidationInfo.mode==json, by invoking the original shared UTCDateTime TypeAdapter.validate_json. Python timestamp strings remain rejected. Valid aware UTC datetime objects are preserved; naive/nonUTC Python datetime objects and invalid JSON timestamps reject. Five Fact statuses and all three Authority groups roundtrip JSON. Nested model_copy tampering is revalidated. No shared alias semantics changed.

Descriptors retain failed/late/unresolved evidence as data. These shapes do not establish external references, model/tool calls, producer rights, full result completeness, Attempt qualification for authority, historical chain integrity, admission, or trusted Actor identity. FactCandidateSubmission preserves bytes but intentionally does not parse/check them. No parser, snapshot, state machine, ProducerDeclaration/activation replacement, output identity projection, authority selection service, database or Rule AST implemented. D01 remains blocked.

Generated Schema encodes scope/producer-source/status restrictions and exact kind constraints. Dynamic ID/version equality, uniqueness by subfield and path relations additionally use explicit local Pydantic validators; Schema shape alone does not claim these semantic checks.
