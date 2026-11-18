"""Finite immutable Fact Schema registry; no parent authenticity or Fact admission."""

from dataclasses import dataclass
from typing import NoReturn, Self

from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.definition_support import FieldPath, reference_identity
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_schemas.models import (
    FactSchemaDefinition,
    ProjectionValidationError,
)
from jimu_runtime.domain.fact_schemas.projection import validate_child_reference
from jimu_runtime.domain.references import ContentRef
from jimu_runtime.domain.schema_types import (
    ResolvedFieldType,
    TypedValueSchemaDescriptor,
)
from jimu_runtime.domain.validation.issues import (
    CheckResult,
    IssuePath,
    ValidationIssue,
    ValidationReport,
)
from jimu_runtime.domain.validation.schema_descriptors import (
    SchemaCheckError,
    describe_field,
)
from jimu_runtime.domain.values import Value, validate_typed_value


class RegistryValidationError(ValueError):
    """A bounded diagnostic report, never a silently missing lookup result."""

    def __init__(self, report: ValidationReport) -> None:
        self.report = ValidationReport.model_validate(report)
        super().__init__(self.report.model_dump_json())


def _fail(check_id: str, code: str, message: str, path: IssuePath = ()) -> NoReturn:
    raise RegistryValidationError(
        ValidationReport(
            scope="registry_core",
            checks=(
                CheckResult(
                    check_id=check_id,
                    status="invalid",
                    issues=(
                        ValidationIssue(
                            code=code, path=path, message=message, related_refs=()
                        ),
                    ),
                ),
            ),
        )
    )


def _checked_ref(exact_ref: ContentRef) -> ContentRef:
    try:
        # Preserve copy/construct-injected fields instead of dropping them via dump.
        data = dict(exact_ref.__dict__)
        if exact_ref.__pydantic_extra__:
            if data.keys() & exact_ref.__pydantic_extra__.keys():
                raise ValueError("conflicting instance fields")
            data.update(exact_ref.__pydantic_extra__)
        checked = ContentRef.model_validate(data)
        if checked.artifact_kind is not ArtifactKind.FACT_SCHEMA:
            raise ValueError("expected fact_schema")
        return checked
    except (ValidationError, ValueError, AttributeError, TypeError):
        _fail(
            "registry.reference_shape",
            "registry.invalid_reference",
            "An exact four-field Fact Schema ContentRef is required.",
        )


@dataclass(frozen=True, slots=True)
class _Registration:
    ref: ContentRef
    definition: FactSchemaDefinition


@dataclass(frozen=True, slots=True, init=False)
class FactSchemaRegistry:
    """Builder-produced snapshot; entries and their contents are immutable."""

    _entries: tuple[_Registration, ...]

    def __init__(self) -> None:
        raise TypeError("Create a registry through FactSchemaRegistryBuilder.freeze().")

    @classmethod
    def _from_registered(cls, entries: tuple[_Registration, ...]) -> Self:
        snapshot = object.__new__(cls)
        object.__setattr__(snapshot, "_entries", entries)
        return snapshot

    def resolve(self, exact_ref: ContentRef) -> FactSchemaDefinition:
        checked = _checked_ref(exact_ref)
        identity = reference_identity(checked)
        for entry in self._entries:
            existing = reference_identity(entry.ref)
            if existing[:3] == identity[:3]:
                if existing != identity:
                    _fail(
                        "registry.reference_shape",
                        "registry.digest_mismatch",
                        "The registered identity has a different asserted parent digest.",
                    )
                return entry.definition
        _fail(
            "registry.reference_shape",
            "registry.unknown_identity",
            "The exact Fact Schema identity is not registered.",
        )

    def validate_payload(self, exact_ref: ContentRef, raw_value: object) -> Value:
        definition = self.resolve(exact_ref)
        try:
            return validate_typed_value(definition.type_definition, raw_value)
        except (ValidationError, ValueError):
            _fail(
                "registry.payload_conformance",
                "registry.invalid_payload",
                "Payload does not conform to the registered strict TypeDefinition.",
            )

    def describe_type(
        self, exact_ref: ContentRef, field_path: FieldPath = ()
    ) -> ResolvedFieldType:
        definition = self.resolve(exact_ref)
        try:
            path = TypeAdapter[FieldPath](FieldPath).validate_python(field_path)
            descriptor = TypedValueSchemaDescriptor.model_validate(
                {
                    "descriptor_kind": "typed_value",
                    "schema_ref": {
                        **definition.payload_schema_ref.model_dump(mode="json"),
                        "schema_dialect": definition.value_schema_generation.schema_dialect,
                    },
                    "type_definition": definition.type_definition,
                }
            )
            return describe_field(descriptor, path)
        except SchemaCheckError as exc:
            _fail(
                "registry.field_resolution",
                exc.issue.code,
                exc.issue.message,
                exc.issue.field_path,
            )
        except (ValidationError, ValueError):
            _fail(
                "registry.field_resolution",
                "registry.invalid_path",
                "A bounded semantic Record field path is required.",
            )


class FactSchemaRegistryBuilder:
    """Single caller-owned mutable construction phase, sealed by freeze."""

    def __init__(self) -> None:
        self._entries: list[_Registration] = []
        self._snapshot: FactSchemaRegistry | None = None

    def register(self, exact_ref: ContentRef, definition: FactSchemaDefinition) -> Self:
        if self._snapshot is not None:
            _fail(
                "registry.identity_conflict",
                "registry.frozen",
                "This builder is sealed; use a new builder for a new snapshot.",
            )
        checked_ref = _checked_ref(exact_ref)
        try:
            checked = FactSchemaDefinition.model_validate(definition)
        except (ValidationError, ValueError, TypeError):
            _fail(
                "registry.definition_contract",
                "registry.invalid_definition",
                "Fact Schema definition failed strict contract validation.",
            )
        if (checked_ref.artifact_id, checked_ref.version) != (
            checked.schema_id,
            checked.version,
        ):
            _fail(
                "registry.parent_identity_binding",
                "registry.parent_identity_mismatch",
                "Caller-provided parent ID/version must equal the definition.",
            )
        try:
            validate_child_reference(checked)
        except ProjectionValidationError as exc:
            _fail("registry.child_derivation", exc.code, exc.message)
        except (ValidationError, ValueError):
            _fail(
                "registry.child_derivation",
                "registry.invalid_child",
                "Derived child schema validation failed.",
            )
        identity = reference_identity(checked_ref)
        for entry in self._entries:
            existing = reference_identity(entry.ref)
            if existing[:3] == identity[:3]:
                if existing != identity or entry.definition != checked:
                    _fail(
                        "registry.identity_conflict",
                        "registry.registration_conflict",
                        "An identity cannot bind different digests or definitions.",
                    )
                return self
        if len(self._entries) >= 1024:
            _fail(
                "registry.identity_conflict",
                "registry.capacity",
                "Registry capacity is 1024 definitions; nothing was registered.",
            )
        # All fallible checks precede this sole mutation.
        self._entries.append(_Registration(checked_ref, checked))
        return self

    def freeze(self) -> FactSchemaRegistry:
        if self._snapshot is None:
            self._snapshot = FactSchemaRegistry._from_registered(tuple(self._entries))
        return self._snapshot
