# S1 handoff (pending Primary review)

Frozen scope: CONTRACT_FIELDS.md §1; PLAN-3-DAG §8. Worktree only, HEAD
c25bef668136acc651e7313ba608ec6fdefd39a5. No commit/push/branch/remote mutation.
All authored product/test/fixture paths are listed and hashed in after_sha256.json.

## Evidence

Each numbered command JSON contains exact argv/cwd/UTC start+finish/exit/stdout/stderr.
01_uv_sync.json initialized the locked CPU environment. The lock and pyproject were
unchanged (20_protected_diff.json); no RFC8785 was installed. Environment/package
versions are in 02_environment.json and uv version in 13_uv_version.json.
The initial read-only shell probe had no `python` executable; uv selected CPython3.12.3.
No project test used the absent bare command.

Final targeted pytest: 122 passed, zero failures/errors/skips (22_pytest.json,
22_junit.xml). Ruff check, mypy src, Ruff format check and 34 generated model/alias/
enum Schema checks passed (23–26). Runtime: RUNTIME_NOT_RUN.

Failures are retained: first 03 pytest (9 failed/102 passed), 07 mypy (4 errors),
and additional UTC Schema regression 15 pytest (1 failed/121 passed). Fixed through
code changes; no tests skipped/removed or dependency/typing checks weakened.

## Public shared interfaces

- definition_enums.py: six frozen WireEnum domains.
- definition_support.py: ShortText, Description, Namespace, SemanticKey, FieldPath,
  PositiveBudget, NonNegativeBudget, UTCDateTime; immutable_sequence and
  reference_identity, require_unique_references.
- DefinitionContract: persisted root with required schema_version='1.0.0'.
- OptionalContract: nested helper base for absent/non-null optional fields;
  DefinitionContract already inherits it. Preserves absence under A's always
  instance-revalidation without accepting explicitly supplied or constructed null.
- AbsentOptional[T] rejects supplied null and removes nullable branches from
  generated Schema. Its model field must use an explicit Pydantic Field with
  default=None, validate_default=False, exclude_if=lambda value: value is None,
  json_schema_extra=omit_default_from_schema. See ExternalCapabilityRequirement.
  PEP695 alias metadata alone cannot supply these field-specific settings.
- Narrow ContentRef types: VocabularyContentRef, AttributeSchemaContentRef,
  StaticAssetContentRef, ContextSchemaContentRef, GeometryResourceContentRef.
  Each keeps four wire fields. ContextSchemaRef is a SchemaRef and requires dialect.
- ReviewReportRef, ExternalCapabilityRequirement, VocabularyMemberRef,
  VersionConstraint, ContextFieldSelector, AssetDimensions.
- definitions.ontology: EntityDefinition, RegionDefinition.
- definitions.resources: VocabularyDefinition, StaticAssetDescriptor.

Finite vocabulary identity/member/domain validation is not provided by these shape
models. The later integration owner must require exact injected catalogue refs and
record missing catalogues as incomplete. Asset descriptor models do no filesystem,
symlink, original-byte/hash or size authenticity verification. Local geometry is
an exact ref only. Domain import-boundary/full A regression belong to later
integration. Existing A numeric representation distinction remains unchanged.

No downstream task or WP acceptance is implied by this handoff.
