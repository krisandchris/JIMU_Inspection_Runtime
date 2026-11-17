# 02 — Runtime Bundle Specification V1.0

**Status:** Proposed normative delivery contract  
**Depends on:** Runtime Domain Model V1.0

**Review revision:** WP-00-R2.1. These clarified contracts are PROPOSED /
PENDING_USER_APPROVAL. WP-01 defines domain contracts only; packaging, closure
admission, digest calculation and signing implementations remain WP-02.
R2 source commit: `40af8a17855b27063da94b3c9891272f3e3fc4e3`.
R2 bytes: `planning/review_evidence/WP-00-R2.1/before/`; R1/R2 audit unchanged.

## 1. Purpose

A Runtime Bundle is the immutable, self-contained configuration package accepted by a Runtime Release. It is not a database dump and does not contain model weights, secrets or executable source code.

Runtime must be able to execute an accepted Bundle without querying the requirements platform.

## 2. Package layout

```text
bundle/
  manifest.json
  ontology/entities.json
  ontology/regions.json
  capabilities/profiles.json
  capabilities/facets.json
  capabilities/questions.json
  tools/registry.json
  tools/plans.json
  rules/registry.json
  rules/sets.json
  models/bindings.json
  prompts/templates/*.json
  prompts/response_contracts/*.schema.json
  schemas/attributes/*.schema.json
  schemas/context/*.schema.json
  schemas/facts/definitions/*.json
  schemas/values/*.schema.json
  schemas/tools/inputs/*.schema.json
  schemas/tools/outputs/*.schema.json
  schemas/objects/*.schema.json
  schemas/decisions/*.schema.json
  policies/execution.json
  policies/evidence.json
  policies/coverage.json
  policies/resources.json
  policies/registry.json
  facts/producers.json
  vocabularies/*.json
  resources/geometry/*
  resources/assets/*
  checksums.json
```

Paths are relative, normalized POSIX paths. Reject absolute paths, `..`, duplicate paths, symlinks and archive path traversal. Define maximum archive size, expanded size, file count and individual file size in deployment policy.

The closed ArtifactKind/path catalogue is 01 §9; no independent Capability
Artifact is introduced. policies/execution.json, evidence.json, coverage.json and
resources.json hold named typed PolicyArtifacts (policy_kind=execution/evidence/
coverage/resource_requirements); policies/registry.json may hold other subtypes.
Each policy identity appears once only, even when referenced by multiple owners.
fact_schema artifacts are registry definitions; value_schema artifacts are their
generated payload schemas. Both are pinned and included, never hand-divergent.

F04 generation is defined in 01 §12.1. ValueSchemaGenerationInput is the normalized
type projection + explicit generation spec only. Generate value_schema content and
its independent digest first, then set FactSchemaDefinition.payload_schema_ref,
then hash that parent. No child input, `$id`, `$ref` or bytes include the parent's
identity/digest or Bundle digest. The input-derived `$id` is not a self-digest.
Two parents with the same normalized type/spec may share one payload Schema;
semantic compatibility still uses exact FactSchemaDefinition refs.

Each declared reference must resolve within this immutable package, including
prompt partials, response schemas, context schemas, Tool IO schemas, vocabularies,
policy resources, geometry and static assets needed for execution. Optional
resources may be absent only when no enabled configuration references them.
Geometry resources are data, not executable spatial code. Device/Camera registry
records and task images are trusted runtime inputs, not hidden Bundle config.

Model weights, Tool executable implementations, credentials, trust keys and
deployment endpoints are explicit external capabilities, not bundled resources.
ModelBinding/Tool ContentRefs declare contracts and version constraints;
admission resolves them against a trusted allowlist/capability report. Self-contained
means configuration closure without consulting the requirements platform or
fetching remote schemas/templates, not bundling weights or implementation code.

## 3. Manifest contract

Manifest fields:
- `format`: `jimu.runtime-bundle`
- `schema_version`
- `bundle_id`, `bundle_version`
- `compiler_version`
- `created_at`
- `source`: source system, source artifact IDs/versions/digests
- `artifacts[]`: ContentRef identity/digest, path, optional JSON Pointer for artifacts stored in an aggregate JSON file; exactly one entry per kind/ID/version
- `compatibility`: supported Runtime version range and required capabilities
- `required_capabilities[]`
- `integrity`: hash_algorithm, canonicalization_id, root_digest, signatures[] envelope

All normative models use strict Pydantic v2 (`extra="forbid"`). JSON Schema is generated from those models and checked in CI. Do not maintain a separate hand-edited schema that can drift.

## 4. Integrity algorithm

Use SHA-256 for file digests and a deterministic manifest/checksum construction.

Candidate algorithm ID: `jcs-sha256-v1`. No implementation or test vector has
been validated in this review; its acceptance belongs to WP-02.

1. Reject duplicate/unsafe paths, duplicate JSON keys and unsupported JSON numbers. Payload set P contains every package file except manifest.json and checksums.json; no undeclared extras are allowed.
2. JSON payloads are stored as RFC 8785/JCS canonical UTF-8 bytes; non-JSON resources use exact stored bytes. File size/hash refer to these stored bytes. No newline normalization of non-JSON resources is implied.
3. Build checksums.json as a versioned object with entries `(path, size, sha256)` for exactly P, sorted lexicographically by normalized path. It never includes itself or manifest.json.
4. Let C be canonical UTF-8 bytes of checksums.json. Let M be canonical UTF-8 bytes of manifest with only `integrity.root_digest` and `integrity.signatures` removed. hash_algorithm and canonicalization_id remain covered. All other manifest metadata remains covered.
5. Root digest = SHA256(domain || u64be(len(M)) || M || u64be(len(C)) || C), where domain is ASCII `JIMU-RUNTIME-BUNDLE-ROOT-v1` followed by one zero byte; u64be is an unsigned 8-byte big-endian length. The length framing and domain prevent ambiguous concatenation.
6. Store the root_digest in manifest; store detached signature envelopes under integrity.signatures. Sign the domain-separated root digest with an approved algorithm/key; signature preimage/envelope, algorithms and rotation/revocation remain WP-02 approval gates. Keys/trust never come solely from the Bundle. Unsigned content cannot be activated as an accepted signed Release.
7. Runtime recomputes payload inventory, checksums, M/C and root independently, then validates signatures against external trust. ZIP ordering, compression metadata and signature bytes are not logical configuration identity.

Artifact ContentRef.digest is SHA-256 of the artifact's own canonical JSON value
(or exact binary resource bytes), not the enclosing aggregate-file hash. Aggregate
file hashes remain in checksums; JSON Pointers must uniquely identify inventory
members. Artifacts do not contain their own digest. Forward ContentRefs include
dependency digests; reverse IdentitySelectors contain no digests, so leaf-first
hashing terminates. JSON Schema `$id` uses versioned identity, never its own hash.

Avoid self-referential hashing. The exact canonicalization, signature algorithm, key rotation and signature envelope must be frozen before implementation. ZIP compression metadata is not the logical Bundle identity.

## 5. Compiler pipeline

1. Resolve approved source artifacts, requiring a known ArtifactKind and the correct kind per referencing field (01 §9).
2. Resolve exact versions and digests recursively.
3. Validate every artifact against its schema.
4. Validate full semantic closure of ContentRefs and remaining explicit applicability IdentitySelectors, then require an acyclic ContentRef graph. Resolve selectors against pinned inventory; derive Question/ToolPlan reverse membership solely from Profile forward refs (D15), never serialize it back into artifact bytes.
5. Validate Profile → Entity/Facet/Question/ToolPlan/RuleSet and ModelBinding/Tool capability bindings; prompt variables/response contracts; Facet allowlist intersection; Profile QuestionSelectionPolicy partition and mandatory producer completeness.
6. Validate ToolPlan DAG acyclicity and typed input/output mappings.
7. Build typed ProducerCatalogue from Question primary outputs and Tool step output mappings; validate collision domains, conditional availability and exact selector/Schema resolution (01 §13).
8. Validate Rule required Facts have valid producers and compatible schemas.
9. Validate Rule AST operator and operand types.
10. Validate ModelBinding capabilities and configured fallback policy.
    Require ProviderPolicy.primary_deployment_id and remote_primary constraints
    (01 §3.9.1), or an explicit trusted capability-scoped LocalPrimaryException.
    Validate each fallback transition's exact contract and independently reviewed
    quality report; an ordered eligible local deployment is never sufficient.
11. Validate scene/Region applicability.
12. Validate Runtime capability compatibility.
13. Package files and calculate integrity data.
14. Emit a compile report with errors/warnings and deterministic Bundle digest.

A Rule depending on a Fact with no producer is a hard compile error. A required Tool or model capability that cannot be satisfied is a hard error. Optional capabilities may be absent only when an explicit policy defines the resulting behavior.

Closure also validates all prompt/context/Tool IO/schema/policy resources and their
transitive references, per-Profile producer scopes, declared Record field paths,
Question one-primary-Fact cardinality and Rule AST compatibility. Missing,
ambiguous, wrong-kind or wrong-digest selector targets reject the Bundle. Compiler
and admission independently perform these checks; WP-01 only defines the records
and checks types against injected finite registries, not a Bundle closure engine.

For each potentially applicable mandatory Rule and required Tool input, every
required Question producer must be mandatory in the Profile's selection policy.
Admission repeats this completeness check independent of model candidate/optional
selection. Rule applicability is typechecked with input_domain=rule_applicability
(no Object Facts), Rule conditions with rule_condition, Tool guards with
tool_condition. Manifest-rooted ProducerCatalogue may reference Profile/Question/
ToolPlan; those definitions never content-reference that derived catalogue.
This prevents Profile→Rule→catalogue→Profile digest cycles.

Runtime external capabilities (deployment implementations/review approvals) are
checked against trusted local registries, not downloaded from Bundle URLs. Full
configuration closure includes all typed policy refs, prompt partials, generated
schemas, response contracts, vocabulary/static assets and producer declarations;
type names alone do not constitute resources. The capability/path table is the
single authority for allowed ArtifactKinds; unknown required kind rejects.

R2.1 adds no ArtifactKind or resource-directory change. SelectionCallPolicy's
explicit additional binding/resource refs, local-primary exception approval refs,
and generation spec must be included/validated under their existing artifact or
trusted external-review categories. Selection runtime Invocation/Attempt evidence
and ToolExecution origins are runtime records, not missing Bundle resources.

Compiler/admission independently regenerate payload Schema from the declared
projection/profile/generator inputs and compare exact canonical content/digest to
payload_schema_ref; parent back-references or drift reject. Fixed-generation-input
repeatability and projection/Schema contracts belong to authorized CPU WP-01
work; complete artifact graph digest resolution, file inventory/root hash,
signatures and admission remain WP-02. No digest implementation/test ran in R2.1.

## 6. Runtime admission

Runtime admission performs independent validation; it must not blindly trust the compiler.

Checks:
- archive safety and resource limits;
- file hashes and root digest;
- signature trust and revocation;
- manifest schema;
- artifact schema and exact refs;
- dependency closure;
- Rule AST validity;
- Fact producer/consumer compatibility;
- Tool registry compatibility;
- Model capability compatibility;
- Runtime version/capability compatibility;
- policy and security constraints.

Return an immutable Admission Receipt:
- `receipt_id`
- `bundle_id`, `bundle_version`, `bundle_digest`
- `runtime_version`
- `accepted` boolean
- required/optional capability report
- validation errors and warnings
- timestamp and Runtime identity

## 7. Release lifecycle and authority

Separate technical admission from business approval and activation.

Suggested lifecycle:
`draft → compiled → submitted → accepted|rejected → approved → active → deprecated|revoked`.

- Compiler: produces Bundle.
- Runtime: accepts or rejects technical compatibility.
- Authorized release approver: approves business deployment.
- Release controller: activates a Release within an explicit scope.
- Runtime: executes tasks pinned to that Release.

For the standalone first phase, a Runtime-local approval identity may be used. Future requirements-platform integration can delegate business approval without changing Runtime execution semantics.

Activation scopes: `global`, `scene`, `region`, `device`. Scope precedence must be explicitly defined and tested. Ambiguous conflicting activations must fail closed.

## 8. Immutability and task pinning

Release contains exact Bundle ID/version/digest and approval/admission receipts. Activation is a separate append-only event/pointer change. Do not mutate a Release to point at a new Bundle.

At Task acceptance, resolve and persist the selected Release. Every Execution pins that Release and digest. Active pointer changes affect only newly accepted Tasks.

## 9. Compatibility policy

Runtime publishes a capability document:
- supported Bundle schema versions;
- supported Tool types and Tool contract versions;
- supported Fact types/schema dialect;
- supported Rule operators;
- supported model modalities and structured-output features;
- runtime feature flags.

Unknown required features cause rejection. Unknown optional metadata may be retained or ignored only if explicitly declared as extension metadata and namespaced.

## 10. Security

- No credentials or API keys in Bundle.
- No arbitrary code, shell command, URL fetch directive or Python expression in Rule/Tool config.
- Tool references resolve only against a local allowlisted registry.
- Remote URLs are subject to SSRF controls and allowlists.
- Bundle signatures use trusted keys configured outside the Bundle.
- Bundle upload is authenticated, size-limited and audited.
- Persist admitted Bundle bytes or a durable immutable copy.

## 11. Failure and rollback

Rejected Bundle never becomes Active. Activation rollback changes the active pointer to a prior accepted Release; it does not mutate or delete either Release. In-flight Tasks retain their pinned Release.

## 12. Acceptance tests

- deterministic digest for same logical content;
- digest changes when any payload changes;
- tampered payload rejected;
- untrusted signature rejected;
- duplicate/path traversal archive rejected;
- unresolved references rejected;
- Rule with unproduced Fact rejected;
- ToolPlan cycle rejected;
- unsupported required capability rejected;
- active pointer rollback leaves historical Release intact;
- in-flight Task remains pinned after activation change.
