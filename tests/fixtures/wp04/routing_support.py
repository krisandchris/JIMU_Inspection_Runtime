"""Synthetic, deterministic route proofs built from the contract baseline."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import product
from pathlib import Path

from jimu_runtime.bundle.capabilities import (
    CapabilitySnapshot,
    DeploymentContract,
    ModelDeploymentRegistration,
    ModelReviewRegistration,
)
from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.domain.definition_support import ReviewReportRef
from jimu_runtime.domain.enums import AttemptStatus, NodeRunStatus, RuntimeRecordKind
from jimu_runtime.domain.policies import DeploymentRequirement
from jimu_runtime.domain.runtime.executions import ConfiguredModelPin
from jimu_runtime.domain.runtime.facts import AttemptRef
from jimu_runtime.domain.runtime.invocations import InvocationError, ModelInvocation
from jimu_runtime.domain.validation.authority_inputs import (
    AttemptDescriptor,
    NodeRunDescriptor,
)
from jimu_runtime.domain.validation.policies import (
    ModelBindingRegistration,
    PolicyRegistration,
)
from jimu_runtime.domain.validation.review_inputs import (
    ReviewedDeployment,
    ReviewMetadata,
)
from jimu_runtime.gateway.invocation import (
    DispatchRequest,
    TransportResult,
    project_dispatch,
)
from jimu_runtime.gateway.routing import (
    CurrentRoutingState,
    DeploymentBudget,
    DeploymentOwnership,
    DeploymentOwnershipRegistration,
    PlannedRoute,
    RecordedInvocation,
    RouteBudget,
    RouteOperation,
    RoutePolicy,
    plan_primary,
)
from jimu_runtime.workflow.pins import ResolvedModelDeployments

AT = datetime(2026, 4, 1, 12, tzinfo=UTC)
VALID_FROM = datetime(2026, 1, 1, tzinfo=UTC)
VALID_UNTIL = datetime(2027, 1, 1, tzinfo=UTC)
DIGEST = "9" * 64
REMOTE = "remote-deployment"
LOCAL = "local-deployment"
OTHER = "other-deployment"


@dataclass(frozen=True)
class RoutingFixture:
    policy: RoutePolicy
    operation: RouteOperation
    current: CurrentRoutingState
    budget: RouteBudget
    attempt: AttemptDescriptor

    def attempt_at(self, number):
        return self.attempt.model_copy(
            update={
                "attempt_number": number,
                "attempt_ref": AttemptRef(
                    record_kind=RuntimeRecordKind.ATTEMPT, record_id=f"try-{number}"
                ),
            }
        )

    def primary_route(self):
        return plan_primary(
            self.policy,
            self.operation,
            self.attempt_at(1),
            self.current,
            self.budget,
            AT,
        )


def routing_fixture(
    *,
    local_primary=False,
    edges=((REMOTE, LOCAL),),
    triggers=("timeout",),
    version_evidence=True,
    endpoint_pin=None,
    model_id="synthetic-model",
    response_schema_digest=None,
    multi_version_deployments=(),
):
    raw = json.loads(
        (Path(__file__).parents[1] / "wp01c/invocations/contracts.json").read_text()
    )
    binding = ModelBindingRegistration.model_validate_json(
        json.dumps(raw["model_binding"])
    )
    provider = raw["local_provider"] if local_primary else raw["policies"]["provider"]
    settings = provider["artifact"]["policy"]
    if any(OTHER in edge for edge in edges):
        settings["deployments"].append(
            {
                **settings["deployments"][0],
                "deployment_id": OTHER,
                "provider_id": "other-provider",
            }
        )
    for row in settings["deployments"]:
        if row["deployment_id"] in multi_version_deployments:
            row["actual_version_constraints"] = {
                **row["actual_version_constraints"],
                "allowed_model_versions": ["1.0.0", "2.0.0"],
            }
    rules = tuple(
        DeploymentRequirement.model_validate_json(json.dumps(row))
        for row in settings["deployments"]
    )
    deployments = tuple(
        ReviewedDeployment(
            provider_id=row.provider_id,
            deployment_id=row.deployment_id,
            provider_kind=row.provider_kind,
            logical_model_id=row.logical_model_id,
            deployment_version="1.0.0",
            model_version="1.0.0",
        )
        for row in rules
    )
    lookup = {item.deployment_id: item for item in deployments}
    evaluation, local_approval = raw["review"]["assessments"]

    def reviewed_versions(deployment_id):
        identity = lookup[deployment_id]
        return (
            (identity, identity.model_copy(update={"model_version": "2.0.0"}))
            if deployment_id in multi_version_deployments
            else (identity,)
        )

    raw["review"]["assessments"] = [
        *(
            {**local_approval, "primary": local.model_dump(mode="json")}
            for local in reviewed_versions(LOCAL)
        ),
        *(
            {
                **evaluation,
                "trigger_errors": list(triggers),
                "source": source.model_dump(mode="json"),
                "target": target.model_dump(mode="json"),
            }
            for source_id, target_id in edges
            for source, target in product(
                reviewed_versions(source_id), reviewed_versions(target_id)
            )
        ),
    ]
    fallback = raw["evaluated_fallback"] if edges else raw["policies"]["fallback"]
    if edges:
        fallback["artifact"]["policy"]["trigger_errors"] = list(triggers)
    fallback["artifact"]["policy"]["allowed_transitions"] = [
        {"from_deployment_id": source, "to_deployment_id": target}
        for source, target in edges
    ]
    ownership = tuple(
        DeploymentOwnership(
            deployment=item,
            owner_ref=TrustPin(
                identity=item.provider_id, version="1.0.0", sha256=DIGEST
            ),
            endpoint_descriptor_ref=(
                endpoint_pin
                if endpoint_pin is not None
                and item.deployment_id == settings["primary_deployment_id"]
                else TrustPin(
                    identity=item.deployment_id, version="1.0.0", sha256=DIGEST
                )
            ),
            model_id=model_id,
            version_evidence=(
                ReviewReportRef(
                    report_id=f"attested-{item.deployment_id}",
                    review_registry_id="trusted-registry",
                    version="1.0.0",
                    sha256=DIGEST,
                )
                if version_evidence
                else None
            ),
        )
        for item in deployments
    )
    raw["policies"]["resources"]["artifact"]["policy"]["input_byte_limit"] = 65536
    raw["policies"]["resources"]["artifact"]["policy"]["output_byte_limit"] = 65536
    raw["policies"]["resources"]["artifact"]["policy"]["timeout_ms"] = 5000
    policy = RoutePolicy(
        execution_input_digest=DIGEST,
        binding=binding,
        configured_pin=ConfiguredModelPin(
            binding_ref=binding.ref,
            provider_policy_ref=binding.definition.provider_policy_ref,
            fallback_policy_ref=binding.definition.fallback_policy_ref,
            primary_deployment_id=settings["primary_deployment_id"],
            deployments=rules,
        ),
        resolved=ResolvedModelDeployments(
            binding_ref=binding.ref, deployments=deployments
        ),
        provider_registration=PolicyRegistration.model_validate_json(
            json.dumps(provider)
        ),
        fallback_registration=PolicyRegistration.model_validate_json(
            json.dumps(fallback)
        ),
        resource_registration=PolicyRegistration.model_validate_json(
            json.dumps(raw["policies"]["resources"])
        ),
        ownership=ownership,
    )
    node = NodeRunDescriptor.model_validate_json(json.dumps(raw["nodes"]["N04"]))
    node = node.model_copy(update={"status": NodeRunStatus.RUNNING})
    previous = ModelInvocation.model_validate_json(
        json.dumps(raw["models"]["observation"])
    )
    snapshot = previous.contract_snapshot
    assert snapshot is not None
    if response_schema_digest is not None:
        snapshot = snapshot.model_copy(
            update={"response_schema_sha256": response_schema_digest}
        )
    operation = RouteOperation(
        operation_id="object-1-observation",
        execution_input_digest=DIGEST,
        node_run=node,
        binding_ref=binding.ref,
        input_digest="b" * 64,
        response_contract_digest=snapshot.response_schema_sha256,
        trace_id=previous.trace_id,
        purposes=previous.purposes,
        profile_refs=previous.profile_refs,
        object_ref=previous.object_ref,
        contract_snapshot=snapshot,
    )
    contract = DeploymentContract(
        capability=binding.definition.capability,
        input_schema_ref=binding.definition.input_schema_ref,
        output_schema_ref=binding.definition.output_schema_ref,
    )
    current = CurrentRoutingState(
        capabilities=CapabilitySnapshot(
            pin=TrustPin(identity="synthetic-host", version="1.0.0", sha256=DIGEST),
            runtime_version="1.0.0",
            bundle_schema_versions=("1.0.0",),
            valid_from=VALID_FROM,
            valid_until=VALID_UNTIL,
            capabilities=(),
            model_deployments=tuple(
                ModelDeploymentRegistration(
                    identity=deployment,
                    supported_features=rule.required_features,
                    contracts=(contract,),
                    valid_from=VALID_FROM,
                    valid_until=VALID_UNTIL,
                    revoked_at=None,
                )
                for deployment, rule in zip(deployments, rules, strict=True)
            ),
            model_reviews=(
                ModelReviewRegistration(
                    metadata=ReviewMetadata.model_validate_json(
                        json.dumps(raw["review"])
                    ),
                    revoked_at=None,
                ),
            ),
        ),
        ownership=tuple(
            DeploymentOwnershipRegistration(
                ownership=item,
                valid_from=VALID_FROM,
                valid_until=VALID_UNTIL,
                revoked_at=None,
            )
            for item in ownership
        ),
    )
    budget = RouteBudget(
        node_remaining=4,
        operation_remaining=4,
        deployments=tuple(
            DeploymentBudget(deployment_id=item.deployment_id, remaining=4)
            for item in deployments
        ),
    )
    attempt = AttemptDescriptor.model_validate_json(
        json.dumps(raw["attempts"]["N04"][0])
    )
    return RoutingFixture(
        policy,
        operation,
        current,
        budget,
        attempt.model_copy(update={"status": AttemptStatus.RUNNING}),
    )


def finish_call(route: PlannedRoute, *, error_class="timeout"):
    request = DispatchRequest(
        f"dispatch-{route.attempt.attempt_number}", route, b"fixture request"
    )
    assert route.operation.contract_snapshot is not None
    outcome = project_dispatch(
        request,
        TransportResult(
            dispatched=True,
            error=InvocationError(
                error_class=error_class, code="fixture-failure", diagnostic_refs=()
            ),
        ),
        started_at=AT,
        completed_at=AT,
        evidence_refs=(route.operation.contract_snapshot.response_schema_evidence_ref,),
    )
    assert outcome.invocation is not None
    return RecordedInvocation(
        route.operation,
        route.attempt.model_copy(
            update={
                "status": AttemptStatus.TIMED_OUT
                if error_class == "timeout"
                else AttemptStatus.FAILED
            }
        ),
        outcome.invocation,
    )
