"""Public offline parking fixtures for the WP-03 worker.

The host supplies accepted Task/Release pins and the actual leased invocation.
These adapters run only against explicitly registered synthetic deployments.
"""

from jimu_runtime.workflow.mock_context import (
    SyntheticContextResult,
    SyntheticGateResult,
    build_context,
    build_gate,
    snapshot_artifact,
    snapshot_ref,
)
from jimu_runtime.workflow.mock_decision import (
    SyntheticAuthority,
    SyntheticDecisionResult,
    coverage_ref,
    finish_decision,
    mandatory_paths,
    stage_decision,
)
from jimu_runtime.workflow.mock_facts import (
    SyntheticEvidence,
    SyntheticObservationResult,
    SyntheticToolResult,
    object_artifact,
    observe,
    prepare_observation,
    run_tool_plan,
)
from jimu_runtime.workflow.mock_perception import (
    SyntheticDiscoveryResult,
    SyntheticGroundingCase,
    SyntheticGroundingResult,
    discover,
    ground,
    original_coordinate_system,
    skipped_discovery,
)
from jimu_runtime.workflow.mock_support import (
    SyntheticArtifact,
    SyntheticExecution,
    artifact,
    record_digest,
    synthetic_id,
)

__all__ = [
    "SyntheticArtifact",
    "SyntheticAuthority",
    "SyntheticContextResult",
    "SyntheticDecisionResult",
    "SyntheticDiscoveryResult",
    "SyntheticEvidence",
    "SyntheticExecution",
    "SyntheticGateResult",
    "SyntheticGroundingCase",
    "SyntheticGroundingResult",
    "SyntheticObservationResult",
    "SyntheticToolResult",
    "artifact",
    "build_context",
    "build_gate",
    "coverage_ref",
    "discover",
    "finish_decision",
    "ground",
    "mandatory_paths",
    "object_artifact",
    "observe",
    "original_coordinate_system",
    "prepare_observation",
    "record_digest",
    "run_tool_plan",
    "skipped_discovery",
    "snapshot_artifact",
    "snapshot_ref",
    "stage_decision",
    "synthetic_id",
]
