"""Static mapping declarations. No value conversion, graph execution or authority lookup."""

from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import (
    ContextFieldSelector,
    ContextSchemaRef,
    immutable_sequence,
    reference_identity,
)
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_selectors import FactSelector, ToolPlanContentRef
from jimu_runtime.domain.primitives import ArtifactId
from jimu_runtime.domain.references import ContentRef
from jimu_runtime.domain.schema_types import (
    FieldSelector,
    ImageSchemaRef,
    ObjectSchemaRef,
    SchemaDescriptor,
    ToolOutputSchemaRef,
)


class ProfileContentRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.PROFILE]


class RegionContentRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.REGION]


class ObjectFieldSelector(FieldSelector):
    schema_ref: ObjectSchemaRef


class ImageFieldSelector(FieldSelector):
    schema_ref: ImageSchemaRef


class StepOutputFieldSelector(FieldSelector):
    schema_ref: ToolOutputSchemaRef


class ContextInput(ContractModel):
    source_kind: Literal["context"]
    selector: ContextFieldSelector


class ObjectInput(ContractModel):
    source_kind: Literal["object"]
    selector: ObjectFieldSelector


class ImageInput(ContractModel):
    source_kind: Literal["image"]
    selector: ImageFieldSelector


class FactInput(ContractModel):
    source_kind: Literal["fact"]
    selector: FactSelector


class StepOutputInput(ContractModel):
    source_kind: Literal["step_output"]
    step_id: ArtifactId
    selector: StepOutputFieldSelector


type InputSource = Annotated[
    ContextInput | ObjectInput | ImageInput | FactInput | StepOutputInput,
    Field(discriminator="source_kind"),
]


class InputBinding(ContractModel):
    source: InputSource
    target: FieldSelector
    required: bool
    on_missing: Literal["block", "omit"]

    @model_validator(mode="after")
    def required_blocks(self) -> Self:
        if self.required and self.on_missing != "block":
            raise ValueError("required target must block on missing input")
        return self


class SnapshotContextRegistration(ContractModel):
    source: Literal["context_snapshot"]
    schema_ref: ContextSchemaRef


class RegionContextRegistration(ContractModel):
    source: Literal["region_metadata"]
    schema_ref: ContextSchemaRef
    region_ref: RegionContentRef


class ProfileContextRegistration(ContractModel):
    source: Literal["profile_metadata"]
    schema_ref: ContextSchemaRef
    profile_ref: ProfileContentRef


type ContextTypeRegistration = Annotated[
    SnapshotContextRegistration
    | RegionContextRegistration
    | ProfileContextRegistration,
    Field(discriminator="source"),
]


class MappingTypeContext(ContractModel):
    profile_ref: ProfileContentRef
    tool_plan_ref: ToolPlanContentRef
    current_step_id: ArtifactId
    context_registrations: Annotated[
        tuple[ContextTypeRegistration, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]
    object_schema_ref: ObjectSchemaRef
    image_schema_ref: ImageSchemaRef
    descriptors: Annotated[
        tuple[SchemaDescriptor, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def unique_inputs(self) -> Self:
        owners = []
        for registration in self.context_registrations:
            if isinstance(registration, ProfileContextRegistration):
                if reference_identity(registration.profile_ref) != reference_identity(
                    self.profile_ref
                ):
                    raise ValueError("context registration belongs to another Profile")
                owner = reference_identity(registration.profile_ref)[:3]
            elif isinstance(registration, RegionContextRegistration):
                owner = reference_identity(registration.region_ref)[:3]
            else:
                owner = None
            owners.append((registration.source, owner))
        if len(owners) != len(set(owners)):
            raise ValueError("duplicate context source/owner")
        identities = [
            (
                item.schema_ref.artifact_kind,
                item.schema_ref.artifact_id,
                item.schema_ref.version,
            )
            for item in self.descriptors
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate or conflicting schema descriptor identity")
        return self
