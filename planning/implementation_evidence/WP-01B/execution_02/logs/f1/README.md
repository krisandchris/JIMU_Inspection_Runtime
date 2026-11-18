# F1 targeted implementation evidence

TEST_PASSED for the recorded targeted final commands only. RUNTIME_NOT_RUN.
Independent Primary code review and whole-WP acceptance remain pending. D01 is still blocked.

01/02: approved dependency lock/sync; lock_before/after prove no existing package version drift.
03: initial source mypy; 04/05/06: original format/lint/test output, including 3 lint findings and 9 test failures.
07..10: corrections and first green F1 targeted result.
11..15: final format/lint/mypy and 127 targeted tests (107 F1 + 20 selectors), no skips.
16: actual environment/dependency versions. owned_sha256.json pins the delivered source/test/fixture/lock files.
Selectors original failure/retest output remains in sibling selectors/.

The pure generation API accepts bounded raw Python type trees or A TypeDefinition, not JSON bytes/text; no duplicate-safe JSON parser is claimed. Pydantic JSON roundtrips exercise generated contract structure only.
No Rule AST/producer activation, registry service, parent/root digest, signature, Bundle admission, policy/vocabulary authenticity, model call or GPU was implemented or inferred.
Unit/coordinate vocabulary semantic-domain checks and EvidencePolicy subtype/content checks need later finite-catalogue integration; local root models validate shape/path only.
The canonicalizer integer limitation is intentionally restricted to the projection boundary; A numeric acceptance is unchanged.
