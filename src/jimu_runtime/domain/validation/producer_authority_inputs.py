"""Finite X3 association inputs, without authority or conditional activation claims."""

from typing import Annotated, Self

from pydantic import BeforeValidator, Field, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import (
    immutable_sequence,
    reference_identity,
    require_unique_references,
)
from jimu_runtime.domain.definitions.capabilities import QuestionDefinition
from jimu_runtime.domain.definitions.tools import ToolDefinition, ToolPlanDefinition
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_selectors import ProducerCatalogueContentRef
from jimu_runtime.domain.primitives import Digest, RecordId
from jimu_runtime.domain.tool_mappings import ProfileContentRef
from jimu_runtime.domain.validation.authority import LocalAuthorityHistoryInput
from jimu_runtime.domain.validation.authority_inputs import (
    AuthorityQuestionInput,
    AuthorityToolPlanInput,
)
from jimu_runtime.domain.validation.producer_inputs import ProducerCatalogueContext
from jimu_runtime.domain.validation.tool_plans import ToolRegistration

type Directory[T] = Annotated[
    tuple[T, ...], Field(max_length=1024), BeforeValidator(immutable_sequence)
]


class ExecutionDefinitionPin(ContractModel):
    """Caller-supplied pin consistency data, not an admitted Release/Execution."""

    execution_id: RecordId
    release_id: RecordId
    bundle_digest: Digest
    producer_catalogue_ref: ProducerCatalogueContentRef
    configured_profile_refs: Annotated[
        tuple[ProfileContentRef, ...],
        Field(max_length=1024, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def unique_profiles(self) -> Self:
        require_unique_references(self.configured_profile_refs)
        return self


type _DefinitionEntry = (
    AuthorityQuestionInput | AuthorityToolPlanInput | ToolRegistration
)
type _Definition = QuestionDefinition | ToolPlanDefinition | ToolDefinition


def _consistent_definitions(entries: tuple[_DefinitionEntry, ...]) -> None:
    seen: dict[
        tuple[ArtifactKind, str, str],
        tuple[tuple[ArtifactKind, str, str, str], _Definition],
    ] = {}
    for entry in entries:
        identity = reference_identity(entry.ref)
        previous = seen.get(identity[:3])
        if previous is not None and (
            previous[0] != identity or previous[1] != entry.definition
        ):
            raise ValueError(
                "Question/Tool/Plan sources contradict the same versioned definition identity"
            )
        seen[identity[:3]] = (identity, entry.definition)


class ProducerAuthorityValidationInput(ContractModel):
    """Shape and known contradictions only; missing bindings remain checkable data.

    Missing pins/catalogues and empty-output obligations are diagnosed by the X3
    validator, not converted to default authority or rejected as malformed shape.
    """

    history: LocalAuthorityHistoryInput
    producer_catalogues: Directory[ProducerCatalogueContext]
    execution_pins: Directory[ExecutionDefinitionPin]

    @model_validator(mode="after")
    def identities_and_known_associations(self) -> Self:
        catalogue_keys = [
            reference_identity(item.registration.ref)[:3]
            for item in self.producer_catalogues
        ]
        if len(set(catalogue_keys)) != len(catalogue_keys):
            raise ValueError("Catalogue contexts require unique kind/ID/version")
        execution_ids = [pin.execution_id for pin in self.execution_pins]
        if len(set(execution_ids)) != len(execution_ids):
            raise ValueError("Execution pins require unique execution_id")
        for pin in self.execution_pins:
            identity = reference_identity(pin.producer_catalogue_ref)
            context = next(
                (
                    item
                    for item in self.producer_catalogues
                    if reference_identity(item.registration.ref)[:3] == identity[:3]
                ),
                None,
            )
            if context is not None:
                if reference_identity(context.registration.ref) != identity:
                    raise ValueError(
                        "Execution pin has a known Catalogue digest conflict"
                    )
                expected = {
                    reference_identity(item.ref) for item in context.context.profiles
                }
                supplied = {
                    reference_identity(item) for item in pin.configured_profile_refs
                }
                if supplied != expected:
                    raise ValueError(
                        "Execution configured Profile set differs from Catalogue context"
                    )
        definitions: list[_DefinitionEntry] = [
            *self.history.questions,
            *self.history.tools,
            *self.history.plans,
        ]
        for item in self.producer_catalogues:
            definitions.extend(item.context.questions)
            definitions.extend(item.context.tools)
            definitions.extend(item.context.plans)
        _consistent_definitions(tuple(definitions))
        return self
