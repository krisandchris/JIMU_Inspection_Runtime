"""Versioned Bad Case attribution for completed offline evaluation reports."""

from typing import Literal, Self

from pydantic import model_validator

from jimu_runtime.domain.definition_support import (
    Description,
    UTCDateTime,
)
from jimu_runtime.domain.primitives import RecordId
from jimu_runtime.domain.runtime.authority import ActorRef
from jimu_runtime.domain.runtime.executions import PinnedRuntimeInput
from jimu_runtime.domain.runtime.facts import EvidenceRef
from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.evaluation.contracts import (
    Entries,
    SnapshotPin,
    VersionedSnapshot,
    require_successor_version,
    require_unique,
)
from jimu_runtime.evaluation.scoring import EvaluationReport


class AttributionEvidence(PinnedRuntimeInput):
    ref: EvidenceRef


class BadCaseAttribution(VersionedSnapshot):
    report: SnapshotPin
    scope: IdempotencyScope
    sample_id: RecordId
    attributed_stage: Literal[
        "dataset",
        "discovery",
        "grounding",
        "question",
        "geometry",
        "rule",
        "task",
        "inconclusive",
    ]
    rationale: Description
    actor: ActorRef
    recorded_at: UTCDateTime
    evidence: Entries[AttributionEvidence]
    supersedes: SnapshotPin | None

    @model_validator(mode="after")
    def attribution_integrity(self) -> Self:
        if self.actor.actor_kind != "human":
            raise ValueError("attribution requires a human actor recorded by the host")
        if self.supersedes is not None:
            if self.supersedes.record_id != self.record_id:
                raise ValueError("attribution must append a revision of this case")
            require_successor_version(self.version, self.supersedes.version)
        require_unique((row.ref for row in self.evidence), "attribution evidence")
        return self

    def validate_source(self, report: EvaluationReport) -> None:
        checked = EvaluationReport.model_validate(report)
        sample = next(
            (row for row in checked.samples if row.sample_id == self.sample_id), None
        )
        if (
            checked.pin() != self.report
            or checked.scope != self.scope
            or sample is None
        ):
            raise ValueError("attribution must address the exact report and sample")
        evidence = {(item.ref, item.digest) for item in sample.evidence}
        if any((row.ref, row.digest) not in evidence for row in self.evidence):
            raise ValueError(
                "attribution evidence must belong to the scored sample inventory"
            )


def revise_attribution(
    report: EvaluationReport,
    previous: BadCaseAttribution,
    revision: BadCaseAttribution,
) -> BadCaseAttribution:
    """Validate a correction against the exact prior attribution without mutation."""
    previous = BadCaseAttribution.model_validate(previous)
    revision = BadCaseAttribution.model_validate(revision)
    previous.validate_source(report)
    revision.validate_source(report)
    if (
        revision.supersedes != previous.pin()
        or revision.sample_id != previous.sample_id
        or revision.recorded_at < previous.recorded_at
    ):
        raise ValueError(
            "attribution revision must pin the same case and follow its prior record"
        )
    return revision
