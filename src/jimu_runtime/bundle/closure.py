"""Offline closure over typed artifact references and packaged Schema resources."""

from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Self
from urllib.parse import unquote, urldefrag, urljoin

from jimu_runtime.bundle.artifacts import (
    DRAFT_2020_12,
    ArtifactIdentity,
    ArtifactLocation,
    BundleArtifactError,
    SchemaResource,
    absolute_schema_id,
    artifact_identity,
    valid_uri_reference,
)
from jimu_runtime.bundle.catalogue import ArtifactCatalogue, ResolvedArtifact
from jimu_runtime.bundle.integrity import BundleIntegrityError, resolve_json_pointer
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import PolicyKind
from jimu_runtime.domain.definition_support import (
    ExternalCapabilityRequirement,
    ReviewReportRef,
    VocabularyMemberRef,
)
from jimu_runtime.domain.definitions.capabilities import ProfileDefinition
from jimu_runtime.domain.definitions.resources import VocabularyDefinition
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_schemas.canonicalization import JSONValue
from jimu_runtime.domain.policies import DeploymentRequirement
from jimu_runtime.domain.references import (
    ContentRef,
    IdentitySelector,
    RuntimeRecordRef,
)

type _SchemaNode = dict[str, JSONValue] | bool
type _SchemaNodeKey = tuple[ArtifactIdentity, str]
type ExternalReferenceValue = (
    ExternalCapabilityRequirement | DeploymentRequirement | ReviewReportRef
)

_SCHEMA_SINGLE = (
    "additionalProperties",
    "contains",
    "contentSchema",
    "else",
    "if",
    "items",
    "not",
    "propertyNames",
    "then",
    "unevaluatedItems",
    "unevaluatedProperties",
)
_SCHEMA_ARRAY = ("allOf", "anyOf", "oneOf", "prefixItems")
_SCHEMA_MAP = (
    "$defs",
    "definitions",
    "dependentSchemas",
    "patternProperties",
    "properties",
)

_POLICY_TARGETS = {
    "additional_resource_requirements_ref": PolicyKind.RESOURCE_REQUIREMENTS,
    "applicability_policy_ref": PolicyKind.APPLICABILITY,
    "conflict_policy_ref": PolicyKind.CONFLICT,
    "context_provenance_policy_ref": PolicyKind.CONTEXT_PROVENANCE,
    "coverage_policy_ref": PolicyKind.COVERAGE,
    "evidence_policy_ref": PolicyKind.EVIDENCE,
    "execution_policy_ref": PolicyKind.TOOL_EXECUTION,
    "failure_policy_ref": PolicyKind.TOOL_FAILURE,
    "fallback_policy_ref": PolicyKind.FALLBACK,
    "provider_policy_ref": PolicyKind.PROVIDER,
    "question_selection_policy_ref": PolicyKind.QUESTION_SELECTION,
    "resource_requirements_ref": PolicyKind.RESOURCE_REQUIREMENTS,
    "rule_policy_ref": PolicyKind.RULE,
    "skip_policy_ref": PolicyKind.TOOL_SKIP,
    "unknown_policy_ref": PolicyKind.UNKNOWN,
    "validation_policy_ref": PolicyKind.QUESTION_VALIDATION,
}


@dataclass(frozen=True, slots=True)
class SchemaTarget:
    artifact: ResolvedArtifact
    location: ArtifactLocation
    node: _SchemaNode


@dataclass(frozen=True, slots=True)
class _IndexedNode:
    target: SchemaTarget
    base: str
    pointer: str


def _path_pointer(location: ArtifactLocation) -> str:
    return "".join(
        "/" + str(segment).replace("~", "~0").replace("/", "~1") for segment in location
    )


def _children(
    node: dict[str, JSONValue],
) -> Iterator[tuple[ArtifactLocation, _SchemaNode]]:
    # Walk Draft keywords that contain subschemas. Keywords such as const,
    # default and examples may contain identical spellings as instance data.
    for keyword in _SCHEMA_SINGLE:
        value = node.get(keyword)
        if isinstance(value, (dict, bool)):
            yield (keyword,), value
    for keyword in _SCHEMA_ARRAY:
        value = node.get(keyword)
        if isinstance(value, list):
            for index, child in enumerate(value):
                if isinstance(child, (dict, bool)):
                    yield (keyword, index), child
    for keyword in _SCHEMA_MAP:
        value = node.get(keyword)
        if isinstance(value, dict):
            for name, child in value.items():
                if isinstance(child, (dict, bool)):
                    yield (keyword, name), child


def _schema_error(
    source: SchemaTarget,
    key: str,
    code: str,
    message: str,
) -> BundleArtifactError:
    return BundleArtifactError(
        code,
        message,
        source.artifact.inventory.path,
        (*source.artifact.document_location, *source.location, key),
    )


def _join_uri(base: str, reference: str) -> str:
    if not reference or reference.startswith("#"):
        return base + reference
    if absolute_schema_id(reference):
        return reference
    # urljoin preserves https relative paths but does not combine fragment-only
    # URN references. Absolute URIs with fragments keep their original spelling.
    uri, _ = urldefrag(reference)
    if absolute_schema_id(uri):
        return reference
    return urljoin(base, reference)


class SchemaIndex:
    """Resolve packaged schema aliases, pointers and anchors without retrieval."""

    def __init__(self, catalogue: ArtifactCatalogue) -> None:
        self._catalogue: Final = catalogue
        self._nodes: dict[_SchemaNodeKey, _IndexedNode] = {}
        self._resources: dict[str, _IndexedNode] = {}
        self._anchors: dict[tuple[str, str], _IndexedNode] = {}
        for artifact in catalogue.artifacts:
            if isinstance(artifact.value, SchemaResource):
                self._index(
                    SchemaTarget(artifact, (), artifact.value.document),
                    artifact.value.resource_id,
                )

    @classmethod
    def from_catalogue(cls, catalogue: ArtifactCatalogue) -> Self:
        return cls(catalogue)

    @property
    def catalogue(self) -> ArtifactCatalogue:
        """Return the exact catalogue whose artifacts were indexed."""
        return self._catalogue

    def _index(self, root: SchemaTarget, default_base: str) -> None:
        pending = [(root, default_base)]
        while pending:
            target, base = pending.pop()
            node = target.node
            pointer = _path_pointer(target.location)
            if isinstance(node, dict) and "$id" in node:
                declared_id = node["$id"]
                if not isinstance(declared_id, str) or not valid_uri_reference(
                    declared_id
                ):
                    raise _schema_error(
                        target, "$id", "schema_id_invalid", "invalid local schema ID"
                    )
                base = _join_uri(base, declared_id)
                if not absolute_schema_id(base):
                    raise _schema_error(
                        target,
                        "$id",
                        "schema_id_invalid",
                        "local schema ID must resolve to an absolute URI without a fragment",
                    )
                if base in self._resources:
                    raise _schema_error(
                        target,
                        "$id",
                        "schema_id_ambiguous",
                        "multiple schema resources declare the same URI",
                    )
                self._resources[base] = _IndexedNode(target, base, pointer)
            indexed = _IndexedNode(target, base, pointer)
            self._nodes[artifact_identity(target.artifact.ref), pointer] = indexed
            if not isinstance(node, dict):
                continue
            if "$schema" in node and node["$schema"] != DRAFT_2020_12:
                raise _schema_error(
                    target,
                    "$schema",
                    "schema_dialect_mismatch",
                    "embedded schema must use Draft 2020-12",
                )
            for keyword in ("$anchor", "$dynamicAnchor"):
                anchor = node.get(keyword)
                if not isinstance(anchor, str):
                    continue
                if (base, anchor) in self._anchors:
                    raise _schema_error(
                        target,
                        keyword,
                        "schema_anchor_ambiguous",
                        "multiple subschemas declare the same anchor",
                    )
                self._anchors[base, anchor] = indexed
            for location, child in _children(node):
                pending.append(
                    (
                        SchemaTarget(
                            target.artifact, target.location + location, child
                        ),
                        base,
                    )
                )

    def resolve_reference(
        self,
        source: ResolvedArtifact,
        node_location: ArtifactLocation,
        reference: str,
        *,
        keyword: str = "$ref",
    ) -> SchemaTarget:
        """Resolve against the exact referencing node and its offline resource base."""
        target = self._resolve_reference(source, node_location, reference, keyword)
        return SchemaTarget(target.artifact, target.location, deepcopy(target.node))

    def _resolve_reference(
        self,
        source: ResolvedArtifact,
        node_location: ArtifactLocation,
        reference: str,
        keyword: str,
    ) -> SchemaTarget:
        local = self._nodes.get(
            (artifact_identity(source.ref), _path_pointer(node_location))
        )
        if local is None or local.target.artifact is not source:
            raise BundleArtifactError(
                "schema_reference_source_missing",
                "reference source is not an indexed schema node",
                source.inventory.path,
                (*source.document_location, *node_location),
            )
        if not valid_uri_reference(reference):
            raise _schema_error(
                local.target,
                keyword,
                "schema_reference_invalid",
                "invalid schema URI reference",
            )
        try:
            uri, fragment = urldefrag(_join_uri(local.base, reference))
            fragment = unquote(fragment, errors="strict")
        except ValueError as error:
            raise _schema_error(
                local.target,
                keyword,
                "schema_reference_invalid",
                "invalid schema URI or URI fragment",
            ) from error
        resource = self._resources.get(uri)
        if resource is None:
            raise _schema_error(
                local.target,
                keyword,
                "schema_resource_missing",
                "reference has no packaged schema resource",
            )
        if not fragment:
            return resource.target
        if fragment.startswith("/"):
            try:
                resolve_json_pointer(
                    resource.target.node,
                    fragment,
                    path=resource.target.artifact.inventory.path,
                )
            except BundleIntegrityError as error:
                raise _schema_error(
                    local.target,
                    keyword,
                    "schema_pointer_missing",
                    "reference does not locate a schema node",
                ) from error
            target = self._nodes.get(
                (
                    artifact_identity(resource.target.artifact.ref),
                    resource.pointer + fragment,
                )
            )
        else:
            target = self._anchors.get((uri, fragment))
        if target is None:
            raise _schema_error(
                local.target,
                keyword,
                "schema_target_missing",
                "reference does not locate a declared subschema or anchor",
            )
        return target.target

    def _dependency_pairs(self) -> Iterator[tuple[SchemaTarget, ResolvedArtifact]]:
        for indexed in self._nodes.values():
            target = indexed.target
            if not isinstance(target.node, dict):
                continue
            for keyword in ("$ref", "$dynamicRef"):
                reference = target.node.get(keyword)
                if isinstance(reference, str):
                    resolved = self._resolve_reference(
                        target.artifact, target.location, reference, keyword
                    )
                    if artifact_identity(resolved.artifact.ref) != artifact_identity(
                        target.artifact.ref
                    ):
                        yield target, resolved.artifact


@dataclass(frozen=True, slots=True)
class ExternalReference:
    origin: ResolvedArtifact
    location: ArtifactLocation
    requirement: ExternalReferenceValue


@dataclass(frozen=True, slots=True)
class BundleClosure:
    schemas: SchemaIndex
    dependencies: Mapping[ArtifactIdentity, tuple[ContentRef, ...]]
    question_profiles: Mapping[ArtifactIdentity, tuple[ContentRef, ...]]
    tool_plan_profiles: Mapping[ArtifactIdentity, tuple[ContentRef, ...]]
    external_references: tuple[ExternalReference, ...]


def _walk_models(
    value: object, location: ArtifactLocation = ()
) -> Iterator[tuple[ArtifactLocation, ContractModel]]:
    if isinstance(value, ContractModel):
        yield location, value
        for field in type(value).model_fields:
            yield from _walk_models(getattr(value, field), (*location, field))
    elif isinstance(value, tuple):
        for index, child in enumerate(value):
            yield from _walk_models(child, (*location, index))


def _validate_typed_dependencies(
    catalogue: ArtifactCatalogue,
    artifact: ResolvedArtifact,
) -> tuple[dict[ArtifactIdentity, ContentRef], list[ExternalReference]]:
    found: dict[ArtifactIdentity, ContentRef] = {}
    external: list[ExternalReference] = []
    path = artifact.inventory.path
    for location, value in _walk_models(artifact.value, artifact.document_location):
        if isinstance(value, ContentRef):
            target = catalogue.resolve(value, path=path, json_location=location)
            found[artifact_identity(target.ref)] = target.ref
            if value.artifact_kind is ArtifactKind.POLICY:
                field = location[-1] if location else None
                expected = (
                    _POLICY_TARGETS.get(field) if isinstance(field, str) else None
                )
                policy_kind = getattr(
                    getattr(target.value, "policy", None), "policy_kind", None
                )
                if expected is None or policy_kind != expected:
                    raise BundleArtifactError(
                        "policy_kind_mismatch",
                        "policy subtype does not match the owner field",
                        path,
                        location,
                    )
        elif isinstance(value, IdentitySelector):
            catalogue.resolve_selector(value, path=path, json_location=location)
        elif isinstance(value, VocabularyMemberRef):
            vocabulary = catalogue.resolve(
                value.vocabulary_ref,
                path=path,
                json_location=(*location, "vocabulary_ref"),
            ).value
            if (
                not isinstance(vocabulary, VocabularyDefinition)
                or value.member not in vocabulary.members
            ):
                raise BundleArtifactError(
                    "vocabulary_member_missing",
                    "member is absent from the pinned vocabulary",
                    path,
                    (*location, "member"),
                )
        elif isinstance(
            value,
            (ExternalCapabilityRequirement, ReviewReportRef, DeploymentRequirement),
        ):
            external.append(ExternalReference(artifact, location, value))
        elif isinstance(value, RuntimeRecordRef):
            raise BundleArtifactError(
                "runtime_record_in_bundle",
                "runtime records are outside Bundle configuration",
                path,
                location,
            )
    return found, external


def _require_acyclic(
    catalogue: ArtifactCatalogue,
    edges: Mapping[ArtifactIdentity, dict[ArtifactIdentity, ContentRef]],
) -> None:
    # Iterative DFS supports a valid inventory near the manifest's maximum size.
    visited: set[ArtifactIdentity] = set()
    active: set[ArtifactIdentity] = set()
    for root in catalogue.artifacts:
        identity = artifact_identity(root.ref)
        if identity in visited:
            continue
        active.add(identity)
        stack = [(identity, iter(edges[identity].items()))]
        while stack:
            parent, children = stack[-1]
            dependency = next(children, None)
            if dependency is None:
                active.remove(parent)
                visited.add(parent)
                stack.pop()
                continue
            child, ref = dependency
            if child in active:
                target = catalogue.resolve(ref)
                raise BundleArtifactError(
                    "artifact_dependency_cycle",
                    f"dependency cycle from {parent[1]} to {child[1]}",
                    target.inventory.path,
                    target.document_location,
                )
            if child not in visited:
                active.add(child)
                stack.append((child, iter(edges[child].items())))


def validate_closure(catalogue: ArtifactCatalogue) -> BundleClosure:
    """Validate complete offline closure independently of downstream type inference."""
    schema_index = SchemaIndex.from_catalogue(catalogue)
    graph: dict[ArtifactIdentity, dict[ArtifactIdentity, ContentRef]] = {}
    questions: dict[ArtifactIdentity, list[ContentRef]] = {}
    tool_plans: dict[ArtifactIdentity, list[ContentRef]] = {}
    external: list[ExternalReference] = []
    for artifact in catalogue.artifacts:
        identity = artifact_identity(artifact.ref)
        graph[identity], external_refs = _validate_typed_dependencies(
            catalogue, artifact
        )
        external.extend(external_refs)
        if isinstance(artifact.value, ProfileDefinition):
            for question in artifact.value.question_refs:
                questions.setdefault(artifact_identity(question), []).append(
                    artifact.ref
                )
            tool_plans.setdefault(
                artifact_identity(artifact.value.tool_plan_ref), []
            ).append(artifact.ref)
    for source, target in schema_index._dependency_pairs():
        graph[artifact_identity(source.artifact.ref)][artifact_identity(target.ref)] = (
            target.ref
        )
    _require_acyclic(catalogue, graph)
    return BundleClosure(
        schema_index,
        MappingProxyType(
            {key: tuple(targets.values()) for key, targets in graph.items()}
        ),
        MappingProxyType({key: tuple(refs) for key, refs in questions.items()}),
        MappingProxyType({key: tuple(refs) for key, refs in tool_plans.items()}),
        tuple(external),
    )
