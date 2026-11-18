"""Allowlisted Tool declarations and deterministic local DAGs, without execution."""

from collections import deque
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, field_validator, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import (
    CapabilityFamily,
    ToolCapabilityTag,
    ToolProviderKind,
    ToolType,
)
from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    DefinitionContract,
    ExternalCapabilityRequirement,
    immutable_sequence,
    omit_default_from_schema,
)
from jimu_runtime.domain.definitions.models import (
    ModelBindingContentRef,
    PolicyContentRef,
)
from jimu_runtime.domain.fact_selectors import ToolContentRef
from jimu_runtime.domain.primitives import ArtifactId, Version
from jimu_runtime.domain.schema_types import ToolInputSchemaRef, ToolOutputSchemaRef
from jimu_runtime.domain.tool_mappings import InputBinding, StepOutputInput
from jimu_runtime.domain.tool_outputs import (
    ToolFactOutputDeclaration,
    ToolFactOutputMapping,
)


class ToolImplementationRequirement(ExternalCapabilityRequirement):
    family: Literal[CapabilityFamily.TOOL_IMPLEMENTATION]

    @field_validator("family", mode="before")
    @classmethod
    def decode_family(cls, value: object) -> CapabilityFamily:
        return CapabilityFamily._decode(value)


class ToolDefinition(DefinitionContract):
    tool_id: ArtifactId
    version: Version
    tool_type: ToolType
    provider_kind: ToolProviderKind
    input_schema_ref: ToolInputSchemaRef
    output_schema_ref: ToolOutputSchemaRef
    resource_requirements_ref: PolicyContentRef
    execution_policy_ref: PolicyContentRef
    evidence_policy_ref: PolicyContentRef
    implementation_requirement: ToolImplementationRequirement
    capability_tags: Annotated[
        tuple[ToolCapabilityTag, ...],
        Field(max_length=5),
        BeforeValidator(immutable_sequence),
    ]
    fact_outputs: Annotated[
        tuple[ToolFactOutputDeclaration, ...],
        Field(max_length=64),
        BeforeValidator(immutable_sequence),
    ]
    model_binding_ref: AbsentOptional[ModelBindingContentRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda v: v is None,
        json_schema_extra=omit_default_from_schema,
    )

    @model_validator(mode="after")
    def unique_declarations(self) -> Self:
        if len(set(self.capability_tags)) != len(self.capability_tags):
            raise ValueError("duplicate capability tag")
        for values in [
            tuple(o.output_name for o in self.fact_outputs),
            tuple(o.fact_key for o in self.fact_outputs),
        ]:
            if len(set(values)) != len(values):
                raise ValueError("Tool output names and Fact keys must be unique")
        return self


class ToolStep(ContractModel):
    step_id: ArtifactId
    tool_ref: ToolContentRef
    depends_on: Annotated[
        tuple[ArtifactId, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]
    input_bindings: Annotated[
        tuple[InputBinding, ...],
        Field(max_length=64),
        BeforeValidator(immutable_sequence),
    ]
    fact_output_mappings: Annotated[
        tuple[ToolFactOutputMapping, ...],
        Field(max_length=64),
        BeforeValidator(immutable_sequence),
    ]
    requirement: Literal["required", "optional"]
    failure_policy_ref: PolicyContentRef
    skip_policy_ref: PolicyContentRef

    @model_validator(mode="after")
    def unique_edges_and_outputs(self) -> Self:
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("duplicate dependency")
        for values in [
            tuple(o.output_name for o in self.fact_output_mappings),
            tuple(o.target_fact_key for o in self.fact_output_mappings),
        ]:
            if len(set(values)) != len(values):
                raise ValueError("duplicate mapped output or Fact key")
        return self


def plan_ancestors(steps: tuple[ToolStep, ...]) -> dict[str, frozenset[str]]:
    """Bounded iterative local DAG validation; never an execution schedule."""
    indexed = {step.step_id: step for step in steps}
    if len(indexed) != len(steps):
        raise ValueError("duplicate step identity")
    following: dict[str, list[str]] = {identity: [] for identity in indexed}
    remaining: dict[str, int] = {}
    ancestors: dict[str, frozenset[str]] = {}
    for step in steps:
        if step.step_id in step.depends_on:
            raise ValueError("self dependency")
        if any(dependency not in indexed for dependency in step.depends_on):
            raise ValueError("dangling dependency")
        remaining[step.step_id] = len(step.depends_on)
        for dependency in step.depends_on:
            following[dependency].append(step.step_id)
    ready = deque(identity for identity in indexed if remaining[identity] == 0)
    while ready:
        identity = ready.popleft()
        step = indexed[identity]
        closure: set[str] = set(step.depends_on)
        for dependency in step.depends_on:
            closure.update(ancestors[dependency])
        ancestors[identity] = frozenset(closure)
        for descendant in following[identity]:
            remaining[descendant] -= 1
            if remaining[descendant] == 0:
                ready.append(descendant)
    if len(ancestors) != len(indexed):
        raise ValueError("cyclic ToolPlan")
    for step in steps:
        for binding in step.input_bindings:
            if (
                isinstance(binding.source, StepOutputInput)
                and binding.source.step_id not in ancestors[step.step_id]
            ):
                raise ValueError("step-output input must reference a strict ancestor")
    return ancestors


class ToolPlanDefinition(DefinitionContract):
    tool_plan_id: ArtifactId
    version: Version
    steps: Annotated[
        tuple[ToolStep, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def deterministic_dag(self) -> Self:
        plan_ancestors(self.steps)
        return self
