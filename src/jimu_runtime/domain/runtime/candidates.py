"""Strict delivery bytes and atomic finite candidate history, without admission."""

import json
import math
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, StrictInt, TypeAdapter, ValidationError

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import (
    immutable_sequence,
    reference_identity,
)
from jimu_runtime.domain.runtime.facts import (
    FactRecord,
    QuestionFactSource,
    SystemFactSource,
    ToolFactSource,
)
from jimu_runtime.domain.validation.authority_inputs import FactCandidateSubmission
from jimu_runtime.domain.validation.issues import (
    CheckResult,
    ValidationIssue,
    ValidationReport,
)


class CandidateParseLimits(ContractModel):
    max_bytes: Annotated[StrictInt, Field(ge=1, le=16 * 1024 * 1024)]
    max_json_depth: Annotated[StrictInt, Field(ge=1, le=32)]
    max_object_members: Annotated[StrictInt, Field(ge=1, le=1024)]
    max_array_items: Annotated[StrictInt, Field(ge=1, le=1024)]
    max_number_chars: Annotated[StrictInt, Field(ge=1, le=4096)]


class CandidateSnapshotLimits(ContractModel):
    max_entries: Annotated[StrictInt, Field(ge=1, le=1024)]
    max_batch_entries: Annotated[StrictInt, Field(ge=1, le=1024)]
    max_total_raw_bytes: Annotated[StrictInt, Field(ge=1, le=64 * 1024 * 1024)]


class CandidateSnapshot(ContractModel):
    entries: Annotated[
        tuple[FactCandidateSubmission, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]


def issue(code: str, path: tuple[str | int, ...], message: str) -> ValidationIssue:
    if len(path) > 32 or any(
        isinstance(part, str) and not 1 <= len(part) <= 128 for part in path
    ):
        path = ()
        message = (
            "Parser-root location: full input location exceeds diagnostic path domain. "
            + message
        )
    return ValidationIssue(code=code, path=path, message=message, related_refs=())


def local_report(
    check_id: str, issues: tuple[ValidationIssue, ...]
) -> ValidationReport:
    overflow = len(issues) > 64
    bounded = (
        issues
        if not overflow
        else issues[:63]
        + (
            issue(
                "diagnostic_overflow",
                (),
                "Diagnostic enumeration exceeded its bound; retained prefix is incomplete.",
            ),
        )
    )
    return ValidationReport(
        scope="authority_admission",
        checks=(
            CheckResult(
                check_id=check_id,
                status="invalid" if bounded else "passed",
                issues=bounded,
            ),
            CheckResult(
                check_id="full_authority_admission",
                status="deferred",
                issues=(
                    issue(
                        "full_authority_deferred",
                        (),
                        "Full Producer activation/authorization and authority admission remain deferred; local checks confer no consumer authority.",
                    ),
                ),
            ),
        ),
    )


class LocallyValidCandidate(ContractModel):
    outcome: Literal["locally_valid"]
    submission: FactCandidateSubmission
    report: ValidationReport


class InvalidCandidate(ContractModel):
    outcome: Literal["invalid"]
    report: ValidationReport


type CandidateParseOutcome = Annotated[
    LocallyValidCandidate | InvalidCandidate, Field(discriminator="outcome")
]


@dataclass(frozen=True)
class _Pairs:
    items: tuple[tuple[str, object], ...]


@dataclass(frozen=True)
class _Number:
    token: str
    kind: Literal["integer", "number", "nonfinite"]


class _ParseFailure(ValueError):
    def __init__(self, code: str, path: tuple[str | int, ...], message: str) -> None:
        self.issue = issue(code, path, message)
        super().__init__(message)


def _depth_guard(text: str, maximum: int) -> None:
    depth, quoted, escaped = 0, False, False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > maximum:
                raise _ParseFailure(
                    "json_depth_limit",
                    (),
                    "Parser-root: JSON nesting exceeds explicit limit.",
                )
        elif character in "]}":
            depth -= 1


def _unicode(value: str, path: tuple[str | int, ...]) -> None:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise _ParseFailure(
            "json_unicode", path, "Invalid Unicode scalar in JSON key or string."
        ) from exc


def _verify_pairs_tree(
    value: object, limits: CandidateParseLimits, path: tuple[str | int, ...] = ()
) -> None:
    if isinstance(value, _Pairs):
        if len(value.items) > limits.max_object_members:
            raise _ParseFailure(
                "json_object_limit", path, "Object member count exceeds explicit limit."
            )
        keys: set[str] = set()
        for key, _ in value.items:
            _unicode(key, path + (key,))
            if key in keys:
                raise _ParseFailure(
                    "json_duplicate_key",
                    path + (key,),
                    "Repeated decoded JSON object key.",
                )
            keys.add(key)
        for key, child in value.items:
            _verify_pairs_tree(child, limits, path + (key,))
    elif isinstance(value, list):
        if len(value) > limits.max_array_items:
            raise _ParseFailure(
                "json_array_limit", path, "Array item count exceeds explicit limit."
            )
        for index, child in enumerate(value):
            _verify_pairs_tree(child, limits, path + (index,))
    elif isinstance(value, _Number):
        if len(value.token) > limits.max_number_chars:
            raise _ParseFailure(
                "json_number_limit", path, "Numeric lexeme exceeds explicit limit."
            )
        try:
            if value.kind == "nonfinite":
                raise _ParseFailure(
                    "json_nonfinite", path, "Nonfinite JSON number is forbidden."
                )
            if value.kind == "integer":
                int(value.token)
            elif not math.isfinite(float(value.token)):
                raise _ParseFailure(
                    "json_nonfinite", path, "Numeric lexeme is nonfinite in binary64."
                )
        except (OverflowError, ValueError) as exc:
            if isinstance(exc, _ParseFailure):
                raise
            raise _ParseFailure(
                "json_number_invalid",
                path,
                "Numeric lexeme cannot be parsed within this environment.",
            ) from exc
    elif isinstance(value, str):
        _unicode(value, path)


def validate_candidate_submission(
    submission: FactCandidateSubmission, *, limits: CandidateParseLimits
) -> CandidateParseOutcome:
    try:
        limits = CandidateParseLimits.model_validate(limits)
        checked = FactCandidateSubmission.model_validate(submission)
        raw = checked.raw_bytes
        if len(raw) > limits.max_bytes:
            raise _ParseFailure(
                "candidate_byte_limit", (), "Raw candidate exceeds explicit byte limit."
            )
        text = raw.decode("utf-8", errors="strict")
        if text.startswith("\ufeff"):
            raise _ParseFailure(
                "json_bom",
                (),
                "UTF8 BOM is unsupported; original bytes are never stripped.",
            )
        _depth_guard(text, limits.max_json_depth)
        tree: object = json.loads(
            text,
            object_pairs_hook=lambda pairs: _Pairs(tuple(pairs)),
            parse_int=lambda token: _Number(token, "integer"),
            parse_float=lambda token: _Number(token, "number"),
            parse_constant=lambda token: _Number(token, "nonfinite"),
        )
        _verify_pairs_tree(tree, limits)
        parsed: FactRecord = TypeAdapter(FactRecord).validate_json(raw)
        if parsed != checked.fact:
            raise _ParseFailure(
                "candidate_content_mismatch",
                (),
                "Original bytes and revalidated typed Fact differ.",
            )
        return LocallyValidCandidate(
            outcome="locally_valid",
            submission=checked,
            report=local_report("candidate_bytes", ()),
        )
    except _ParseFailure as exc:
        problems = (exc.issue,)
    except UnicodeDecodeError:
        problems = (issue("json_utf8", (), "Original bytes are not strict UTF8."),)
    except json.JSONDecodeError:
        problems = (issue("json_syntax", (), "Parser-root: malformed JSON document."),)
    except ValidationError:
        problems = (
            issue(
                "candidate_contract",
                (),
                "Typed candidate or parsed Fact violates its strict contract.",
            ),
        )
    except (RecursionError, OverflowError, ValueError):
        problems = (
            issue(
                "candidate_parse_failure",
                (),
                "Parser-root: bounded candidate parsing failed.",
            ),
        )
    return InvalidCandidate(
        outcome="invalid", report=local_report("candidate_bytes", problems)
    )


def candidate_key(fact: FactRecord) -> tuple[object, ...]:
    source = fact.source
    if isinstance(source, QuestionFactSource):
        producer: tuple[object, ...] = (
            source.source_kind,
            reference_identity(source.question_ref),
            "primary",
        )
    elif isinstance(source, ToolFactSource):
        producer = (
            source.source_kind,
            reference_identity(source.tool_plan_ref),
            source.step_id,
            reference_identity(source.tool_ref),
            source.output_name,
        )
    elif isinstance(source, SystemFactSource):
        producer = (
            source.source_kind,
            source.requirement.family,
            source.requirement.capability_id,
            source.requirement.contract_version,
            source.output_name,
        )
    return (
        fact.execution_id,
        fact.scope,
        fact.object_id,
        producer,
        fact.fact_key,
        fact.attempt_id,
    )


class CandidateLocation(ContractModel):
    origin: Literal["prior", "batch"]
    index: Annotated[StrictInt, Field(ge=0, le=1023)]


class ConflictRecord(ContractModel):
    conflict_kind: Literal["same_candidate_different_bytes", "fact_id_rebinding"]
    old_location: CandidateLocation
    new_index: Annotated[StrictInt, Field(ge=0, le=1023)]
    old_submission: FactCandidateSubmission
    new_submission: FactCandidateSubmission


class ValidatedPrior(ContractModel):
    prior_qualification: Literal["validated"]
    snapshot: CandidateSnapshot


class UnavailablePrior(ContractModel):
    prior_qualification: Literal["unavailable"]


type RejectedPrior = Annotated[
    ValidatedPrior | UnavailablePrior, Field(discriminator="prior_qualification")
]
type CandidateCount = Annotated[StrictInt, Field(ge=0, le=1024)]


class AppendedCandidates(ContractModel):
    outcome: Literal["appended"]
    snapshot: CandidateSnapshot
    report: ValidationReport
    appended_count: Annotated[StrictInt, Field(ge=1, le=1024)]
    duplicate_count: CandidateCount


class DuplicateCandidates(ContractModel):
    outcome: Literal["duplicate"]
    snapshot: CandidateSnapshot
    report: ValidationReport
    duplicate_count: CandidateCount


class RejectedCandidates(ContractModel):
    outcome: Literal["rejected"]
    report: ValidationReport
    prior: RejectedPrior
    conflicts: Annotated[
        tuple[ConflictRecord, ...],
        Field(max_length=64),
        BeforeValidator(immutable_sequence),
    ]
    conflict_completeness: Literal["complete", "overflow"]


type CandidateAppendOutcome = Annotated[
    AppendedCandidates | DuplicateCandidates | RejectedCandidates,
    Field(discriminator="outcome"),
]


def append_candidates(
    prior: CandidateSnapshot,
    submissions: tuple[FactCandidateSubmission, ...],
    *,
    parse_limits: CandidateParseLimits,
    snapshot_limits: CandidateSnapshotLimits,
) -> CandidateAppendOutcome:
    problems: list[ValidationIssue] = []
    qualified: RejectedPrior = UnavailablePrior(prior_qualification="unavailable")
    conflicts: list[ConflictRecord] = []
    conflict_count = 0
    try:
        parse_limits = CandidateParseLimits.model_validate(parse_limits)
        budgets = CandidateSnapshotLimits.model_validate(snapshot_limits)
        prior = CandidateSnapshot.model_validate(prior)
        if (
            len(prior.entries) > budgets.max_entries
            or sum(len(entry.raw_bytes) for entry in prior.entries)
            > budgets.max_total_raw_bytes
        ):
            raise _ParseFailure(
                "prior_budget", (), "Prior history exceeds this call's explicit budget."
            )
        key_index: dict[
            tuple[object, ...], tuple[FactCandidateSubmission, CandidateLocation]
        ] = {}
        id_index: dict[str, tuple[FactCandidateSubmission, CandidateLocation]] = {}
        for index, entry in enumerate(prior.entries):
            result = validate_candidate_submission(entry, limits=parse_limits)
            if isinstance(result, InvalidCandidate):
                raise _ParseFailure(
                    "invalid_prior",
                    ("prior", index),
                    "Prior candidate cannot be independently revalidated.",
                )
            key = candidate_key(entry.fact)
            old = key_index.get(key) or id_index.get(entry.fact.fact_id)
            if old is not None and (
                old[0].raw_bytes != entry.raw_bytes or candidate_key(old[0].fact) != key
            ):
                raise _ParseFailure(
                    "prior_conflict",
                    ("prior", index),
                    "Prior contains conflicting immutable candidates.",
                )
            if old is None:
                location = CandidateLocation(origin="prior", index=index)
                key_index[key] = (entry, location)
                id_index[entry.fact.fact_id] = (entry, location)
        qualified = ValidatedPrior(prior_qualification="validated", snapshot=prior)
        if type(submissions) not in (list, tuple):
            raise _ParseFailure(
                "batch_contract",
                (),
                "Batch requires an array of candidate submissions.",
            )
        if len(submissions) > budgets.max_batch_entries:
            raise _ParseFailure(
                "batch_budget", (), "Batch count exceeds explicit limit."
            )
        checked_batch = tuple(
            FactCandidateSubmission.model_validate(item) for item in submissions
        )
        if (
            sum(len(entry.raw_bytes) for entry in (*prior.entries, *checked_batch))
            > budgets.max_total_raw_bytes
        ):
            raise _ParseFailure(
                "total_byte_budget",
                (),
                "Combined prior and batch bytes exceed budget, counting repeated delivery.",
            )
        added: list[FactCandidateSubmission] = []
        duplicates = 0
        for index, entry in enumerate(checked_batch):
            parsed = validate_candidate_submission(entry, limits=parse_limits)
            if isinstance(parsed, InvalidCandidate):
                problems.extend(
                    issue(
                        problem.code,
                        ("submissions", index, "raw_bytes", *problem.path),
                        problem.message,
                    )
                    for problem in parsed.report.checks[0].issues
                )
                continue
            key = candidate_key(entry.fact)
            old_key, old_id = key_index.get(key), id_index.get(entry.fact.fact_id)
            old = old_key or old_id
            if old is not None:
                if (
                    old[0].raw_bytes == entry.raw_bytes
                    and candidate_key(old[0].fact) == key
                    and old[0].fact.fact_id == entry.fact.fact_id
                ):
                    duplicates += 1
                    continue
                conflict_count += 1
                if len(conflicts) < 64:
                    conflicts.append(
                        ConflictRecord(
                            conflict_kind="same_candidate_different_bytes"
                            if old_key is not None
                            else "fact_id_rebinding",
                            old_location=old[1],
                            new_index=index,
                            old_submission=old[0],
                            new_submission=entry,
                        )
                    )
                continue
            location = CandidateLocation(origin="batch", index=index)
            key_index[key] = (entry, location)
            id_index[entry.fact.fact_id] = (entry, location)
            added.append(entry)
        if conflict_count:
            problems.append(
                issue(
                    "candidate_conflict",
                    (),
                    "Conflicting candidates were preserved as rejected input, never overwritten.",
                )
            )
        if conflict_count > 64:
            problems.append(
                issue(
                    "conflict_overflow",
                    (),
                    "More than64 conflicts; returned conflict enumeration is incomplete.",
                )
            )
        if len(prior.entries) + len(added) > budgets.max_entries:
            problems.append(
                issue(
                    "snapshot_entry_budget",
                    (),
                    "Resulting candidate snapshot exceeds explicit entry limit.",
                )
            )
        if not problems:
            report = local_report("candidate_snapshot", ())
            if not added:
                return DuplicateCandidates(
                    outcome="duplicate",
                    snapshot=prior,
                    report=report,
                    duplicate_count=duplicates,
                )
            snapshot = CandidateSnapshot(entries=prior.entries + tuple(added))
            return AppendedCandidates(
                outcome="appended",
                snapshot=snapshot,
                report=report,
                appended_count=len(added),
                duplicate_count=duplicates,
            )
    except _ParseFailure as exc:
        problems.append(exc.issue)
    except (ValidationError, ValueError, TypeError):
        problems.append(
            issue(
                "snapshot_contract",
                (),
                "Snapshot or submission input violates its strict contract.",
            )
        )
    return RejectedCandidates(
        outcome="rejected",
        report=local_report("candidate_snapshot", tuple(problems)),
        prior=qualified,
        conflicts=tuple(conflicts),
        conflict_completeness="overflow" if conflict_count > 64 else "complete",
    )
