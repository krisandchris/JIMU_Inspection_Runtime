# WP-01A — Foundation Types & Reference System

## 1. Acceptance and authorization

| Acceptance axis | Result | Evidence / limit |
|---|---|---|
| Code Acceptance | PASS (local self-review; independent GPT-6 review pending) | Foundation scope only; no unresolved C0/C1/C2 Findings |
| Test Acceptance | TEST_PASSED / PASS | Final primary and isolated public-index rebuild each: 324 passed, 0 failed, 0 errors, 0 skipped |
| Runtime Acceptance | RUNTIME_NOT_RUN | WP-01A has no Workflow/Provider/Tool/service implementation or deployment to verify |

The user explicitly approved D01–D15 at commit
`827fa77c9d6a561b0df85d7d93f50d38a912f4ee`, staged WP-01A/B/C implementation,
CPU dependency installation/locking, automated tests, Schema validation and static
checks. **This execution is WP-01A only.** B cannot start until A passes independent
review and user acceptance. Commit/Push, real model API calls, GPU installation,
Bundle compiler, workflow engine and requirements-platform changes remain excluded.

Older pending-approval/documentation-only language in the starter documents is
historical. Authorization is recorded in [baseline.json](baseline.json); original
documents and WP-00 evidence were not rewritten to change their historical state.
The optional engineering execution/acceptance standard did not exist at baseline.

## 2. Baseline and protected history

- Repository: `krisandchris/JIMU_Inspection_Runtime`; branch `main`.
- Expected/observed initial and final HEAD:
  `827fa77c9d6a561b0df85d7d93f50d38a912f4ee`.
- Initial worktree: clean; 70 tracked files, including 54 historical review evidence
  files. Mandatory reading completed for AGENTS, README, all four specs, ADR,
  implementation plan, dependencies, test strategy, roadmap and all four examples.
- All 70 baseline files remain byte-identical, including AGENTS, architecture
  specs, original PACKAGE_MANIFEST, examples and R1/R2/R2.1/R2.1.1 audit history.
- Final worktree contains only new WP-01A files; nothing staged/committed/pushed.
  No branch, Git reset/restore/checkout or remote setting changes were made.

Exact initial Git commands, exit codes and output are in baseline.json. This is
code/test evidence on an uncommitted worktree, not a new commit or runtime release.

## 3. Bounded implementation and changed files

The flat module layout follows the approved implementation plan §4. No business
definition, Fact Registry, producer Registry, Rule AST/TypeChecker, Applicability
Evaluator, Bundle compiler, database, API, service or UI was implemented.

| Added files | Responsibility |
|---|---|
| `.gitignore`, `.python-version`, `pyproject.toml`, `uv.lock` | CPU package/tool configuration, isolated Python 3.12.3 environment and reproducible dependency lock |
| `src/jimu_runtime/__init__.py`, `domain/__init__.py` | Package boundaries; no bulk re-exports or import side effects |
| `domain/base.py` | Strict/extra-forbid/frozen/revalidated ContractModel; deterministic JSON comparison helper |
| `domain/enums.py` | 25 ArtifactKinds, 26 RuntimeRecordKinds, six independent status/result enums and strict wire-string decoding |
| `domain/primitives.py` | Version, SHA-256 Digest, ArtifactId and RecordId textual constraints |
| `domain/references.py` | ContentRef, IdentitySelector, RuntimeRecordRef and constrained SchemaRef |
| `domain/values.py` | Seven payload types, bounded TypeDefinitions, Record fields and pure local ValueContract validation |
| `tests/conftest.py`, `tests/fixtures/foundation_contracts.json` | Deterministic illustrative fixtures; automatic socket-network prohibition during tests |
| `tests/unit/test_foundation.py`, `test_value_boundaries.py` | Positive, roundtrip, boundary, immutability and cross-domain cases |
| `tests/negative/test_references.py`, `test_values.py` | Invalid input and injection regressions |
| `tests/contract/test_json_schema.py`, `test_dependency_boundaries.py` | Generated schemas, explicit semantic-validation limits, import DAG and CPU dependency/index constraints |
| This evidence directory | Exact commands/results, dependencies, Schema snapshots, Findings and SHA-256 inventories |

Complete paths/actions are in [changed_files.json](changed_files.json), with hashes
in [after_sha256.json](after_sha256.json). Virtual environments and tool caches are
ignored local build/test artifacts, not source deliverables.

## 4. Implemented contracts and compatibility

- **ArtifactKind:** exactly the 25 values in Domain §9; no new capability artifacts.
  RuntimeRecordKind is separate and cannot be used as an ArtifactKind.
- **ContentRef:** `artifact_kind`, `artifact_id`, `version`, `digest`, immutable and
  closed. SHA-256 is 64 lowercase hex characters, without prefixes or coercion.
  Format validation does not prove the referenced bytes exist or match a digest.
- **IdentitySelector:** separate type with `artifact_kind` restricted to approved
  Profile/Region applicability targets, `artifact_id`, exact `version_selector`;
  digest, ranges/latest and implicit conversion to ContentRef are rejected.
- **RuntimeRecordRef:** `record_kind` + `record_id`; no definition version/digest.
  It is a nested identity in the owner's pinned scope, not a resolver. In particular
  Execution-local Object IDs must never be resolved globally or cross Execution.
- **SchemaRef:** ContentRef subtype restricted to the nine approved Schema kinds;
  `artifact_id/version/digest` are the Schema ID/version/digest, with required
  `schema_dialect=https://json-schema.org/draft/2020-12/schema`. No second identity
  system or competing Schema ID/digest fields were added.
- **Version/IDs:** stable exact `major.minor.patch`, no leading zeros, prerelease,
  build metadata, wildcard, range or trimming. Artifact ID follows the approved
  lower-case 128-character pattern. Runtime record IDs are bounded nonempty ASCII
  identities; neither type creates or resolves identities.
- **Values:** BooleanValue, IntegerValue, NumberValue, StringValue, EnumValue,
  ListValue and RecordValue use `kind`-discriminated unions. BooleanValue is the
  user-requested name for the plan's BoolValue; no duplicate alias/model exists.
  String/list/Record/container bounds are 4096/256/64/8, with tighter declared
  string/list bounds supported. Numeric bounds are inclusive. Integer rejects bool
  and float; Number requires a finite float and rejects integer/bool coercion.
  Known payload false/0/0.0/empty string/empty list remain distinct from missing/null.
- **Type binding:** TypeDefinition is local constraint data, not a Fact Schema or
  Registry. `validate_typed_value(caller_owned_definition, raw_payload)` / ValueContract
  enforce enum members, homogeneous declared item types, closed Record fields,
  required/optional fields, bounds and finite depth. An untrusted payload cannot
  supply its own allowed members/fields. Bare Value models validate algebra shape
  only and must not be treated as validated Facts or business-schema conformance.
- **Immutability:** list/member/field sequences are tuples; Record values use copied
  read-only mappings; nested models are frozen. Mutation of original input does not
  change validated models. Boundary revalidation rejects forged invalid copies.
- **Statuses:** TaskStatus, ExecutionStatus, NodeRunStatus, AttemptStatus, FactStatus
  and DecisionResult are distinct Enum classes. Exact strings are decoded on wire;
  foreign enum objects are rejected, including identical values such as `failed`.
  No lifecycle state machine or Rule truth evaluator was added.

These are nested foundation types. They inherit the future persisted root's explicit
schema_version; no independently persisted business record is introduced by A.
Package version `0.1.0` is not an artifact version or persisted contract version.
`stable_json()` is deterministic comparison serialization, **not JCS, an artifact
hasher, Bundle root digest or signature implementation**. Existing illustrative
examples are not silently imported, normalized or migrated. New enum values/wire
shape changes need compatibility review; original history is preserved.

## 5. CPU dependencies and lock

| Component | Actually used |
|---|---|
| Python | 3.12.3, system interpreter with isolated project virtual environment |
| uv | 0.9.15 |
| Pydantic / pydantic-core | 2.12.5 / 2.41.5 |
| pytest / jsonschema | 8.4.2 / 4.25.1 |
| Ruff / mypy | 0.14.14 / 1.18.2 |
| Build backend | uv-build 0.9.15 |

Runtime has only the bounded Pydantic dependency; dev tools are bounded as declared
in pyproject.toml. uv-build is necessary to install the src package; pinning it in
both build-system and dev dependencies puts the build tool's artifacts in uv.lock.
It is not a service/model dependency. There are 22 locked packages (including a
platform-conditional package) and 21 installed here, with identical primary and
clean-rebuild versions. All final registry entries are the declared public PyPI.

Finding F04 documents an initially inherited machine-level mirror configuration.
Only project index priority and the new project lock were corrected; no user/system
configuration was edited. Regeneration used `uv lock --no-config --default-index
https://pypi.org/simple --python /usr/bin/python3`, without --upgrade. Versions did
not change. A new isolated environment installed the final lock with --no-config
and repeated the full test suite. Normal project `uv sync --locked` was also verified.

Full tested host/interpreter details are in [environment.json](environment.json),
all exact versions in [dependency_versions.json](dependency_versions.json), and
the lock hash in [lock_sha256.txt](lock_sha256.txt). Only the recorded Linux/Python
3.12.3 CPU matrix is verified. No CUDA/PyTorch/vLLM/SAM3/service/SDK dependencies
were installed; no GPU compatibility or real model quality claim is made.

## 6. Actual tests and static checks

| Final verification | Actual result |
|---|---|
| `uv run --locked pytest -q --junitxml=planning/implementation_evidence/WP-01A/pytest_final_results.xml` | exit 0; 324 passed; 0 failed/errors/skipped |
| Full pytest in a new public-index environment, with `uv run --no-config --locked` | exit 0; same 324 passed; 0 failed/errors/skipped |
| `uv run --locked ruff check .` | exit 0; All checks passed |
| `uv run --locked ruff format --check .` | exit 0; 14 Python files already formatted |
| `uv run --locked mypy src/jimu_runtime` | exit 0; no issues in 7 source files, strict configuration |
| `uv lock --no-config --default-index https://pypi.org/simple --check` | exit 0; 22 packages resolved, lock current |
| Pydantic Schema snapshot/2020-12 meta-validation/repeated-byte comparison | exit 0; all 34 schemas valid and identical on repeated generation |

Final pytest groups: **unit 130, negative 130, contract 64**. Unit includes both
positive/boundary cases and rejection regressions; it is not 130 purely positive
cases. Positive coverage includes every ArtifactKind/RuntimeRecordKind/status,
all reference types and all seven values, roundtrip and input key-order variants.
Negative coverage includes all requested malformed refs/digests/versions, enum
mixing, bool/int/float/null confusion, NaN/Infinity, undeclared Record fields,
mixed list types, required missing fields and valid-payload extra-field injection.
Boundary coverage exercises exact upper limits, tighter limits, false/zero/empty
values, optional absence versus null, and container depth 8/9.

The import-graph test parses Python imports (not a Rule AST): Domain uses only
stdlib/Pydantic and its core, has no module cycle, Any contract fields, forbidden
SDK imports, eval/exec or dynamic import entrypoint. Tests perform no model calls.

All commands, timestamps, exit codes and raw stdout/stderr are retained in
[command_results.jsonl](command_results.jsonl), [test_commands.txt](test_commands.txt),
[test_results.txt](test_results.txt) and [static_checks.txt](static_checks.txt).
Primary/final and rebuild JUnit XML files are preserved separately. Early probe
failure and the first pytest result (296 passed / 4 failed), followed by corrected
runs, are deliberately retained; they are not misrepresented as initial success.
No tests were skipped and no lint/type errors were ignored to obtain acceptance.

## 7. Schema evidence and mandatory semantic stage

[schemas/index.json](schemas/index.json) identifies 34 Pydantic-generated foundation
schemas, their source modules, dialect and SHA-256. These are audit snapshots,
not a hand-maintained Schema source or a business/Fact payload generator. The exact
one-off snapshot command source is preserved in schema_snapshot_command.py.txt.

JSON Schema validates structural shape, kind enums, required fields, extra fields
and expressible bounds. Cross-property constraints (declared enum membership,
Record fields, homogeneous nested contracts, uniqueness, dependent bounds and
depth) additionally require Pydantic ValueContract/local validation. JSON Schema
mathematics treats 1.0 as an integer and permits integer 1 under number; Python
strict Integer/Number intentionally keep these representations distinct. Nonfinite
numbers are rejected by the model and strict JSON serialization. These boundaries
have explicit tests; a structural JSON Schema pass alone is not semantic approval.
Full Fact-schema projection/generation, Fact Registry and Bundle integrity remain
outside WP-01A. Reference existence, scope authority and content verification are
not inferred from syntactically valid IDs/digests.

## 8. Findings, independent self-review and impact

[findings.json](findings.json) records four implementation/tooling Findings, all
**C2 / RESOLVED** with regression evidence: wire Enum Literal decoding, tuple
constraint placement, regex end semantics, and host-index influence. No approved
business semantics or architecture specs were changed to resolve them. There are
no unresolved C0/C1/C2 Findings in this local review.

The second review pass traced requirements back through source, tests and actual
output: contract inventory, ref separation/kind restrictions, deep immutability,
wire/JSON parity, numeric strictness, Record/list/enum binding, status-domain
separation, package/import/dependency boundaries, generated-Schema limits, historical
protection and authorization scope. Source/tests/configuration did not change after
their final successful verification; later writes are evidence files only.

Security/data migration impact: no secrets, model credentials, real images, DB
migrations, platform edits, historical Release/Execution mutation or external
business actions. No model/GPU/service runtime was run. Local dependency download
and CPU validation are code/test evidence only; production readiness, model quality,
latency/throughput and GPU capacity remain unverified.

Rollback/recovery is limited to the newly added WP-01A files and its isolated
environments after review; there is no DB/Release activation to undo. No automatic
rollback, file deletion or Git reset/restore was performed.

## 9. Stop gate and recommendation

WP-01A local conclusion: **PASS** for Code/Test, **RUNTIME_NOT_RUN** for Runtime.
Wait for GPT-6 independent code review and user final acceptance. This local PASS
does not authorize proceeding through the next phase gate.

Recommended WP-01B preparation after A acceptance: confirm its exact staged model
inventory against the approved overall plan; reuse these foundation types for
Definition/Fact Schema contracts and the bounded in-memory Fact Registry with
their own fixtures and acceptance report. Keep Rule AST/TypeChecker in the later
assigned stage and all Bundle/Workflow/model/GPU work outside this package. No
WP-01B files or implementation were started. No Commit or Push was performed.

API behavior was checked against official [Pydantic strict-mode documentation](https://docs.pydantic.dev/latest/concepts/strict_mode/),
[Pydantic JSON Schema documentation](https://docs.pydantic.dev/latest/concepts/json_schema/)
and [uv locking/sync documentation](https://docs.astral.sh/uv/concepts/projects/sync/),
then verified in the actual pinned environment above.
