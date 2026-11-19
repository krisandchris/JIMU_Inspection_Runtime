"""Dispatch configured perception nodes through the lease-fenced Worker gateway."""

from dataclasses import dataclass

from jimu_runtime.domain.definitions.capabilities import ModelBindingContentRef
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.gateway.bridge import CapabilityGateway
from jimu_runtime.inspection.perception.candidates import (
    CandidateModelResult,
    predict_candidates,
)
from jimu_runtime.inspection.perception.context import build_context
from jimu_runtime.inspection.perception.discovery import discover
from jimu_runtime.inspection.perception.grounding import StageMarkedFrame, ground
from jimu_runtime.inspection.perception.model import (
    ImageLoader,
    MarkedImageLoader,
    ModelAccess,
)
from jimu_runtime.inspection.perception.observation import StageRequestEvidence, observe
from jimu_runtime.workflow.worker import AdapterRejected, ClaimedNode
from jimu_runtime.workflow.worker_nodes import NodeResult, invocation_envelope


@dataclass(frozen=True, slots=True)
class ConfiguredPerception:
    """Trusted host callback bindings for the release-scoped N02–N04 adapters."""

    load_image: ImageLoader
    stage_request_evidence: StageRequestEvidence
    resource_scope_id: str
    stage_marked_frame: StageMarkedFrame | None = None
    load_marked_image: MarkedImageLoader | None = None

    async def execute(
        self, claimed: ClaimedNode, gateway: CapabilityGateway
    ) -> NodeResult:
        envelope = invocation_envelope(
            claimed.opened, claimed.branch, claimed.node, claimed.attempt
        )
        access = ModelAccess(
            self.load_image, self.resource_scope_id, self.load_marked_image
        )
        match claimed.node.node_id:
            case NodeId.N01:
                return build_context(claimed, envelope)
            case NodeId.N02:
                return await discover(claimed, envelope, gateway, access)
            case NodeId.N03:
                return await ground(
                    claimed, envelope, gateway, access, self.stage_marked_frame
                )
            case NodeId.N04:
                return await observe(
                    claimed,
                    envelope,
                    gateway,
                    access,
                    self.stage_request_evidence,
                )
            case _:
                raise AdapterRejected("perception.unsupported_node")

    async def predict_candidates(
        self,
        claimed: ClaimedNode,
        gateway: CapabilityGateway,
        binding_ref: ModelBindingContentRef,
    ) -> CandidateModelResult:
        return await predict_candidates(
            claimed,
            gateway,
            ModelAccess(self.load_image, self.resource_scope_id),
            binding_ref,
        )
