"""Trusted inventory for one physical host and one shared resource database."""

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from json import dumps
from re import fullmatch
from typing import Literal

type ResourceClass = Literal["cpu", "remote", "gpu"]


class LeaseLifetime(StrEnum):
    RUN = "run"
    RESIDENT = "resident"


class RemoteCapacity(StrEnum):
    LOCAL_TRANSPORT = "local_transport"
    PROVIDER_INFLIGHT = "provider_inflight"


def require_identity(value: str) -> None:
    if not isinstance(value, str) or not fullmatch(r"[a-zA-Z0-9_:/.-]{1,192}", value):
        raise ValueError("invalid resource identity")


def _require_budget(value: int, minimum: int = 1) -> None:
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise ValueError("invalid host resource budget")


@dataclass(frozen=True, slots=True)
class RemoteQuota:
    quota_id: str
    slots: int
    capacity: RemoteCapacity

    def __post_init__(self) -> None:
        require_identity(self.quota_id)
        _require_budget(self.slots)
        if not isinstance(self.capacity, RemoteCapacity):
            raise ValueError("invalid remote capacity kind")


@dataclass(frozen=True, slots=True)
class GPUDevice:
    device_id: str
    memory_bytes: int
    run_slots: int
    resident_slots: int

    def __post_init__(self) -> None:
        require_identity(self.device_id)
        _require_budget(self.memory_bytes)
        _require_budget(self.run_slots, 0)
        _require_budget(self.resident_slots, 0)


@dataclass(frozen=True, slots=True)
class GPUWorkerPool:
    pool_id: str
    device_id: str
    run_slots: int
    resident_slots: int

    def __post_init__(self) -> None:
        require_identity(self.pool_id)
        require_identity(self.device_id)
        _require_budget(self.run_slots, 0)
        _require_budget(self.resident_slots, 0)


@dataclass(frozen=True, slots=True)
class ResourceScope:
    scope_id: str
    resource_class: ResourceClass
    run_slots: int
    resident_slots: int = 0
    pool_or_quota_id: str | None = None

    def __post_init__(self) -> None:
        require_identity(self.scope_id)
        _require_budget(self.run_slots, 0)
        _require_budget(self.resident_slots, 0)
        if self.resource_class == "cpu":
            if self.pool_or_quota_id is not None:
                raise ValueError("CPU scope uses the host CPU budget")
        elif self.resource_class in ("remote", "gpu"):
            if self.pool_or_quota_id is None:
                raise ValueError("resource scope requires a host pool or quota")
            require_identity(self.pool_or_quota_id)
            if self.resource_class == "remote" and self.resident_slots:
                raise ValueError("remote scope does not reserve resident workloads")
        else:
            raise ValueError("invalid resource class")


@dataclass(frozen=True, slots=True)
class HostInventory:
    inventory_id: str
    lease_ttl_ms: int
    cpu_slots: int
    scopes: tuple[ResourceScope, ...]
    remote_quotas: tuple[RemoteQuota, ...] = ()
    gpu_devices: tuple[GPUDevice, ...] = ()
    gpu_worker_pools: tuple[GPUWorkerPool, ...] = ()

    def __post_init__(self) -> None:
        require_identity(self.inventory_id)
        _require_budget(self.cpu_slots, 0)
        _require_budget(self.lease_ttl_ms)
        if self.lease_ttl_ms > 86_400_000:
            raise ValueError("lease TTL exceeds one day")
        for keys in (
            tuple(scope.scope_id for scope in self.scopes),
            tuple(quota.quota_id for quota in self.remote_quotas),
            tuple(device.device_id for device in self.gpu_devices),
            tuple(pool.pool_id for pool in self.gpu_worker_pools),
        ):
            if len(keys) != len(set(keys)):
                raise ValueError("duplicate host resource identity")
        gpu_ids = {device.device_id for device in self.gpu_devices}
        pool_ids = {pool.pool_id for pool in self.gpu_worker_pools}
        quota_ids = {quota.quota_id for quota in self.remote_quotas}
        if any(pool.device_id not in gpu_ids for pool in self.gpu_worker_pools):
            raise ValueError("worker pool names an unknown physical GPU")
        for scope in self.scopes:
            if scope.resource_class == "gpu" and scope.pool_or_quota_id not in pool_ids:
                raise ValueError("GPU scope names an unknown worker pool")
            if (
                scope.resource_class == "remote"
                and scope.pool_or_quota_id not in quota_ids
            ):
                raise ValueError("remote scope names an unknown quota")

    def fingerprint(self) -> str:
        normalized = (
            self.inventory_id,
            self.lease_ttl_ms,
            self.cpu_slots,
            sorted(
                (
                    scope.scope_id,
                    scope.resource_class,
                    scope.run_slots,
                    scope.resident_slots,
                    scope.pool_or_quota_id,
                )
                for scope in self.scopes
            ),
            sorted(
                (quota.quota_id, quota.slots, quota.capacity)
                for quota in self.remote_quotas
            ),
            sorted(
                (
                    device.device_id,
                    device.memory_bytes,
                    device.run_slots,
                    device.resident_slots,
                )
                for device in self.gpu_devices
            ),
            sorted(
                (pool.pool_id, pool.device_id, pool.run_slots, pool.resident_slots)
                for pool in self.gpu_worker_pools
            ),
        )
        return sha256(dumps(normalized, separators=(",", ":")).encode()).hexdigest()
