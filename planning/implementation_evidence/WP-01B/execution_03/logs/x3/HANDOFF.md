# X3 finite composition handoff

Status: TEST_PASSED for the authorized X3 CPU component; independent Primary review pending. RUNTIME_NOT_RUN. No full-WP or production admission claim.

## Scope and files

- `domain/validation/producer_authority_inputs.py`: strict immutable ExecutionDefinitionPin and ProducerAuthorityValidationInput; bounded unique directories and known cross-source identity conflicts.
- `domain/validation/producer_authority.py`: complete finite catalogue/source/pin/Result/selection composition using the actual X2 validators, R1 injected registry and unchanged A2 local history validator.
- Six owned `test_b_producer_authority*` files cover inputs and composition; `fixtures/wp01b/x3/composition.json` carries independent synthetic Q/Tool/System histories. Its nonempty candidate raw bytes are real UTF-8 Python bytes decoded separately from existing persisted JSON roots, not an invented JSON bytes wire.
- No Registry, A2 local API, other-owner modules, exporter, dependency, runtime or Git mutations.

## Actual finite behavior

All supplied contexts are checked, including unused entries. Question/Tool/Plan definitions merge only after identity/content agreement, without source precedence. Every candidate, Result and selection Execution needs its actual pin, including zero-Fact outputs. Every supplied Result has static owner/scope/step/source checks; emitted references must reciprocally point to actual owned candidates. Unselected failed/late partial history is retained. Selected N04/N05/System groups satisfy actual effective/mapped/unique-slot completeness independently of conditions. Actual implementation versions come from Results. System requirement constraints compare unique sets without rewriting raw bytes.

Only three exactly named deferred local checks are replaced by implemented finite checks. Local invalid/incomplete reports and external_provenance survive composition. Explicit future diagnostics retain activation truth, expression TypeChecker, actual Release admission and external trust/history completeness. No consumer authority view is exposed.

## Verification

Final combined targeted command and raw output are recorded in `composition_commands.jsonl`, with cwd, UTC start/end and actual subprocess exit code for each invocation. `composition_final.xml`: 78 passed, zero failures/skips. Ruff check and format-check: exit0. Strict mypy of the two source files: exit0. Generated helper schemas checked by Draft202012Validator in the targeted contract tests.

Environment: Python3.12.3, Pydantic2.12.5, rfc8785 0.1.4, pytest8.4.2, jsonschema4.25.1, Ruff0.14.14, mypy1.18.2. Dependencies used locked/no-sync.

Earlier failed console/raw JSON output is retained in composition_commands.jsonl and first/second/third_composition_pytest.txt. The initial combined JUnit file was written at composition_final.xml, then renamed (not reconstructed) to composition_prerelease_failure.xml before the final run: its original testsuite timestamp is 2026-11-18T02:05:01.310061+08:00, with 78 tests, 1 failure, 0 errors/skips. The present composition_final.xml contains only the final 78-pass run, testsuite timestamp 2026-11-18T02:05:55.039971+08:00. Append-only command records retain the original output path; the renamed failed XML is the preserved original file. Early shell wrappers printed pytest output using cat, so their shell exit0 must not be treated as pytest success; final commands use subprocess returncodes directly. A bootstrap `python` shell attempt returned127 (`python: command not found`), corrected to system python3 before any intended source mutation.

## Seal and limitations

Source/test writer released to Primary and E2. `composition_final_sha256.json` records all owned source/test/fixture hashes; `composition_summary.json` records final checks. Checker SHA256: 4179223f05cc4a226936a6c83ff832919a119b5754495bc3715c34856c4349af.

No live service/GPU/model/database evidence or migration. Security and data impact: finite in-memory validation only; no new persistence or secret handling. Injected descriptors/pins assert internal consistency and never authenticate actual external records. Independent review tests are owned separately and are not included in this 78 count. Stop at the X3 handoff gate; Primary owns independent review and final acceptance.
