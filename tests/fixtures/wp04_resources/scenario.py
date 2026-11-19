"""Small host quotas and reproducible trusted commands for live Linux tests."""

from jimu_runtime.domain.definition_enums import PolicyKind
from jimu_runtime.domain.policies import (
    CPUResourceRequirements,
    GPUResourceRequirements,
    RemoteResourceRequirements,
)
from jimu_runtime.workers.resource_inventory import HostInventory, ResourceScope

# Fixture limits apply to the declared command, independent of host capacity.
BUDGET = {
    "policy_kind": PolicyKind.RESOURCE_REQUIREMENTS,
    "max_concurrency": 10,
    "timeout_ms": 10_000,
    "input_byte_limit": 256 * 1024,
    "output_byte_limit": 256 * 1024,
}


def cpu(*, concurrency: int = 10) -> CPUResourceRequirements:
    return CPUResourceRequirements.model_validate(
        {**BUDGET, "max_concurrency": concurrency, "resource_class": "cpu"}
    )


def gpu(pool: str, memory: int) -> GPUResourceRequirements:
    return GPUResourceRequirements.model_validate(
        {
            **BUDGET,
            "resource_class": "gpu",
            "worker_pool_id": pool,
            "gpu_memory_limit_bytes": memory,
        }
    )


def remote(quota_id: str) -> RemoteResourceRequirements:
    return RemoteResourceRequirements.model_validate(
        {**BUDGET, "resource_class": "remote", "remote_quota_id": quota_id}
    )


def worker_inventory(*, ttl: int = 1000, slots: int = 1) -> HostInventory:
    return HostInventory(
        "isolated.host", ttl, slots, (ResourceScope("test.cpu", "cpu", 4),)
    )
