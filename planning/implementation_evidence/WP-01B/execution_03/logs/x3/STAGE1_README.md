# X3 stage1 input contracts

Delivered for Primary review: validation/producer_authority_inputs.py, three prefix-owned tests and fixtures/wp01b/x3. Public nonpersistent roots are ExecutionDefinitionPin and ProducerAuthorityValidationInput. Strict/frozen fields preserve existing ref wire; no schema_version or artifact kind was added.

TEST_PASSED:29 targeted tests, zero failures/errors/skips, stage1_pytest_second.xml. Ruff check/format check and one-owned-source mypy exited0. Exact commands, cwd, UTC start/end, original output and failures are in stage1_commands.jsonl. Baseline dependencies/initially absent paths are in stage1_baseline.json; final owned hashes are in stage1_after_sha256.json.

Actual checks: unique versioned Catalogue context identities, unique Execution IDs, duplicate/conflicting configured Profile refs, known Catalogue digest conflicts and configured Profile set equality independent of storage order. Question/Tool/Plan sources compare exact ref identity and actual definition content, allowing equal cross-source bodies without wrapper-class equality and rejecting unused conflicting definitions. Input instance taint is revalidated; containers are copied to immutable tuples.

Missing Catalogue/pin references remain representable input for forthcoming X3 incomplete diagnostics, not default authority. The fixture includes a zero-Fact System optional_absence result and its selection/Execution, so obligations cannot be inferred only from emitted Facts. Byte-carrying candidates are intentionally empty in this stage1 JSON fixture; no new bytes JSON wire is invented. This test fixture does not assert its runtime pins/system declaration are fully admitted.

No producer_authority.py combination validator has been written, and X2 full validators have not been substituted. Input shape success establishes neither complete Producer/Authority admission nor dynamic activation. Full source/candidate/Scope/cardinality/history/pin coverage and named runtime/external deferred boundaries belong to the next authorized integration stage. Runtime Acceptance N/A; RUNTIME_NOT_RUN.

Initial formatting findings and a self-caused duplicate-basename pytest collection failure are preserved. The unit file was renamed to test_b_producer_authority_input_views.py without modifying existing conftest/configuration or deleting caches. No other owner source, old local API, Registry, exporter or Git resource was modified. Stage1 source writer is released.
