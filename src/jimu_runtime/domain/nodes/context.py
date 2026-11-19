"""N01 builds a Snapshot or reports a required context failure."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import ContextSchemaRef
from jimu_runtime.domain.enums import NodeRunStatus, RuntimeRecordKind
from jimu_runtime.domain.nodes.envelopes import (
    NodeInputEnvelope,
    NodeIssues,
    NodeOutputEnvelope,
    check_envelope_pair,
)
from jimu_runtime.domain.primitives import Digest
from jimu_runtime.domain.runtime.applicability import ContextSnapshotRef
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.facts import PolicyContentRef
from jimu_runtime.domain.runtime.intake import (
    AcceptedTask,
    ContextSnapshot,
    RegistryRecordRef,
)
from jimu_runtime.domain.runtime.media import MediaAsset
from jimu_runtime.domain.tool_mappings import RegionContentRef


class ContextBuildInput(ContractModel):
    schema_version: Literal["1.0.0"]
    envelope: NodeInputEnvelope
    accepted_task: AcceptedTask
    media_asset: MediaAsset
    device_ref: RegistryRecordRef
    camera_ref: RegistryRecordRef
    region_ref: RegionContentRef
    context_schema_ref: ContextSchemaRef
    context_provenance_policy_ref: PolicyContentRef

    @model_validator(mode="after")
    def check_context_pins(self) -> Self:
        env, task = self.envelope, self.accepted_task
        if env.node_id is not NodeId.N01 or (
            env.task_id,
            env.release_id,
            env.bundle_digest,
        ) != (task.task_id, task.release_id, task.bundle_digest):
            raise ValueError(
                "N01 invocation must belong to the accepted Task and its pinned Release"
            )
        task.validate_media(self.media_asset)
        if (
            self.device_ref.record_id,
            self.camera_ref.record_id,
            self.region_ref.artifact_id,
        ) != (task.context.device_id, task.context.camera_id, task.context.region_id):
            raise ValueError(
                "resolved device, camera and Region must match their submitted identities"
            )
        return self


class UsableContextReport(ContractModel):
    disposition: Literal["usable"]
    optional_issues: NodeIssues


class RequiredContextFailureReport(ContractModel):
    disposition: Literal["required_context_failure"]
    required_issues: Annotated[NodeIssues, Field(min_length=1)]
    optional_issues: NodeIssues


type ContextValidationReport = Annotated[
    UsableContextReport | RequiredContextFailureReport,
    Field(discriminator="disposition"),
]


class _ContextBuildOutput(ContractModel):
    schema_version: Literal["1.0.0"]
    envelope: NodeOutputEnvelope

    @model_validator(mode="after")
    def check_n01(self) -> Self:
        if self.envelope.node_id is not NodeId.N01:
            raise ValueError("a Context build result requires the N01 node")
        if any(
            item.ref.record_kind
            not in {RuntimeRecordKind.CONTEXT_SNAPSHOT, RuntimeRecordKind.EVIDENCE}
            for item in self.envelope.output_artifacts
        ):
            raise ValueError(
                "N01 may only produce ContextSnapshot and context evidence artifacts"
            )

        return self

    def check_invocation(self, inputs: ContextBuildInput) -> None:
        check_envelope_pair(inputs.envelope, self.envelope)


class SuccessfulContextBuild(_ContextBuildOutput):
    disposition: Literal["usable"]
    validation_report: UsableContextReport
    context_snapshot_ref: ContextSnapshotRef
    context_snapshot_digest: Digest

    @model_validator(mode="after")
    def check_snapshot_ref(self) -> Self:
        if self.envelope.status is not NodeRunStatus.SUCCEEDED or not any(
            artifact.ref.record_kind is RuntimeRecordKind.CONTEXT_SNAPSHOT
            and artifact.ref.record_id == self.context_snapshot_ref.record_id
            and artifact.digest == self.context_snapshot_digest
            for artifact in self.envelope.output_artifacts
        ):
            raise ValueError("a usable context requires a successful N01 output pin")
        return self

    def check_snapshot(
        self, inputs: ContextBuildInput, snapshot: ContextSnapshot
    ) -> None:
        self.check_invocation(inputs)
        task = inputs.accepted_task
        if (
            snapshot.context_snapshot_id,
            snapshot.execution_id,
            snapshot.task_id,
            snapshot.release_id,
            snapshot.bundle_digest,
            snapshot.media_ref,
            snapshot.device_ref,
            snapshot.camera_ref,
            snapshot.region_ref,
            snapshot.context_schema_ref,
            snapshot.context_provenance_policy_ref,
            snapshot.clock,
        ) != (
            self.context_snapshot_ref.record_id,
            inputs.envelope.execution_id,
            task.task_id,
            task.release_id,
            task.bundle_digest,
            task.media_ref,
            inputs.device_ref,
            inputs.camera_ref,
            inputs.region_ref,
            inputs.context_schema_ref,
            inputs.context_provenance_policy_ref,
            task.context.clock,
        ):
            raise ValueError("ContextSnapshot must retain the exact N01 inputs")


class FailedContextBuild(_ContextBuildOutput):
    disposition: Literal["required_context_failure"]
    validation_report: RequiredContextFailureReport

    @model_validator(mode="after")
    def prohibit_snapshot(self) -> Self:
        if self.envelope.status is not NodeRunStatus.FAILED or any(
            artifact.ref.record_kind is RuntimeRecordKind.CONTEXT_SNAPSHOT
            for artifact in self.envelope.output_artifacts
        ):
            raise ValueError(
                "required context failure cannot produce a usable ContextSnapshot"
            )
        return self


type ContextBuildOutput = Annotated[
    SuccessfulContextBuild | FailedContextBuild, Field(discriminator="disposition")
]
