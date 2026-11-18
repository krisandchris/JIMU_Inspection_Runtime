"""Complete finite Producer/authority conformance, never runtime authority or truth."""

from collections.abc import Callable
from functools import partial
from typing import NoReturn

from pydantic import ValidationError

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import FactScope, ProducerCardinality
from jimu_runtime.domain.definition_support import reference_identity
from jimu_runtime.domain.definitions.capabilities import ProfileDefinition
from jimu_runtime.domain.definitions.models import PolicyContentRef
from jimu_runtime.domain.enums import ArtifactKind, FactStatus
from jimu_runtime.domain.fact_schemas.registry import (
    FactSchemaRegistry,
    RegistryValidationError,
)
from jimu_runtime.domain.fact_selectors import QuestionContentRef
from jimu_runtime.domain.policies import QuestionSelectionPolicyArtifact
from jimu_runtime.domain.producers import (
    FactProducerDeclaration,
    SystemProducer,
    ToolStepProducer,
)
from jimu_runtime.domain.references import ContentRef, RuntimeRecordRef
from jimu_runtime.domain.runtime.authority import (
    AuthoritativeResultSelection,
    SystemOutputSelection,
)
from jimu_runtime.domain.runtime.candidates import (
    CandidateParseLimits,
    CandidateSnapshotLimits,
)
from jimu_runtime.domain.runtime.facts import (
    FactRecord,
    QuestionFactSource,
    ScopedRecord,
    SystemFactSource,
    ToolFactSource,
)
from jimu_runtime.domain.validation.authority import (
    LocalAuthorityHistoryInput,
    validate_local_authority_history,
)
from jimu_runtime.domain.validation.authority_inputs import (
    AttemptDescriptor,
    EmittedOutput,
    NodeAttemptOwner,
    ObservationResultDescriptor,
    QuestionSelectionDescriptor,
    SystemAttemptOwner,
    SystemOutputResultDescriptor,
    ToolStepResultDescriptor,
)
from jimu_runtime.domain.validation.issues import (
    CheckResult,
    CheckStatus,
    ValidationIssue,
    ValidationReport,
)
from jimu_runtime.domain.validation.policies import resolve_policy
from jimu_runtime.domain.validation.producer_authority_inputs import (
    ProducerAuthorityValidationInput,
)
from jimu_runtime.domain.validation.producer_inputs import (
    ProducerCatalogueContext,
    ProducerValidationContext,
    SystemCapabilityRegistration,
)
from jimu_runtime.domain.validation.producers import (
    requirements_agree,
    validate_fact_producer,
    validate_producer_catalogue,
)
from jimu_runtime.domain.validation.schema_descriptors import SchemaCheckError

type _Result = (
    ObservationResultDescriptor
    | ToolStepResultDescriptor
    | SystemOutputResultDescriptor
)

# Only these unconditional LOCAL deferrals have additional finite proofs in X3.
# Failures/incomplete outcomes with these IDs are never filtered.
_REPLACED_LOCAL_DEFERRED = frozenset(
    {
        "full_authority_admission",
        "profile_release_admission",
        "system_output_declaration",
    }
)


class _Failure(ValueError):
    def __init__(
        self, code: str, message: str, status: CheckStatus = "invalid"
    ) -> None:
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def _fail(code: str, message: str, status: CheckStatus = "invalid") -> NoReturn:
    raise _Failure(code, message, status)


class _Overflow(ValueError):
    pass


class _Checks:
    def __init__(self) -> None:
        self.groups: dict[tuple[str, CheckStatus], list[ValidationIssue]] = {}
        self.known_invalid = False

    def add(
        self, name: str, status: CheckStatus, problem: ValidationIssue | None = None
    ) -> None:
        self.known_invalid |= status == "invalid"
        values = self.groups.setdefault((name, status), [])
        if problem is not None:
            if len(values) >= 64:
                raise _Overflow("Finite diagnostic budget exceeded")
            values.append(problem)
        if len(self.groups) > 120:
            raise _Overflow("Finite diagnostic check budget exceeded")

    def error(
        self, name: str, code: str, message: str, status: CheckStatus = "invalid"
    ) -> None:
        self.add(
            name,
            status,
            ValidationIssue(code=code, path=(), message=message, related_refs=()),
        )

    def merge(
        self,
        report: ValidationReport,
        origin: str,
        *,
        related: ContentRef | None = None,
        exclude_local_deferred: bool = False,
        only_fact: bool = False,
    ) -> None:
        for check in report.checks:
            if (
                exclude_local_deferred
                and check.status == "deferred"
                and check.check_id in _REPLACED_LOCAL_DEFERRED
            ):
                continue
            if only_fact and check.check_id != "producer.fact":
                continue  # The exact Catalogue report was already checked and retained.
            name = f"{origin}.{check.check_id}"
            if check.status == "passed":
                self.add(name, "passed")
            else:
                for problem in check.issues:
                    refs = problem.related_refs
                    if related is not None and not any(
                        _same(item, related) for item in refs
                    ):
                        if len(refs) >= 16:
                            raise _Overflow("Diagnostic reference budget exceeded")
                        refs = (*refs, related)
                    self.add(
                        name,
                        check.status,
                        ValidationIssue(
                            code=problem.code,
                            path=problem.path,
                            message=problem.message,
                            related_refs=refs,
                        ),
                    )

    def run(self, name: str, action: Callable[[], object]) -> bool:
        before = sum(
            len(items)
            for (_, status), items in self.groups.items()
            if status != "passed"
        )
        try:
            action()
        except _Failure as exc:
            self.error(name, exc.code, exc.message, exc.status)
        except RegistryValidationError as exc:
            for check in exc.report.checks:
                for problem in check.issues:
                    self.add(
                        name,
                        "incomplete"
                        if problem.code == "registry.unknown_identity"
                        else check.status,
                        problem,
                    )
        except SchemaCheckError as exc:
            self.add(
                name,
                exc.issue.outcome,
                ValidationIssue(
                    code=exc.issue.code,
                    path=exc.issue.field_path,
                    message=exc.issue.message,
                    related_refs=(),
                ),
            )
        except _Overflow:
            raise
        except (ValidationError, ValueError, TypeError):
            self.error(
                name,
                "x3.invalid_contract",
                "A finite supplied contract failed revalidation.",
            )
        else:
            after = sum(
                len(items)
                for (_, status), items in self.groups.items()
                if status != "passed"
            )
            if after != before:
                return False
            self.add(name, "passed")
            return True
        return False

    def future(self) -> None:
        for name, message in (
            (
                "activation_truth",
                "Conditions are retained as data; dynamic activation and scheduling availability were not evaluated.",
            ),
            (
                "typechecker",
                "Expression operand/Boolean/nominal type semantics require later TypeChecker validation.",
            ),
            (
                "release_admission",
                "Injected Release/Bundle pins are not authenticated admission or signature evidence.",
            ),
            (
                "runtime_trust",
                "Actual Actor/Invocation/evidence/host trust and outside-history completeness are not established.",
            ),
        ):
            self.error("future", f"x3.deferred.{name}", message, "deferred")

    def report(self, *, overflow: bool = False) -> ValidationReport:
        if overflow:
            self.groups = {key: value for key, value in list(self.groups.items())[:116]}
            # Reserve room, retaining the original bounded prefix and known invalidity.
            self.groups[
                ("diagnostics", "invalid" if self.known_invalid else "incomplete")
            ] = [
                ValidationIssue(
                    code="x3.diagnostic_overflow",
                    path=(),
                    related_refs=(),
                    message="Diagnostic enumeration exceeded its budget; retained prefix is incomplete.",
                )
            ]
        self.future()
        return ValidationReport(
            scope="authority_admission",
            checks=tuple(
                CheckResult(
                    check_id=f"x3.{name}.{status}",
                    status=status,
                    issues=tuple(problems),
                )
                for (name, status), problems in self.groups.items()
            ),
        )


def _same(left: ContentRef, right: ContentRef) -> bool:
    return reference_identity(left) == reference_identity(right)


def _scope(record: ScopedRecord) -> tuple[object, ...]:
    return (
        record.execution_id,
        record.scope,
        record.object_id,
        None if record.profile_ref is None else reference_identity(record.profile_ref),
    )


def _merge[T: ContractModel](
    entries: tuple[T, ...],
    ref: Callable[[T], ContentRef],
    body: Callable[[T], ContractModel],
) -> tuple[T, ...]:
    indexed: dict[tuple[ArtifactKind, str, str], T] = {}
    for entry in entries:
        identity = reference_identity(ref(entry))
        previous = indexed.get(identity[:3])
        if previous is not None and (
            not _same(ref(previous), ref(entry)) or body(previous) != body(entry)
        ):
            _fail(
                "x3.directory_conflict",
                "Finite sources contradict a versioned definition, including unused content.",
            )
        indexed[identity[:3]] = entry
    if len(indexed) > 1024:
        _fail(
            "x3.directory_budget",
            "Merged finite view exceeds1024; no truncated lookup is used.",
            "incomplete",
        )
    return tuple(
        indexed[key] for key in sorted(indexed, key=lambda k: (k[0].value, k[1], k[2]))
    )


def _definitions(
    inputs: ProducerAuthorityValidationInput,
) -> LocalAuthorityHistoryInput:
    questions = (
        *inputs.history.questions,
        *(
            entry
            for catalogue in inputs.producer_catalogues
            for entry in catalogue.context.questions
        ),
    )
    tools = (
        *inputs.history.tools,
        *(
            entry
            for catalogue in inputs.producer_catalogues
            for entry in catalogue.context.tools
        ),
    )
    plans = (
        *inputs.history.plans,
        *(
            entry
            for catalogue in inputs.producer_catalogues
            for entry in catalogue.context.plans
        ),
    )
    return LocalAuthorityHistoryInput.model_validate(
        inputs.history.model_copy(
            update={
                "questions": _merge(questions, lambda x: x.ref, lambda x: x.definition),
                "tools": _merge(tools, lambda x: x.ref, lambda x: x.definition),
                "plans": _merge(plans, lambda x: x.ref, lambda x: x.definition),
            }
        )
    )


def _context(
    catalogue: ProducerCatalogueContext, history: LocalAuthorityHistoryInput
) -> ProducerCatalogueContext:
    context = ProducerValidationContext.model_validate(
        catalogue.context.model_copy(
            update={
                "questions": _merge(
                    (*catalogue.context.questions, *history.questions),
                    lambda x: x.ref,
                    lambda x: x.definition,
                ),
                "tools": _merge(
                    (*catalogue.context.tools, *history.tools),
                    lambda x: x.ref,
                    lambda x: x.definition,
                ),
                "plans": _merge(
                    (*catalogue.context.plans, *history.plans),
                    lambda x: x.ref,
                    lambda x: x.definition,
                ),
            }
        )
    )
    return ProducerCatalogueContext(
        registration=catalogue.registration, context=context
    )


def _result_ref(fact: FactRecord) -> RuntimeRecordRef:
    source = fact.source
    if isinstance(source, QuestionFactSource):
        return source.observation_batch_ref
    if isinstance(source, ToolFactSource):
        return source.tool_execution_ref
    return source.system_output_ref


class _Validation:
    def __init__(
        self,
        inputs: ProducerAuthorityValidationInput,
        history: LocalAuthorityHistoryInput,
        catalogues: tuple[ProducerCatalogueContext, ...],
        registry: FactSchemaRegistry,
        checks: _Checks,
    ) -> None:
        self.inputs, self.history, self.catalogues = inputs, history, catalogues
        self.registry, self.checks = registry, checks

    def catalogue(self, execution_id: str) -> ProducerCatalogueContext:
        pin = next(
            (
                item
                for item in self.inputs.execution_pins
                if item.execution_id == execution_id
            ),
            None,
        )
        if pin is None:
            _fail(
                "x3.execution_pin_missing",
                "Execution pin is required, including for zero-Fact Results.",
                "incomplete",
            )
        for catalogue in self.catalogues:
            if (
                reference_identity(catalogue.registration.ref)[:3]
                == reference_identity(pin.producer_catalogue_ref)[:3]
            ):
                if not _same(catalogue.registration.ref, pin.producer_catalogue_ref):
                    _fail(
                        "x3.catalogue_digest",
                        "Execution Catalogue digest contradicts known identity.",
                    )
                return catalogue
        _fail(
            "x3.catalogue_missing",
            "Exact pinned Catalogue/context is absent.",
            "incomplete",
        )

    def profile(
        self, catalogue: ProducerCatalogueContext, ref: ContentRef
    ) -> ProfileDefinition:
        for entry in catalogue.context.profiles:
            if reference_identity(entry.ref)[:3] == reference_identity(ref)[:3]:
                if not _same(entry.ref, ref):
                    _fail(
                        "x3.profile_digest",
                        "Profile digest contradicts its Catalogue context.",
                    )
                return entry.definition
        _fail(
            "x3.profile_missing",
            "Scoped Profile is not configured in the pinned Catalogue.",
        )

    def result(self, execution: str, ref: RuntimeRecordRef) -> _Result:
        results: tuple[_Result, ...] = (
            *self.history.observation_results,
            *self.history.tool_results,
            *self.history.system_results,
        )
        for item in results:
            if item.execution_id == execution and item.result_ref == ref:
                return item
        _fail(
            "x3.result_missing",
            "Actual candidate source Result is absent in its Execution.",
            "incomplete",
        )

    def attempt(self, execution: str, attempt_id: str) -> AttemptDescriptor:
        attempt = next(
            (
                item
                for item in self.history.attempts
                if item.execution_id == execution
                and item.attempt_ref.record_id == attempt_id
            ),
            None,
        )
        if attempt is None:
            _fail(
                "x3.attempt_missing",
                "Candidate/Result needs an actual same-Execution Attempt.",
                "incomplete",
            )
        return attempt

    def fact(self, fact: FactRecord) -> None:
        catalogue = self.catalogue(fact.execution_id)
        if not _same(fact.producer_catalogue_ref, catalogue.registration.ref):
            _fail(
                "x3.fact_catalogue_pin",
                "Fact Catalogue does not equal the Execution's pinned Catalogue.",
            )
        self.checks.merge(
            validate_fact_producer(
                fact,
                catalogue.registration,
                catalogue.context,
                fact_schemas=self.registry,
            ),
            "fact",
            related=catalogue.registration.ref,
            only_fact=True,
        )
        attempt = self.attempt(fact.execution_id, fact.attempt_id)
        if fact.scope is FactScope.OBJECT:
            if fact.profile_ref is None:
                _fail("x3.profile_missing", "Object Fact requires exact Profile.")
            self.profile(catalogue, fact.profile_ref)
            obj = next(
                (
                    item
                    for item in self.history.objects
                    if item.execution_id == fact.execution_id
                    and item.object_ref.record_id == fact.object_id
                ),
                None,
            )
            if obj is None:
                _fail(
                    "x3.object_missing",
                    "Candidate Object is absent from its Execution.",
                    "incomplete",
                )
            if not _same(obj.profile_ref, fact.profile_ref):
                _fail(
                    "x3.object_profile",
                    "Candidate Object has a contradictory exact Profile.",
                )
        source = fact.source
        if isinstance(source, SystemFactSource):
            if not isinstance(
                attempt.owner, SystemAttemptOwner
            ) or not requirements_agree(attempt.owner.requirement, source.requirement):
                _fail(
                    "x3.system_attempt",
                    "System candidate Attempt owner contradicts its source requirements.",
                )
        else:
            if not isinstance(attempt.owner, NodeAttemptOwner):
                _fail(
                    "x3.node_attempt",
                    "Question/Tool candidate requires a node-owned Attempt.",
                )
            node = next(
                (
                    item
                    for item in self.history.node_runs
                    if item.execution_id == fact.execution_id
                    and item.node_run_ref == attempt.owner.node_run_ref
                ),
                None,
            )
            if node is None:
                _fail(
                    "x3.node_missing",
                    "Candidate's actual owning NodeRun is absent.",
                    "incomplete",
                )
            if node.node_id != (
                "N04" if isinstance(source, QuestionFactSource) else "N05"
            ):
                _fail(
                    "x3.wrong_node",
                    "Candidate producer belongs to the wrong business node.",
                )
        result = self.result(fact.execution_id, _result_ref(fact))
        if (
            _scope(result) != _scope(fact)
            or result.attempt_ref.record_id != fact.attempt_id
        ):
            _fail(
                "x3.fact_result_scope",
                "Candidate source Result has another scope/Attempt.",
            )
        if isinstance(source, QuestionFactSource):
            if (
                not isinstance(result, ObservationResultDescriptor)
                or result.model_invocation_ref != source.model_invocation_ref
                or not any(
                    _same(item.question_ref, source.question_ref)
                    and item.fact_ref.record_id == fact.fact_id
                    for item in result.question_results
                )
            ):
                _fail(
                    "x3.question_result_source",
                    "Question candidate lacks its exact Result/Question/invocation membership.",
                )
        elif isinstance(source, ToolFactSource):
            if (
                not isinstance(result, ToolStepResultDescriptor)
                or not _same(result.tool_plan_ref, source.tool_plan_ref)
                or result.step_id != source.step_id
                or not _same(result.tool_ref, source.tool_ref)
                or not any(
                    isinstance(item, EmittedOutput)
                    and item.output_name == source.output_name
                    and item.fact_ref.record_id == fact.fact_id
                    and item.fact_status is fact.status
                    for item in result.mapped_outputs
                )
            ):
                _fail(
                    "x3.tool_result_source",
                    "Tool candidate source/output/status contradicts its actual Result.",
                )
        elif (
            not isinstance(result, SystemOutputResultDescriptor)
            or not requirements_agree(result.requirement, source.requirement)
            or result.output_name != source.output_name
            or not isinstance(result.output, EmittedOutput)
            or result.output.fact_ref.record_id != fact.fact_id
            or result.output.fact_status is not fact.status
        ):
            _fail(
                "x3.system_result_source",
                "System candidate source/output/status contradicts its actual Result.",
            )

    def question_set(self, anchor: QuestionSelectionDescriptor) -> None:
        catalogue = self.catalogue(anchor.execution_id)
        profile = self.profile(catalogue, anchor.profile_ref)
        artifact = resolve_policy(
            PolicyContentRef.model_validate(
                profile.question_selection_policy_ref.model_dump(mode="json")
            ),
            catalogue.context.policies,
        )
        if not isinstance(artifact, QuestionSelectionPolicyArtifact):
            _fail(
                "x3.selection_policy_subtype",
                "Profile Question selection needs the actual matching Policy subtype.",
            )
        policy = artifact.policy
        declared = tuple(reference_identity(ref) for ref in profile.question_refs)
        effective = tuple(
            reference_identity(ref) for ref in anchor.effective_question_refs
        )
        mandatory = {reference_identity(ref) for ref in policy.mandatory_question_refs}
        optional = {reference_identity(ref) for ref in policy.optional_question_refs}
        if mandatory & optional or mandatory | optional != set(declared):
            _fail(
                "x3.question_partition",
                "Mandatory/optional Policy partition does not equal exact Profile catalogue.",
            )
        if (
            not set(effective) <= set(declared)
            or not mandatory <= set(effective)
            or tuple(ref for ref in declared if ref in set(effective)) != effective
        ):
            _fail(
                "x3.effective_questions",
                "Effective Questions must be an ordered legal subset including mandatory Questions.",
            )
        if (
            policy.mode == "all_declared" or policy.optional_source == "all"
        ) and effective != declared:
            _fail(
                "x3.all_questions",
                "all_declared requires the entire Profile Question sequence.",
            )

    def system_slot(
        self, result: SystemOutputResultDescriptor, catalogue: ProducerCatalogueContext
    ) -> FactProducerDeclaration:
        matches = [
            item
            for item in catalogue.registration.definition.declarations
            if isinstance(item.producer, SystemProducer)
            and item.scope is result.scope
            and (
                item.profile_ref is None
                and result.profile_ref is None
                or item.profile_ref is not None
                and result.profile_ref is not None
                and _same(item.profile_ref, result.profile_ref)
            )
            and item.producer.requirement.family is result.requirement.family
            and item.producer.requirement.capability_id
            == result.requirement.capability_id
            and item.producer.requirement.contract_version
            == result.requirement.contract_version
            and item.output_name == result.output_name
        ]
        if len(matches) != 1:
            _fail(
                "x3.system_slot",
                "System output must resolve exactly one declared scope/producer/output slot.",
            )
        return matches[0]

    def reciprocal_outputs(self, result: _Result) -> None:
        outputs: tuple[
            tuple[str, QuestionContentRef | None, str | None, FactStatus | None], ...
        ]
        if isinstance(result, ObservationResultDescriptor):
            outputs = tuple(
                (item.fact_ref.record_id, item.question_ref, None, None)
                for item in result.question_results
            )
        elif isinstance(result, ToolStepResultDescriptor):
            outputs = tuple(
                (item.fact_ref.record_id, None, item.output_name, item.fact_status)
                for item in result.mapped_outputs
                if isinstance(item, EmittedOutput)
            )
        elif isinstance(result.output, EmittedOutput):
            outputs = (
                (
                    result.output.fact_ref.record_id,
                    None,
                    result.output_name,
                    result.output.fact_status,
                ),
            )
        else:
            outputs = ()
        for fact_id, question_ref, output_name, status in outputs:
            fact = next(
                (
                    item.fact
                    for item in self.history.candidates.entries
                    if item.fact.fact_id == fact_id
                ),
                None,
            )
            if fact is None:
                _fail(
                    "x3.emitted_candidate_missing",
                    "An emitted Result reference has no supplied candidate.",
                    "incomplete",
                )
            if (
                fact.execution_id != result.execution_id
                or _scope(fact) != _scope(result)
                or fact.attempt_id != result.attempt_ref.record_id
                or _result_ref(fact) != result.result_ref
            ):
                _fail(
                    "x3.emitted_candidate_owner",
                    "Emitted candidate belongs to another Execution/scope/Attempt/Result.",
                )
            if question_ref is not None:
                if not isinstance(fact.source, QuestionFactSource) or not _same(
                    fact.source.question_ref, question_ref
                ):
                    _fail(
                        "x3.emitted_question",
                        "Emitted Question reference contradicts candidate source.",
                    )
            elif (
                not isinstance(fact.source, (ToolFactSource, SystemFactSource))
                or fact.source.output_name != output_name
                or fact.status is not status
            ):
                _fail(
                    "x3.emitted_output",
                    "Emitted output name/status contradicts candidate source.",
                )

    def result_contract(self, result: _Result) -> None:
        catalogue = self.catalogue(result.execution_id)
        attempt = self.attempt(result.execution_id, result.attempt_ref.record_id)
        if isinstance(result, (ObservationResultDescriptor, ToolStepResultDescriptor)):
            if not isinstance(attempt.owner, NodeAttemptOwner):
                _fail(
                    "x3.result_attempt_owner",
                    "Node Result requires its actual node-owned Attempt.",
                )
            node = next(
                (
                    item
                    for item in self.history.node_runs
                    if item.execution_id == result.execution_id
                    and item.node_run_ref == attempt.owner.node_run_ref
                ),
                None,
            )
            if node is None:
                _fail(
                    "x3.result_node_missing",
                    "Result owning NodeRun is absent.",
                    "incomplete",
                )
            if node.node_id != (
                "N04" if isinstance(result, ObservationResultDescriptor) else "N05"
            ):
                _fail(
                    "x3.result_node", "Result has a contradictory business node owner."
                )
        if result.scope is FactScope.OBJECT:
            obj = next(
                (
                    item
                    for item in self.history.objects
                    if item.execution_id == result.execution_id
                    and item.object_ref.record_id == result.object_id
                ),
                None,
            )
            if obj is None:
                _fail(
                    "x3.result_object_missing",
                    "Object-scoped Result requires its actual scoped Object.",
                    "incomplete",
                )
            if result.profile_ref is None or not _same(
                obj.profile_ref, result.profile_ref
            ):
                _fail(
                    "x3.result_object_profile",
                    "Result and Object have contradictory exact Profile owners.",
                )
        if result.profile_ref is not None:
            profile = self.profile(catalogue, result.profile_ref)
        else:
            profile = None
        if isinstance(result, ObservationResultDescriptor):
            anchor = next(
                (
                    item
                    for item in self.history.question_selections
                    if item.execution_id == result.execution_id
                    and item.question_selection_ref == result.question_selection_ref
                ),
                None,
            )
            if anchor is None:
                _fail(
                    "x3.result_question_anchor_missing",
                    "N04 Result requires an actual same-Execution Question selection anchor.",
                    "incomplete",
                )
            if (
                result.profile_ref is None
                or not _same(anchor.profile_ref, result.profile_ref)
                or anchor.effective_question_refs != result.effective_question_refs
            ):
                _fail(
                    "x3.result_question_anchor",
                    "N04 Result contradicts its Profile/effective Question anchor.",
                )
            if not {
                reference_identity(item.question_ref)
                for item in result.question_results
            } <= {reference_identity(item) for item in result.effective_question_refs}:
                _fail(
                    "x3.result_question_extra",
                    "Even an unselected Result cannot associate an output with a foreign Question.",
                )
        elif isinstance(result, ToolStepResultDescriptor):
            if profile is None or not _same(
                profile.tool_plan_ref, result.tool_plan_ref
            ):
                _fail(
                    "x3.result_plan",
                    "Tool Result must use the actual Profile's exact ToolPlan.",
                )
            plan = next(
                (
                    item.definition
                    for item in catalogue.context.plans
                    if _same(item.ref, result.tool_plan_ref)
                ),
                None,
            )
            if plan is None:
                _fail(
                    "x3.result_plan_missing",
                    "Actual Result Plan is absent from finite definitions.",
                    "incomplete",
                )
            step = next(
                (item for item in plan.steps if item.step_id == result.step_id), None
            )
            if step is None or not _same(step.tool_ref, result.tool_ref):
                _fail(
                    "x3.result_step_tool",
                    "Result step/Tool does not match its actual pinned Plan.",
                )
            if not {item.output_name for item in result.mapped_outputs} <= {
                item.output_name for item in step.fact_output_mappings
            }:
                _fail(
                    "x3.result_output_extra",
                    "Result contains an output that was not mapped by its actual step.",
                )
            tool = next(
                (
                    item.definition
                    for item in catalogue.context.tools
                    if _same(item.ref, result.tool_ref)
                ),
                None,
            )
            if tool is None:
                _fail(
                    "x3.result_tool_missing",
                    "Actual Result Tool is absent from complete finite definitions.",
                    "incomplete",
                )
            if (
                result.actual_implementation_version
                not in tool.implementation_requirement.allowed_implementation_versions
            ):
                _fail(
                    "x3.tool_actual_version",
                    "Actual Tool version does not satisfy its configured implementation requirement.",
                )
        elif isinstance(result, SystemOutputResultDescriptor):
            declaration = self.system_slot(result, catalogue)
            producer = declaration.producer
            if not isinstance(producer, SystemProducer) or not requirements_agree(
                producer.requirement, result.requirement
            ):
                _fail(
                    "x3.system_requirement",
                    "System Result constraints differ from the exact declaration.",
                )
            host = next(
                (
                    item
                    for item in catalogue.context.system_capabilities
                    if item.family is result.requirement.family
                    and item.capability_id == result.requirement.capability_id
                    and item.contract_version == result.requirement.contract_version
                ),
                None,
            )
            if host is None:
                _fail(
                    "x3.system_host_missing",
                    "Actual system implementation needs a supplied host registration.",
                    "incomplete",
                )
            if (
                result.actual_implementation_version
                not in producer.requirement.allowed_implementation_versions
                or result.actual_implementation_version
                not in host.available_implementation_versions
            ):
                _fail(
                    "x3.system_actual_version",
                    "Actual System version fails declared or supplied host constraints.",
                )
            attempt = self.attempt(result.execution_id, result.attempt_ref.record_id)
            if not isinstance(
                attempt.owner, SystemAttemptOwner
            ) or not requirements_agree(
                attempt.owner.requirement, producer.requirement
            ):
                _fail(
                    "x3.system_attempt_requirement",
                    "System Attempt requirement differs from the declaration.",
                )
            if (
                not isinstance(result.output, EmittedOutput)
                and declaration.cardinality is not ProducerCardinality.ZERO_OR_ONE
            ):
                _fail(
                    "x3.required_system_absence",
                    "exactly_one System output cannot use optional_absence.",
                )
            if isinstance(result.output, EmittedOutput):
                fact = next(
                    (
                        entry.fact
                        for entry in self.history.candidates.entries
                        if entry.fact.fact_id == result.output.fact_ref.record_id
                    ),
                    None,
                )
                if fact is None:
                    _fail(
                        "x3.system_candidate_missing",
                        "System emitted candidate is absent from supplied finite snapshot.",
                        "incomplete",
                    )
                if fact.producer_declaration_id != declaration.declaration_id:
                    _fail(
                        "x3.system_candidate_declaration",
                        "System output candidate uses a different declaration ID.",
                    )

    def selected_outputs(self, selection: AuthoritativeResultSelection) -> None:
        catalogue = self.catalogue(selection.execution_id)
        result = self.result(selection.execution_id, selection.selected_result_ref)
        if isinstance(selection, SystemOutputSelection):
            if not isinstance(result, SystemOutputResultDescriptor):
                _fail(
                    "x3.system_selection_result",
                    "System selection has the wrong actual Result kind.",
                )
            declaration = self.system_slot(result, catalogue)
            if not isinstance(
                declaration.producer, SystemProducer
            ) or not requirements_agree(
                selection.requirement, declaration.producer.requirement
            ):
                _fail(
                    "x3.system_selection_requirement",
                    "System selection constraints differ from its unique declaration.",
                )
        if isinstance(result, ObservationResultDescriptor):
            expected = {
                reference_identity(item) for item in result.effective_question_refs
            }
            actual = {
                reference_identity(item.question_ref)
                for item in result.question_results
            }
            if actual != expected:
                _fail(
                    "x3.complete_question_batch",
                    "Selected whole batch must include every effective Question, regardless of activation data.",
                )
        elif isinstance(result, ToolStepResultDescriptor):
            plan = next(
                (
                    item.definition
                    for item in catalogue.context.plans
                    if _same(item.ref, result.tool_plan_ref)
                ),
                None,
            )
            step = (
                None
                if plan is None
                else next(
                    (item for item in plan.steps if item.step_id == result.step_id),
                    None,
                )
            )
            if step is None:
                _fail(
                    "x3.result_step_missing",
                    "Selected actual ToolPlan step is absent.",
                    "incomplete",
                )
            if {item.output_name for item in result.mapped_outputs} != {
                item.output_name for item in step.fact_output_mappings
            }:
                _fail(
                    "x3.complete_tool_outputs",
                    "Selected step must account for all actual mappings; conditions never remove slots.",
                )
            for output in result.mapped_outputs:
                declarations = [
                    item
                    for item in catalogue.registration.definition.declarations
                    if isinstance(item.producer, ToolStepProducer)
                    and item.profile_ref is not None
                    and result.profile_ref is not None
                    and _same(item.profile_ref, result.profile_ref)
                    and _same(item.producer.tool_plan_ref, result.tool_plan_ref)
                    and item.producer.step_id == result.step_id
                    and _same(item.producer.tool_ref, result.tool_ref)
                    and item.output_name == output.output_name
                ]
                if len(declarations) != 1:
                    _fail(
                        "x3.tool_slot",
                        "Mapped Tool output must resolve one exact declared slot.",
                    )
                if (
                    not isinstance(output, EmittedOutput)
                    and declarations[0].cardinality
                    is not ProducerCardinality.ZERO_OR_ONE
                ):
                    _fail(
                        "x3.required_tool_absence",
                        "Required Tool declaration cannot disappear behind a condition.",
                    )


def _preflight_contexts(inputs: ProducerAuthorityValidationInput) -> None:
    profiles = tuple(
        item
        for catalogue in inputs.producer_catalogues
        for item in catalogue.context.profiles
    )
    policies = tuple(
        item
        for catalogue in inputs.producer_catalogues
        for item in catalogue.context.policies
    )
    descriptors = tuple(
        item
        for catalogue in inputs.producer_catalogues
        for use in catalogue.context.condition_contexts
        for item in use.descriptors
    )
    _merge(profiles, lambda x: x.ref, lambda x: x.definition)
    _merge(policies, lambda x: x.ref, lambda x: x.artifact)
    _merge(descriptors, lambda x: x.schema_ref, lambda x: x)
    hosts: dict[tuple[object, str, str], SystemCapabilityRegistration] = {}
    for catalogue in inputs.producer_catalogues:
        for host in catalogue.context.system_capabilities:
            key = (host.family, host.capability_id, host.contract_version)
            prior = hosts.get(key)
            if prior is not None and (
                prior.registry_id != host.registry_id
                or set(prior.available_implementation_versions)
                != set(host.available_implementation_versions)
                or set(prior.supported_features) != set(host.supported_features)
            ):
                _fail(
                    "x3.host_conflict",
                    "Catalogue contexts supply contradictory host data for the same capability identity.",
                )
            hosts[key] = host
    releases: dict[str, str] = {}
    for pin in inputs.execution_pins:
        if pin.release_id in releases and releases[pin.release_id] != pin.bundle_digest:
            _fail(
                "x3.release_pin_conflict",
                "One supplied Release identity cannot bind two Bundle digests.",
            )
        releases[pin.release_id] = pin.bundle_digest


def validate_producer_authority_contracts(
    inputs: ProducerAuthorityValidationInput,
    *,
    fact_schemas: FactSchemaRegistry,
    parse_limits: CandidateParseLimits,
    snapshot_limits: CandidateSnapshotLimits,
) -> ValidationReport:
    """Compose actual finite checks while preserving specific future responsibilities."""
    checks = _Checks()
    try:
        try:
            checked = ProducerAuthorityValidationInput.model_validate(inputs)
            parse_limits = CandidateParseLimits.model_validate(parse_limits)
            snapshot_limits = CandidateSnapshotLimits.model_validate(snapshot_limits)
        except (ValidationError, ValueError, TypeError):
            checks.error(
                "input",
                "x3.invalid_input",
                "Input contains malformed or contradictory finite associations.",
            )
            checks.error(
                "prerequisites",
                "x3.input_prerequisite",
                "Malformed inputs prevented finite conformance checks.",
                "incomplete",
            )
            return checks.report()
        checks.add("input", "passed")
        sources_ok = checks.run("directories", partial(_preflight_contexts, checked))
        history = checked.history
        merged: list[LocalAuthorityHistoryInput] = []

        def merge_sources() -> None:
            merged.append(_definitions(checked))

        merged_ok = checks.run("directories", merge_sources)
        if merged_ok:
            history = merged[0]
        catalogues = []
        for supplied in checked.producer_catalogues:
            views: list[ProducerCatalogueContext] = []

            def enrich(
                supplied: ProducerCatalogueContext = supplied,
                views: list[ProducerCatalogueContext] = views,
            ) -> None:
                views.append(_context(supplied, history))

            enriched_ok = checks.run("directories", enrich)
            sources_ok = sources_ok and enriched_ok
            enriched = views[0] if views else supplied
            catalogues.append(enriched)
            checks.merge(
                validate_producer_catalogue(
                    enriched.registration, enriched.context, fact_schemas=fact_schemas
                ),
                "catalogue",
                related=enriched.registration.ref,
            )
        local = validate_local_authority_history(
            history,
            fact_schemas=fact_schemas,
            parse_limits=parse_limits,
            snapshot_limits=snapshot_limits,
        )
        checks.merge(local, "local", exclude_local_deferred=True)
        local_sources_ok = all(
            check.status == "passed"
            for check in local.checks
            if check.check_id in ("history_input", "history_identity")
        )
        state = _Validation(checked, history, tuple(catalogues), fact_schemas, checks)
        executions = {entry.fact.execution_id for entry in history.candidates.entries}
        executions.update(
            item.execution_id
            for item in (
                *history.observation_results,
                *history.tool_results,
                *history.system_results,
                *history.prior_selections,
                *history.proposed_selections,
            )
        )
        for execution in sorted(executions):
            checks.run("execution_pin", partial(state.catalogue, execution))
        for pin in checked.execution_pins:
            checks.run("execution_pin", partial(state.catalogue, pin.execution_id))
        if not (sources_ok and merged_ok and local_sources_ok):
            checks.error(
                "prerequisites",
                "x3.source_prerequisite",
                "Conflicting/incomplete finite directory or candidate-byte checks prevent scoped consumption.",
                "incomplete",
            )
            return checks.report()
        for candidate in history.candidates.entries:
            checks.run("candidate_binding", partial(state.fact, candidate.fact))
        results: tuple[_Result, ...] = (
            *history.observation_results,
            *history.tool_results,
            *history.system_results,
        )
        for result in results:
            checks.run("emitted_membership", partial(state.reciprocal_outputs, result))
            checks.run("result_contract", partial(state.result_contract, result))
        for anchor in history.question_selections:
            checks.run("question_selection", partial(state.question_set, anchor))
        for selection in (*history.prior_selections, *history.proposed_selections):
            checks.run("selected_outputs", partial(state.selected_outputs, selection))
        return checks.report()
    except _Overflow:
        return checks.report(overflow=True)
