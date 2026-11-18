# X4 C2 review remediation

Primary-authorized correction under existing CONTRACT_FIELDS §2.2: context registration uniqueness is (source, exact owner), excluding Schema from the slot key. context_snapshot has the enclosing current use as its implicit owner within one helper call. Profile/Region use their full exact owner reference. Duplicate slots reject even with unused registrations, literal true guards or otherwise valid local-selector projections. Existing failure handling clears all selectors_for_matching.

Changed only validation/conditions.py and tests/negative/test_b_condition_bindings_invalid.py, plus this exclusive evidence directory. Existing contracts/shared owners/exporter/Git untouched. Prior x4 hashes/logs remain unchanged; before_sha256, after_sha256 and changed_sha256 record this remediation separately.

Before actual run: before_regression.json/xml, 6 failed and 4 passed (31 deselected). All six distinct Schema/duplicate source-owner cases passed incorrectly before correction, covering snapshot/Profile/Region registrations in local and constant guards. The four System activation scope cases already behaved correctly before the change; they are retained as explicit regression evidence.

TEST_PASSED: targeted.json/xml: 138 passed, zero failures/errors/skips (64 X4 +74 previous V1). Includes actual generated Schema contract validation. System activation scope matrix explicitly rejects execution-use→object-Fact with no projection, and accepts object-use→execution-Fact with finite references passed while operand types/truth stay deferred; same-scope object/object and execution/execution also pass finite bindings.

STATIC: lint.json PASS (9 owned source/test/fixture files), format_check.json PASS (9 already formatted), owned_mypy.json PASS (3 owned source files). Whole-source mypy.json actually FAILED on C-owned producers.py:239 and :253 while C was implementing X2 (47 source files); both exact diagnostics are retained and C/Primary notified. No C file modified or error ignored to obtain green.

All remediation command records contain exact argv/cwd, actual UTC start/end, exit, stdout/stderr; pytest includes JUnit. No runtime execution, dependency changes, Git mutation or WP acceptance. RUNTIME_NOT_RUN. Helper API and data wire unchanged; files released for independent review and C integration.
