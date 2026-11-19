"""Host scheduling budgets are exact and preserve explicit node overrides."""

from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.workflow.scheduling import NodeBudgetOverride, SchedulingLimits
from tests.fixtures.wp03.jobs.support import budget


def test_explicit_schedule_round_trip_preserves_node_override():
    common = budget()
    specialized = budget(attempts=1, timeout=124, lease_retry=False)
    rules = SchedulingLimits(
        schema_version="1.0.0",
        version="1.2.3",
        default=common,
        overrides=(NodeBudgetOverride(node_id=NodeId.N04, budget=specialized),),
    )
    restored = SchedulingLimits.model_validate_json(rules.stable_json())
    assert restored == rules
    assert restored.for_node(NodeId.N06) == common
    assert restored.for_node(NodeId.N04) == specialized
