"""Finite condition reference bindings, without truth or operand type checking."""

from collections.abc import Callable, Iterator
from functools import partial
from typing import Literal

from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.conditions import (
    ConditionExpr,
    LocalToolFactSelector,
    condition_references,
    validate_condition_data,
)
from jimu_runtime.domain.definition_enums import (
    ConditionInputDomain,
    FactScope,
    ProducerKind,
)
from jimu_runtime.domain.definition_support import (
    ContextFieldSelector,
    reference_identity,
)
from jimu_runtime.domain.definitions.tools import ToolPlanDefinition, plan_ancestors
from jimu_runtime.domain.fact_schemas.registry import (
    FactSchemaRegistry,
    RegistryValidationError,
)
from jimu_runtime.domain.fact_selectors import (
    FactSelector,
    QuestionProducerSelector,
    SystemProducerSelector,
    ToolProducerSelector,
)
from jimu_runtime.domain.references import ContentRef
from jimu_runtime.domain.tool_mappings import (
    ProfileContextRegistration,
    RegionContextRegistration,
)
from jimu_runtime.domain.validation.authority_inputs import (
    AuthorityQuestionInput,
    AuthorityToolPlanInput,
)
from jimu_runtime.domain.validation.condition_inputs import (
    ConditionReferenceContext,
    ConditionReferenceResult,
    ProducerActivationConditionContext,
)
from jimu_runtime.domain.validation.definition_inputs import ProfileRegistration
from jimu_runtime.domain.validation.issues import (
    CheckResult,
    CheckStatus,
    ValidationIssue,
    ValidationReport,
)
from jimu_runtime.domain.validation.schema_descriptors import (
    SchemaCheckError,
    describe_field,
    resolve_descriptor,
)
from jimu_runtime.domain.validation.tool_plans import ToolRegistration


class _Failure(ValueError):
    def __init__(self, code: str, status: CheckStatus = "invalid") -> None:
        self.code, self.status = code, status


def _same(a: ContentRef, b: ContentRef) -> bool:
    return reference_identity(a) == reference_identity(b)


class _Checks:
    def __init__(self) -> None:
        self.errors: list[tuple[CheckStatus, ValidationIssue]] = []
        self.overflow = False

    def add(self, code: str, status: CheckStatus = "invalid") -> None:
        if len(self.errors) >= 4096:
            self.overflow = True
            return
        self.errors.append(
            (
                status,
                ValidationIssue(
                    code=code,
                    path=(),
                    message=f"Condition reference check: {code}.",
                    related_refs=(),
                ),
            )
        )

    def run(self, action: Callable[[], object]) -> bool:
        try:
            action()
            return True
        except _Failure as exc:
            self.add(exc.code, exc.status)
        except SchemaCheckError as exc:
            self.add(f"condition.{exc.issue.code}", exc.issue.outcome)
        except RegistryValidationError as exc:
            for check in exc.report.checks:
                for issue in check.issues:
                    self.add(
                        issue.code,
                        "incomplete"
                        if issue.code == "registry.unknown_identity"
                        else check.status,
                    )
        except (ValueError, TypeError, ValidationError):
            self.add("condition.contract_invalid")
        return False

    def report(self) -> ValidationReport:
        if self.overflow:
            self.errors = [
                (
                    "invalid",
                    ValidationIssue(
                        code="condition.diagnostic_budget",
                        path=(),
                        message="Condition diagnostic budget exceeded; enumeration is not complete.",
                        related_refs=(),
                    ),
                )
            ]
        checks: list[CheckResult] = []
        for status in ("invalid", "incomplete", "deferred"):
            issues = tuple(issue for outcome, issue in self.errors if outcome == status)
            for start in range(0, len(issues), 64):
                checks.append(
                    CheckResult(
                        check_id=f"condition.{status}.{start // 64}",
                        status=status,
                        issues=issues[start : start + 64],
                    )
                )
        if not any(status in ("invalid", "incomplete") for status, _ in self.errors):
            checks.insert(
                0,
                CheckResult(
                    check_id="condition.references", status="passed", issues=()
                ),
            )
        if not checks:
            checks.append(
                CheckResult(check_id="condition.references", status="passed", issues=())
            )
        return ValidationReport(scope="definition_relations", checks=tuple(checks))


def _lookup[
    T: (
        ProfileRegistration,
        AuthorityQuestionInput,
        ToolRegistration,
        AuthorityToolPlanInput,
    )
](ref: ContentRef, entries: tuple[T, ...]) -> T:
    for entry in entries:
        actual = entry.ref
        if reference_identity(actual)[:3] == reference_identity(ref)[:3]:
            if not _same(actual, ref):
                raise _Failure("condition.digest_mismatch")
            return entry
    raise _Failure("condition.owner_unavailable", "incomplete")


def _references(value: object) -> Iterator[ContentRef]:
    """Walk already revalidated finite data, including unused directory claims."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, ContentRef):
            yield item
        elif isinstance(item, ContractModel):
            pending.extend(item.__dict__.values())
        elif isinstance(item, tuple):
            pending.extend(item)


def validate_condition_references(
    condition: ConditionExpr | Literal[True],
    context: ConditionReferenceContext,
    *,
    input_domain: ConditionInputDomain,
    profiles: tuple[ProfileRegistration, ...],
    questions: tuple[AuthorityQuestionInput, ...],
    tools: tuple[ToolRegistration, ...],
    plans: tuple[AuthorityToolPlanInput, ...],
    fact_schemas: FactSchemaRegistry,
    context_allowlist: tuple[ContextFieldSelector, ...] | None,
) -> ConditionReferenceResult:
    """Bind finite identities and paths only; callers authenticate condition origin."""
    checks = _Checks()
    projected: list[FactSelector] = []
    try:
        context = TypeAdapter[ConditionReferenceContext](
            ConditionReferenceContext
        ).validate_python(context)
        input_domain = ConditionInputDomain._decode(input_domain)
        if not isinstance(fact_schemas, FactSchemaRegistry):
            raise _Failure("condition.registry_required")
        directories = (profiles, questions, tools, plans)
        if any(
            type(items) not in (tuple, list) or len(items) > 1024
            for items in directories
        ):
            raise _Failure("condition.directory_budget")
        profiles = tuple(ProfileRegistration.model_validate(item) for item in profiles)
        questions = tuple(
            AuthorityQuestionInput.model_validate(item) for item in questions
        )
        tools = tuple(ToolRegistration.model_validate(item) for item in tools)
        plans = tuple(AuthorityToolPlanInput.model_validate(item) for item in plans)
        if context_allowlist is not None:
            if type(context_allowlist) is not tuple or len(context_allowlist) > 1024:
                raise _Failure("condition.allowlist_budget")
            context_allowlist = tuple(
                ContextFieldSelector.model_validate(item) for item in context_allowlist
            )
        applicability = context.use_role == "applicability"
        if applicability != (
            input_domain is ConditionInputDomain.RULE_APPLICABILITY
        ) or applicability != (context_allowlist is not None):
            raise _Failure("condition.input_domain")
        if (
            not applicability
            and input_domain is not ConditionInputDomain.TOOL_CONDITION
        ):
            raise _Failure("condition.input_domain")
        if condition is True:
            if applicability:
                raise _Failure("condition.bare_true_domain")
            refs = None
        else:
            condition = validate_condition_data(condition, input_domain=input_domain)
            refs = condition_references(condition)
    except (_Failure, ValueError, TypeError, ValidationError) as exc:
        checks.add(
            exc.code if isinstance(exc, _Failure) else "condition.contract_invalid"
        )
        return ConditionReferenceResult(
            report=checks.report(), selectors_for_matching=()
        )

    # All supplied identities participate, including unused descriptors and owners.
    claims: list[ContentRef] = list(
        _references(
            (profiles, questions, tools, plans, context, condition, context_allowlist)
        )
    )
    for directory in (profiles, questions, tools, plans):
        seen: set[tuple[object, ...]] = set()
        for entry in directory:
            key = reference_identity(entry.ref)[:3]
            if key in seen:
                checks.add("condition.duplicate_registration")
            seen.add(key)
            claims.append(entry.ref)
    claims.extend(descriptor.schema_ref for descriptor in context.descriptors)
    claims.extend(
        registration.schema_ref for registration in context.context_registrations
    )
    for registration in context.context_registrations:
        if isinstance(registration, ProfileContextRegistration):
            claims.append(registration.profile_ref)
        if isinstance(registration, RegionContextRegistration):
            claims.append(registration.region_ref)
    for value in context.__dict__.values():
        if isinstance(value, ContentRef):
            claims.append(value)
    selectors = (() if refs is None else refs.context_selectors) + (
        context_allowlist or ()
    )
    claims.extend(selector.schema_ref for selector in selectors)
    if refs is not None:
        for selector in refs.fact_selectors:
            claims.append(selector.fact_schema_ref)
            producer = selector.producer_selector
            if isinstance(producer, QuestionProducerSelector):
                claims.append(producer.question_ref)
            elif isinstance(producer, ToolProducerSelector):
                claims.extend((producer.tool_plan_ref, producer.tool_ref))
        for local_selector in refs.local_tool_selectors:
            claims.extend((local_selector.fact_schema_ref, local_selector.tool_ref))
    pins: dict[tuple[object, ...], str] = {}
    for ref in claims:
        key = reference_identity(ref)[:3]
        if key in pins and pins[key] != ref.digest:
            checks.add("condition.digest_conflict")
        pins[key] = ref.digest
    descriptor_keys = [
        reference_identity(item.schema_ref)[:3] for item in context.descriptors
    ]
    if len(set(descriptor_keys)) != len(descriptor_keys):
        checks.add("condition.duplicate_descriptor")
    registration_keys: list[tuple[object, ...]] = []
    for registration in context.context_registrations:
        owner: tuple[object, ...] | None = None
        if isinstance(registration, ProfileContextRegistration):
            owner = reference_identity(registration.profile_ref)
        elif isinstance(registration, RegionContextRegistration):
            owner = reference_identity(registration.region_ref)
        # Snapshot owner is the enclosing current use, implicit within this call.
        # Schema identity is a claim of that source/owner, not a second slot key.
        registration_keys.append((registration.source, owner))
    if len(set(registration_keys)) != len(registration_keys):
        checks.add("condition.duplicate_context")

    profile_ref = context.profile_ref
    profile = None
    if profile_ref is not None:

        def profile_check() -> None:
            nonlocal profile
            profile = _lookup(profile_ref, profiles).definition
            if context.use_role == "applicability" and context.question_ref is not None:
                if not any(
                    _same(context.question_ref, ref) for ref in profile.question_refs
                ):
                    raise _Failure("condition.foreign_question")
                _lookup(context.question_ref, questions)

        checks.run(profile_check)
    for registration in context.context_registrations:
        if isinstance(registration, ProfileContextRegistration):
            if profile_ref is None or not _same(registration.profile_ref, profile_ref):
                checks.add("condition.foreign_profile")
        if isinstance(registration, RegionContextRegistration):
            if context.region_ref is None:
                checks.add("condition.region_unavailable", "incomplete")
            elif not _same(registration.region_ref, context.region_ref):
                checks.add("condition.foreign_region")
    attached = context.use_role == "tool_step_guard" or (
        isinstance(context, ProducerActivationConditionContext)
        and context.producer_kind is ProducerKind.TOOL
    )
    anchor_plan_ref = (
        context.tool_plan_ref if context.use_role != "applicability" else None
    )
    anchor_step_id = context.step_id if context.use_role != "applicability" else None
    attached_plan: ToolPlanDefinition | None = None
    if attached:

        def anchor_check() -> None:
            nonlocal attached_plan
            assert anchor_plan_ref is not None and anchor_step_id is not None
            if profile is not None and not _same(
                profile.tool_plan_ref, anchor_plan_ref
            ):
                raise _Failure("condition.foreign_plan")
            actual_plan = _lookup(anchor_plan_ref, plans).definition
            if not any(step.step_id == anchor_step_id for step in actual_plan.steps):
                raise _Failure("condition.unknown_step")
            attached_plan = actual_plan

        checks.run(anchor_check)

    def context_check(selector: ContextFieldSelector) -> None:
        descriptor = resolve_descriptor(selector.schema_ref, context.descriptors)
        describe_field(descriptor, selector.field_path)
        matches = [
            item
            for item in context.context_registrations
            if item.source == selector.source.value
            and _same(item.schema_ref, selector.schema_ref)
        ]
        if not matches:
            raise _Failure("condition.context_unavailable", "incomplete")
        if len(matches) != 1:
            raise _Failure("condition.context_ambiguous")
        registration = matches[0]
        if isinstance(registration, ProfileContextRegistration):
            if profile_ref is None or not _same(profile_ref, registration.profile_ref):
                raise _Failure("condition.foreign_profile")
        if isinstance(registration, RegionContextRegistration):
            if context.region_ref is None:
                raise _Failure("condition.region_unavailable", "incomplete")
            if not _same(context.region_ref, registration.region_ref):
                raise _Failure("condition.foreign_region")

    # Declared unused allowlist entries have the same owner/type obligations.
    for context_selector in selectors:
        checks.run(partial(context_check, context_selector))
    if context_allowlist is not None and refs is not None:
        for context_selector in refs.context_selectors:
            if context_selector not in context_allowlist:
                checks.add("condition.context_not_allowlisted")

    def schema_check(selector: FactSelector) -> None:
        if profile_ref is None and selector.scope is FactScope.OBJECT:
            raise _Failure("condition.foreign_scope")
        schema = fact_schemas.resolve(selector.fact_schema_ref)
        if (
            schema.scope is not selector.scope
            or selector.producer_selector.producer_kind
            not in schema.allowed_producer_kinds
        ):
            raise _Failure("condition.fact_schema_domain")
        fact_schemas.describe_type(selector.fact_schema_ref, selector.field_path)

    def tool_output(selector: FactSelector, producer: ToolProducerSelector) -> None:
        if profile is not None and not _same(
            profile.tool_plan_ref, producer.tool_plan_ref
        ):
            raise _Failure("condition.foreign_plan")
        plan = _lookup(producer.tool_plan_ref, plans).definition
        step = next(
            (step for step in plan.steps if step.step_id == producer.step_id), None
        )
        if step is None or not _same(step.tool_ref, producer.tool_ref):
            raise _Failure("condition.tool_step_identity")
        tool = _lookup(producer.tool_ref, tools).definition
        output = next(
            (
                out
                for out in tool.fact_outputs
                if out.output_name == producer.output_name
            ),
            None,
        )
        mapping = next(
            (
                out
                for out in step.fact_output_mappings
                if out.output_name == producer.output_name
            ),
            None,
        )
        if (
            output is None
            or mapping is None
            or mapping.target_fact_key != selector.fact_key
            or output.fact_key != selector.fact_key
            or not _same(output.fact_schema_ref, selector.fact_schema_ref)
        ):
            raise _Failure("condition.tool_output_identity")

    def fact_check(selector: FactSelector, *, local: bool = False) -> None:
        schema_check(selector)
        producer = selector.producer_selector
        if isinstance(producer, QuestionProducerSelector):
            if profile is not None and not any(
                _same(producer.question_ref, ref) for ref in profile.question_refs
            ):
                raise _Failure("condition.foreign_question")
            question = _lookup(producer.question_ref, questions).definition
            if question.primary_fact_key != selector.fact_key or not _same(
                question.primary_fact_schema_ref, selector.fact_schema_ref
            ):
                raise _Failure("condition.question_output_identity")
        elif isinstance(producer, ToolProducerSelector):
            if attached and not local:
                raise _Failure("condition.exact_tool_guard_forbidden")
            tool_output(selector, producer)
        elif isinstance(producer, SystemProducerSelector):
            pass  # C binds actual System declaration; no fabricated output authority.

    if refs is not None:
        for selector in refs.fact_selectors:
            if checks.run(partial(fact_check, selector)):
                projected.append(selector)
        for local in refs.local_tool_selectors:

            def bind(local: LocalToolFactSelector = local) -> None:
                if not attached or attached_plan is None:
                    raise _Failure(
                        "condition.local_plan_unavailable",
                        "incomplete" if attached else "invalid",
                    )
                assert anchor_step_id is not None and anchor_plan_ref is not None
                if (
                    local.step_id
                    not in plan_ancestors(attached_plan.steps)[anchor_step_id]
                ):
                    raise _Failure("condition.local_not_ancestor")
                selector = FactSelector(
                    scope=local.scope,
                    fact_key=local.fact_key,
                    fact_schema_ref=local.fact_schema_ref,
                    field_path=local.field_path,
                    producer_selector=ToolProducerSelector(
                        producer_kind=ProducerKind.TOOL,
                        tool_plan_ref=anchor_plan_ref,
                        step_id=local.step_id,
                        tool_ref=local.tool_ref,
                        output_name=local.output_name,
                    ),
                )
                fact_check(selector, local=True)
                projected.append(selector)

            checks.run(bind)
    checks.add("condition.deferred.operand_types_and_truth", "deferred")
    report = checks.report()
    return ConditionReferenceResult(
        report=report,
        selectors_for_matching=()
        if report.aggregate_status in ("invalid", "incomplete")
        else tuple(projected),
    )
