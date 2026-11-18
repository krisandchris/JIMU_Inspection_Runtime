"""Append-only authority selection data; not a selector or persistence service."""

from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, model_validator
from pydantic.json_schema import JsonSchemaValue

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import FactScope
from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    PositiveBudget,
    omit_default_from_schema,
)
from jimu_runtime.domain.fact_selectors import (
    SystemProducerRequirement,
    ToolPlanContentRef,
)
from jimu_runtime.domain.primitives import ArtifactId, RecordId
from jimu_runtime.domain.runtime.facts import (
    AuthoritySelectionRef,
    ExecutionEventRef,
    FactRefs,
    ObservationBatchRef,
    RuntimeUTCDateTime,
    ScopedRecord,
    ToolExecutionRef,
    require_unique_runtime_refs,
    scope_constraints,
)


class ActorRef(ContractModel):
    actor_kind: Literal["service", "human"]
    actor_id: RecordId
    identity_registry_id: RecordId


def _selection_schema(schema: JsonSchemaValue) -> None:
    constraints = scope_constraints()
    constraints.append(
        {
            "if": {
                "properties": {"authority_revision": {"const": 1}},
                "required": ["authority_revision"],
            },
            "then": {"properties": {"expected_previous_selection_ref": False}},
            "else": {"required": ["expected_previous_selection_ref"]},
        }
    )
    schema["allOf"] = constraints


class _SelectionBase(ScopedRecord):
    model_config = ConfigDict(json_schema_extra=_selection_schema)
    schema_version: Literal["1.0.0"]
    selection_id: RecordId
    selected_attempt_id: RecordId
    selected_fact_refs: FactRefs
    reason: Literal["validated_success", "explicit_retry_resolution", "manual_review"]
    authority_revision: PositiveBudget
    expected_previous_selection_ref: AbsentOptional[AuthoritySelectionRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    actor_ref: ActorRef
    created_at: RuntimeUTCDateTime

    @model_validator(mode="after")
    def validate_revision_shape(self) -> Self:
        require_unique_runtime_refs(self.selected_fact_refs)
        if (
            self.authority_revision == 1
            and self.expected_previous_selection_ref is not None
        ):
            raise ValueError("first authority revision forbids a previous selection")
        if self.authority_revision > 1 and self.expected_previous_selection_ref is None:
            raise ValueError("later authority revision requires a previous selection")
        if (
            self.expected_previous_selection_ref is not None
            and self.expected_previous_selection_ref.record_id == self.selection_id
        ):
            raise ValueError("selection cannot supersede itself")
        return self


class ObservationBatchSelection(_SelectionBase):
    selection_group: Literal["observation_batch"]
    scope: Literal[FactScope.OBJECT]
    selected_result_ref: ObservationBatchRef


class ToolStepSelection(_SelectionBase):
    selection_group: Literal["tool_step"]
    scope: Literal[FactScope.OBJECT]
    tool_plan_ref: ToolPlanContentRef
    step_id: ArtifactId
    selected_result_ref: ToolExecutionRef


class SystemOutputSelection(_SelectionBase):
    selection_group: Literal["system_output"]
    requirement: SystemProducerRequirement
    output_name: ArtifactId
    selected_result_ref: ExecutionEventRef


type AuthoritativeResultSelection = Annotated[
    ObservationBatchSelection | ToolStepSelection | SystemOutputSelection,
    Field(discriminator="selection_group"),
]
