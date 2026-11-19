"""Finite Tool schema and local plan checks; not Bundle or runtime admission."""

from dataclasses import dataclass
from typing import Self

from pydantic import ValidationError, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import reference_identity
from jimu_runtime.domain.definitions.tools import ToolDefinition, ToolPlanDefinition
from jimu_runtime.domain.fact_schemas.registry import (
    FactSchemaRegistry,
    RegistryValidationError,
)
from jimu_runtime.domain.fact_selectors import ToolContentRef, ToolPlanContentRef
from jimu_runtime.domain.schema_types import (
    FieldSelector,
    ImageSchemaRef,
    ObjectSchemaRef,
    ResolvedFieldType,
    SchemaDescriptor,
    ToolResultSchemaDescriptor,
)
from jimu_runtime.domain.tool_mappings import (
    ContextInput,
    ContextTypeRegistration,
    FactInput,
    ImageInput,
    MappingTypeContext,
    ObjectInput,
    ProfileContentRef,
    StepOutputInput,
)
from jimu_runtime.domain.validation.issues import (
    CheckResult,
    CheckStatus,
    ValidationIssue,
    ValidationReport,
)
from jimu_runtime.domain.validation.schema_descriptors import (
    SchemaCheckError,
    SchemaCheckIssue,
    describe_field,
    require_mapping_compatibility,
    require_type_containment,
    resolve_descriptor,
    resolve_field,
    validate_descriptor,
    validate_target_bindings,
)
from jimu_runtime.domain.values import NumberTypeDefinition


class ToolRegistration(ContractModel):
    """Caller-pinned finite identity; content authenticity is a separate check."""

    ref: ToolContentRef
    definition: ToolDefinition

    @model_validator(mode="after")
    def identity_matches(self) -> Self:
        if (
            self.ref.artifact_id != self.definition.tool_id
            or self.ref.version != self.definition.version
        ):
            raise ValueError("Tool registration identity mismatch")
        return self


def _check(
    check_id: str, status: CheckStatus, code: str = "", message: str = ""
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        status=status,
        issues=()
        if status == "passed"
        else (ValidationIssue(code=code, path=(), message=message, related_refs=()),),
    )


def _schema_failure(check_id: str, exc: SchemaCheckError) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        status=exc.issue.outcome,
        issues=(
            ValidationIssue(
                code=exc.issue.code,
                path=exc.issue.field_path,
                message=exc.issue.message,
                related_refs=(),
            ),
        ),
    )


def _registry_failure(check_id: str, exc: RegistryValidationError) -> CheckResult:
    """Adapt local lookup absence without serializing a bounded report into text."""
    try:
        report = ValidationReport.model_validate(exc.report)
    except ValidationError:
        return _check(
            check_id,
            "invalid",
            "tool.registry_error_contract",
            "Registry exception did not contain a valid bounded diagnostic report",
        )
    if report.scope != "registry_core":
        return _check(
            check_id,
            "invalid",
            "tool.registry_error_scope",
            "Registry exception has an unexpected diagnostic scope",
        )
    issues = tuple(issue for check in report.checks for issue in check.issues)
    if not issues:
        return _check(
            check_id,
            "invalid",
            "tool.registry_error_empty",
            "Registry exception has no diagnostic issues",
        )
    if len(issues) > 64:
        return _check(
            check_id,
            "invalid",
            "tool.registry_issue_limit",
            "Registry error exceeds64 issues; the bounded Tool diagnostic cannot inline this report",
        )
    status: CheckStatus = (
        "incomplete"
        if all(issue.code == "registry.unknown_identity" for issue in issues)
        else "invalid"
    )
    return CheckResult(check_id=check_id, status=status, issues=issues)


def _report(checks: tuple[CheckResult, ...]) -> ValidationReport:
    if len(checks) <= 128:
        return ValidationReport(scope="definition_relations", checks=checks)
    # Preserve every issue while keeping the shared report/check bounds.
    compact: list[CheckResult] = []
    for status in ("invalid", "incomplete", "deferred"):
        issues = [
            ValidationIssue(
                code=issue.code,
                path=(check.check_id, *issue.path),
                message=issue.message,
                related_refs=issue.related_refs,
            )
            for check in checks
            if check.status == status
            for issue in check.issues
        ]
        for index in range(0, len(issues), 64):
            compact.append(
                CheckResult(
                    check_id=f"tool_plan.{status}.{index // 64}",
                    status=status,
                    issues=tuple(issues[index : index + 64]),
                )
            )
    if any(check.status == "passed" for check in checks):
        compact.append(_check("tool_plan.performed_checks", "passed"))
    return ValidationReport(scope="definition_relations", checks=tuple(compact))


def validate_tool_definition(
    tool: ToolDefinition,
    descriptors: tuple[SchemaDescriptor, ...],
    fact_schemas: FactSchemaRegistry | None = None,
) -> ValidationReport:
    tool = ToolDefinition.model_validate(tool)
    checks: list[CheckResult] = []
    try:
        resolve_descriptor(tool.input_schema_ref, descriptors)
        output = resolve_descriptor(tool.output_schema_ref, descriptors)
        if not isinstance(output, ToolResultSchemaDescriptor):
            return ValidationReport(
                scope="definition_relations",
                checks=(
                    _check(
                        "tool.io",
                        "invalid",
                        "tool.output_descriptor",
                        "Tool output requires normalized ToolResult descriptor",
                    ),
                ),
            )
        validate_descriptor(output)
        declarations = {item.output_name: item for item in output.output_declarations}
        if set(declarations) != {item.output_name for item in tool.fact_outputs}:
            checks.append(
                _check(
                    "tool.output_set",
                    "invalid",
                    "tool.output_set",
                    "Descriptor and Tool named output sets differ",
                )
            )
        else:
            checks.append(_check("tool.output_set", "passed"))
        for index, item in enumerate(tool.fact_outputs):
            check_id = f"tool.output.{index}"
            declaration = declarations.get(item.output_name)
            if declaration is None:
                continue
            if (
                declaration.value_path != item.value_path
                or declaration.required != item.required
            ):
                checks.append(
                    _check(
                        check_id,
                        "invalid",
                        "tool.output_declaration",
                        "Descriptor path/requirement differs from Tool declaration",
                    )
                )
                continue
            resolved = describe_field(output, item.value_path)
            slots = {slot.name for slot in output.evidence_fields}
            if item.evidence_paths is not None and any(
                path[0] not in slots for path in item.evidence_paths
            ):
                checks.append(
                    _check(
                        check_id,
                        "invalid",
                        "tool.evidence_path",
                        "Evidence path addresses undeclared named slot",
                    )
                )
                continue
            if item.confidence_path is not None and not isinstance(
                describe_field(output, item.confidence_path).type_definition,
                NumberTypeDefinition,
            ):
                checks.append(
                    _check(
                        check_id,
                        "invalid",
                        "tool.confidence_type",
                        "Confidence path must select finite Number",
                    )
                )
                continue
            if fact_schemas is None:
                checks.append(
                    _check(
                        check_id,
                        "incomplete",
                        "tool.fact_schema_unavailable",
                        "Fact Schema registry is not supplied",
                    )
                )
                continue
            fact = fact_schemas.resolve(item.fact_schema_ref)
            if fact.scope != item.scope or not any(
                kind.value == "tool" for kind in fact.allowed_producer_kinds
            ):
                checks.append(
                    _check(
                        check_id,
                        "invalid",
                        "tool.fact_schema_scope",
                        "Fact Schema does not permit this Tool scope/producer",
                    )
                )
                continue
            require_type_containment(resolved.type_definition, fact.type_definition)
            checks.append(_check(check_id, "passed"))
        checks.append(_check("tool.io", "passed"))
    except SchemaCheckError as exc:
        checks.append(_schema_failure("tool.io", exc))
    except RegistryValidationError as exc:
        checks.append(_registry_failure("tool.fact_schema", exc))
    checks.append(
        _check(
            "tool.external_admission",
            "deferred",
            "tool.external_admission",
            "Implementation allowlist, Policy contents, model binding and resource admission require separate finite catalogues",
        )
    )
    return _report(tuple(checks))


@dataclass(frozen=True, slots=True)
class ToolMappingUse:
    """Application-projected site; unused source roles require no synthetic pin."""

    profile_ref: ProfileContentRef
    tool_plan_ref: ToolPlanContentRef
    current_step_id: str
    context_registrations: tuple[ContextTypeRegistration, ...]
    descriptors: tuple[SchemaDescriptor, ...]
    object_schema_ref: ObjectSchemaRef | None
    image_schema_ref: ImageSchemaRef | None


def _source_type(
    source: ContextInput | FactInput | ImageInput | ObjectInput | StepOutputInput,
    context: MappingTypeContext | ToolMappingUse,
    tools: tuple[ToolRegistration, ...],
    plan: ToolPlanDefinition,
    fact_schemas: FactSchemaRegistry | None,
) -> ResolvedFieldType:
    if isinstance(source, FactInput):
        if fact_schemas is None:
            raise SchemaCheckError(
                SchemaCheckIssue(
                    outcome="incomplete",
                    code="tool_plan.fact_schema_missing",
                    field_path=(),
                    message="Fact Schema registry is not supplied",
                )
            )
        definition = fact_schemas.resolve(source.selector.fact_schema_ref)
        if (
            definition.scope != source.selector.scope
            or source.selector.producer_selector.producer_kind
            not in definition.allowed_producer_kinds
        ):
            raise ValueError("Fact source scope/producer conflicts with Schema")
        return fact_schemas.describe_type(
            source.selector.fact_schema_ref, source.selector.field_path
        )
    selector = source.selector
    if isinstance(source, ImageInput) and (
        context.image_schema_ref is None
        or reference_identity(selector.schema_ref)
        != reference_identity(context.image_schema_ref)
    ):
        raise ValueError("Image input is not the current Task image view")
    if isinstance(source, ObjectInput) and (
        context.object_schema_ref is None
        or reference_identity(selector.schema_ref)
        != reference_identity(context.object_schema_ref)
    ):
        raise ValueError("Object input is not the current Object view")
    if isinstance(source, ContextInput):
        matches = [
            registration
            for registration in context.context_registrations
            if registration.source == source.selector.source.value
            and reference_identity(registration.schema_ref)
            == reference_identity(selector.schema_ref)
        ]
        if len(matches) != 1:
            raise SchemaCheckError(
                SchemaCheckIssue(
                    outcome="incomplete" if not matches else "invalid",
                    code="tool_plan.context_registration",
                    field_path=(),
                    message="Context source requires one exact owner/schema registration",
                )
            )
    if isinstance(source, StepOutputInput):
        upstream = next(step for step in plan.steps if step.step_id == source.step_id)
        registration = next(
            (
                entry
                for entry in tools
                if reference_identity(entry.ref)
                == reference_identity(upstream.tool_ref)
            ),
            None,
        )
        if registration is None:
            raise SchemaCheckError(
                SchemaCheckIssue(
                    outcome="incomplete",
                    code="tool_plan.upstream_missing",
                    field_path=(),
                    message="Upstream Tool definition is unavailable",
                )
            )
        if reference_identity(
            registration.definition.output_schema_ref
        ) != reference_identity(selector.schema_ref):
            raise ValueError("Upstream Tool output Schema mismatch")
    return resolve_field(
        FieldSelector(schema_ref=selector.schema_ref, field_path=selector.field_path),
        context.descriptors,
    )


def validate_tool_plan(
    plan: ToolPlanDefinition,
    tools: tuple[ToolRegistration, ...],
    mapping_contexts: tuple[MappingTypeContext | ToolMappingUse, ...] = (),
    fact_schemas: FactSchemaRegistry | None = None,
    *,
    plan_ref: ToolPlanContentRef,
) -> ValidationReport:
    """Finite local IO/mapping checks; explicitly retains unperformed content admission."""
    plan = ToolPlanDefinition.model_validate(plan)
    plan_ref = ToolPlanContentRef.model_validate(plan_ref)
    if plan_ref.artifact_id != plan.tool_plan_id or plan_ref.version != plan.version:
        return _report(
            (
                _check(
                    "tool_plan.identity",
                    "invalid",
                    "tool_plan.identity_mismatch",
                    "Plan ref ID/version differs from definition",
                ),
            )
        )
    checked_tools = tuple(ToolRegistration.model_validate(entry) for entry in tools)
    contexts = tuple(
        entry
        if isinstance(entry, ToolMappingUse)
        else MappingTypeContext.model_validate(entry)
        for entry in mapping_contexts
    )
    checks: list[CheckResult] = [_check("tool_plan.local_dag", "passed")]
    identities = [(entry.ref.artifact_id, entry.ref.version) for entry in checked_tools]
    if len(set(identities)) != len(identities):
        return ValidationReport(
            scope="definition_relations",
            checks=(
                _check(
                    "tool_plan.tools",
                    "invalid",
                    "tool_plan.tool_conflict",
                    "Duplicate or conflicting Tool identity",
                ),
            ),
        )
    if len(checked_tools) > 1024 or len(contexts) > 1024:
        return ValidationReport(
            scope="definition_relations",
            checks=(
                _check(
                    "tool_plan.inputs",
                    "invalid",
                    "tool_plan.input_limit",
                    "Finite input collection exceeds 1024",
                ),
            ),
        )
    if len({reference_identity(context.profile_ref) for context in contexts}) > 1:
        return _report(
            (
                _check(
                    "tool_plan.profile_scope",
                    "invalid",
                    "tool_plan.profile_scope",
                    "Validate one exact Profile scope per call",
                ),
            )
        )
    context_ids = [context.current_step_id for context in contexts]
    if len(set(context_ids)) != len(context_ids):
        return ValidationReport(
            scope="definition_relations",
            checks=(
                _check(
                    "tool_plan.contexts",
                    "invalid",
                    "tool_plan.context_conflict",
                    "Duplicate mapping context",
                ),
            ),
        )
    step_ids = {step.step_id for step in plan.steps}
    for supplied_context in contexts:
        if reference_identity(supplied_context.tool_plan_ref) != reference_identity(
            plan_ref
        ):
            return _report(
                (
                    _check(
                        "tool_plan.contexts",
                        "invalid",
                        "tool_plan.context_owner",
                        "Every supplied context must match the complete pinned Plan identity",
                    ),
                )
            )
        if supplied_context.current_step_id not in step_ids:
            return _report(
                (
                    _check(
                        "tool_plan.contexts",
                        "invalid",
                        "tool_plan.context_step_unknown",
                        "A supplied context refers to a step outside this Plan",
                    ),
                )
            )
    for index, step in enumerate(plan.steps):
        check_id = f"tool_plan.step.{index}"
        registration = next(
            (
                entry
                for entry in checked_tools
                if reference_identity(entry.ref) == reference_identity(step.tool_ref)
            ),
            None,
        )
        if registration is None:
            conflict = any(
                entry.ref.artifact_id == step.tool_ref.artifact_id
                and entry.ref.version == step.tool_ref.version
                for entry in checked_tools
            )
            checks.append(
                _check(
                    check_id,
                    "invalid" if conflict else "incomplete",
                    "tool_plan.tool_digest_mismatch"
                    if conflict
                    else "tool_plan.tool_unavailable",
                    "Exact Tool digest differs"
                    if conflict
                    else "Exact Tool definition is missing",
                )
            )
            continue
        tool = registration.definition
        outputs = {output.output_name: output for output in tool.fact_outputs}
        mapped = {mapping.output_name: mapping for mapping in step.fact_output_mappings}
        if any(
            name not in outputs or mapping.target_fact_key != outputs[name].fact_key
            for name, mapping in mapped.items()
        ):
            checks.append(
                _check(
                    check_id,
                    "invalid",
                    "tool_plan.output_mapping",
                    "Unknown output or aliased key",
                )
            )
            continue
        context = next(
            (entry for entry in contexts if entry.current_step_id == step.step_id), None
        )
        if context is None:
            checks.append(
                _check(
                    check_id,
                    "incomplete",
                    "tool_plan.mapping_context",
                    "Finite mapping type context is missing",
                )
            )
            continue
        if reference_identity(context.tool_plan_ref) != reference_identity(plan_ref):
            checks.append(
                _check(
                    check_id,
                    "invalid",
                    "tool_plan.context_owner",
                    "Mapping context belongs to another plan",
                )
            )
            continue
        try:
            target_descriptor = resolve_descriptor(
                tool.input_schema_ref, context.descriptors
            )
            validate_target_bindings(target_descriptor, step.input_bindings)
            for binding in step.input_bindings:
                target = resolve_field(binding.target, context.descriptors)
                source = _source_type(
                    binding.source, context, checked_tools, plan, fact_schemas
                )
                require_mapping_compatibility(source, target, binding)
            checks.append(_check(check_id, "passed"))
        except SchemaCheckError as exc:
            checks.append(_schema_failure(check_id, exc))
        except RegistryValidationError as exc:
            checks.append(_registry_failure(check_id, exc))
        except ValueError as exc:
            checks.append(
                _check(check_id, "invalid", "tool_plan.source_context", str(exc))
            )
    checks.append(
        _check(
            "tool_plan.policy_contents",
            "deferred",
            "tool_plan.policy_contents",
            "This local API has no complete Policy catalogue; subtype/budget, failure and ToolSkip condition checks require upper-layer finite checks. Actual activation, expression typing, Release admission and external trust are not proven here.",
        )
    )
    checks.append(
        _check(
            "tool_plan.producer_authority",
            "deferred",
            "tool_plan.producer_authority",
            "Static types do not establish Fact producer authority or runtime source availability",
        )
    )
    return _report(tuple(checks))
