"""Resolve a complete typed artifact catalogue from pinned Bundle payload bytes."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from jimu_runtime.bundle.artifacts import (
    ArtifactIdentity,
    ArtifactLocation,
    BundleArtifactError,
    SchemaResource,
    artifact_identity,
    validate_artifact_root,
)
from jimu_runtime.bundle.integrity import (
    canonical_json,
    read_canonical_json,
    resolve_json_pointer,
    sha256_digest,
    validate_payload_path,
)
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definitions.resources import StaticAssetDescriptor
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_schemas.canonicalization import JSONValue
from jimu_runtime.domain.references import ContentRef, IdentitySelector
from jimu_runtime.domain.runtime.bundle_manifest import (
    ArtifactInventoryEntry,
    BundleManifest,
    CapabilityRequirements,
)


@dataclass(frozen=True, slots=True)
class ResolvedArtifact:
    inventory: ArtifactInventoryEntry
    value: ContractModel | SchemaResource
    canonical_bytes: bytes
    asset_bytes: bytes | None = None
    document_location: ArtifactLocation = ()

    @property
    def ref(self) -> ContentRef:
        return self.inventory.content_ref


@dataclass(frozen=True, slots=True)
class ArtifactCatalogue:
    artifacts: tuple[ResolvedArtifact, ...]
    _by_identity: Mapping[ArtifactIdentity, ResolvedArtifact]
    _by_id_version: Mapping[tuple[str, str], frozenset[ArtifactKind]]
    required_capabilities: CapabilityRequirements

    def of_kind(self, kind: ArtifactKind) -> tuple[ResolvedArtifact, ...]:
        return tuple(item for item in self.artifacts if item.ref.artifact_kind is kind)

    def resolve(
        self,
        ref: ContentRef,
        *,
        path: str = "manifest.json",
        json_location: tuple[str | int, ...] = (),
    ) -> ResolvedArtifact:
        target = self._resolve_identity(artifact_identity(ref), path, json_location)
        if target.ref.digest != ref.digest:
            raise BundleArtifactError(
                "reference_digest_mismatch",
                f"reference to {ref.artifact_id} has an incorrect digest",
                path,
                json_location,
            )
        return target

    def resolve_selector(
        self,
        selector: IdentitySelector,
        *,
        path: str = "manifest.json",
        json_location: tuple[str | int, ...] = (),
    ) -> ResolvedArtifact:
        """Resolve exact applicability membership without creating a digest edge."""
        return self._resolve_identity(
            (selector.artifact_kind, selector.artifact_id, selector.version_selector),
            path,
            json_location,
        )

    def _resolve_identity(
        self,
        identity: ArtifactIdentity,
        path: str,
        json_location: tuple[str | int, ...],
    ) -> ResolvedArtifact:
        target = self._by_identity.get(identity)
        if target is not None:
            return target
        kind, artifact_id, version = identity
        kinds = self._by_id_version.get((artifact_id, version))
        code = "reference_kind_mismatch" if kinds else "reference_missing"
        raise BundleArtifactError(
            code, f"expected {kind.value} {artifact_id}@{version}", path, json_location
        )


def _file_members(
    document: JSONValue, entries: list[ArtifactInventoryEntry], path: str
) -> dict[str, ArtifactLocation]:
    pointers = [entry.json_pointer or "" for entry in entries]
    declared_pointers = set(pointers)
    if len(pointers) != len(declared_pointers) or (
        "" in declared_pointers and len(pointers) != 1
    ):
        raise BundleArtifactError(
            "artifact_locator_overlap", "file locators overlap or repeat", path
        )
    if "" in declared_pointers:
        return {"": ()}
    pending: list[tuple[str, JSONValue, ArtifactLocation]] = [("", document, ())]
    matched: dict[str, ArtifactLocation] = {}
    while pending:
        pointer, value, location = pending.pop()
        if pointer in declared_pointers:
            matched[pointer] = location
            continue
        if isinstance(value, list) and value:
            pending.extend(
                (f"{pointer}/{index}", child, (*location, index))
                for index, child in enumerate(value)
            )
        elif isinstance(value, dict) and value:
            pending.extend(
                (
                    f"{pointer}/{key.replace('~', '~0').replace('/', '~1')}",
                    child,
                    (*location, key),
                )
                for key, child in value.items()
            )
        else:
            raise BundleArtifactError(
                "aggregate_inventory_mismatch",
                "unclaimed aggregate member",
                path,
                location,
            )
    unmatched = declared_pointers - matched.keys()
    if unmatched:
        raise BundleArtifactError(
            "aggregate_inventory_mismatch",
            f"locator {min(unmatched)} is missing or overlaps another member",
            path,
        )
    return matched


def _asset_data(
    descriptor: StaticAssetDescriptor,
    payloads: Mapping[str, bytes],
    asset_data_paths: set[str],
    max_bytes: int,
) -> bytes:
    path = validate_payload_path(descriptor.data_path)
    data = payloads.get(path)
    if data is None:
        raise BundleArtifactError(
            "asset_data_missing", "descriptor data is missing", path
        )
    if len(data) > max_bytes:
        raise BundleArtifactError(
            "asset_data_limit", "asset exceeds file byte budget", path
        )
    if len(data) != descriptor.byte_size or sha256_digest(data) != descriptor.sha256:
        raise BundleArtifactError(
            "asset_data_mismatch", "size or digest disagrees with descriptor", path
        )
    asset_data_paths.add(path)
    return data


def build_catalogue(
    manifest: BundleManifest,
    payloads: Mapping[str, bytes],
    *,
    max_bytes: int,
    max_depth: int,
) -> ArtifactCatalogue:
    """Require a bijection between inventoried locators and logical payload members.

    The caller checks the archive, checksums and root separately. Payloads exclude
    the two Bundle metadata files. Raw asset data comes only from descriptors and
    remains byte-exact even if its name ends in .json.
    """
    by_path: dict[str, list[ArtifactInventoryEntry]] = {}
    for entry in manifest.artifacts:
        path = validate_payload_path(entry.path)
        by_path.setdefault(path, []).append(entry)

    by_identity: dict[ArtifactIdentity, ResolvedArtifact] = {}
    by_id_version: dict[tuple[str, str], set[ArtifactKind]] = {}
    asset_data_paths: set[str] = set()
    for path, entries in by_path.items():
        raw = payloads.get(path)
        if raw is None:
            raise BundleArtifactError(
                "artifact_file_missing", "inventory payload is absent", path
            )
        document = read_canonical_json(
            raw, max_bytes=max_bytes, max_depth=max_depth, path=path
        )
        member_locations = _file_members(document, entries, path)
        for entry in entries:
            ref = entry.content_ref
            identity = artifact_identity(ref)
            location = member_locations[entry.json_pointer or ""]
            if identity in by_identity:
                raise BundleArtifactError(
                    "artifact_identity_duplicate",
                    "kind, ID and version appear more than once",
                    path,
                    location,
                )
            if entry.json_pointer:
                value = resolve_json_pointer(document, entry.json_pointer, path=path)
                canonical = canonical_json(
                    value, max_bytes=max_bytes, max_depth=max_depth, path=path
                )
            else:
                value, canonical = document, raw
            if sha256_digest(canonical) != ref.digest:
                raise BundleArtifactError(
                    "artifact_digest_mismatch",
                    "artifact content differs from inventory digest",
                    path,
                    location,
                )
            try:
                root = validate_artifact_root(ref, path, value, canonical)
            except BundleArtifactError as error:
                if not location:
                    raise
                raise BundleArtifactError(
                    error.code, error.message, path, (*location, *error.json_location)
                ) from error
            data = (
                _asset_data(root, payloads, asset_data_paths, max_bytes)
                if isinstance(root, StaticAssetDescriptor)
                else None
            )
            by_identity[identity] = ResolvedArtifact(
                entry, root, canonical, data, location
            )
            by_id_version.setdefault((ref.artifact_id, ref.version), set()).add(
                ref.artifact_kind
            )

    orphan = next(
        (
            path
            for path in sorted(payloads)
            if path not in by_path and path not in asset_data_paths
        ),
        None,
    )
    if orphan is not None:
        raise BundleArtifactError(
            "payload_unaccounted",
            "payload is neither inventoried nor declared asset data",
            orphan,
        )

    artifacts = tuple(
        by_identity[artifact_identity(entry.content_ref)]
        for entry in manifest.artifacts
    )
    return ArtifactCatalogue(
        artifacts,
        MappingProxyType(by_identity),
        MappingProxyType(
            {identity: frozenset(kinds) for identity, kinds in by_id_version.items()}
        ),
        manifest.required_capabilities,
    )
