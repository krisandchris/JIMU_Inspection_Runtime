# S1 remediation 01 — strict instance revalidation

Primary identified a real defect: reconstructing instance input by iterating only
cls.model_fields dropped copy-injected extra fields before extra="forbid" validation.
The same defect also discarded subclass declared fields and allowed extra storage.

The regression was executed before the fix: 8 failed, 1 passed, 80 deselected.
The focused deselection was intentional for the reproducer only. The complete S1
suite after remediation has 133 passed, zero failure/error/skip/deselection.
Exact commands/UTC timestamps/exit/stdout/stderr are in numbered JSON logs; both
before/after JUnit files are preserved. All earlier S1 failure logs remain untouched.

Only definition_support.py and test_b_ontology_invalid.py changed. The validator now
starts from the full actual instance __dict__, includes __pydantic_extra__, rejects
conflicting stored keys, then removes only declared None defaults proven absent by
model_fields_set. It passes the resulting complete payload to the normal strict
validator. Nested model objects are preserved for their own revalidation; model_dump
is deliberately not used because it can omit undeclared nested instance fields.

Regression coverage includes all four persisted roots and ExternalCapabilityRequirement
with model_copy extra, a nested tainted requirement, nested tainted ReviewReportRef,
subclasses with added declared fields or extra="allow" storage, constructed instances
with missing/invalid fields or explicit null, and retained construct+copy extra data.
Existing absent/default/null/roundtrip/immutability tests still pass.

Ruff check/format, mypy src (12 files), and 34 generated Schema checks passed. No
sync/install/lock changes, A changes, other module changes or runtime execution.
Runtime: RUNTIME_NOT_RUN. Public interfaces are unchanged. This directory's
post-remediation after_sha256.json supersedes the earlier S1 product snapshot;
the earlier manifest is retained as historical evidence. Primary review is pending.

Pydantic model_construct is an explicitly unsafe factory: if a caller already
irreversibly discarded extra input before supplying an instance, no later validator
can recover that missing input. Retained invalid/extra instance data is now checked;
S1 does not claim auditing the construction history of arbitrary Python objects.
