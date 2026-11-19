"""Reconstruct the WP-01 finite Schema views from verified Bundle resources."""

from collections import defaultdict
from dataclasses import dataclass
from typing import NoReturn, cast

from pydantic import TypeAdapter, ValidationError

from jimu_runtime.bundle.artifacts import (
    DRAFT_2020_12,
    ArtifactIdentity,
    ArtifactLocation,
    BundleArtifactError,
    SchemaResource,
    artifact_identity,
)
from jimu_runtime.bundle.catalogue import ArtifactCatalogue, ResolvedArtifact
from jimu_runtime.bundle.closure import SchemaIndex
from jimu_runtime.bundle.schema_validation import INSUFFICIENT_VISIBILITY
from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.definitions.models import ModelBinding
from jimu_runtime.domain.definitions.tools import ToolDefinition
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_schemas.canonicalization import JSONValue, canonical_bytes
from jimu_runtime.domain.fact_schemas.models import (
    FactSchemaDefinition,
    ProjectionValidationError,
)
from jimu_runtime.domain.fact_schemas.projection import validate_child_reference
from jimu_runtime.domain.runtime.tool_executions import ToolResult
from jimu_runtime.domain.schema_types import (
    EvidenceFieldSpec,
    RawDtoSchemaDescriptor,
    RawSchemaRef,
    SchemaDescriptor,
    ToolInputFieldSpec,
    ToolInputRecordDescriptor,
    ToolInputSchemaRef,
    ToolOutputDescriptor,
    ToolOutputSchemaRef,
    ToolResultSchemaDescriptor,
    TypedSchemaRef,
    TypedValueSchemaDescriptor,
)
from jimu_runtime.domain.validation.schema_descriptors import (
    SchemaCheckError,
    validate_descriptor,
    validate_independent_tool_outputs,
)
from jimu_runtime.domain.values import (
    MAX_CONTAINER_DEPTH,
    MAX_RECORD_FIELDS,
    BooleanTypeDefinition,
    RecordTypeDefinition,
    TypeDefinition,
)

type SchemaObject = dict[str, JSONValue]
_METADATA = frozenset(
    {"$schema", "$id", "$defs", "$anchor", "title", "description", "$comment"}
)
_RAW = frozenset(
    {
        ArtifactKind.CONTEXT_SCHEMA,
        ArtifactKind.ATTRIBUTE_SCHEMA,
        ArtifactKind.RESPONSE_SCHEMA,
        ArtifactKind.TOOL_INPUT_SCHEMA,
        ArtifactKind.OBJECT_SCHEMA,
        ArtifactKind.DECISION_SCHEMA,
    }
)
_TYPED = frozenset(
    {
        ArtifactKind.ATTRIBUTE_SCHEMA,
        ArtifactKind.RESPONSE_SCHEMA,
        ArtifactKind.TOOL_INPUT_SCHEMA,
    }
)
_TAGS = {
    "bool": "boolean",
    "integer": "integer",
    "number": "number",
    "string": "string",
    "enum": "string",
    "list": "array",
    "record": "object",
}
_VISIBILITY_MARKER_KEYS = {
    "const",
    "type",
    "title",
    "description",
    "default",
    "examples",
    "$comment",
    "deprecated",
}


@dataclass(frozen=True, slots=True)
class _Node:
    artifact: ResolvedArtifact
    location: ArtifactLocation
    value: JSONValue

    def child(self, key: str | int, value: JSONValue) -> "_Node":
        return _Node(self.artifact, self.location + (key,), value)

    def reject(self, code: str, message: str) -> NoReturn:
        raise BundleArtifactError(
            code, message, self.artifact.inventory.path, self.location
        )


def _semantic_keys(node: SchemaObject) -> set[str]:
    return set(node) - _METADATA


def _same_json(left: JSONValue, right: JSONValue) -> bool:
    return canonical_bytes(left) == canonical_bytes(right)


class _Projection:
    def __init__(
        self,
        index: SchemaIndex,
        type_adapter: TypeAdapter[TypeDefinition],
        *,
        observation_output: bool = False,
    ) -> None:
        self.index = index
        self.type_adapter = type_adapter
        self.observation_output = observation_output
        self.remaining = 16_384

    def resolve(self, position: _Node) -> tuple[_Node, SchemaObject]:
        seen: set[tuple[ArtifactIdentity, ArtifactLocation]] = set()
        while True:
            self.remaining -= 1
            if self.remaining < 0:
                position.reject(
                    "schema_projection_limit",
                    "schema view expansion exceeds the supported budget",
                )
            data = position.value
            if not isinstance(data, dict):
                position.reject(
                    "schema_view_unsupported", "view requires an explicit Schema object"
                )
            if "$ref" not in data:
                return position, data
            if _semantic_keys(data) != {"$ref"} or not isinstance(data["$ref"], str):
                position.reject(
                    "schema_ref_siblings_unsupported",
                    "view cannot project a reference with assertion siblings",
                )
            key = (artifact_identity(position.artifact.ref), position.location)
            if key in seen:
                position.reject(
                    "schema_view_cycle", "cyclic reference in finite Schema view"
                )
            seen.add(key)
            target = self.index.resolve_reference(
                position.artifact, position.location, data["$ref"]
            )
            position = _Node(target.artifact, target.location, target.node)

    def typed_shape(self, position: _Node) -> bool:
        current, data = self.resolve(position)
        props = data.get("properties")
        if not isinstance(props, dict) or "kind" not in props or "value" not in props:
            return False
        _, tag = self.resolve(
            current.child("properties", props).child("kind", props["kind"])
        )
        return "const" in tag

    def record(
        self, position: _Node, data: SchemaObject
    ) -> tuple[SchemaObject, set[str]]:
        properties = data.get("properties")
        required = data.get("required", [])
        if (
            data.get("type") != "object"
            or data.get("additionalProperties") is not False
            or _semantic_keys(data)
            - {"type", "properties", "required", "additionalProperties"}
            or not isinstance(properties, dict)
            or not isinstance(required, list)
            or any(
                not isinstance(name, str) or name not in properties for name in required
            )
        ):
            position.reject(
                "schema_record_unsupported",
                "view requires a closed Record and explicit properties",
            )
        if len(properties) > MAX_RECORD_FIELDS:
            position.reject(
                "schema_record_limit", "Record exceeds the WP-01 field limit"
            )
        return properties, set(cast(list[str], required))

    def definition(
        self,
        position: _Node,
        *,
        typed: bool,
        depth: int = 0,
        observation_slot: bool = False,
    ) -> TypeDefinition:
        """Read and verify every assertion in the supported finite projection."""
        position, data = self.resolve(position)
        if observation_slot:
            known = self._visibility_known_type(position, data, depth)
            if known is not None:
                return known
        tag: str | None = None
        if typed:
            properties, required = self.record(position, data)
            if set(properties) != {"kind", "value"} or required != {"kind", "value"}:
                position.reject(
                    "schema_typed_wrapper", "typed Value requires only kind and value"
                )
            property_position = position.child("properties", properties)
            tag_position, tag_schema = self.resolve(
                property_position.child("kind", properties["kind"])
            )
            raw_tag = tag_schema.get("const")
            if (
                _semantic_keys(tag_schema) != {"const"}
                or not isinstance(raw_tag, str)
                or raw_tag not in _TAGS
            ):
                tag_position.reject(
                    "schema_typed_tag", "kind must be an exact registered Value const"
                )
            tag = raw_tag
            position, data = self.resolve(
                property_position.child("value", properties["value"])
            )
            if data.get("type") != _TAGS[tag]:
                position.reject(
                    "schema_typed_payload", "payload type disagrees with its Value tag"
                )
        definition = self._payload(position, data, tag=tag, typed=typed, depth=depth)
        try:
            return self.type_adapter.validate_python(definition)
        except ValidationError as exc:
            first = exc.errors(include_url=False)[0]
            position.reject("schema_type_invalid", first["msg"])

    def _visibility_known_type(
        self, position: _Node, data: SchemaObject, depth: int
    ) -> TypeDefinition | None:
        # The finite view describes known values. The exact wire schema validates
        # the explicit visibility alternative before any Fact is projected.
        branches = data.get("anyOf")
        if (
            _semantic_keys(data) == {"anyOf"}
            and isinstance(branches, list)
            and len(branches) == 2
        ):
            for index, branch in enumerate(branches):
                if (
                    isinstance(branch, dict)
                    and branch.get("const") == INSUFFICIENT_VISIBILITY
                    and branch.get("type", "string") == "string"
                    and branch.keys() <= _VISIBILITY_MARKER_KEYS
                ):
                    known = 1 - index
                    definition = self.definition(
                        position.child("anyOf", branches).child(known, branches[known]),
                        typed=False,
                        depth=depth,
                    )
                    if isinstance(
                        definition, (BooleanTypeDefinition, RecordTypeDefinition)
                    ):
                        return definition
                    break
        return None

    def _payload(
        self,
        position: _Node,
        data: SchemaObject,
        *,
        tag: str | None,
        typed: bool,
        depth: int,
    ) -> dict[str, object]:
        wire_type = data.get("type")
        if wire_type == "object":
            if depth >= MAX_CONTAINER_DEPTH:
                position.reject(
                    "schema_type_depth", "Record exceeds the WP-01 type depth"
                )
            properties, required = self.record(position, data)
            fields = position.child("properties", properties)

            return {
                "kind": "record",
                "fields": [
                    {
                        "name": name,
                        "required": name in required,
                        "type_definition": self.definition(
                            fields.child(name, properties[name]),
                            typed=typed,
                            depth=depth + 1,
                            observation_slot=self.observation_output
                            and depth == 0
                            and not typed,
                        ),
                    }
                    for name in sorted(properties)
                ],
            }
        if wire_type == "array":
            if depth >= MAX_CONTAINER_DEPTH:
                position.reject(
                    "schema_type_depth", "List exceeds the WP-01 type depth"
                )
            return {
                "kind": "list",
                **_bounds(
                    position,
                    data,
                    {"minItems": "min_items", "maxItems": "max_items"},
                    required={"maxItems"},
                    extra={"items"},
                ),
                "item_type": self.definition(
                    position.child("items", data.get("items")),
                    typed=typed,
                    depth=depth + 1,
                ),
            }
        if wire_type == "string":
            if tag == "enum" or (tag is None and "enum" in data):
                return {
                    "kind": "enum",
                    **_bounds(position, data, {"enum": "members"}, required={"enum"}),
                }
            return {
                "kind": "string",
                **_bounds(
                    position,
                    data,
                    {"minLength": "min_length", "maxLength": "max_length"},
                    required={"maxLength"},
                ),
            }
        if wire_type in ("integer", "number"):
            return {
                "kind": wire_type,
                **_bounds(position, data, {"minimum": "minimum", "maximum": "maximum"}),
            }
        if wire_type == "boolean":
            return {"kind": "bool", **_bounds(position, data, {})}
        position.reject(
            "schema_type_unsupported", "no finite WP-01 view for this Schema type"
        )


def _bounds(
    position: _Node,
    data: SchemaObject,
    keywords: dict[str, str],
    *,
    required: set[str] | None = None,
    extra: set[str] | None = None,
) -> dict[str, JSONValue]:
    missing = (required or set()) - data.keys()
    if missing:
        keyword = min(missing)
        position.reject(
            "schema_explicit_bound_missing", f"projection requires {keyword}"
        )
    unsupported = _semantic_keys(data) - {"type", *keywords, *(extra or ())}
    if unsupported:
        keyword = min(unsupported)
        position.child(keyword, data[keyword]).reject(
            "schema_assertion_unsupported", f"cannot preserve {keyword} in a WP-01 type"
        )
    return {
        name: data[keyword] for keyword, name in keywords.items() if keyword in data
    }


def _schema_ref(artifact: ResolvedArtifact) -> dict[str, str]:
    return {
        "artifact_kind": artifact.ref.artifact_kind.value,
        "artifact_id": artifact.ref.artifact_id,
        "version": artifact.ref.version,
        "digest": artifact.ref.digest,
        "schema_dialect": DRAFT_2020_12,
    }


def _regular_view(projector: _Projection, root: _Node) -> SchemaDescriptor:
    kind = root.artifact.ref.artifact_kind
    ref = _schema_ref(root.artifact)
    if projector.typed_shape(root):
        if kind not in _TYPED:
            root.reject(
                "schema_representation_mismatch",
                f"{kind.value} requires a raw DTO view",
            )
        return TypedValueSchemaDescriptor(
            descriptor_kind="typed_value",
            schema_ref=TypedSchemaRef.model_validate(ref),
            type_definition=projector.definition(root, typed=True),
        )

    if kind is ArtifactKind.TOOL_INPUT_SCHEMA:
        position, data = projector.resolve(root)
        if data.get("type") == "object":
            props, required = projector.record(position, data)
            props_position = position.child("properties", props)
            fields = [
                (name, props_position.child(name, props[name]))
                for name in sorted(props)
            ]
            typed = {name for name, child in fields if projector.typed_shape(child)}
            if typed:
                return ToolInputRecordDescriptor(
                    descriptor_kind="tool_input_record",
                    schema_ref=ToolInputSchemaRef.model_validate(ref),
                    fields=tuple(
                        ToolInputFieldSpec(
                            name=name,
                            required=name in required,
                            representation="typed_value"
                            if name in typed
                            else "raw_dto",
                            type_definition=projector.definition(
                                child, typed=name in typed
                            ),
                        )
                        for name, child in fields
                    ),
                )

    return RawDtoSchemaDescriptor(
        descriptor_kind="raw_dto",
        schema_ref=RawSchemaRef.model_validate(ref),
        type_definition=projector.definition(root, typed=False),
    )


def _fact_views(catalogue: ArtifactCatalogue) -> dict[ArtifactIdentity, TypeDefinition]:
    children: dict[ArtifactIdentity, TypeDefinition] = {}
    for artifact in catalogue.of_kind(ArtifactKind.FACT_SCHEMA):
        fact = artifact.value
        assert isinstance(fact, FactSchemaDefinition)
        path = artifact.inventory.path
        try:
            child = validate_child_reference(fact)
        except ProjectionValidationError as exc:
            raise BundleArtifactError(
                exc.code, str(exc), path, ("payload_schema_ref",)
            ) from exc
        actual = catalogue.resolve(
            child.child_ref, path=path, json_location=("payload_schema_ref",)
        )
        if actual.canonical_bytes != child.canonical_schema_bytes:
            raise BundleArtifactError(
                "value_schema_bytes_mismatch",
                "F04 regeneration differs from actual child bytes",
                actual.inventory.path,
            )
        children[artifact_identity(child.child_ref)] = child.generation_input.projection
    return children


def _evidence_schema(
    slots: tuple[EvidenceFieldSpec, ...], *, succeeded: bool
) -> SchemaObject:
    """Use a closed finite slot union and array-level occurrence constraints."""
    if not slots:
        return {"type": "array", "maxItems": 0}
    branches: list[JSONValue] = []
    counts: list[JSONValue] = []
    for slot in slots:
        name_schema: SchemaObject = {"const": slot.name}
        refs: SchemaObject = {"$ref": "#/$defs/EvidenceRefs", "minItems": 1}
        if not slot.multiple:
            refs["maxItems"] = 1
        branches.append(
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"name": name_schema, "evidence_refs": refs},
                "required": ["name", "evidence_refs"],
            }
        )
        counts.append(
            {
                "contains": {"properties": {"name": name_schema}},
                "minContains": int(succeeded and slot.required),
                "maxContains": 1,
            }
        )
    return {
        "type": "array",
        "maxItems": 64,
        "items": {"oneOf": branches},
        "allOf": counts,
    }


def _evidence_fields(position: _Node) -> tuple[EvidenceFieldSpec, ...]:
    data = position.value
    if isinstance(data, dict) and _same_json(
        data,
        _evidence_schema((), succeeded=True),
    ):
        return ()
    items = data.get("items") if isinstance(data, dict) else None
    branches = items.get("oneOf") if isinstance(items, dict) else None
    counts = data.get("allOf") if isinstance(data, dict) else None
    if (
        not isinstance(branches, list)
        or not isinstance(counts, list)
        or not 1 <= len(branches) == len(counts) <= 64
    ):
        position.reject(
            "tool_evidence_contract_unproven",
            "generic evidence array cannot establish named slots, requirement or multiplicity",
        )
    slots = []
    for index, (branch, count) in enumerate(zip(branches, counts, strict=True)):
        props = branch.get("properties") if isinstance(branch, dict) else None
        name = props.get("name") if isinstance(props, dict) else None
        refs = props.get("evidence_refs") if isinstance(props, dict) else None
        if (
            not isinstance(name, dict)
            or not isinstance(name.get("const"), str)
            or not isinstance(refs, dict)
            or not isinstance(count, dict)
        ):
            position.child("items", items).child("oneOf", branches).child(
                index, branch
            ).reject(
                "tool_evidence_contract_unproven",
                "each evidence slot must name one explicit constant and occurrence constraint",
            )
        slots.append(
            EvidenceFieldSpec.model_validate(
                {
                    "name": name["const"],
                    "required": count.get("minContains") == 1,
                    "multiple": "maxItems" not in refs,
                }
            )
        )
    return tuple(slots)


def _schema_names(value: JSONValue) -> set[str]:
    """The trusted ToolResult schema only uses direct local definition references."""
    names: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "$ref" and isinstance(item, str):
                prefix = "#/$defs/"
                if not item.startswith(prefix):
                    raise RuntimeError("generated ToolResult schema reference changed")
                names.add(item[len(prefix) :])
            else:
                names.update(_schema_names(item))
    elif isinstance(value, list):
        for item in value:
            names.update(_schema_names(item))
    return names


def _check_envelope_definitions(
    root: _Node,
    generated: SchemaObject,
    actual: SchemaObject,
    pending: set[str],
) -> None:
    seen: set[str] = set()
    while pending:
        name = min(pending)
        pending.remove(name)
        expected = generated[name]
        if name not in actual or not _same_json(actual[name], expected):
            root.child("$defs", actual).child(name, actual.get(name)).reject(
                "tool_result_envelope_mismatch",
                "ToolResult envelope definition differs from the WP-01 generation",
            )
        seen.add(name)
        pending.update(_schema_names(expected) - seen)


def _tool_result(
    projector: _Projection,
    root: _Node,
    declarations: tuple[ToolOutputDescriptor, ...],
) -> ToolResultSchemaDescriptor:
    trusted = cast(SchemaObject, TypeAdapter[ToolResult](ToolResult).json_schema())
    owner = root.artifact
    root, actual = projector.resolve(root)
    if not _same_json(
        {k: v for k, v in trusted.items() if k not in _METADATA},
        {k: v for k, v in actual.items() if k not in _METADATA},
    ):
        root.reject(
            "tool_result_envelope_mismatch",
            "output must use the three generated WP-01 ToolResult status branches",
        )
    source = trusted.get("$defs")
    definitions = actual.get("$defs")
    assert isinstance(source, dict)
    if not isinstance(definitions, dict):
        root.reject(
            "tool_result_envelope_mismatch", "ToolResult definitions are missing"
        )
    location = root.child("$defs", definitions)
    payloads: dict[str, TypeDefinition] = {}
    evidence: dict[str, _Node] = {}
    pending = {"EvidenceRefs"}
    for name in ("SucceededToolResult", "PartialToolResult", "FailedToolResult"):
        expected = source[name]
        branch = definitions.get(name)
        position = location.child(name, branch)
        assert isinstance(expected, dict)
        expected_properties = expected["properties"]
        assert isinstance(expected_properties, dict)
        if not isinstance(branch, dict) or not isinstance(
            branch.get("properties"), dict
        ):
            position.reject(
                "tool_result_envelope_mismatch",
                "ToolResult branch properties are missing",
            )
        props = cast(SchemaObject, branch["properties"])
        actual_position = position.child("properties", props)
        ignored = {"evidence"}
        if name != "FailedToolResult":
            ignored.add("payload")
            payloads[name] = projector.definition(
                actual_position.child("payload", props.get("payload")), typed=True
            )
        evidence[name] = actual_position.child("evidence", props.get("evidence"))
        expected_common = {
            k: v for k, v in expected_properties.items() if k not in ignored
        }
        actual_common = {k: v for k, v in props.items() if k not in ignored}
        if not _same_json(
            {**expected, "properties": expected_common},
            {**branch, "properties": actual_common},
        ):
            position.reject(
                "tool_result_envelope_mismatch",
                "ToolResult branch changes the WP-01 envelope",
            )
        pending.update(_schema_names(expected_common))
    payload = payloads["SucceededToolResult"]
    if payload != payloads["PartialToolResult"]:
        location.child(
            "PartialToolResult", definitions.get("PartialToolResult")
        ).reject(
            "tool_result_payload_conflict",
            "partial and succeeded payloads need the same finite type",
        )
    slots = _evidence_fields(evidence["SucceededToolResult"])
    for branch_name, position in evidence.items():
        data = position.value
        if not isinstance(data, dict) or not _same_json(
            data,
            _evidence_schema(slots, succeeded=branch_name == "SucceededToolResult"),
        ):
            position.reject(
                "tool_evidence_contract_mismatch",
                "slot occurrences, cardinality or required semantics differ from the projected contract",
            )
    _check_envelope_definitions(root, source, definitions, pending)
    return ToolResultSchemaDescriptor(
        descriptor_kind="tool_result",
        schema_ref=ToolOutputSchemaRef.model_validate(_schema_ref(owner)),
        payload_representation="typed_value",
        payload_type=payload,
        evidence_fields=slots,
        output_declarations=declarations,
    )


def _tool_outputs(
    catalogue: ArtifactCatalogue,
) -> dict[ArtifactIdentity, list[tuple[ResolvedArtifact, ToolDefinition]]]:
    owners: dict[ArtifactIdentity, list[tuple[ResolvedArtifact, ToolDefinition]]] = (
        defaultdict(list)
    )
    for artifact in catalogue.of_kind(ArtifactKind.TOOL):
        tool = artifact.value
        assert isinstance(tool, ToolDefinition)
        output = catalogue.resolve(
            tool.output_schema_ref,
            path=artifact.inventory.path,
            json_location=("output_schema_ref",),
        )
        owners[artifact_identity(output.ref)].append((artifact, tool))
    return owners


def _tool_view(
    projector: _Projection,
    root: _Node,
    owners: list[tuple[ResolvedArtifact, ToolDefinition]],
) -> ToolResultSchemaDescriptor:
    if not owners:
        root.reject(
            "tool_result_owner_unavailable",
            "ToolResult requires a pinned Tool output declaration source",
        )
    declarations: tuple[ToolOutputDescriptor, ...] | None = None
    for artifact, tool in owners:
        derived = tuple(
            ToolOutputDescriptor(
                output_name=item.output_name,
                value_path=item.value_path,
                required=item.required,
            )
            for item in sorted(tool.fact_outputs, key=lambda item: item.output_name)
        )
        if declarations is not None and derived != declarations:
            raise BundleArtifactError(
                "tool_result_owner_conflict",
                "Tools sharing an exact output schema disagree on declared output names, paths or required flags",
                artifact.inventory.path,
                ("fact_outputs",),
            )
        declarations = derived
    assert declarations is not None
    descriptor = _tool_result(projector, root, declarations)
    declared_slots = {slot.name for slot in descriptor.evidence_fields}
    for artifact, tool in owners:
        for index, output in enumerate(tool.fact_outputs):
            if any(
                path[0] not in declared_slots for path in output.evidence_paths or ()
            ):
                raise BundleArtifactError(
                    "tool_evidence_path_unproven",
                    "Tool output names an evidence slot absent from its pinned output schema",
                    artifact.inventory.path,
                    ("fact_outputs", index, "evidence_paths"),
                )
    return descriptor


def build_schema_descriptors(
    catalogue: ArtifactCatalogue,
    *,
    schema_index: SchemaIndex | None = None,
    require_independent_tool_outputs: bool = False,
) -> tuple[SchemaDescriptor, ...]:
    """Build finite views independently from the pinned schemas and owners."""
    if schema_index is None:
        schema_index = SchemaIndex.from_catalogue(catalogue)
    elif schema_index.catalogue is not catalogue:
        raise BundleArtifactError(
            "schema_reference_index_mismatch",
            "local Schema index belongs to a different catalogue",
            "manifest.json",
        )
    facts = _fact_views(catalogue)
    tools = _tool_outputs(catalogue)
    observation_outputs: set[ArtifactIdentity] = set()
    other_model_schemas: set[ArtifactIdentity] = set()
    for member in catalogue.of_kind(ArtifactKind.MODEL_BINDING):
        binding = member.value
        assert isinstance(binding, ModelBinding)
        other_model_schemas.add(artifact_identity(binding.input_schema_ref))
        outputs = (
            observation_outputs
            if binding.capability is ModelCapability.OBJECT_OBSERVATION
            else other_model_schemas
        )
        outputs.add(artifact_identity(binding.output_schema_ref))
    observation_outputs -= other_model_schemas
    descriptors: list[SchemaDescriptor] = []
    type_adapter = TypeAdapter[TypeDefinition](TypeDefinition)
    for artifact in catalogue.artifacts:
        resource = artifact.value
        if not isinstance(resource, SchemaResource):
            continue
        root = _Node(artifact, (), resource.document)
        kind = artifact.ref.artifact_kind
        identity = artifact_identity(artifact.ref)
        if len(descriptors) == 1024:
            root.reject(
                "schema_descriptor_limit", "WP-01 views permit at most 1024 descriptors"
            )
        projector = _Projection(
            schema_index,
            type_adapter,
            observation_output=(
                kind is ArtifactKind.RESPONSE_SCHEMA and identity in observation_outputs
            ),
        )
        try:
            if kind is ArtifactKind.VALUE_SCHEMA:
                fact_type = facts.get(identity)
                if fact_type is None:
                    root.reject(
                        "value_schema_source_unavailable",
                        "missing pinned F04 generation source",
                    )
                descriptor: SchemaDescriptor = TypedValueSchemaDescriptor(
                    descriptor_kind="typed_value",
                    schema_ref=TypedSchemaRef.model_validate(_schema_ref(artifact)),
                    type_definition=fact_type,
                )
            elif kind is ArtifactKind.TOOL_OUTPUT_SCHEMA:
                descriptor = _tool_view(projector, root, tools.get(identity, []))
            elif kind in _RAW:
                descriptor = _regular_view(projector, root)
            else:
                root.reject(
                    "schema_kind_unsupported",
                    "schema resource has no WP-01 descriptor kind",
                )
            validate_descriptor(descriptor)
            if require_independent_tool_outputs and isinstance(
                descriptor, ToolResultSchemaDescriptor
            ):
                validate_independent_tool_outputs(descriptor)
        except SchemaCheckError as exc:
            raise BundleArtifactError(
                exc.issue.code,
                exc.issue.message,
                artifact.inventory.path,
                exc.issue.field_path,
            ) from exc
        except ValidationError as exc:
            raise BundleArtifactError(
                "schema_descriptor_invalid",
                exc.errors(include_url=False)[0]["msg"],
                artifact.inventory.path,
            ) from exc
        descriptors.append(descriptor)
    return tuple(descriptors)
