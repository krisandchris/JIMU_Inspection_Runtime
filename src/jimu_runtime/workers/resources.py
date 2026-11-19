"""Atomic host capacity accounting shared across independent worker processes."""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from tempfile import mkstemp
from time import monotonic, monotonic_ns
from typing import Protocol
from uuid import uuid4

from jimu_runtime.domain.policies import (
    CPUResourceRequirements,
    GPUResourceRequirements,
    RemoteResourceRequirements,
    ResourceRequirements,
)
from jimu_runtime.workers.process_identity import HostProcessIdentity, process_start
from jimu_runtime.workers.resource_inventory import (
    HostInventory,
    LeaseLifetime,
    RemoteCapacity,
    ResourceClass,
    require_identity,
)

_SCHEMA_VERSION = 4
_SCHEMA = """
CREATE TABLE inventory (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL,
    inventory_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL
) STRICT;
CREATE TABLE leases (
    lease_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    owner_generation TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    resource_class TEXT NOT NULL,
    capacity_id TEXT NOT NULL,
    pool_id TEXT,
    lifetime TEXT NOT NULL,
    memory_bytes INTEGER NOT NULL,
    scope_limit INTEGER NOT NULL,
    timeout_ms INTEGER NOT NULL,
    input_byte_limit INTEGER NOT NULL,
    output_byte_limit INTEGER NOT NULL,
    boot_id TEXT NOT NULL,
    pid_namespace INTEGER NOT NULL,
    expires_ns INTEGER NOT NULL,
    state TEXT NOT NULL,
    activity TEXT,
    pid INTEGER,
    process_start TEXT,
    tree_reaped INTEGER NOT NULL DEFAULT 0 CHECK (tree_reaped IN (0, 1)),
    dispatch_id TEXT UNIQUE
) STRICT;
CREATE TABLE provider_dispatches (
    dispatch_id TEXT PRIMARY KEY,
    lease_id TEXT NOT NULL UNIQUE
) STRICT;
CREATE INDEX lease_capacity ON leases (resource_class, capacity_id);
CREATE INDEX lease_scope ON leases (scope_id, lifetime);
"""
_OWNED = "lease_id = ? AND owner_id = ? AND owner_generation = ? AND scope_id = ?"


class ResourceUnavailable(RuntimeError):
    """The host inventory cannot admit this workload."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class LeaseState(StrEnum):
    RESERVED = "reserved"
    ARMED = "armed"
    ACTIVE = "active"
    DRAINING = "draining"


class _Activity(StrEnum):
    GROUP = "group"
    TRANSPORT = "transport"
    PROVIDER = "provider"


@dataclass(frozen=True, slots=True)
class ResourceLease:
    lease_id: str
    owner_id: str
    owner_generation: str
    scope_id: str
    resource_class: ResourceClass
    lifetime: LeaseLifetime
    timeout_ms: int
    input_byte_limit: int
    output_byte_limit: int


class ProviderTerminalVerifier(Protocol):
    def __call__(
        self, scope_id: str, dispatch_id: str, terminal_evidence_id: str
    ) -> bool:
        """Look up trusted evidence of terminal execution for this exact dispatch."""
        ...


@dataclass(frozen=True, slots=True)
class _RequestedCapacity:
    capacity_id: str
    pool_id: str | None
    slots: int
    pool_slots: int
    scope_limit: int
    memory_bytes: int = 0
    memory_capacity: int = 0


def _lease_key(lease: ResourceLease) -> tuple[str, str, str, str]:
    return lease.lease_id, lease.owner_id, lease.owner_generation, lease.scope_id


class ResourceAdmission:
    """Use a single persistent local SQLite file for the whole host inventory."""

    def __init__(
        self,
        path: Path,
        inventory: HostInventory,
        *,
        provider_terminal_verifier: ProviderTerminalVerifier | None = None,
    ) -> None:
        self._uri = path.absolute().as_uri() + "?mode=rw"
        try:
            self._identity = HostProcessIdentity.current()
        except (OSError, ValueError):
            raise ResourceUnavailable("worker_host_unverifiable") from None
        self._inventory = inventory
        self._identity_row = (
            _SCHEMA_VERSION,
            inventory.inventory_id,
            inventory.fingerprint(),
        )
        self._verify_terminal = provider_terminal_verifier
        self._scopes = {scope.scope_id: scope for scope in inventory.scopes}
        self._devices = {device.device_id: device for device in inventory.gpu_devices}
        self._pools = {pool.pool_id: pool for pool in inventory.gpu_worker_pools}
        self._quotas = {quota.quota_id: quota for quota in inventory.remote_quotas}
        with self._transaction():
            pass

    @classmethod
    def provision(
        cls,
        path: Path,
        inventory: HostInventory,
        *,
        provider_terminal_verifier: ProviderTerminalVerifier | None = None,
    ) -> "ResourceAdmission":
        """Explicit one-time bootstrap in a trusted, host-local directory."""
        fd, name = mkstemp(prefix=f".{path.name}.", suffix=".staging", dir=path.parent)
        os.close(fd)
        staged = Path(name)
        try:
            with closing(sqlite3.connect(staged)) as db, db:
                db.execute("PRAGMA synchronous=FULL")
                db.executescript(_SCHEMA)
                db.execute(
                    "INSERT INTO inventory VALUES (1, ?, ?, ?)",
                    (_SCHEMA_VERSION, inventory.inventory_id, inventory.fingerprint()),
                )
            # A hard link publishes the committed inode without replacing an
            # existing or concurrently provisioned inventory.
            os.link(staged, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            staged.unlink(missing_ok=True)
        return cls(
            path, inventory, provider_terminal_verifier=provider_terminal_verifier
        )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            with closing(sqlite3.connect(self._uri, uri=True, timeout=0.25)) as db, db:
                db.row_factory = sqlite3.Row
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT schema_version, inventory_id, fingerprint FROM inventory"
                ).fetchone()
                if row is None or tuple(row) != self._identity_row:
                    raise ResourceUnavailable("inventory_mismatch")
                yield db
        except sqlite3.DatabaseError:
            raise ResourceUnavailable("ledger_unavailable") from None

    def _capacity(
        self, requirements: ResourceRequirements, scope_id: str, lifetime: LeaseLifetime
    ) -> _RequestedCapacity:
        scope = self._scopes.get(scope_id)
        if scope is None or scope.resource_class != requirements.resource_class:
            raise ResourceUnavailable("no_host_budget")
        scope_slots = (
            scope.run_slots if lifetime == LeaseLifetime.RUN else scope.resident_slots
        )
        limit = min(scope_slots, requirements.max_concurrency)
        if isinstance(requirements, CPUResourceRequirements):
            return _RequestedCapacity("cpu", None, self._inventory.cpu_slots, 0, limit)
        if isinstance(requirements, RemoteResourceRequirements):
            quota = self._quotas.get(requirements.remote_quota_id)
            if quota is not None and quota.quota_id == scope.pool_or_quota_id:
                return _RequestedCapacity(quota.quota_id, None, quota.slots, 0, limit)
        if isinstance(requirements, GPUResourceRequirements):
            pool = self._pools.get(requirements.worker_pool_id)
            if pool is not None and pool.pool_id == scope.pool_or_quota_id:
                device = self._devices[pool.device_id]
                run = lifetime == LeaseLifetime.RUN
                return _RequestedCapacity(
                    device.device_id,
                    pool.pool_id,
                    device.run_slots if run else device.resident_slots,
                    pool.run_slots if run else pool.resident_slots,
                    limit,
                    requirements.gpu_memory_limit_bytes,
                    device.memory_bytes,
                )
        raise ResourceUnavailable("no_host_budget")

    def _has_capacity(
        self, db: sqlite3.Connection, lease: ResourceLease, capacity: _RequestedCapacity
    ) -> bool:
        physical = db.execute(
            """SELECT scope_id, pool_id, lifetime, memory_bytes
               FROM leases WHERE resource_class = ? AND capacity_id = ?""",
            (lease.resource_class, capacity.capacity_id),
        ).fetchall()
        # CPU slots are shared by resident and per-call work. Each GPU has separate
        # slot limits for resident workers and calls, but a common memory budget.
        slots_used = sum(
            lease.resource_class != "gpu" or row["lifetime"] == lease.lifetime
            for row in physical
        )
        if slots_used >= capacity.slots:
            return False
        if capacity.pool_id is not None:
            pool_used = sum(
                row["pool_id"] == capacity.pool_id and row["lifetime"] == lease.lifetime
                for row in physical
            )
            memory_used = sum(row["memory_bytes"] for row in physical)
            if (
                pool_used >= capacity.pool_slots
                or memory_used + capacity.memory_bytes > capacity.memory_capacity
            ):
                return False
        limits = [
            int(row["scope_limit"])
            for row in db.execute(
                "SELECT scope_limit FROM leases WHERE scope_id = ? AND lifetime = ?",
                (lease.scope_id, lease.lifetime),
            )
        ]
        return len(limits) < min([capacity.scope_limit, *limits])

    def reserve(
        self,
        requirements: ResourceRequirements,
        scope_id: str,
        *,
        owner_id: str,
        lifetime: LeaseLifetime = LeaseLifetime.RUN,
    ) -> ResourceLease:
        require_identity(owner_id)
        if not isinstance(lifetime, LeaseLifetime):
            raise ValueError("invalid lease lifetime")
        capacity = self._capacity(requirements, scope_id, lifetime)
        lease = ResourceLease(
            uuid4().hex,
            owner_id,
            uuid4().hex,
            scope_id,
            requirements.resource_class,
            lifetime,
            requirements.timeout_ms,
            requirements.input_byte_limit,
            requirements.output_byte_limit,
        )
        with self._transaction() as db:
            self._reconcile(db)
            if self._has_capacity(db, lease, capacity):
                db.execute(
                    """INSERT INTO leases (
                           lease_id, owner_id, owner_generation, scope_id, resource_class,
                           capacity_id, pool_id, lifetime, memory_bytes, scope_limit,
                           timeout_ms, input_byte_limit, output_byte_limit,
                           boot_id, pid_namespace, expires_ns, state
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        *_lease_key(lease),
                        lease.resource_class,
                        capacity.capacity_id,
                        capacity.pool_id,
                        lease.lifetime,
                        capacity.memory_bytes,
                        capacity.scope_limit,
                        lease.timeout_ms,
                        lease.input_byte_limit,
                        lease.output_byte_limit,
                        self._identity.boot_id,
                        self._identity.pid_namespace,
                        self._expires_at(),
                        LeaseState.RESERVED,
                    ),
                )
                return lease
        raise ResourceUnavailable("capacity_exhausted")

    def _expires_at(self) -> int:
        return monotonic_ns() + self._inventory.lease_ttl_ms * 1_000_000

    @property
    def heartbeat_interval_seconds(self) -> float:
        return self._inventory.lease_ttl_ms / 3000

    def _owned_row(
        self, db: sqlite3.Connection, lease: ResourceLease
    ) -> sqlite3.Row | None:
        row = db.execute(
            f"SELECT * FROM leases WHERE {_OWNED}", _lease_key(lease)
        ).fetchone()
        if not isinstance(row, sqlite3.Row):
            return None
        if (
            row["resource_class"],
            row["lifetime"],
            row["timeout_ms"],
            row["input_byte_limit"],
            row["output_byte_limit"],
        ) != (
            lease.resource_class,
            lease.lifetime,
            lease.timeout_ms,
            lease.input_byte_limit,
            lease.output_byte_limit,
        ):
            return None
        return row

    def state(self, lease: ResourceLease) -> LeaseState | None:
        with self._transaction() as db:
            row = self._owned_row(db, lease)
            return None if row is None else LeaseState(row["state"])

    def renew(self, lease: ResourceLease) -> bool:
        with self._transaction() as db:
            row = self._owned_row(db, lease)
            if (
                row is None
                or row["state"] == LeaseState.DRAINING
                or not self._fresh(row)
            ):
                return False
            db.execute(
                f"UPDATE leases SET expires_ns = ? WHERE {_OWNED}",
                (self._expires_at(), *_lease_key(lease)),
            )
            return True

    def _fresh(self, row: sqlite3.Row) -> bool:
        return (
            row["boot_id"] == self._identity.boot_id
            and row["pid_namespace"] == self._identity.pid_namespace
            and int(row["expires_ns"]) > monotonic_ns()
        )

    @property
    def guardian_ledger_uri(self) -> str:
        """Give the trusted guardian access to its durable completion record."""
        return self._uri

    def attach_process_group(
        self, lease: ResourceLease, pgid: int, *, deadline: float | None = None
    ) -> None:
        """Record the isolated group while its launcher gate is still closed."""
        if (
            lease.resource_class == "remote"
            or pgid <= 1
            or pgid == os.getpgrp()
            or os.getpgid(pgid) != pgid
        ):
            raise ResourceUnavailable("invalid_worker_group")
        _, started = process_start(pgid)
        self._activate(
            lease,
            _Activity.GROUP,
            pid=pgid,
            process_started=started,
            deadline=deadline,
        )

    def _remote_activity(self, lease: ResourceLease) -> _Activity:
        scope = self._scopes.get(lease.scope_id)
        if (
            lease.resource_class != "remote"
            or scope is None
            or scope.resource_class != "remote"
            or scope.pool_or_quota_id is None
        ):
            raise ResourceUnavailable("invalid_remote_lease")
        quota = self._quotas[scope.pool_or_quota_id]
        return (
            _Activity.PROVIDER
            if quota.capacity == RemoteCapacity.PROVIDER_INFLIGHT
            else _Activity.TRANSPORT
        )

    @staticmethod
    def _record_provider_dispatch(
        db: sqlite3.Connection, lease_id: str, dispatch_id: str
    ) -> None:
        inserted = db.execute(
            """INSERT INTO provider_dispatches (dispatch_id, lease_id)
               VALUES (?, ?) ON CONFLICT (dispatch_id) DO NOTHING""",
            (dispatch_id, lease_id),
        )
        if inserted.rowcount != 1:
            raise ResourceUnavailable("provider_dispatch_reused")

    def prepare_remote_send(
        self, lease: ResourceLease, dispatch_id: str, *, deadline: float | None = None
    ) -> None:
        """Bind a dispatch without authorizing any transport to start."""
        require_identity(dispatch_id)
        activity = self._remote_activity(lease)
        with self._transaction() as db:
            row = self._owned_row(db, lease)
            if (
                row is None
                or row["state"] != LeaseState.RESERVED
                or not self._fresh(row)
                or (deadline is not None and monotonic() >= deadline)
            ):
                raise ResourceUnavailable("lease_inactive")
            if activity == _Activity.PROVIDER:
                self._record_provider_dispatch(db, lease.lease_id, dispatch_id)
            db.execute(
                f"UPDATE leases SET state = ?, dispatch_id = ? WHERE {_OWNED}",
                (LeaseState.ARMED, dispatch_id, *_lease_key(lease)),
            )

    def abort_remote_before_send(self, lease: ResourceLease, dispatch_id: str) -> bool:
        """Reclaim the exact owner's armed dispatch while sending is still forbidden."""
        require_identity(dispatch_id)
        if lease.resource_class != "remote":
            return False
        with self._transaction() as db:
            row = self._owned_row(db, lease)
            if (
                row is None
                or row["state"] != LeaseState.ARMED
                or row["dispatch_id"] != dispatch_id
                or row["activity"] is not None
            ):
                return False
            self._delete(db, lease.lease_id)
            return True

    def activate_remote(
        self, lease: ResourceLease, dispatch_id: str, *, deadline: float | None = None
    ) -> None:
        """Authorize transport entry. Provider work is now uncertain until settled."""
        require_identity(dispatch_id)
        activity = self._remote_activity(lease)
        pid: int | None = None
        started: str | None = None
        if activity == _Activity.TRANSPORT:
            pid = os.getpid()
            _, started = process_start(pid)
        self._activate(
            lease,
            activity,
            pid=pid,
            process_started=started,
            dispatch_id=dispatch_id,
            deadline=deadline,
        )

    def _activate(
        self,
        lease: ResourceLease,
        activity: _Activity,
        *,
        pid: int | None = None,
        process_started: str | None = None,
        dispatch_id: str | None = None,
        deadline: float | None = None,
    ) -> None:
        with self._transaction() as db:
            row = self._owned_row(db, lease)
            if (
                row is None
                or not self._fresh(row)
                or (deadline is not None and monotonic() >= deadline)
            ):
                raise ResourceUnavailable("lease_inactive")
            armed_dispatch = (
                row["state"] == LeaseState.ARMED
                and row["activity"] is None
                and dispatch_id is not None
                and row["dispatch_id"] == dispatch_id
            )
            if row["state"] != LeaseState.RESERVED and not armed_dispatch:
                raise ResourceUnavailable("lease_inactive")
            if activity == _Activity.PROVIDER and not armed_dispatch:
                assert dispatch_id is not None
                self._record_provider_dispatch(db, lease.lease_id, dispatch_id)
            db.execute(
                f"""UPDATE leases SET state = ?, activity = ?, pid = ?,
                       process_start = ?, dispatch_id = ?, expires_ns = ?
                    WHERE {_OWNED}""",
                (
                    LeaseState.ACTIVE,
                    activity,
                    pid,
                    process_started,
                    dispatch_id,
                    self._expires_at(),
                    *_lease_key(lease),
                ),
            )

    def worker_completion_proven(self, lease: ResourceLease) -> bool:
        """Read the exact worker's durable tree-completion witness."""
        with self._transaction() as db:
            row = self._owned_row(db, lease)
            return (
                row is not None
                and str(row["activity"]) == _Activity.GROUP
                and bool(row["tree_reaped"])
            )

    def begin_draining(self, lease: ResourceLease) -> bool:
        with self._transaction() as db:
            if self._owned_row(db, lease) is None:
                return False
            self._drain(db, lease.lease_id)
            return True

    def release_reserved(self, lease: ResourceLease) -> bool:
        """Abandon an unstarted reservation without affecting a concurrent launch."""
        with self._transaction() as db:
            row = self._owned_row(db, lease)
            if row is None or row["state"] != LeaseState.RESERVED:
                return False
            self._delete(db, lease.lease_id)
            return True

    def release(self, lease: ResourceLease) -> bool:
        """Release a finished reservation, retaining unconfirmed physical work."""
        with self._transaction() as db:
            row = self._owned_row(db, lease)
            if row is None:
                return False
            if (
                self._physically_exited(row)
                or row["activity"] == _Activity.TRANSPORT
                and row["pid"] == os.getpid()
                and row["process_start"] == process_start(os.getpid())[1]
                and row["pid_namespace"] == self._identity.pid_namespace
                and row["boot_id"] == self._identity.boot_id
            ):
                self._delete(db, lease.lease_id)
                return True
            self._drain(db, lease.lease_id)
            return False

    def settle_provider(
        self, lease_id: str, dispatch_id: str, terminal_evidence_id: str
    ) -> bool:
        """Allow restart recovery using a host verifier and an exact dispatch."""
        require_identity(dispatch_id)
        require_identity(terminal_evidence_id)
        if self._verify_terminal is None:
            return False
        with self._transaction() as db:
            row = db.execute(
                """SELECT scope_id FROM leases
                   WHERE lease_id = ? AND dispatch_id = ? AND activity = ?""",
                (lease_id, dispatch_id, _Activity.PROVIDER),
            ).fetchone()
        if row is None or not self._verify_terminal(
            row["scope_id"], dispatch_id, terminal_evidence_id
        ):
            return False
        with self._transaction() as db:
            result = db.execute(
                "DELETE FROM leases WHERE lease_id = ? AND dispatch_id = ? AND activity = ?",
                (lease_id, dispatch_id, _Activity.PROVIDER),
            )
            return result.rowcount == 1

    def reconcile(self) -> tuple[str, ...]:
        """Recover reservations and terminated local work with expired/draining leases."""
        with self._transaction() as db:
            return self._reconcile(db)

    def _reconcile(self, db: sqlite3.Connection) -> tuple[str, ...]:
        reclaimed = []
        for row in db.execute(
            """SELECT * FROM leases WHERE expires_ns <= ? OR boot_id != ? OR state = ?
               ORDER BY lease_id""",
            (monotonic_ns(), self._identity.boot_id, LeaseState.DRAINING),
        ).fetchall():
            lease_id = row["lease_id"]
            if self._physically_exited(row):
                self._delete(db, lease_id)
                reclaimed.append(lease_id)
            else:
                self._drain(db, lease_id)
        return tuple(reclaimed)

    def _physically_exited(self, row: sqlite3.Row) -> bool:
        if row["activity"] is None:
            return True  # No launch may begin before its activity is recorded.
        if row["activity"] == _Activity.GROUP:
            # Setsid descendants may outlive a killed guardian and its original
            # process group. Require the guardian's durable subreaper receipt.
            if row["boot_id"] != self._identity.boot_id:
                return True
            return bool(row["tree_reaped"]) and self._identity.process_exited(
                row["boot_id"], row["pid_namespace"], row["pid"], row["process_start"]
            )
        if row["activity"] == _Activity.TRANSPORT:
            return self._identity.process_exited(
                row["boot_id"], row["pid_namespace"], row["pid"], row["process_start"]
            )
        return False  # A local timeout or restart cannot prove provider termination.

    @staticmethod
    def _drain(db: sqlite3.Connection, lease_id: str) -> None:
        db.execute(
            "UPDATE leases SET state = ? WHERE lease_id = ?",
            (LeaseState.DRAINING, lease_id),
        )

    @staticmethod
    def _delete(db: sqlite3.Connection, lease_id: str) -> None:
        db.execute("DELETE FROM leases WHERE lease_id = ?", (lease_id,))
