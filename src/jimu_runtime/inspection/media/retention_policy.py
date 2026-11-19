"""Resolve retention policy bytes from a Task's original approved Bundle."""

from asyncio import to_thread
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from jimu_runtime.bundle.archive import BundleLimits, read_bundle
from jimu_runtime.bundle.catalogue import build_catalogue
from jimu_runtime.bundle.releases import ReleaseManager
from jimu_runtime.bundle.store import LocalBundleStore, StoreError
from jimu_runtime.domain.runtime.facts import PolicyContentRef
from jimu_runtime.domain.runtime.intake import AcceptedTask

type PolicyResolver = Callable[[AcceptedTask, PolicyContentRef], Awaitable[bytes]]


@dataclass(frozen=True, slots=True)
class HistoricalEvidencePolicies:
    releases: ReleaseManager
    bundles: LocalBundleStore
    limits: BundleLimits

    async def __call__(self, task: AcceptedTask, ref: PolicyContentRef) -> bytes:
        return (await self.resolve_many(task, (ref,)))[ref]

    async def resolve_many(
        self, task: AcceptedTask, refs: tuple[PolicyContentRef, ...]
    ) -> dict[PolicyContentRef, bytes]:
        """Read the immutable archive once for this bounded set of policy pins."""
        try:
            return await to_thread(self._resolve_many, task, refs)
        except (OSError, StoreError):
            raise ValueError("retention_policy_store_unavailable") from None

    def _resolve_many(
        self, task: AcceptedTask, refs: tuple[PolicyContentRef, ...]
    ) -> dict[PolicyContentRef, bytes]:
        activation = self.releases.get_task_activation(task)
        if (
            activation is None
            or activation.bundle_ref.bundle_digest != task.bundle_digest
        ):
            raise ValueError("retention_activation_unverified")
        release = self.releases.get_release(task.release_id, scope=activation.scope)
        if (
            release is None
            or release.bundle_ref != activation.bundle_ref
            or release.approval_receipt is None
            or release.admission_receipt is None
        ):
            raise ValueError("retention_release_unverified")
        admitted = self.bundles.get_receipt(release.admission_receipt.receipt_id)
        if (
            admitted is None
            or not admitted.accepted
            or admitted.receipt != release.admission_receipt
            or admitted.archive_size > self.limits.max_archive_bytes
        ):
            raise ValueError("retention_admission_unverified")
        package = read_bundle(
            self.bundles.read_archive(admitted.receipt.receipt_id), self.limits
        )
        if package.manifest.integrity.root_digest != task.bundle_digest:
            raise ValueError("retention_bundle_unverified")
        catalogue = build_catalogue(
            package.manifest,
            package.payloads,
            max_bytes=self.limits.max_file_bytes,
            max_depth=self.limits.max_json_depth,
        )
        return {ref: catalogue.resolve(ref).canonical_bytes for ref in refs}
