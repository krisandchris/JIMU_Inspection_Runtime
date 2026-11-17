# 02 — Runtime Bundle Specification V1.0

**Status:** Proposed normative delivery contract  
**Depends on:** Runtime Domain Model V1.0

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
  schemas/facts/*.schema.json
  schemas/objects/*.schema.json
  schemas/decisions/*.schema.json
  policies/execution.json
  policies/evidence.json
  checksums.json
```

Paths are relative, normalized POSIX paths. Reject absolute paths, `..`, duplicate paths, symlinks and archive path traversal. Define maximum archive size, expanded size, file count and individual file size in deployment policy.

## 3. Manifest contract

Manifest fields:
- `format`: `jimu.runtime-bundle`
- `schema_version`
- `bundle_id`, `bundle_version`
- `compiler_version`
- `created_at`
- `source`: source system, source artifact IDs/versions/digests
- `artifacts[]`: artifact ID, version, digest, path, kind
- `compatibility`: supported Runtime version range and required capabilities
- `required_capabilities[]`
- `integrity`: hash algorithm, root digest, signature metadata

All normative models use strict Pydantic v2 (`extra="forbid"`). JSON Schema is generated from those models and checked in CI. Do not maintain a separate hand-edited schema that can drift.

## 4. Integrity algorithm

Use SHA-256 for file digests and a deterministic manifest/checksum construction.

1. Reject duplicate or unsafe paths.
2. Canonicalize each JSON document using a specified canonical JSON algorithm (recommend RFC 8785/JCS; implementation choice must be verified).
3. Compute SHA-256 for each payload file.
4. Sort normalized relative paths lexicographically.
5. Build `checksums.json` from `(path, size, sha256)` entries.
6. Compute the root digest over the canonical checksum manifest and the canonical manifest payload with the integrity signature field excluded.
7. Sign the root digest separately using an approved signing algorithm and key ID.
8. Verify signature against a trusted key registry; never trust a public key supplied only by the Bundle itself.

Avoid self-referential hashing. The exact canonicalization, signature algorithm, key rotation and signature envelope must be frozen before implementation. ZIP compression metadata is not the logical Bundle identity.

## 5. Compiler pipeline

1. Resolve approved source artifacts.
2. Resolve exact versions and digests recursively.
3. Validate every artifact against its schema.
4. Validate reference closure.
5. Validate Profile → Entity/Facet/Question/ToolPlan relations.
6. Validate ToolPlan DAG acyclicity and typed input/output mappings.
7. Build Fact producer/consumer graph.
8. Validate Rule required Facts have valid producers and compatible schemas.
9. Validate Rule AST operator and operand types.
10. Validate ModelBinding capabilities and configured fallback policy.
11. Validate scene/Region applicability.
12. Validate Runtime capability compatibility.
13. Package files and calculate integrity data.
14. Emit a compile report with errors/warnings and deterministic Bundle digest.

A Rule depending on a Fact with no producer is a hard compile error. A required Tool or model capability that cannot be satisfied is a hard error. Optional capabilities may be absent only when an explicit policy defines the resulting behavior.

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
