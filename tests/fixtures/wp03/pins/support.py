"""An independently compiled, signed, admitted and activated synthetic Release."""

import json
from dataclasses import dataclass, replace
from pathlib import Path

from jimu_runtime.bundle.capabilities import CapabilitySnapshot, ModelReviewRegistration
from jimu_runtime.bundle.compiler import BundleCompileInput, compile_bundle
from jimu_runtime.bundle.integrity import canonical_json, sha256_digest
from jimu_runtime.bundle.releases import ReleaseManager
from jimu_runtime.bundle.semantic_inputs import require_definition
from jimu_runtime.bundle.store import LocalBundleStore
from jimu_runtime.domain.definition_support import ReviewReportRef
from jimu_runtime.domain.definitions.models import ModelBinding
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_schemas.canonicalization import JSONValue
from jimu_runtime.domain.runtime.intake import AcceptedTask
from jimu_runtime.domain.validation.review_inputs import (
    BindingReviewScope,
    FallbackTransitionAssessment,
    ReviewBindingRef,
    ReviewMetadata,
)
from jimu_runtime.workflow.pins import ResolvedExecutionPins, resolve_execution_pins
from tests.fixtures.wp02.admission.support import sign_bundle, submit
from tests.fixtures.wp02.compiler.scenario import (
    LIMITS,
    approve_source_bytes,
    capability_snapshot,
    compile_environment,
    source_scenario,
)
from tests.fixtures.wp02.releases.support import (
    GLOBAL,
    FixtureHost,
    activate,
    approve,
    register,
    submission,
)
from tests.fixtures.wp02.trust.support import AT


@dataclass(frozen=True)
class AcceptedScenario:
    store: LocalBundleStore
    manager: ReleaseManager
    authority: FixtureHost
    task: AcceptedTask
    host: CapabilitySnapshot

    def resolve(
        self, *, host: CapabilitySnapshot | None = None
    ) -> ResolvedExecutionPins:
        return resolve_execution_pins(
            self.manager,
            self.store,
            self.task,
            scope=self.task.idempotency_scope,
            host=self.host if host is None else host,
            validation_at=AT,
            limits=LIMITS,
        )


def activate_scenario(
    work_dir: Path,
    store: LocalBundleStore,
    manager: ReleaseManager,
    authority: FixtureHost,
    source: BundleCompileInput,
    host: CapabilitySnapshot,
    *,
    number: int,
    expected: str | None = None,
) -> None:
    work_dir.mkdir()
    build = compile_bundle(
        source,
        limits=LIMITS,
        environment=replace(compile_environment(work_dir), capabilities=host),
    )
    assert build.bundle is not None, build.errors
    signed = sign_bundle(
        work_dir, build.bundle.archive_bytes, build.bundle.bundle_ref.bundle_digest
    )
    admission = submit(
        store,
        signed.archive,
        f"submit-{number}",
        replace(signed.environment, capabilities=host),
    )
    receipt = admission.receipt
    assert receipt is not None and receipt.accepted, receipt
    register(manager, authority, number, receipt.receipt_id)
    approve(manager, authority, number, GLOBAL)
    activate(manager, authority, number, GLOBAL, expected=expected)


def accepted_scenario(
    tmp_path: Path,
    *,
    source: BundleCompileInput | None = None,
    host: CapabilitySnapshot | None = None,
) -> AcceptedScenario:
    authority = FixtureHost()
    store = LocalBundleStore(tmp_path / "ledger")
    manager = authority.manager(store)
    capabilities = capability_snapshot() if host is None else host
    activate_scenario(
        tmp_path / "build",
        store,
        manager,
        authority,
        source_scenario() if source is None else source,
        capabilities,
        number=1,
    )
    task = manager.accept_task(submission(), principal=authority.task_client)
    return AcceptedScenario(store, manager, authority, task, capabilities)


def evaluated_fallback(
    baseline: ResolvedExecutionPins, host: CapabilitySnapshot
) -> tuple[BundleCompileInput, CapabilitySnapshot]:
    """Build a second source bundle and its exact independent review scope."""

    def encoded(document: JSONValue) -> bytes:
        return canonical_json(
            document, max_bytes=LIMITS.max_file_bytes, max_depth=LIMITS.max_json_depth
        )

    (original,) = host.model_deployments
    fallback = original.identity.model_copy(
        update={"deployment_id": "fixture.remote.fallback"}
    )
    report_ref = ReviewReportRef(
        report_id="wp03-fallback-review",
        version="1.0.0",
        sha256="fd" * 32,
        review_registry_id="fixture.reviews",
    )

    provider_pin = baseline.pins.model_pins[0].provider_policy_ref
    fallback_pin = baseline.pins.model_pins[0].fallback_policy_ref
    provider_document = json.loads(
        baseline.catalogue.resolve(provider_pin).canonical_bytes
    )
    primary = provider_document["policy"]["deployments"][0]
    provider_document["policy"]["deployments"].append(
        {**primary, "deployment_id": fallback.deployment_id}
    )
    fallback_document = json.loads(
        baseline.catalogue.resolve(fallback_pin).canonical_bytes
    )
    fallback_document["policy"] = {
        "policy_kind": "fallback",
        "contract_compatibility": "exact",
        "mode": "explicit_evaluated",
        "trigger_errors": ["timeout"],
        "allowed_transitions": [
            {
                "from_deployment_id": original.identity.deployment_id,
                "to_deployment_id": fallback.deployment_id,
            }
        ],
        "evaluation_refs": [report_ref.model_dump(mode="json")],
    }
    policy_contents = {
        provider_pin.artifact_id: encoded(provider_document),
        fallback_pin.artifact_id: encoded(fallback_document),
    }

    assessments = []
    for artifact in baseline.catalogue.of_kind(ArtifactKind.MODEL_BINDING):
        binding = require_definition(baseline.catalogue, artifact.ref, ModelBinding)
        document = binding.model_dump(mode="json")
        for name, ref in (
            ("provider_policy_ref", provider_pin),
            ("fallback_policy_ref", fallback_pin),
        ):
            document[name]["digest"] = sha256_digest(policy_contents[ref.artifact_id])
        binding_ref = ReviewBindingRef.model_validate(
            {**artifact.ref.model_dump(), "digest": sha256_digest(encoded(document))}
        )
        assessments.append(
            FallbackTransitionAssessment(
                assessment_kind="fallback_transition",
                source=original.identity,
                target=fallback,
                capability=binding.capability,
                scope=BindingReviewScope(
                    scope_kind="binding", binding_refs=(binding_ref,)
                ),
                trigger_errors=("timeout",),
                input_schema_ref=binding.input_schema_ref,
                output_schema_ref=binding.output_schema_ref,
                contract_compatibility="exact",
                quality_evaluated=True,
            )
        )
    review = ReviewMetadata(
        report_ref=report_ref,
        verdict="approved",
        valid_from=host.valid_from,
        valid_until=host.valid_until,
        assessments=tuple(assessments),
    )
    source = source_scenario()
    artifacts = tuple(
        replace(artifact, content=policy_contents[artifact.artifact_id])
        if artifact.kind is ArtifactKind.POLICY
        and artifact.artifact_id in policy_contents
        else artifact
        for artifact in source.artifacts
    )
    return (
        approve_source_bytes(replace(source, artifacts=artifacts)),
        host.model_copy(
            update={
                "model_deployments": (
                    original,
                    original.model_copy(update={"identity": fallback}),
                ),
                "model_reviews": (
                    ModelReviewRegistration(metadata=review, revoked_at=None),
                ),
            }
        ),
    )
