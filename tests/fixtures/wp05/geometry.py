"""Helpers for exercising the signed scene and real supervised CPU child."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256

from examples.nonmotor_vehicle_illegal_parking.compilable.lifecycle import (
    ACTOR,
    PROJECT,
    exercise_lifecycle,
)
from examples.nonmotor_vehicle_illegal_parking.compilable.scene import DIRECTORY, STEPS
from jimu_runtime.bundle.semantic_inputs import require_definition
from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.domain.definition_enums import EvidenceType
from jimu_runtime.domain.definitions.capabilities import ProfileDefinition
from jimu_runtime.domain.definitions.geometry import GeometryResourceDefinition
from jimu_runtime.domain.definitions.ontology import RegionDefinition
from jimu_runtime.domain.definitions.resources import StaticAssetDescriptor
from jimu_runtime.domain.definitions.tools import ToolDefinition
from jimu_runtime.domain.enums import (
    ArtifactKind,
    AttemptStatus,
    ExecutionStatus,
    NodeRunStatus,
)
from jimu_runtime.domain.enums import (
    RuntimeRecordKind as K,
)
from jimu_runtime.domain.nodes.envelopes import NodeInputEnvelope
from jimu_runtime.domain.nodes.observation import ObjectImageView
from jimu_runtime.domain.nodes.tool_execution import ToolNodeInput
from jimu_runtime.domain.policies import EvidencePolicyArtifact
from jimu_runtime.domain.runtime.applicability import ContextSnapshotRef
from jimu_runtime.domain.runtime.authority import ToolStepSelection
from jimu_runtime.domain.runtime.evidence import (
    EvidenceAsset,
    ExecutionEvidenceScope,
    ObjectEvidenceScope,
    OriginalImageEvidence,
    RetentionPolicyRef,
    TaskEvidenceAccess,
)
from jimu_runtime.domain.runtime.executions import (
    Attempt,
    AttemptLink,
    ExecutionRecord,
    NodeId,
    NodeRun,
    PinnedRuntimeInput,
)
from jimu_runtime.domain.runtime.facts import (
    AttemptRef,
    ModelInvocationRef,
    NodeRunRef,
    PolicyContentRef,
    ProfileContentRef,
    QuestionSelectionRef,
    ToolExecutionRef,
)
from jimu_runtime.domain.runtime.media import (
    BBoxXYXY,
    CoordinateSystem,
    FinalizedAsset,
    ImageDimensions,
    MediaAsset,
    StorageReference,
)
from jimu_runtime.domain.runtime.perception import (
    GroundingRecordRef,
    ModelGroundingProvenance,
    ObjectRecord,
)
from jimu_runtime.domain.tool_mappings import RegionContentRef
from jimu_runtime.domain.validation.authority_inputs import NodeAttemptOwner
from jimu_runtime.inspection.geometry import (
    CPU_ARTIFACT,
    VERSION,
    EvidenceBytes,
    GeometryInput,
    MaskInput,
    synthetic_cpu_implementations,
    verify_parking_area,
)
from jimu_runtime.tools.executor import AncestorToolEvidence, PlanStepCall, ToolExecutor
from jimu_runtime.tools.registry import ToolWorkerRegistry
from jimu_runtime.tools.step_attempts import StepCompletion, StepResult
from jimu_runtime.workers.resource_inventory import HostInventory, ResourceScope
from jimu_runtime.workers.resources import ResourceAdmission
from jimu_runtime.workflow.mock_facts import object_artifact


class GeometryFixture:
    def __init__(self, tmp_path, *, media_bytes=None, allowlisted=True):
        scene = exercise_lifecycle(tmp_path)
        self.resolved = scene.execution
        self.task = scene.task
        self.payload = (
            (DIRECTORY / "assets" / "synthetic-frame.png").read_bytes()
            if media_bytes is None
            else media_bytes
        )
        if media_bytes is not None:
            self.task = self.task.model_copy(
                update={
                    "media_ref": self.task.media_ref.model_copy(
                        update={"sha256": sha256(self.payload).hexdigest()}
                    )
                }
            )
        self.coordinates = CoordinateSystem(
            coordinate_system_id="geometry-fixture.original",
            frame="original_image",
            dimensions=ImageDimensions(width=64, height=48),
        )
        self.bbox = BBoxXYXY(xyxy=(40.0, 23.0, 52.0, 33.0))
        at = datetime.now(UTC)
        self.execution = ExecutionRecord(
            schema_version=VERSION,
            execution_id="geometry-fixture",
            task_id=self.task.task_id,
            pins=self.resolved.pins,
            trace=self.task.trace,
            status=ExecutionStatus.RUNNING,
            created_at=at,
            started_at=at,
            model_invocation_refs=(),
        )
        self.media = MediaAsset(
            schema_version=VERSION,
            media_ref=self.task.media_ref,
            storage=StorageReference(store_id="fixture", object_key="test/image.png"),
            stage=self.finalized(at, self.task.media_ref.sha256),
            color_space="RGB",
            mime_type="image/jpeg"
            if self.payload.startswith(b"\xff\xd8")
            else "image/png",
            byte_size=len(self.payload),
            dimensions=self.coordinates.dimensions,
        )
        catalog = self.resolved.catalogue
        (profile_entry,) = catalog.of_kind(ArtifactKind.PROFILE)
        self.profile = require_definition(catalog, profile_entry.ref, ProfileDefinition)
        self.profile_ref = ProfileContentRef.model_validate(
            profile_entry.ref.model_dump()
        )
        self.object = ObjectRecord(
            schema_version=VERSION,
            object_id="geometry-object",
            execution_id=self.execution.execution_id,
            profile_ref=self.profile_ref,
            question_selection_ref=QuestionSelectionRef(
                record_kind=K.QUESTION_SELECTION_RECORD, record_id="geometry-questions"
            ),
            effective_question_refs=self.profile.question_refs,
            entity_ref=self.profile.entity_ref,
            grounding_record_ref=GroundingRecordRef(
                record_kind=K.GROUNDING_RECORD, record_id="geometry-grounding"
            ),
            source_media_ref=self.media.media_ref,
            coordinate_system=self.coordinates,
            bbox=self.bbox,
            grounding_producer=ModelGroundingProvenance(
                kind="model",
                model_binding_ref=self.profile.grounding_binding.model_binding_ref,
                model_invocation_ref=ModelInvocationRef(
                    record_kind=K.MODEL_INVOCATION, record_id="geometry-grounding-model"
                ),
            ),
            visibility="visible",
            localization_status="localized",
            evidence_refs=(),
        )
        (region_entry,) = catalog.of_kind(ArtifactKind.REGION)
        self.region_ref = RegionContentRef.model_validate(region_entry.ref.model_dump())
        self.region = region = require_definition(
            catalog, self.region_ref, RegionDefinition
        )
        self.region_geometry = geometry = require_definition(
            catalog, region.geometry_ref, GeometryResourceDefinition
        )
        self.region_source = source = require_definition(
            catalog, geometry.provenance.source_asset_ref, StaticAssetDescriptor
        )
        self.region_bytes = self.resolved.files.payloads[source.data_path]
        self.parking_area = self.verify_region(self.region_bytes)
        self.tools = {
            pin.tool_ref.artifact_id: require_definition(
                catalog, pin.tool_ref, ToolDefinition
            )
            for pin in self.resolved.pins.tool_pins
        }
        self.proofs = {
            tool.implementation_requirement.capability_id: TrustPin(
                identity=tool.implementation_requirement.capability_id,
                version=VERSION,
                sha256=sha256(CPU_ARTIFACT.read_bytes()).hexdigest(),
            )
            for tool in self.tools.values()
        }
        resources = ResourceAdmission.provision(
            tmp_path / "geometry-resources.sqlite3",
            HostInventory(
                "wp05-geometry",
                lease_ttl_ms=8000,
                cpu_slots=1,
                scopes=(ResourceScope("wp05-synthetic", "cpu", 1),),
            ),
        )
        self.executor = ToolExecutor(
            ToolWorkerRegistry(
                synthetic_cpu_implementations(
                    resources,
                    "wp05-synthetic",
                    "wp05-geometry",
                    tuple(self.tools.values()),
                    self.proofs,
                    synthetic_image_digests=frozenset({self.media.media_ref.sha256})
                    if allowlisted
                    else frozenset(),
                )
            )
        )
        self.sidecars = {}
        self.stdout = {}
        self.requests = {}
        self.calls = {}
        self.original = EvidenceBytes(
            EvidenceAsset(
                schema_version=VERSION,
                evidence_id="geometry-original",
                evidence_type=EvidenceType.ORIGINAL_IMAGE,
                scope=ExecutionEvidenceScope(
                    scope="execution", execution_id=self.execution.execution_id
                ),
                storage=self.media.storage,
                sha256=self.media.media_ref.sha256,
                mime_type=self.media.mime_type,
                byte_size=self.media.byte_size,
                stage=self.media.stage,
                spatial=OriginalImageEvidence(
                    kind="original_image",
                    original_media_ref=self.media.media_ref,
                    coordinate_system=self.coordinates,
                ),
                producer_id="fixture-intake",
                producer_version=VERSION,
                access_scope=self.access(),
                retention=self.retention(STEPS[0]),
                created_at=self.media.stage.staged_at,
            ),
            self.payload,
        )

    def verify_region(self, content):
        return verify_parking_area(
            catalogue=self.resolved.catalogue,
            region_ref=self.region_ref,
            region=self.region,
            geometry_ref=self.region.geometry_ref,
            geometry=self.region_geometry,
            source=self.region_source,
            source_bytes=content,
            coordinates=self.coordinates,
        )

    def access(self):
        return TaskEvidenceAccess(
            scope="task", project_id=PROJECT, task_id=self.task.task_id
        )

    def retention(self, step):
        tool = self.tools[step]
        policy = require_definition(
            self.resolved.catalogue, tool.evidence_policy_ref, EvidencePolicyArtifact
        ).policy
        return RetentionPolicyRef(
            evidence_policy_ref=PolicyContentRef.model_validate(
                tool.evidence_policy_ref.model_dump()
            ),
            retention_class=policy.retention_class,
        )

    @staticmethod
    def finalized(at, digest):
        return FinalizedAsset(
            stage="finalized",
            staged_at=at,
            verified_at=at,
            verified_sha256=digest,
            object_version=VERSION,
        )

    def _evidence(
        self, dispatch, evidence_id, evidence_type, mime_type, content, spatial
    ):
        at = datetime.now(UTC)
        digest = sha256(content).hexdigest()
        return EvidenceAsset(
            schema_version=VERSION,
            evidence_id=evidence_id,
            evidence_type=evidence_type,
            scope=ObjectEvidenceScope(
                scope="object",
                execution_id=dispatch.execution_id,
                object_ref=dispatch.object_ref,
                profile_ref=dispatch.profile_ref,
            ),
            storage=StorageReference(
                store_id="fixture", object_key="test/" + evidence_id.replace(":", ".")
            ),
            sha256=digest,
            mime_type=mime_type,
            byte_size=len(content),
            stage=self.finalized(at, digest),
            spatial=spatial,
            producer_id=dispatch.tool_ref.artifact_id,
            producer_version=dispatch.implementation_version,
            access_scope=self.access(),
            retention=self.retention(dispatch.tool_ref.artifact_id),
            created_at=at,
        )

    async def stage(self, dispatch, sidecar):
        asset = self._evidence(
            dispatch,
            f"{dispatch.tool_execution_id}.{sidecar.slot}",
            sidecar.evidence_type,
            sidecar.mime_type,
            sidecar.content,
            sidecar.spatial,
        )
        self.sidecars[asset.evidence_id] = sidecar.content
        return asset

    async def record_response(self, dispatch, content):
        from jimu_runtime.domain.runtime.evidence import NonSpatialEvidence

        self.stdout[dispatch.tool_ref.artifact_id] = content
        return self._evidence(
            dispatch,
            f"{dispatch.tool_execution_id}.stdout",
            EvidenceType.TOOL_OUTPUT,
            "application/json",
            content,
            NonSpatialEvidence(kind="non_spatial"),
        )

    async def call(
        self,
        step,
        *,
        inputs=None,
        session=None,
        obj=None,
        ancestor=True,
        proof=None,
        cpu_exchange=None,
    ):
        obj = self.object if obj is None else obj
        artifact_pin = object_artifact(obj).pin
        object_pin = PinnedRuntimeInput(
            ref=artifact_pin.ref, digest=artifact_pin.digest
        )
        node_ref = NodeRunRef(record_kind=K.NODE_RUN, record_id=f"child.{step}")
        attempt_ref = AttemptRef(record_kind=K.ATTEMPT, record_id=f"attempt.{step}")
        node = NodeRun(
            schema_version=VERSION,
            node_run_id=node_ref.record_id,
            execution_id=self.execution.execution_id,
            node_id=NodeId.N05,
            trace=self.execution.trace,
            config_ref=self.resolved.policy_ref,
            upstream_inputs=(object_pin,),
            input_digest=self.resolved.pins.input_digest,
            status=NodeRunStatus.RUNNING,
            created_at=self.execution.created_at,
            started_at=self.execution.started_at,
            attempt_history=(AttemptLink(attempt_ref=attempt_ref, attempt_number=1),),
            output_refs=(),
        )
        attempt = Attempt(
            schema_version=VERSION,
            attempt_id=attempt_ref.record_id,
            execution_id=self.execution.execution_id,
            owner=NodeAttemptOwner(owner_kind="node", node_run_ref=node_ref),
            attempt_number=1,
            status=AttemptStatus.RUNNING,
            created_at=self.execution.created_at,
            started_at=self.execution.started_at,
        )
        site = PlanStepCall(
            ToolNodeInput(
                schema_version=VERSION,
                envelope=NodeInputEnvelope(
                    schema_version=VERSION,
                    node_id=NodeId.N05,
                    task_id=self.task.task_id,
                    execution_id=self.execution.execution_id,
                    release_id=self.task.release_id,
                    bundle_digest=self.task.bundle_digest,
                    node_run_id=node.node_run_id,
                    attempt_id=attempt.attempt_id,
                    trace=self.execution.trace,
                    upstream_artifacts=(object_pin,),
                    node_config_ref=self.resolved.policy_ref,
                ),
                object_record=obj,
                image_view=ObjectImageView(roi=obj.bbox, transforms=()),
                context_snapshot_ref=ContextSnapshotRef(
                    record_kind=K.CONTEXT_SNAPSHOT, record_id="geometry-context"
                ),
                tool_plan_ref=self.profile.tool_plan_ref,
                validated_observation_fact_refs=(),
            ),
            step,
        )
        self.calls[step] = site, node, attempt
        if inputs is None:
            inputs = {"image": self.media.media_ref.model_dump(mode="json")}
            if step == STEPS[0]:
                inputs["bbox"] = obj.bbox.model_dump(mode="json")
        if session is None:
            session = GeometryInput(
                self.media, self.original, self.stage, object_bbox=obj.bbox
            )

        async def clearance(dispatch, lease):
            self.requests[step] = (dispatch.request, dispatch.physical_request, lease)
            return True

        return await self.executor.execute(
            resolved=self.resolved,
            execution=self.execution,
            node=node,
            attempt=attempt,
            call=site,
            media=self.media,
            input_fields=inputs,
            implementation_proof=proof or self.proofs[f"synthetic.cpu.{step}"],
            deadline=datetime.now(UTC) + timedelta(seconds=10),
            before_dispatch=clearance,
            record_response=self.record_response,
            ancestor_evidence=session.ancestor_evidence if ancestor else (),
            cpu_exchange=cpu_exchange or session.exchange(),
        )

    def selected_mask(self, step, result):
        tool = result.execution
        (fact,) = result.facts
        asset = next(
            item for item in result.evidence if item.evidence_type is EvidenceType.MASK
        )
        completion = StepCompletion(
            result=StepResult(
                execution=tool, facts=result.facts, evidence=result.evidence
            ),
            attempt_status="succeeded",
            selection=ToolStepSelection(
                schema_version=VERSION,
                selection_id=f"selection.{step}",
                execution_id=tool.execution_id,
                profile_ref=tool.profile_ref,
                object_id=tool.object_ref.record_id,
                scope="object",
                selection_group="tool_step",
                tool_plan_ref=self.profile.tool_plan_ref,
                step_id=step,
                selected_attempt_id=tool.attempt_ref.record_id,
                selected_result_ref=ToolExecutionRef(
                    record_kind=K.TOOL_EXECUTION, record_id=tool.tool_execution_id
                ),
                selected_fact_refs=tool.generated_fact_refs,
                reason="validated_success",
                authority_revision=1,
                actor_ref=ACTOR,
                created_at=tool.completed_at,
            ),
        )
        return MaskInput(
            AncestorToolEvidence((fact.fact_key,), completion, asset),
            self.sidecars[asset.evidence_id],
        )

    async def masks(self):
        wheel = await self.call(STEPS[0])
        tactile = await self.call(STEPS[1])
        assert (wheel.execution.status, tactile.execution.status) == (
            "succeeded",
            "succeeded",
        ), (wheel, tactile)
        return self.selected_mask(STEPS[0], wheel), self.selected_mask(
            STEPS[1], tactile
        )

    def geometry(self, masks):
        inputs = {
            item.proof.input_path[0]: item.proof.completion.result.facts[
                0
            ].value.model_dump(mode="json")
            for item in masks
        }
        inputs["parking_area"] = self.parking_area.model_dump(mode="json")
        return inputs, GeometryInput(
            self.media,
            self.original,
            self.stage,
            parking_area=self.parking_area,
            masks=masks,
        )
