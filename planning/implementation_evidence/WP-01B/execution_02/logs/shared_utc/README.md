# Shared UTC correction evidence

TEST_PASSED only for the recorded targeted checks. RUNTIME_NOT_RUN. Primary review pending.

Temporary exclusive shared-file write ownership was explicitly handed to Sol-C. Only definition_support.py, owned runtime/facts.py and new test_b_utc_nested.py changed. No A baseline file, B policies/test file, lock, F1 source or old evidence/hash was modified. Existing A1/S1 recorded hashes remain historical; after_sha256.json records this new authorized correction.

01 reproduces the old alias accepting numeric text "0" as epoch and a space-separated datetime. 02 reproduces nested LocalPrimaryProviderPolicy and ProviderPolicyArtifact valid UTC failures. 03..08 preserve initial checks and two test expectation failures: optional jsonschema date-time checker is absent in the locked environment, so a lexical regex cannot prove calendar validity. The corrected tests explicitly verify the two-phase contract instead of claiming format execution. 09..14 are final Ruff/format/mypy and 300 UTC+S1+A1 targeted tests, no skips/warnings; initial and final JUnit are both retained. 15 records actual environment versions.

The single shared UTCDateTime alias accepts JSON RFC3339 date-time strings in the normalized internal Z/z/+00:00 support domain and validates calendar dates using the bare strict Pydantic datetime parser, followed by the existing aware zero-offset guard. It never recursively parses through its own alias. Python input still requires an aware zero-offset datetime object; no Python string/numeric coercion. RuntimeUTCDateTime is only a compatibility alias pointing to shared UTCDateTime, without a second parser or Policy-to-runtime dependency.

Schema pattern states the same lexical support domain; calendar validity is separately enforced by Pydantic. RFC3339 -00:00 represents a known UTC instant with unknown local-offset provenance; this internal normalized domain rejects it instead of discarding provenance, not because RFC3339 prohibits it. Leap-second second=60 remains outside Pydantic datetime support and is explicitly rejected/tested; no leap-second execution support is claimed.

Actual two nested Policy roots use the read-only B fixture in this independent test; Sol-B was notified to rerun its own P1 suite. Shared write ownership is returned after this handoff.
