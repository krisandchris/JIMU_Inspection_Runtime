"""Compile approved source bytes into a deterministic Runtime Bundle candidate."""

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from typing import cast

from pydantic import ValidationError

from jimu_runtime.bundle.archive import (
    BundleLimits,
    read_archive,
    read_bundle,
    write_archive,
)
from jimu_runtime.bundle.artifacts import (
    ARTIFACT_LAYOUTS,
    DRAFT_2020_12,
    ArtifactIdentity,
    BundleArtifactError,
    artifact_identity,
    validate_artifact_root,
)
from jimu_runtime.bundle.capabilities import CapabilitySnapshot
from jimu_runtime.bundle.catalogue import build_catalogue
from jimu_runtime.bundle.closure import validate_closure
from jimu_runtime.bundle.integrity import (
    CHECKSUMS_PATH,
    MANIFEST_PATH,
    BundleIntegrityError,
    JSONLocation,
    build_checksums,
    canonical_json,
    canonicalize_json,
    compute_root_digest,
    read_canonical_json,
    sha256_digest,
    validate_payload_path,
    verify_bundle_files,
)
from jimu_runtime.bundle.producer_rules import (
    SystemProducerGrant,
    derive_producer_catalogue,
)
from jimu_runtime.bundle.semantics import validate_bundle_semantics
from jimu_runtime.bundle.trust import TrustError, TrustSource, check_bundle_source
from jimu_runtime.domain.definition_support import UTCDateTime
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_schemas.canonicalization import JSONValue
from jimu_runtime.domain.fact_schemas.models import (
    ProjectionValidationError,
    ValueSchemaGenerationSpec,
)
from jimu_runtime.domain.fact_schemas.projection import generate_value_schema
from jimu_runtime.domain.primitives import ArtifactId, RecordId, Version
from jimu_runtime.domain.producers import FactProducerDeclaration
from jimu_runtime.domain.references import ContentRef
from jimu_runtime.domain.runtime.bundle_manifest import (
    ArtifactInventoryEntry,
    BundleManifest,
    BundleRef,
    CapabilityRequirements,
    ChecksumManifest,
    CompatibilityRequirement,
    DetachedSignatureRef,
    IntegrityEnvelope,
    SourceProvenance,
)
from jimu_runtime.domain.runtime.releases import CapabilityReport
from jimu_runtime.domain.validation.issues import CheckResult, ValidationIssue


@dataclass(frozen=True, slots=True, kw_only=True)
class ReferenceSlot:
    """A compiler-only instruction to replace one explicit JSON null with a pin.

    The enclosing source definition validates the kind and shape at this field.
    Already pinned definitions can omit slots and are independently checked.
    """

    location: JSONLocation
    target: ArtifactIdentity
    schema_ref: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceIdentity:
    """Link bytes to the approved external source row within one source system."""

    artifact_id: RecordId
    version: Version


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceArtifact:
    """One approved logical artifact before the compiler lays out package files."""

    kind: ArtifactKind
    artifact_id: ArtifactId
    version: Version
    path: str
    content: bytes
    origin: SourceIdentity
    reference_slots: tuple[ReferenceSlot, ...] = ()

    @property
    def identity(self) -> ArtifactIdentity:
        return self.kind, self.artifact_id, self.version


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceAssetBytes:
    """Exact raw data associated with a bundle StaticAssetDescriptor."""

    path: str
    content: bytes
    origin: SourceIdentity


@dataclass(frozen=True, slots=True, kw_only=True)
class BundleBuildMetadata:
    bundle_id: ArtifactId
    bundle_version: Version
    compiler_version: Version
    created_at: UTCDateTime
    source: SourceProvenance
    compatibility: CompatibilityRequirement
    required_capabilities: CapabilityRequirements
    producer_catalogue_id: ArtifactId
    producer_catalogue_version: Version


@dataclass(frozen=True, slots=True, kw_only=True)
class BundleCompileInput:
    metadata: BundleBuildMetadata
    artifacts: tuple[SourceArtifact, ...]
    assets: tuple[SourceAssetBytes, ...] = ()
    system_declarations: tuple[FactProducerDeclaration, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class CompileEnvironment:
    capabilities: CapabilitySnapshot
    trust: TrustSource
    validation_at: UTCDateTime
    system_grants: tuple[SystemProducerGrant, ...] = ()


@dataclass(frozen=True, slots=True)
class CompileIssue:
    code: str
    message: str
    path: str | None = None
    location: JSONLocation = ()


@dataclass(frozen=True, slots=True)
class CompiledBundle:
    bundle_ref: BundleRef
    archive_bytes: bytes


@dataclass(frozen=True, slots=True)
class CompileReport:
    bundle: CompiledBundle | None
    errors: tuple[CompileIssue, ...]
    warnings: tuple[CompileIssue, ...]
    semantic_checks: tuple[CheckResult, ...] = ()
    capabilities: CapabilityReport | None = None

    @property
    def succeeded(self) -> bool:
        return self.bundle is not None


@dataclass(slots=True)
class _SourceDocument:
    artifact: SourceArtifact
    value: JSONValue


@dataclass(frozen=True, slots=True)
class _Member:
    path: str
    ref: ContentRef
    value: JSONValue
    canonical_bytes: bytes


def _json_options(limits: BundleLimits) -> dict[str, int]:
    return {"max_bytes": limits.max_file_bytes, "max_depth": limits.max_json_depth}


def _bad_source(
    artifact: SourceArtifact, code: str, message: str
) -> BundleArtifactError:
    return BundleArtifactError(code, message, artifact.path)


def _source_documents(
    source: BundleCompileInput, limits: BundleLimits
) -> tuple[dict[ArtifactIdentity, _SourceDocument], dict[str, bytes]]:
    if len(source.artifacts) > 16_384 or len(source.assets) > limits.max_files:
        raise BundleIntegrityError(
            "source_inventory_limit", "source inventory exceeds budget"
        )
    expanded = sum(len(item.content) for item in source.artifacts) + sum(
        len(item.content) for item in source.assets
    )
    if expanded > limits.max_expanded_bytes:
        raise BundleIntegrityError(
            "source_expanded_limit", "source bytes exceed budget"
        )
    documents: dict[ArtifactIdentity, _SourceDocument] = {}
    for artifact in source.artifacts:
        path = validate_payload_path(artifact.path)
        if not isinstance(artifact.kind, ArtifactKind):
            raise _bad_source(
                artifact,
                "source_artifact_kind_invalid",
                "expected a known ArtifactKind",
            )
        if artifact.kind is ArtifactKind.FACT_PRODUCER_CATALOG:
            raise _bad_source(
                artifact, "source_catalogue_supplied", "the catalogue is derived"
            )
        if artifact.identity in documents:
            raise _bad_source(
                artifact, "source_identity_duplicate", "source identity repeats"
            )
        if not ARTIFACT_LAYOUTS[artifact.kind].matches(path):
            raise _bad_source(
                artifact, "artifact_path_mismatch", "source path disagrees with kind"
            )
        if artifact.reference_slots and ARTIFACT_LAYOUTS[artifact.kind].root is None:
            raise _bad_source(
                artifact,
                "source_schema_slot",
                "schema resources use offline URI references",
            )
        if len(artifact.reference_slots) > len(artifact.content) // 4:
            raise _bad_source(
                artifact,
                "source_reference_slot_limit",
                "every slot requires a separate JSON null",
            )
        slots: set[JSONLocation] = set()
        for slot in artifact.reference_slots:
            if (
                not slot.location
                or len(slot.location) > limits.max_json_depth
                or slot.location in slots
            ):
                raise BundleArtifactError(
                    "source_reference_slot_invalid",
                    "slot locations must be unique, nonempty and within the depth budget",
                    path,
                    slot.location,
                )
            slots.add(slot.location)
        options = _json_options(limits)
        raw = canonicalize_json(artifact.content, path=path, **options)
        value = read_canonical_json(raw, path=path, **options)
        documents[artifact.identity] = _SourceDocument(artifact, value)
    assets: dict[str, bytes] = {}
    for asset in source.assets:
        path = validate_payload_path(asset.path)
        if path in assets:
            raise BundleIntegrityError(
                "source_asset_duplicate", "raw asset path repeats", path=path
            )
        if len(asset.content) > limits.max_file_bytes:
            raise BundleIntegrityError(
                "source_asset_limit", "raw asset exceeds file budget", path=path
            )
        assets[path] = asset.content
    return documents, assets


def _check_source_provenance(source: BundleCompileInput) -> None:
    provenance = SourceProvenance.model_validate(source.metadata.source)
    approved = {
        (row.artifact_id, row.version): row.digest for row in provenance.artifacts
    }
    seen: set[tuple[str, str]] = set()
    inputs: tuple[SourceArtifact | SourceAssetBytes, ...] = (
        *source.artifacts,
        *source.assets,
    )
    for item in inputs:
        key = item.origin.artifact_id, item.origin.version
        if key in seen:
            raise BundleIntegrityError(
                "source_provenance_duplicate",
                "approved source row is bound more than once",
                path=item.path,
            )
        if key not in approved:
            raise BundleIntegrityError(
                "source_provenance_missing",
                "input bytes do not map to an approved source row",
                path=item.path,
            )
        if sha256_digest(item.content) != approved[key]:
            raise BundleIntegrityError(
                "source_provenance_digest_mismatch",
                "raw bytes differ from the approved source digest",
                path=item.path,
            )
        seen.add(key)
    if seen != approved.keys():
        raise BundleIntegrityError(
            "source_provenance_orphan",
            "approved source row has no input bytes",
            path=MANIFEST_PATH,
        )


def _member(
    identity: ArtifactIdentity, path: str, value: JSONValue, limits: BundleLimits
) -> _Member:
    raw = canonical_json(value, path=path, **_json_options(limits))
    kind, identifier, version = identity
    ref = ContentRef(
        artifact_kind=kind,
        artifact_id=identifier,
        version=version,
        digest=sha256_digest(raw),
    )
    validate_artifact_root(ref, path, value, raw)
    return _Member(path, ref, value, raw)


def _generated_members(
    documents: dict[ArtifactIdentity, _SourceDocument], limits: BundleLimits
) -> dict[ArtifactIdentity, _Member]:
    generated: dict[ArtifactIdentity, _Member] = {}
    for source in documents.values():
        if source.artifact.kind is not ArtifactKind.FACT_SCHEMA:
            continue
        if not isinstance(source.value, dict):
            raise _bad_source(
                source.artifact,
                "source_fact_schema_shape",
                "expected a Fact Schema object",
            )
        generation = ValueSchemaGenerationSpec.model_validate(
            source.value.get("value_schema_generation")
        )
        result = generate_value_schema(source.value.get("type_definition"), generation)
        ref = result.child_ref
        implicit = cast(dict[str, JSONValue], ref.model_dump(mode="json"))
        existing = source.value.get("payload_schema_ref")
        if existing is not None and existing != implicit:
            raise _bad_source(
                source.artifact,
                "source_value_schema_pin",
                "supplied pin differs from generated child",
            )
        source.value["payload_schema_ref"] = implicit
        identity = artifact_identity(ref)
        if identity in generated:
            continue
        supplied = documents.get(identity)
        path = (
            supplied.artifact.path
            if supplied
            else f"schemas/values/{ref.artifact_id}.schema.json"
        )
        raw = result.canonical_schema_bytes
        value = read_canonical_json(raw, path=path, **_json_options(limits))
        if (
            supplied is not None
            and canonical_json(supplied.value, path=path, **_json_options(limits))
            != raw
        ):
            raise _bad_source(
                supplied.artifact,
                "source_value_schema_drift",
                "supplied child differs from generation",
            )
        validate_artifact_root(ref, path, value, raw)
        generated[identity] = _Member(path, ref, value, raw)
    return generated


def _apply_slot(
    source: _SourceDocument, slot: ReferenceSlot, target: ContentRef
) -> None:
    current = source.value
    for offset, key in enumerate(slot.location):
        if isinstance(current, dict) and type(key) is str and key in current:
            child = current[key]
        elif isinstance(current, list) and type(key) is int and 0 <= key < len(current):
            child = current[key]
        else:
            break
        if offset + 1 != len(slot.location):
            current = child
            continue
        if child is not None:
            break
        replacement = cast(dict[str, JSONValue], target.model_dump(mode="json"))
        if slot.schema_ref:
            replacement["schema_dialect"] = DRAFT_2020_12
        if isinstance(current, dict) and isinstance(key, str):
            current[key] = replacement
        elif isinstance(current, list) and isinstance(key, int):
            current[key] = replacement
        return
    raise BundleArtifactError(
        "source_reference_slot_invalid",
        "slot must address an existing, unbound JSON null",
        source.artifact.path,
        slot.location,
    )


def _sort_identity(identity: ArtifactIdentity) -> tuple[str, str, str]:
    kind, identifier, version = identity
    return kind.value, identifier, version


def _resolve_members(
    documents: dict[ArtifactIdentity, _SourceDocument], limits: BundleLimits
) -> tuple[_Member, ...]:
    resolved = _generated_members(documents, limits)
    graph: TopologicalSorter[ArtifactIdentity] = TopologicalSorter()
    for identity in sorted(documents, key=_sort_identity):
        source = documents[identity]
        if (
            source.artifact.kind is ArtifactKind.VALUE_SCHEMA
            and identity not in resolved
        ):
            raise _bad_source(
                source.artifact,
                "source_value_schema_orphan",
                "no Fact Schema generates this child",
            )
        dependencies = []
        for slot in source.artifact.reference_slots:
            if slot.target not in resolved:
                if slot.target not in documents:
                    raise BundleArtifactError(
                        "source_reference_missing",
                        "slot target is missing from source inventory",
                        source.artifact.path,
                        slot.location,
                    )
                dependencies.append(slot.target)
        graph.add(identity, *dependencies)
    try:
        order = tuple(graph.static_order())
    except CycleError as error:
        raise BundleIntegrityError(
            "source_reference_cycle", "source slots form a content-reference cycle"
        ) from error
    for identity in order:
        if identity in resolved:
            continue
        source = documents[identity]
        for slot in source.artifact.reference_slots:
            _apply_slot(source, slot, resolved[slot.target].ref)
        resolved[identity] = _member(
            identity, source.artifact.path, source.value, limits
        )
    return tuple(
        resolved[identity] for identity in sorted(resolved, key=_sort_identity)
    )


def _payloads(
    members: tuple[_Member, ...], assets: Mapping[str, bytes], limits: BundleLimits
) -> tuple[tuple[ArtifactInventoryEntry, ...], dict[str, bytes]]:
    grouped: dict[str, list[_Member]] = defaultdict(list)
    for member in members:
        grouped[member.path].append(member)
    payloads = dict(assets)
    entries: list[ArtifactInventoryEntry] = []
    for path, values in sorted(grouped.items()):
        if path in payloads:
            raise BundleIntegrityError(
                "source_path_overlap", "artifact path overlaps raw data", path=path
            )
        if len(values) == 1:
            member = values[0]
            entries.append(ArtifactInventoryEntry(content_ref=member.ref, path=path))
            payloads[path] = member.canonical_bytes
            continue
        if not _is_aggregate_path(path, values):
            raise BundleIntegrityError(
                "source_path_overlap",
                "distinct standalone artifacts share one path",
                path=path,
            )
        for index, member in enumerate(values):
            entries.append(
                ArtifactInventoryEntry(
                    content_ref=member.ref, path=path, json_pointer=f"/{index}"
                )
            )
        document = [member.value for member in values]
        payloads[path] = canonical_json(document, path=path, **_json_options(limits))
    return tuple(entries), payloads


def _is_aggregate_path(path: str, members: list[_Member]) -> bool:
    return all(
        ARTIFACT_LAYOUTS[member.ref.artifact_kind].location == path
        or (
            member.ref.artifact_kind is ArtifactKind.POLICY
            and path.startswith("policies/")
        )
        for member in members
    )


def _manifest(
    metadata: BundleBuildMetadata,
    entries: tuple[ArtifactInventoryEntry, ...],
) -> BundleManifest:
    return BundleManifest(
        format="jimu.runtime-bundle",
        schema_version="1.0.0",
        bundle_id=metadata.bundle_id,
        bundle_version=metadata.bundle_version,
        compiler_version=metadata.compiler_version,
        created_at=metadata.created_at,
        source=metadata.source,
        artifacts=entries,
        compatibility=metadata.compatibility,
        required_capabilities=metadata.required_capabilities,
        integrity=IntegrityEnvelope(
            hash_algorithm="sha256",
            canonicalization_id="jcs-sha256-v1",
            root_digest="0" * 64,
            signatures=(),
        ),
    )


def _seal_package(
    manifest: BundleManifest, payloads: Mapping[str, bytes], limits: BundleLimits
) -> tuple[bytes, BundleRef]:
    checksums = build_checksums(payloads)
    document = cast(dict[str, JSONValue], manifest.model_dump(mode="json"))
    digest = compute_root_digest(document, checksums, **_json_options(limits))
    integrity = cast(dict[str, JSONValue], document["integrity"])
    integrity["root_digest"] = digest
    raw = _write_package(document, checksums, payloads, limits)
    return raw, BundleRef(
        bundle_id=manifest.bundle_id,
        bundle_version=manifest.bundle_version,
        bundle_digest=digest,
    )


def _write_package(
    manifest: dict[str, JSONValue],
    checksums: ChecksumManifest,
    payloads: Mapping[str, bytes],
    limits: BundleLimits,
) -> bytes:
    options = _json_options(limits)
    return write_archive(
        {
            **payloads,
            MANIFEST_PATH: canonical_json(manifest, path=MANIFEST_PATH, **options),
            CHECKSUMS_PATH: canonical_json(
                checksums.model_dump(mode="json"), path=CHECKSUMS_PATH, **options
            ),
        },
        limits,
    )


def _from_validation_issue(issue: ValidationIssue) -> CompileIssue:
    return CompileIssue(issue.code, issue.message, location=issue.path)


def _from_source_error(
    error: BundleIntegrityError
    | BundleArtifactError
    | ValidationError
    | ProjectionValidationError
    | TrustError,
) -> CompileIssue:
    if isinstance(error, TrustError):
        return _from_validation_issue(error.issue)
    if isinstance(error, ValidationError):
        issue = error.errors(
            include_input=False, include_url=False, include_context=False
        )[0]
        return CompileIssue(
            "compiler_contract_invalid", issue["msg"], location=issue["loc"]
        )
    if isinstance(error, ProjectionValidationError):
        return CompileIssue(error.code, error.message, location=error.path)
    return CompileIssue(error.code, str(error), error.path, error.json_location)


def compile_bundle(
    source: BundleCompileInput, *, limits: BundleLimits, environment: CompileEnvironment
) -> CompileReport:
    """Emit an unsigned candidate only after the shared offline validations pass."""
    checks: tuple[CheckResult, ...] = ()
    warnings: tuple[CompileIssue, ...] = ()
    capabilities: CapabilityReport | None = None
    try:
        metadata = source.metadata
        options = _json_options(limits)
        documents, assets = _source_documents(source, limits)
        _check_source_provenance(source)
        members = _resolve_members(documents, limits)
        inventory, payloads = _payloads(members, assets, limits)
        provisional = _manifest(metadata, inventory)
        catalogue = build_catalogue(provisional, payloads, **options)
        validate_closure(catalogue)
        producer_catalogue = derive_producer_catalogue(
            catalogue,
            catalogue_id=metadata.producer_catalogue_id,
            version=metadata.producer_catalogue_version,
            schema_version="1.0.0",
            system_declarations=source.system_declarations,
        )
        producer_member = _member(
            (
                ArtifactKind.FACT_PRODUCER_CATALOG,
                producer_catalogue.catalogue_id,
                producer_catalogue.version,
            ),
            ARTIFACT_LAYOUTS[ArtifactKind.FACT_PRODUCER_CATALOG].location,
            producer_catalogue.model_dump(mode="json"),
            limits,
        )
        inventory, payloads = _payloads((*members, producer_member), assets, limits)
        archive, bundle_ref = _seal_package(
            _manifest(metadata, inventory), payloads, limits
        )
        verified = read_bundle(archive, limits)
        catalogue = build_catalogue(verified.manifest, verified.payloads, **options)
        closure = validate_closure(catalogue)
        semantics = validate_bundle_semantics(
            catalogue,
            closure,
            verified.manifest,
            environment.capabilities,
            validation_at=environment.validation_at,
            max_bytes=limits.max_file_bytes,
            max_depth=limits.max_json_depth,
            system_grants=environment.system_grants,
        )
        checks = semantics.checks
        errors = tuple(
            _from_validation_issue(issue)
            for check in checks
            if check.status in ("invalid", "incomplete")
            for issue in check.issues
        )
        warnings = tuple(
            _from_validation_issue(issue)
            for check in checks
            if check.status == "deferred"
            for issue in check.issues
        )
        capabilities = semantics.capabilities.report
        if not semantics.accepted:
            return CompileReport(None, errors, warnings, checks, capabilities)
        check_bundle_source(
            verified.manifest,
            bundle_ref.bundle_digest,
            environment.trust,
            validation_at=environment.validation_at,
        )
        return CompileReport(
            CompiledBundle(bundle_ref, archive), (), warnings, checks, capabilities
        )
    except (
        BundleIntegrityError,
        BundleArtifactError,
        ValidationError,
        ProjectionValidationError,
        TrustError,
    ) as error:
        return CompileReport(
            None, (_from_source_error(error),), warnings, checks, capabilities
        )


def attach_signature_refs(
    raw: bytes, signatures: tuple[DetachedSignatureRef, ...], *, limits: BundleLimits
) -> bytes:
    """Add detached evidence references to valid transport bytes for submission."""
    files = read_archive(raw, limits)
    verified = verify_bundle_files(
        files,
        max_file_bytes=limits.max_file_bytes,
        max_json_depth=limits.max_json_depth,
    )
    manifest = cast(
        dict[str, JSONValue],
        read_canonical_json(
            files[MANIFEST_PATH],
            path=MANIFEST_PATH,
            **_json_options(limits),
        ),
    )
    all_signatures = (*verified.manifest.integrity.signatures, *signatures)
    integrity = IntegrityEnvelope(
        hash_algorithm=verified.manifest.integrity.hash_algorithm,
        canonicalization_id=verified.manifest.integrity.canonicalization_id,
        root_digest=verified.manifest.integrity.root_digest,
        signatures=tuple(
            sorted(
                all_signatures,
                key=lambda ref: (
                    ref.signature_registry_id,
                    ref.signature_id,
                    ref.version,
                ),
            )
        ),
    )
    envelope = cast(dict[str, JSONValue], manifest["integrity"])
    envelope["signatures"] = integrity.model_dump(mode="json")["signatures"]
    attached = _write_package(manifest, verified.checksums, verified.payloads, limits)
    read_bundle(attached, limits)
    return attached
