"""Project scoring inputs from validated Bundle and terminal workflow records."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from jimu_runtime.bundle.catalogue import ArtifactCatalogue
from jimu_runtime.bundle.semantic_inputs import require_definition
from jimu_runtime.domain.definition_support import reference_identity
from jimu_runtime.domain.definitions.capabilities import (
    ProfileDefinition,
    QuestionDefinition,
)
from jimu_runtime.domain.definitions.tools import ToolPlanDefinition
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_schemas.models import FactSchemaDefinition
from jimu_runtime.domain.fact_selectors import (
    FactSchemaContentRef,
    ProducerCatalogueContentRef,
)
from jimu_runtime.domain.producers import (
    FactProducerDeclaration,
    ProducerCatalogue,
    ToolStepProducer,
)
from jimu_runtime.domain.rules.definitions import RuleDefinition, RuleSetDefinition
from jimu_runtime.domain.runtime.coverage import ExecutionCoverage
from jimu_runtime.domain.runtime.decisions import (
    DecisionRecord,
    EvaluatedRuleOutcome,
    RulePath,
)
from jimu_runtime.domain.runtime.facts import (
    FactRecord,
    KnownFact,
    ProfileContentRef,
    QuestionFactSource,
    ToolFactSource,
)
from jimu_runtime.domain.runtime.media import MediaAsset
from jimu_runtime.domain.runtime.perception import ObjectRecord
from jimu_runtime.domain.runtime.tool_executions import (
    ToolExecution,
    ToolPlanStepOrigin,
)
from jimu_runtime.domain.validation.authority_inputs import EmittedOutput
from jimu_runtime.evaluation.contracts import (
    EvaluationCatalogue,
    FactBinding,
    GeometryPrediction,
    PredictedObject,
    ProfileBinding,
    ProfilePrediction,
    QuestionBinding,
    QuestionPrediction,
    RuleBinding,
    RulePrediction,
    canonical_digest,
    require_unique,
)


class EvaluationHistoryError(ValueError):
    """Stable diagnostic for an unavailable or inadmissible historical source."""

    def __init__(
        self,
        code: str,
        *,
        sample_id: str | None = None,
        execution_id: str | None = None,
    ) -> None:
        self.code = code
        self.sample_id = sample_id
        self.execution_id = execution_id
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _ToolProduction:
    catalogue_ref: ProducerCatalogueContentRef
    declaration: FactProducerDeclaration
    producer: ToolStepProducer

    def matches(
        self, fact: FactRecord, source: ToolFactSource, execution: ToolExecution | None
    ) -> bool:
        if execution is None or execution.object_ref is None:
            return False
        origin = execution.invocation_origin
        observation = execution.authority_observation
        return (
            observation is not None
            and observation.arrival_qualification == "on_time"
            and observation.envelope_validation == "trusted"
            and observation.association_validation == "trusted"
            and any(
                isinstance(row, EmittedOutput)
                and (row.output_name, row.fact_status, row.fact_ref.record_id)
                == (source.output_name, fact.status, fact.fact_id)
                for row in observation.mapped_outputs
            )
            and isinstance(origin, ToolPlanStepOrigin)
            and (
                fact.producer_catalogue_ref,
                fact.producer_declaration_id,
                fact.fact_key,
                fact.producer_id,
                fact.producer_version,
                source.output_name,
                source.tool_plan_ref,
                source.step_id,
                source.tool_ref,
            )
            == (
                self.catalogue_ref,
                self.declaration.declaration_id,
                self.declaration.fact_key,
                self.declaration.producer_id,
                self.declaration.producer_version,
                self.declaration.output_name,
                self.producer.tool_plan_ref,
                self.producer.step_id,
                self.producer.tool_ref,
            )
            and (
                execution.execution_id,
                execution.profile_ref,
                execution.object_ref.record_id,
                execution.attempt_ref.record_id,
                origin.tool_plan_ref,
                origin.step_id,
                execution.tool_ref,
            )
            == (
                fact.execution_id,
                fact.profile_ref,
                fact.object_id,
                fact.attempt_id,
                source.tool_plan_ref,
                source.step_id,
                source.tool_ref,
            )
            and fact.producer_declaration_id in origin.producer_declaration_ids
            and any(
                ref.record_id == fact.fact_id for ref in execution.generated_fact_refs
            )
        )


@dataclass(frozen=True, slots=True)
class HistoricalCatalogue:
    catalogue: EvaluationCatalogue
    digest: str
    tool_productions: Mapping[
        tuple[ProfileContentRef, FactSchemaContentRef], _ToolProduction
    ]


def _fact_binding(
    artifacts: ArtifactCatalogue, ref: FactSchemaContentRef
) -> FactBinding:
    artifact = artifacts.resolve(ref)
    definition = require_definition(artifacts, ref, FactSchemaDefinition)
    # The canonical authored value can omit defaults that validation materializes.
    raw = (
        None
        if canonical_digest(definition) == ref.digest
        else artifact.canonical_bytes.decode("utf-8")
    )
    return FactBinding(ref=ref, definition=definition, canonical_source=raw)


def _profile_binding(
    artifacts: ArtifactCatalogue,
    profile_ref: ProfileContentRef,
    definitions: tuple[tuple[ProducerCatalogueContentRef, ProducerCatalogue], ...],
    productions: dict[tuple[ProfileContentRef, FactSchemaContentRef], _ToolProduction],
) -> ProfileBinding:
    profile = require_definition(artifacts, profile_ref, ProfileDefinition)
    questions = []
    for ref in profile.question_refs:
        question = require_definition(artifacts, ref, QuestionDefinition)
        questions.append(
            QuestionBinding(
                question_ref=ref,
                fact=_fact_binding(artifacts, question.primary_fact_schema_ref),
            )
        )
    plan = require_definition(artifacts, profile.tool_plan_ref, ToolPlanDefinition)
    steps = {(row.step_id, row.tool_ref): row for row in plan.steps}
    geometry: dict[FactSchemaContentRef, FactBinding] = {}
    for catalogue_ref, catalogue in definitions:
        for fact in catalogue.declarations:
            producer = fact.producer
            if (
                (
                    fact.profile_ref is None
                    or reference_identity(fact.profile_ref)
                    != reference_identity(profile_ref)
                )
                or not isinstance(producer, ToolStepProducer)
                or producer.tool_plan_ref != profile.tool_plan_ref
            ):
                continue
            step = steps.get((producer.step_id, producer.tool_ref))
            if step is None or not any(
                (row.output_name, row.target_fact_key)
                == (fact.output_name, fact.fact_key)
                for row in step.fact_output_mappings
            ):
                raise EvaluationHistoryError("evaluation_geometry_binding_mismatch")
            if fact.fact_schema_ref in geometry:
                raise EvaluationHistoryError("evaluation_geometry_binding_ambiguous")
            geometry[fact.fact_schema_ref] = _fact_binding(
                artifacts, fact.fact_schema_ref
            )
            productions[profile_ref, fact.fact_schema_ref] = _ToolProduction(
                catalogue_ref, fact, producer
            )
    rules = []
    for set_ref in profile.rule_set_refs:
        rule_set = require_definition(artifacts, set_ref, RuleSetDefinition)
        for rule_ref in rule_set.rule_refs:
            rule = require_definition(artifacts, rule_ref, RuleDefinition)
            rules.append(
                RuleBinding(
                    path=RulePath(rule_set_ref=set_ref, rule_ref=rule_ref),
                    category=rule.violation_category,
                )
            )
    return ProfileBinding(
        profile_ref=profile_ref,
        questions=tuple(questions),
        geometry=tuple(geometry.values()),
        rules=tuple(rules),
    )


def catalogue_from_bundle(
    artifacts: ArtifactCatalogue, bundle_digest: str
) -> HistoricalCatalogue:
    """Read the complete scoring catalogue from an already verified exact Bundle."""
    producers = tuple(
        (
            ProducerCatalogueContentRef.model_validate(item.ref.model_dump()),
            require_definition(artifacts, item.ref, ProducerCatalogue),
        )
        for item in artifacts.of_kind(ArtifactKind.FACT_PRODUCER_CATALOG)
    )
    productions: dict[
        tuple[ProfileContentRef, FactSchemaContentRef], _ToolProduction
    ] = {}
    catalogue = EvaluationCatalogue(
        schema_version="1.0.0",
        bundle_digest=bundle_digest,
        profiles=tuple(
            _profile_binding(
                artifacts,
                ProfileContentRef.model_validate(item.ref.model_dump()),
                producers,
                productions,
            )
            for item in sorted(
                artifacts.of_kind(ArtifactKind.PROFILE),
                key=lambda item: item.ref.artifact_id,
            )
        ),
    )

    return HistoricalCatalogue(
        catalogue, canonical_digest(catalogue), MappingProxyType(productions)
    )


def _rules(decision: DecisionRecord | None) -> tuple[RulePrediction, ...]:
    if decision is None:
        return ()
    return (
        *(
            RulePrediction(
                path=row.rule,
                status=row.result.outcome,
                predicate=row.result.predicate.truth
                if isinstance(row.result, EvaluatedRuleOutcome)
                else None,
            )
            for row in decision.rule_evaluations
        ),
        *(
            RulePrediction(path=path, status="not_evaluated")
            for path in decision.explanation.not_evaluated_rules
        ),
    )


def _object(
    media: MediaAsset,
    obj: ObjectRecord,
    binding: ProfileBinding,
    facts: tuple[FactRecord, ...],
    decision: DecisionRecord | None,
    productions: Mapping[
        tuple[ProfileContentRef, FactSchemaContentRef], _ToolProduction
    ],
    tools: Mapping[str, ToolExecution],
) -> PredictedObject:
    if (
        obj.source_media_ref != media.media_ref
        or obj.coordinate_system.dimensions != media.dimensions
    ):
        raise EvaluationHistoryError("evaluation_object_image_mismatch")
    expected = {row.question_ref: row.fact.ref for row in binding.questions}
    questions: dict[str, QuestionPrediction] = {}
    geometry: dict[FactSchemaContentRef, GeometryPrediction] = {}
    for row in facts:
        if (row.profile_ref, row.execution_id, row.object_id) != (
            obj.profile_ref,
            obj.execution_id,
            obj.object_id,
        ):
            raise EvaluationHistoryError("evaluation_fact_owner_mismatch")
        producer = row.source
        value = row.value if isinstance(row, KnownFact) else None
        if isinstance(producer, QuestionFactSource):
            if (
                producer.question_ref not in obj.effective_question_refs
                or expected.get(producer.question_ref) != row.fact_schema_ref
            ):
                raise EvaluationHistoryError("evaluation_question_fact_mismatch")
            key = producer.question_ref.artifact_id
            if key in questions:
                raise EvaluationHistoryError("evaluation_question_fact_ambiguous")
            questions[key] = QuestionPrediction(
                question_ref=producer.question_ref, status=row.status, value=value
            )
        elif isinstance(producer, ToolFactSource):
            production = productions.get((obj.profile_ref, row.fact_schema_ref))
            if production is None or not production.matches(
                row, producer, tools.get(producer.tool_execution_ref.record_id)
            ):
                raise EvaluationHistoryError("evaluation_tool_fact_mismatch")
            if row.fact_schema_ref in geometry:
                raise EvaluationHistoryError("evaluation_geometry_fact_ambiguous")
            geometry[row.fact_schema_ref] = GeometryPrediction(
                fact_schema_ref=row.fact_schema_ref, status=row.status, value=value
            )
    return PredictedObject(
        object_id=obj.object_id,
        box=obj.bbox,
        questions=tuple(questions.values()),
        geometry=tuple(geometry.values()),
        rules=_rules(decision),
    )


def project_profiles(
    historical: HistoricalCatalogue,
    media: MediaAsset,
    coverage: ExecutionCoverage,
    objects: tuple[ObjectRecord, ...],
    facts: tuple[FactRecord, ...],
    decisions: tuple[DecisionRecord, ...],
    tool_executions: tuple[ToolExecution, ...],
) -> tuple[ProfilePrediction, ...]:
    bindings = {row.profile_ref: row for row in historical.catalogue.profiles}
    outcomes = {row.profile_ref: row for row in coverage.profiles}
    object_map = {row.object_id: row for row in objects}
    decision_map = {row.object_ref.record_id: row for row in decisions}
    covered = tuple(
        (profile.profile_ref, ref.record_id)
        for profile in coverage.profiles
        for ref in profile.object_refs
    )
    hits = set(coverage.hit_profile_refs)
    if (
        len(object_map) != len(objects)
        or any(row.execution_id != coverage.execution_id for row in objects)
        or len(set(covered)) != len(covered)
        or any(profile not in hits for profile, _ in covered)
        or {(row.profile_ref, row.object_id) for row in objects} != set(covered)
    ):
        raise EvaluationHistoryError("evaluation_coverage_object_mismatch")
    if (
        len(decision_map) != len(decisions)
        or len({row.decision_id for row in decisions}) != len(decisions)
        or any(
            row.execution_id != coverage.execution_id
            or row.execution_coverage_ref != coverage.coverage_ref
            or (obj := object_map.get(row.object_ref.record_id)) is None
            or obj.profile_ref != row.profile_ref
            for row in decisions
        )
    ):
        raise EvaluationHistoryError("evaluation_decision_object_mismatch")
    tools = {row.tool_execution_id: row for row in tool_executions}
    require_unique(
        (row.tool_execution_id for row in tool_executions), "historical ToolExecution"
    )

    grouped: dict[str, list[FactRecord]] = {}
    for fact in facts:
        if fact.object_id is None or fact.object_id not in object_map:
            raise EvaluationHistoryError("evaluation_fact_without_grounded_object")
        grouped.setdefault(fact.object_id, []).append(fact)
    result = []
    for ref in coverage.hit_profile_refs:
        profile = outcomes.get(ref)
        binding = bindings.get(ref)
        if binding is None or profile is None:
            raise EvaluationHistoryError("evaluation_coverage_profile_mismatch")
        projected = []
        for item in profile.object_refs:
            obj = object_map.get(item.record_id)
            if obj is None or obj.profile_ref != ref:
                raise EvaluationHistoryError("evaluation_coverage_object_mismatch")
            projected.append(
                _object(
                    media,
                    obj,
                    binding,
                    tuple(grouped.get(obj.object_id, ())),
                    decision_map.get(obj.object_id),
                    historical.tool_productions,
                    tools,
                )
            )
        result.append(
            ProfilePrediction(
                profile_ref=ref,
                grounding=profile.grounding_outcome,
                objects=tuple(projected),
            )
        )
    return tuple(result)
