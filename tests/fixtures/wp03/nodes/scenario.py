"""An admitted, approved and activated synthetic parking scenario for WP-03."""

from dataclasses import dataclass, replace
from json import loads
from pathlib import Path

from jimu_runtime.bundle.archive import read_archive
from jimu_runtime.bundle.catalogue import build_catalogue
from jimu_runtime.bundle.compiler import (
    BundleCompileInput,
    ReferenceSlot,
    SourceArtifact,
    SourceIdentity,
    compile_bundle,
)
from jimu_runtime.bundle.integrity import sha256_digest, verify_bundle_files
from jimu_runtime.bundle.releases import ReleaseManager, ResolvedTaskScope
from jimu_runtime.bundle.store import LocalBundleStore
from jimu_runtime.domain.definition_support import ContextSchemaRef
from jimu_runtime.domain.definitions.models import ModelBinding, ModelBindingContentRef
from jimu_runtime.domain.definitions.ontology import RegionDefinition
from jimu_runtime.domain.definitions.tools import ToolDefinition
from jimu_runtime.domain.enums import ArtifactKind, RuntimeRecordKind
from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
from jimu_runtime.domain.fact_selectors import ToolContentRef
from jimu_runtime.domain.nodes.context import ContextBuildInput
from jimu_runtime.domain.nodes.envelopes import NodeInputEnvelope
from jimu_runtime.domain.policies import ExecutionPolicyArtifact, ProviderPolicyArtifact
from jimu_runtime.domain.runtime.executions import (
    ConfiguredModelPin,
    ConfiguredToolPin,
    ExecutionPins,
    NodeId,
)
from jimu_runtime.domain.runtime.facts import PolicyContentRef
from jimu_runtime.domain.runtime.intake import RegistryRecordRef, TaskSubmission
from jimu_runtime.domain.runtime.media import (
    FinalizedAsset,
    ImageDimensions,
    MediaAsset,
    MediaRef,
    StorageReference,
)
from jimu_runtime.domain.runtime.releases import AdmissionReceipt, SupportedCapability
from jimu_runtime.domain.tool_mappings import RegionContentRef
from jimu_runtime.domain.values import RecordValue, StringValue
from jimu_runtime.workflow.providers import (
    SyntheticArtifact,
    SyntheticExecution,
    artifact,
    record_digest,
    synthetic_id,
)
from tests.fixtures.wp02.admission.support import sign_bundle, submit
from tests.fixtures.wp02.compiler.scenario import (
    LIMITS,
    QUESTION,
    VEHICLE,
    VERSION,
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

K = RuntimeRecordKind
REGION = "test_parking"

# Portable Pixmap, RGB, 8 x 8. Bytes and all media metadata are deterministic.
RGB_IMAGE = b"P6\n8 8\n255\n" + bytes((32, 64, 96)) * 64


def _add_question(artifacts: list[SourceArtifact]) -> None:
    question_id = "secondary_parking_state"
    schema_id = "secondary-parked"
    original_schema = next(
        item
        for item in artifacts
        if item.kind is ArtifactKind.FACT_SCHEMA and item.artifact_id == "parked"
    )
    schema = loads(original_schema.content)
    schema["schema_id"] = schema_id
    schema["semantic_key"] = "parking.secondary"
    artifacts.append(
        replace(
            original_schema,
            artifact_id=schema_id,
            path=f"schemas/facts/definitions/{schema_id}.json",
            content=canonical_bytes(schema),
            origin=SourceIdentity(
                artifact_id=f"fact_schema:{schema_id}", version=VERSION
            ),
        )
    )
    original_question = next(
        item
        for item in artifacts
        if item.kind is ArtifactKind.QUESTION and item.artifact_id == QUESTION
    )
    question = loads(original_question.content)
    question["question_id"] = question_id
    question["primary_fact_key"] = "parking.secondary"
    question_slots = tuple(
        replace(slot, target=(ArtifactKind.FACT_SCHEMA, schema_id, VERSION))
        if slot.location == ("primary_fact_schema_ref",)
        else slot
        for slot in original_question.reference_slots
    )
    artifacts.append(
        replace(
            original_question,
            artifact_id=question_id,
            content=canonical_bytes(question),
            origin=SourceIdentity(
                artifact_id=f"question:{question_id}", version=VERSION
            ),
            reference_slots=question_slots,
        )
    )

    for index, item in enumerate(artifacts):
        value = loads(item.content)
        if item.kind is ArtifactKind.PROFILE:
            value["question_refs"].append(None)
            position = ("question_refs", 1)
            target = (ArtifactKind.QUESTION, question_id, VERSION)
        elif item.kind is ArtifactKind.POLICY and item.artifact_id == "selection":
            value["policy"]["optional_question_refs"].append(None)
            position = ("policy", "optional_question_refs", 0)
            target = (ArtifactKind.QUESTION, question_id, VERSION)
        elif item.kind is ArtifactKind.FACET:
            value["fact_schema_refs"].append(None)
            position = ("fact_schema_refs", 1)
            target = (ArtifactKind.FACT_SCHEMA, schema_id, VERSION)
        else:
            continue
        artifacts[index] = replace(
            item,
            content=canonical_bytes(value),
            reference_slots=(
                *item.reference_slots,
                ReferenceSlot(location=position, target=target),
            ),
        )


def parking_source(
    *,
    second_profile: bool = False,
    excluded: bool = False,
    second_question: bool = False,
) -> BundleCompileInput:
    """Use the WP-02 parking source with narrowly scoped fixture variations."""
    source = source_scenario()
    artifacts = list(source.artifacts)
    if excluded:
        index = next(
            index
            for index, item in enumerate(artifacts)
            if item.kind is ArtifactKind.POLICY and item.artifact_id == "applicability"
        )
        original = artifacts[index]
        value = loads(original.content)
        value["policy"]["condition_ast"]["constant"]["value"]["value"] = False
        artifacts[index] = replace(original, content=canonical_bytes(value))
    if second_profile:
        original = next(
            item
            for item in artifacts
            if item.kind is ArtifactKind.PROFILE and item.artifact_id == VEHICLE
        )
        value = loads(original.content)
        value["profile_id"] = "nonmotor_vehicle_second"
        artifacts.append(
            replace(
                original,
                artifact_id=value["profile_id"],
                origin=SourceIdentity(
                    artifact_id="profile:nonmotor_vehicle_second", version=VERSION
                ),
                content=canonical_bytes(value),
            )
        )
    if second_question:
        _add_question(artifacts)
    return approve_source_bytes(replace(source, artifacts=tuple(artifacts)))


@dataclass(frozen=True, slots=True)
class ParkingFixture:
    scope: SyntheticExecution
    store: LocalBundleStore
    manager: ReleaseManager
    host: FixtureHost
    submission: TaskSubmission
    receipt: AdmissionReceipt

    def envelope(
        self, node: NodeId, *upstream: SyntheticArtifact, branch: str = "full"
    ) -> NodeInputEnvelope:
        scope = self.scope
        return NodeInputEnvelope(
            schema_version=VERSION,
            node_id=node,
            task_id=scope.task.task_id,
            execution_id=scope.execution_id,
            release_id=scope.pins.release_id,
            bundle_digest=scope.pins.bundle_digest,
            node_run_id=synthetic_id(
                "test-node", scope.execution_id, node.value, branch
            ),
            attempt_id=synthetic_id(
                "test-attempt", scope.execution_id, node.value, branch
            ),
            trace=scope.task.trace,
            upstream_artifacts=tuple(item.pin for item in upstream),
            node_config_ref=scope.execution_config_ref,
        )

    def context_input(self) -> ContextBuildInput:
        scope = self.scope
        (region_artifact,) = scope.catalogue.of_kind(ArtifactKind.REGION)
        region = scope.resolve(region_artifact.ref, RegionDefinition)
        definition = scope.resolve(scope.execution_config_ref, ExecutionPolicyArtifact)
        return ContextBuildInput(
            schema_version=VERSION,
            envelope=self.envelope(
                NodeId.N01, artifact(K.TASK, scope.task.task_id, scope.task)
            ),
            accepted_task=scope.task,
            media_asset=scope.media,
            device_ref=RegistryRecordRef(
                record_id=scope.task.context.device_id,
                version=VERSION,
                digest=sha256_digest(b"fixture-device"),
            ),
            camera_ref=RegistryRecordRef(
                record_id=scope.task.context.camera_id,
                version=VERSION,
                digest=sha256_digest(b"fixture-camera"),
            ),
            region_ref=RegionContentRef.model_validate(
                region_artifact.ref.model_dump()
            ),
            context_schema_ref=ContextSchemaRef.model_validate(
                {
                    **region.context_schema_ref.model_dump(),
                    "schema_dialect": "https://json-schema.org/draft/2020-12/schema",
                }
            ),
            context_provenance_policy_ref=PolicyContentRef.model_validate(
                definition.policy.context_provenance_policy_ref.model_dump()
            ),
        )


def parking_fixture(
    tmp_path: Path,
    *,
    second_profile: bool = False,
    excluded: bool = False,
    metadata: RecordValue | None = None,
    second_question: bool = False,
) -> ParkingFixture:
    source = parking_source(
        second_profile=second_profile,
        excluded=excluded,
        second_question=second_question,
    )
    report = compile_bundle(
        source, limits=LIMITS, environment=compile_environment(tmp_path)
    )
    assert report.bundle is not None, report.errors
    signed = sign_bundle(
        tmp_path, report.bundle.archive_bytes, report.bundle.bundle_ref.bundle_digest
    )
    store = LocalBundleStore(tmp_path / "ledger")
    admitted = submit(store, signed.archive, "admit-parking", signed.environment)
    receipt = admitted.receipt
    assert receipt is not None and admitted.accepted, admitted.verification.result
    verified = verify_bundle_files(
        read_archive(store.read_archive(receipt.receipt_id), LIMITS),
        max_file_bytes=LIMITS.max_file_bytes,
        max_json_depth=LIMITS.max_json_depth,
    )
    catalogue = build_catalogue(
        verified.manifest,
        verified.payloads,
        max_bytes=LIMITS.max_file_bytes,
        max_depth=LIMITS.max_json_depth,
    )
    (region,) = tuple(
        item.value
        for item in catalogue.of_kind(ArtifactKind.REGION)
        if isinstance(item.value, RegionDefinition)
    )
    host = FixtureHost(
        trusted_scope=ResolvedTaskScope(
            device_id="rover-01",
            camera_id="front-camera",
            region_id=REGION,
            scene_type=region.scene_type,
        )
    )
    manager = host.manager(store)
    register(manager, host, 1, receipt.receipt_id)
    approve(manager, host, 1, GLOBAL)
    activate(manager, host, 1, GLOBAL)
    original = submission("synthetic-parking")
    capture = original.context.clock.captured_at
    media_ref = MediaRef(media_id="synthetic-rgb", sha256=sha256_digest(RGB_IMAGE))
    submitted = TaskSubmission.model_validate(
        {
            **original.model_dump(),
            "media_ref": media_ref,
            "context": original.context.model_copy(
                update={
                    "region_id": REGION,
                    "metadata": RecordValue(
                        kind="record",
                        value={
                            "zone": StringValue(kind="string", value="fixture-parking")
                        },
                    )
                    if metadata is None
                    else metadata,
                }
            ),
        }
    )
    task = manager.accept_task(submitted, principal=host.task_client)
    media = MediaAsset(
        schema_version=VERSION,
        media_ref=media_ref,
        storage=StorageReference(
            store_id="fixture.media", object_key="original/parking.ppm"
        ),
        stage=FinalizedAsset(
            stage="finalized",
            staged_at=capture,
            verified_at=capture,
            verified_sha256=media_ref.sha256,
            object_version=VERSION,
        ),
        mime_type="image/x-portable-pixmap",
        byte_size=len(RGB_IMAGE),
        dimensions=ImageDimensions(width=8, height=8),
        color_space="RGB",
    )
    models = []
    for item in catalogue.of_kind(ArtifactKind.MODEL_BINDING):
        binding = item.value
        assert isinstance(binding, ModelBinding)
        provider = catalogue.resolve(binding.provider_policy_ref).value
        assert isinstance(provider, ProviderPolicyArtifact)
        models.append(
            ConfiguredModelPin.model_validate(
                {
                    "binding_ref": ModelBindingContentRef.model_validate(
                        item.ref.model_dump()
                    ),
                    "provider_policy_ref": binding.provider_policy_ref.model_dump(),
                    "fallback_policy_ref": binding.fallback_policy_ref.model_dump(),
                    "primary_deployment_id": provider.policy.primary_deployment_id,
                    "deployments": provider.policy.deployments,
                }
            )
        )
    tools = []
    for item in catalogue.of_kind(ArtifactKind.TOOL):
        tool = item.value
        assert isinstance(tool, ToolDefinition)
        (capability,) = tuple(
            cap
            for cap in receipt.capability_report.required
            if isinstance(cap, SupportedCapability)
            and cap.requirement.model_dump()
            == tool.implementation_requirement.model_dump()
        )
        tools.append(
            ConfiguredToolPin(
                tool_ref=ToolContentRef.model_validate(item.ref.model_dump()),
                implementation_version=capability.implementation_version,
            )
        )
    scope = SyntheticExecution(
        task=task,
        execution_id=synthetic_id("test-execution", task.task_id),
        pins=ExecutionPins(
            release_id=task.release_id,
            bundle_digest=task.bundle_digest,
            runtime_version=receipt.runtime_version,
            workflow_version=VERSION,
            model_pins=tuple(models),
            tool_pins=tuple(tools),
            input_digest=record_digest(submitted),
        ),
        media=media,
        files=verified,
        deployment=capability_snapshot().model_deployments[0].identity,
        max_file_bytes=LIMITS.max_file_bytes,
        max_json_depth=LIMITS.max_json_depth,
    )
    return ParkingFixture(scope, store, manager, host, submitted, receipt)
