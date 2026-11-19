"""Independent truth snapshots backed by append-only human reviews."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import (
    Description,
    ReviewReportRef,
    UTCDateTime,
)
from jimu_runtime.domain.primitives import Digest, RecordId, Version
from jimu_runtime.domain.runtime.authority import ActorRef
from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.evaluation.contracts import (
    DatasetManifest,
    Entries,
    ReviewState,
    SampleLabels,
    Samples,
    SnapshotPin,
    VersionedSnapshot,
    canonical_digest,
    require_successor_version,
    require_unique,
)


class ReviewSubject(ContractModel):
    review_id: RecordId
    dataset: SnapshotPin
    scope: IdempotencyScope
    sample_id: RecordId
    catalogue_digest: Digest
    base_truth: SnapshotPin | None


class ReviewVote(ContractModel):
    schema_version: Literal["1.0.0"]
    vote_id: RecordId
    subject: ReviewSubject
    actor: ActorRef
    recorded_at: UTCDateTime
    rationale: Description
    labels: SampleLabels | None  # A null vote explicitly abstains.


class Adjudication(ContractModel):
    schema_version: Literal["1.0.0"]
    adjudication_id: RecordId
    subject: ReviewSubject
    review_revision: SnapshotPin
    actor: ActorRef
    recorded_at: UTCDateTime
    rationale: Description
    selected_vote_id: RecordId | None
    selected_vote_digest: Digest | None  # Null records an affirmative abstention.


class ReviewHistory(VersionedSnapshot):
    dataset: SnapshotPin
    scope: IdempotencyScope
    split: Literal["holdout"]
    sample_id: RecordId
    base_truth: SnapshotPin | None
    catalogue_digest: Digest
    previous: SnapshotPin | None
    votes: Entries[ReviewVote]
    adjudication: Adjudication | None

    @property
    def subject(self) -> ReviewSubject:
        return ReviewSubject(
            review_id=self.record_id,
            dataset=self.dataset,
            scope=self.scope,
            sample_id=self.sample_id,
            catalogue_digest=self.catalogue_digest,
            base_truth=self.base_truth,
        )

    @model_validator(mode="after")
    def review_integrity(self) -> Self:
        if self.previous is not None:
            if self.previous.record_id != self.record_id:
                raise ValueError("previous review must pin this history")
            require_successor_version(self.version, self.previous.version)
        require_unique((vote.vote_id for vote in self.votes), "vote ID")
        require_unique((vote.actor for vote in self.votes), "review voter")
        if any(vote.subject != self.subject for vote in self.votes):
            raise ValueError("votes must address this exact review and sample")
        if any(vote.actor.actor_kind != "human" for vote in self.votes):
            raise ValueError("a vote requires a human actor recorded by the host")
        if tuple(vote.recorded_at for vote in self.votes) != tuple(
            sorted(vote.recorded_at for vote in self.votes)
        ):
            raise ValueError("votes must be in append order")
        if self.adjudication is not None:
            self._check_adjudication(self.adjudication)
        return self

    def _check_adjudication(self, result: Adjudication) -> None:
        if result.subject != self.subject or result.review_revision != self.previous:
            raise ValueError(
                "adjudication must address this exact review revision and sample"
            )
        if (result.selected_vote_id is None) != (result.selected_vote_digest is None):
            raise ValueError(
                "adjudication must supply both selected vote ID and digest"
            )
        if not self.votes or result.recorded_at < self.votes[-1].recorded_at:
            raise ValueError("adjudication must follow the recorded votes")
        if result.actor.actor_kind != "human" or result.actor in {
            vote.actor for vote in self.votes
        }:
            raise ValueError("adjudication requires an independent human actor")
        if result.selected_vote_id is not None and not any(
            vote.vote_id == result.selected_vote_id
            and vote.labels is not None
            and canonical_digest(vote) == result.selected_vote_digest
            for vote in self.votes
        ):
            raise ValueError("adjudication must select a recorded substantive vote")

    def _append(
        self,
        *,
        version: Version,
        votes: tuple[ReviewVote, ...],
        adjudication: Adjudication | None,
    ) -> Self:
        if self.adjudication is not None:
            raise ValueError(
                "resolved review is closed; open a new review against revised truth"
            )
        return type(self).model_validate(
            {
                **self.model_dump(mode="python"),
                "version": version,
                "previous": self.pin(),
                "votes": votes,
                "adjudication": adjudication,
            }
        )

    def append_vote(self, vote: ReviewVote, *, version: Version) -> Self:
        return self._append(
            version=version, votes=(*self.votes, vote), adjudication=None
        )

    def adjudicate(self, result: Adjudication, *, version: Version) -> Self:
        return self._append(version=version, votes=self.votes, adjudication=result)

    def _disposition(self) -> tuple[ReviewState, SampleLabels | None]:
        result = self.adjudication
        if result is not None and result.selected_vote_id is not None:
            vote = next(
                vote for vote in self.votes if vote.vote_id == result.selected_vote_id
            )
            return "labeled", vote.labels
        substantive = [vote.labels for vote in self.votes if vote.labels is not None]
        if result is not None or (self.votes and not substantive):
            return "abstained", None
        if substantive:
            first_claim = _label_claim(substantive[0])
            if any(_label_claim(labels) != first_claim for labels in substantive[1:]):
                return "conflicted", None
        return "unlabeled", None

    def reviewed_entry(self, review_ref: ReviewReportRef) -> TruthEntry:
        """Project recorded adjudication or unresolved review into an independent label."""
        state, labels = self._disposition()
        return TruthEntry(
            sample_id=self.sample_id,
            state=state,
            labels=labels,
            review_ref=review_ref,
            review=self,
        )


def _label_claim(labels: SampleLabels) -> tuple[object, ...]:
    # Annotators may use different local label IDs and ordering for the same claims.
    objects = sorted(
        canonical_digest(
            obj.model_copy(
                update={
                    "label_id": "label",
                    "questions": tuple(
                        sorted(
                            obj.questions,
                            key=lambda row: canonical_digest(row.question_ref),
                        )
                    ),
                    "geometry": tuple(
                        sorted(
                            obj.geometry,
                            key=lambda row: canonical_digest(row.fact_schema_ref),
                        )
                    ),
                    "rules": tuple(
                        sorted(obj.rules, key=lambda row: canonical_digest(row.path))
                    ),
                }
            )
        )
        for obj in labels.objects
    )
    profiles = labels.present_profiles
    return (
        None if profiles is None else frozenset(profiles),
        labels.task_violation,
        frozenset(labels.complete_grounding_profiles),
        None
        if labels.violation_categories is None
        else frozenset(labels.violation_categories),
        objects,
    )


class TruthEntry(ContractModel):
    sample_id: RecordId
    state: ReviewState
    labels: SampleLabels | None = None
    review_ref: ReviewReportRef | None = None

    review: ReviewHistory | None = None

    @model_validator(mode="after")
    def reviewed_truth(self) -> Self:
        if self.review is None:
            if (
                self.state != "unlabeled"
                or self.labels is not None
                or self.review_ref is not None
            ):
                raise ValueError("resolved truth requires recorded human review")
            return self
        if (
            self.review.sample_id != self.sample_id
            or self.review_ref is None
            or self.review_ref.sha256 != canonical_digest(self.review)
            or self.review_ref.version != self.review.version
        ):
            raise ValueError("truth must pin the exact review revision and sample")
        if (self.state, self.labels) != self.review._disposition():
            raise ValueError("truth must match the recorded review disposition")
        return self


class TruthSnapshot(VersionedSnapshot):
    dataset: SnapshotPin
    scope: IdempotencyScope
    split: Literal["holdout"]
    prior: SnapshotPin | None
    catalogue_digest: Digest
    entries: Samples[TruthEntry]

    @model_validator(mode="after")
    def truth_identity(self) -> Self:
        require_unique((item.sample_id for item in self.entries), "truth sample ID")
        if self.prior is not None:
            if self.prior.record_id != self.record_id:
                raise ValueError("prior truth must pin this snapshot identity")
            require_successor_version(self.version, self.prior.version)
        for entry in self.entries:
            review = entry.review
            if review is None:
                continue
            if (
                review.dataset != self.dataset
                or review.scope != self.scope
                or review.split != self.split
                or review.catalogue_digest != self.catalogue_digest
            ):
                raise ValueError(
                    "review must match the exact truth dataset, scope and split"
                )
            if review.base_truth is not None:
                if review.base_truth.record_id != self.record_id:
                    raise ValueError("review base must pin this truth")
                require_successor_version(self.version, review.base_truth.version)
        return self


def revise_truth(
    dataset: DatasetManifest,
    previous: TruthSnapshot,
    review: ReviewHistory,
    review_ref: ReviewReportRef,
    *,
    version: Version,
) -> TruthSnapshot:
    """Publish a new in-memory truth revision with one independently reviewed entry."""
    dataset = DatasetManifest.model_validate(dataset)
    previous = TruthSnapshot.model_validate(previous)
    review = ReviewHistory.model_validate(review)
    source = previous.pin()
    if (
        dataset.pin() != previous.dataset
        or review.base_truth != source
        or review.dataset != previous.dataset
        or review.scope != previous.scope
        or dataset.scope != previous.scope
        or review.split != previous.split
        or review.catalogue_digest != previous.catalogue_digest
    ):
        raise ValueError(
            "review and prior truth must pin the exact dataset, scope and revision"
        )
    sample = next(
        (row for row in dataset.samples if row.sample_id == review.sample_id), None
    )
    holdout = {row.sample_id for row in dataset.samples if row.split == previous.split}
    if (
        sample is None
        or sample.split != previous.split
        or holdout != {row.sample_id for row in previous.entries}
    ):
        raise ValueError("truth review must address the exact holdout sample")
    updated = review.reviewed_entry(review_ref)
    original = next(
        row for row in previous.entries if row.sample_id == review.sample_id
    )
    if not review.votes or (
        original.state == "labeled" and review.adjudication is None
    ):
        raise ValueError(
            "publishing truth requires recorded votes and explicit adjudication to change labeled truth"
        )
    if updated.labels is not None:
        for obj in updated.labels.objects:
            obj.box.validate_bounds(sample.image_dimensions)
    return TruthSnapshot(
        schema_version="1.0.0",
        record_id=previous.record_id,
        version=version,
        dataset=previous.dataset,
        scope=previous.scope,
        split=previous.split,
        prior=source,
        catalogue_digest=previous.catalogue_digest,
        entries=tuple(
            updated if row.sample_id == review.sample_id else row
            for row in previous.entries
        ),
    )
