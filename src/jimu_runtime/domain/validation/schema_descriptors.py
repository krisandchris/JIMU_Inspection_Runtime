"""Pure finite schema checks, with explicit invalid/incomplete diagnostics.

No file loading, arbitrary JSON Schema import, execution, authority or graph claims.
"""

from typing import Literal, NoReturn

from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import (
    Description,
    FieldPath,
    reference_identity,
)
from jimu_runtime.domain.primitives import ArtifactId
from jimu_runtime.domain.references import SchemaRef
from jimu_runtime.domain.schema_types import (
    FieldSelector,
    RawDtoSchemaDescriptor,
    Representation,
    ResolvedFieldType,
    SchemaDescriptor,
    ToolInputRecordDescriptor,
    ToolResultSchemaDescriptor,
    TypedValueSchemaDescriptor,
)
from jimu_runtime.domain.tool_mappings import InputBinding
from jimu_runtime.domain.validation.issues import IssuePath
from jimu_runtime.domain.values import (
    EnumTypeDefinition,
    IntegerTypeDefinition,
    ListTypeDefinition,
    NumberTypeDefinition,
    RecordTypeDefinition,
    StringTypeDefinition,
    TypeDefinition,
)


class SchemaCheckIssue(ContractModel):
    """Local diagnostic only; not the project-wide validation report."""

    outcome: Literal["invalid", "incomplete", "deferred"]
    code: ArtifactId
    field_path: IssuePath
    message: Description


class SchemaCheckError(ValueError):
    def __init__(self, issue: SchemaCheckIssue) -> None:
        self.issue = issue
        super().__init__(
            f"{issue.outcome}:{issue.code}:{issue.field_path}: {issue.message}"
        )


def _fail(
    code: str,
    path: tuple[str | int, ...],
    message: str,
    outcome: Literal["invalid", "incomplete", "deferred"] = "invalid",
) -> NoReturn:
    raise SchemaCheckError(
        SchemaCheckIssue(outcome=outcome, code=code, field_path=path, message=message)
    )


def _same_ref(left: SchemaRef, right: SchemaRef) -> bool:
    return (
        reference_identity(left) == reference_identity(right)
        and left.schema_dialect == right.schema_dialect
    )


def resolve_descriptor(
    schema_ref: SchemaRef, descriptors: tuple[SchemaDescriptor, ...]
) -> SchemaDescriptor:
    """Resolve only supplied exact identities; missing is incomplete, never PASS."""
    schema_ref = SchemaRef.model_validate(schema_ref)
    if len(descriptors) > 1024:
        _fail("descriptor_limit", (), "at most 1024 descriptors")
    seen: set[tuple[object, str, str]] = set()
    match: SchemaDescriptor | None = None
    for original in descriptors:
        descriptor = TypeAdapter[SchemaDescriptor](SchemaDescriptor).validate_python(
            original
        )
        ref = descriptor.schema_ref
        key = (ref.artifact_kind, ref.artifact_id, ref.version)
        if key in seen:
            _fail("descriptor_conflict", (), "duplicate kind/id/version descriptor")
        seen.add(key)
        if key == (
            schema_ref.artifact_kind,
            schema_ref.artifact_id,
            schema_ref.version,
        ):
            if not _same_ref(schema_ref, ref):
                _fail(
                    "schema_digest_mismatch",
                    (),
                    "requested identity has a different digest",
                )
            match = descriptor
    if match is None:
        _fail(
            "descriptor_unavailable",
            (),
            "exact schema descriptor not supplied",
            "incomplete",
        )
    return match


def _walk(
    definition: TypeDefinition,
    path: tuple[str, ...],
    representation: Representation,
    prefix: tuple[str, ...] = (),
    optional: tuple[tuple[str, ...], ...] = (),
    wire_prefix: tuple[str, ...] = (),
) -> tuple[TypeDefinition, tuple[tuple[str, ...], ...], tuple[str, ...]]:
    node = definition
    wire = wire_prefix
    current = prefix
    missing = optional
    for name in path:
        current += (name,)
        if not isinstance(node, RecordTypeDefinition):
            _fail(
                "path_not_record",
                current,
                "only declared Record fields may be traversed",
            )
        field = next((item for item in node.fields if item.name == name), None)
        if field is None:
            _fail("field_unknown", current, "field is not declared")
        if not field.required:
            missing += (current,)
        wire += ("value", name) if representation == "typed_value" else (name,)
        node = field.type_definition
    return node, missing, wire


def describe_field(
    descriptor: SchemaDescriptor, field_path: FieldPath
) -> ResolvedFieldType:
    """Resolve a semantic path; Tool-result paths and wire paths are payload-relative."""
    descriptor = TypeAdapter[SchemaDescriptor](SchemaDescriptor).validate_python(
        descriptor
    )
    path = TypeAdapter[FieldPath](FieldPath).validate_python(field_path)
    optional: tuple[tuple[str, ...], ...] = ()
    wire: tuple[str, ...] = ()
    remaining = path
    prefix: tuple[str, ...] = ()
    if isinstance(descriptor, ToolInputRecordDescriptor):
        if not path:
            _fail("mixed_whole_input_forbidden", (), "select an explicit input field")
        field = next((item for item in descriptor.fields if item.name == path[0]), None)
        if field is None:
            _fail("field_unknown", path[:1], "input field is not declared")
        node = field.type_definition
        representation = field.representation
        prefix = path[:1]
        wire = prefix
        optional = () if field.required else (prefix,)
        remaining = path[1:]
    elif isinstance(descriptor, ToolResultSchemaDescriptor):
        node = descriptor.payload_type
        representation = descriptor.payload_representation
        # The envelope permits absent payload. Preserve this missing ancestor,
        # including when selecting the entire payload or a required leaf.
        optional = ((),)
    elif isinstance(descriptor, RawDtoSchemaDescriptor):
        node = descriptor.type_definition
        representation = "raw_dto"
    elif isinstance(descriptor, TypedValueSchemaDescriptor):
        node = descriptor.type_definition
        representation = "typed_value"
    node, optional, wire = _walk(
        node, remaining, representation, prefix, optional, wire
    )
    return ResolvedFieldType(
        schema_ref=descriptor.schema_ref,
        field_path=path,
        representation=representation,
        type_definition=node,
        optional_ancestor_paths=optional,
        wire_path=wire,
    )


def resolve_field(
    selector: FieldSelector, descriptors: tuple[SchemaDescriptor, ...]
) -> ResolvedFieldType:
    selector = FieldSelector.model_validate(selector)
    return describe_field(
        resolve_descriptor(selector.schema_ref, descriptors), selector.field_path
    )


def validate_descriptor(descriptor: SchemaDescriptor) -> None:
    """Check local Tool payload/output path consistency, not Tool definition closure."""
    descriptor = TypeAdapter[SchemaDescriptor](SchemaDescriptor).validate_python(
        descriptor
    )
    if isinstance(descriptor, ToolResultSchemaDescriptor):
        for output in descriptor.output_declarations:
            resolved = describe_field(descriptor, output.value_path)
            # () is the envelope-level absence, not a transport Record optional.
            if output.value_path and not any(resolved.optional_ancestor_paths):
                _fail(
                    "nonknown_payload_path_not_omittable",
                    output.value_path,
                    "named nonknown output must be omittable within payload",
                )


def tool_output_omission_path(
    descriptor: ToolResultSchemaDescriptor,
    path: tuple[str, ...],
    protected_paths: tuple[tuple[str, ...], ...],
) -> tuple[str, ...] | None:
    """Find the deepest optional subtree that can disappear independently."""
    candidates = describe_field(descriptor, path).optional_ancestor_paths
    return next(
        (
            candidate
            for candidate in reversed(candidates)
            if all(
                candidate[: len(protected)] != protected
                and protected[: len(candidate)] != candidate
                for protected in protected_paths
            )
        ),
        None,
    )


def validate_independent_tool_outputs(descriptor: ToolResultSchemaDescriptor) -> None:
    """Require new Tool contracts to represent each non-known output separately."""
    outputs = descriptor.output_declarations
    for output in outputs:
        protected = tuple(
            sibling.value_path for sibling in outputs if sibling != output
        )
        if tool_output_omission_path(descriptor, output.value_path, protected) is None:
            _fail(
                "tool_output_omission_shared",
                output.value_path,
                "non-known Tool output must be omittable without removing another declared output",
            )


def require_type_containment(
    source: TypeDefinition,
    target: TypeDefinition,
    field_path: IssuePath = (),
) -> None:
    """Prove containment; reserve eight diagnostic segments for finite type depth."""
    source = TypeAdapter[TypeDefinition](TypeDefinition).validate_python(source)
    target = TypeAdapter[TypeDefinition](TypeDefinition).validate_python(target)
    try:
        path = TypeAdapter[IssuePath](IssuePath).validate_python(field_path)
    except ValidationError:
        _fail(
            "diagnostic_prefix_invalid", (), "diagnostic prefix must be a bounded path"
        )
    if len(path) > 24:
        _fail("diagnostic_prefix_invalid", (), "prefix exceeds 24 diagnostic segments")
    _require_type_containment(source, target, path)


def _require_type_containment(
    source: TypeDefinition, target: TypeDefinition, path: IssuePath
) -> None:
    if source.kind != target.kind:
        _fail("kind_mismatch", path, "source and target kinds must be identical")
    if isinstance(source, (IntegerTypeDefinition, NumberTypeDefinition)) and isinstance(
        target, (IntegerTypeDefinition, NumberTypeDefinition)
    ):
        if target.minimum is not None and (
            source.minimum is None or source.minimum < target.minimum
        ):
            _fail("range_not_contained", path, "source minimum is wider than target")
        if target.maximum is not None and (
            source.maximum is None or source.maximum > target.maximum
        ):
            _fail("range_not_contained", path, "source maximum is wider than target")
    elif isinstance(source, StringTypeDefinition) and isinstance(
        target, StringTypeDefinition
    ):
        if (
            source.min_length < target.min_length
            or source.max_length > target.max_length
        ):
            _fail("length_not_contained", path, "source string bounds are wider")
    elif isinstance(source, EnumTypeDefinition) and isinstance(
        target, EnumTypeDefinition
    ):
        if not set(source.members).issubset(target.members):
            _fail(
                "enum_not_contained", path, "source enum contains unsupported members"
            )
    elif isinstance(source, ListTypeDefinition) and isinstance(
        target, ListTypeDefinition
    ):
        if source.min_items < target.min_items or source.max_items > target.max_items:
            _fail("length_not_contained", path, "source list bounds are wider")
        _require_type_containment(source.item_type, target.item_type, path)
    elif isinstance(source, RecordTypeDefinition) and isinstance(
        target, RecordTypeDefinition
    ):
        source_fields = {item.name: item for item in source.fields}
        target_fields = {item.name: item for item in target.fields}
        for name, source_field in source_fields.items():
            if name not in target_fields:
                _fail(
                    "extra_source_field",
                    path + (name,),
                    "closed target forbids this source field",
                )
            target_field = target_fields[name]
            if target_field.required and not source_field.required:
                _fail(
                    "optional_source_member",
                    path + (name,),
                    "whole-record mapping cannot repair missing member",
                )
            _require_type_containment(
                source_field.type_definition,
                target_field.type_definition,
                path + (name,),
            )
        for name, target_field in target_fields.items():
            if target_field.required and name not in source_fields:
                _fail(
                    "required_target_member",
                    path + (name,),
                    "required target member is absent",
                )


def require_mapping_compatibility(
    source: ResolvedFieldType, target: ResolvedFieldType, binding: InputBinding
) -> None:
    """Check explicitly resolved types; does not resolve producer authority or DAGs."""
    source = ResolvedFieldType.model_validate(source)
    target = ResolvedFieldType.model_validate(target)
    binding = InputBinding.model_validate(binding)
    if (
        not _same_ref(binding.target.schema_ref, target.schema_ref)
        or binding.target.field_path != target.field_path
    ):
        _fail(
            "target_resolution_mismatch",
            target.field_path,
            "resolved target differs from binding",
        )
    if binding.required != (not target.optional_ancestor_paths):
        _fail(
            "target_requirement_mismatch",
            target.field_path,
            "required must match effective target obligation",
        )
    if source.representation != target.representation:
        _fail(
            "representation_mismatch",
            target.field_path,
            "typed/raw conversion is not permitted",
        )
    require_type_containment(source.type_definition, target.type_definition)


def validate_target_coverage(
    descriptor: SchemaDescriptor, bindings: tuple[InputBinding, ...]
) -> None:
    """Check finite target coverage, without claiming a consumer owner/schema match."""
    descriptor = TypeAdapter[SchemaDescriptor](SchemaDescriptor).validate_python(
        descriptor
    )
    if isinstance(descriptor, ToolResultSchemaDescriptor):
        _fail("target_schema_kind", (), "ToolResult cannot be an input target")
    if len(bindings) > 64:
        _fail("binding_limit", (), "at most 64 mapped fields")
    paths: list[tuple[str, ...]] = []
    for original in bindings:
        binding = InputBinding.model_validate(original)
        if not _same_ref(binding.target.schema_ref, descriptor.schema_ref):
            _fail(
                "target_schema_mismatch",
                binding.target.field_path,
                "target SchemaRef mismatch",
            )
        resolved = describe_field(descriptor, binding.target.field_path)
        if binding.required != (not resolved.optional_ancestor_paths):
            _fail(
                "target_requirement_mismatch",
                binding.target.field_path,
                "incorrect effective required flag",
            )
        path = binding.target.field_path
        if any(
            path[: len(other)] == other or other[: len(path)] == path for other in paths
        ):
            _fail("overlapping_targets", path, "duplicate or prefix-overlapping writes")
        paths.append(path)

    def visit(node: TypeDefinition, path: tuple[str, ...], required: bool) -> None:
        if path in paths:
            return
        descendants = any(item[: len(path)] == path for item in paths)
        if not descendants:
            if required:
                _fail(
                    "required_target_unmapped", path, "required target has no binding"
                )
            return
        if isinstance(node, RecordTypeDefinition):
            for field in node.fields:
                visit(field.type_definition, path + (field.name,), field.required)
        else:
            _fail(
                "target_construction_invalid",
                path,
                "only a Record may be constructed by child writes",
            )

    if isinstance(descriptor, ToolInputRecordDescriptor):
        for field in descriptor.fields:
            visit(field.type_definition, (field.name,), field.required)
    else:
        visit(descriptor.type_definition, (), True)


def validate_target_bindings(
    descriptor: SchemaDescriptor, bindings: tuple[InputBinding, ...]
) -> None:
    """Tool-specific guard around generic coverage; sources remain unverified."""
    descriptor = TypeAdapter[SchemaDescriptor](SchemaDescriptor).validate_python(
        descriptor
    )
    if descriptor.schema_ref.artifact_kind.value != "tool_input_schema":
        _fail("target_schema_kind", (), "target must be a Tool input schema")
    validate_target_coverage(descriptor, bindings)
