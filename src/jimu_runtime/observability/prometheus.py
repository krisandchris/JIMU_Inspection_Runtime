"""Render only registered metrics and bounded, low-cardinality dimensions."""

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import get_args

from jimu_runtime.domain.enums import (
    AttemptStatus,
    ExecutionStatus,
    NodeRunStatus,
    TaskStatus,
)
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.observability.contracts import CallErrorClass

CALL_ERROR_CLASSES = frozenset(get_args(CallErrorClass.__value__))

_NODES = frozenset(item.value for item in NodeId)
_KINDS = frozenset(("model", "tool", "unknown"))
_BOUNDARIES = frozenset(("not_dispatched", "sent_unknown", "acknowledged"))
_CALL_STATUSES = frozenset(
    ("succeeded", "partial", "failed", "not_dispatched", "unqualified")
)
_TOOL_STATUSES = frozenset(("succeeded", "failed", "timed_out"))
_SIDES = frozenset(("input", "output"))
_NO_LABEL = frozenset(("",))
_BUCKETS = ("0.05", "0.1", "0.25", "0.5", "1", "2", "5", "10", "30", "120")

# Persisted row counts can decrease after retention or restore. Their gauge
# names intentionally describe snapshots of retained audit data.
_COUNTERS = {
    "tasks": (
        "jimu_retained_tasks",
        ("status", frozenset(x.value for x in TaskStatus)),
        ("", _NO_LABEL),
    ),
    "accepted_tasks": (
        "jimu_retained_accepted_tasks",
        ("", _NO_LABEL),
        ("", _NO_LABEL),
    ),
    "executions": (
        "jimu_retained_executions",
        ("status", frozenset(x.value for x in ExecutionStatus)),
        ("", _NO_LABEL),
    ),
    "jobs": (
        "jimu_workflow_jobs",
        ("status", frozenset(("ready", "leased", "staged", "done"))),
        ("", _NO_LABEL),
    ),
    "nodes": (
        "jimu_retained_nodes",
        ("node", _NODES),
        ("status", frozenset(x.value for x in NodeRunStatus)),
    ),
    "retries": ("jimu_retained_node_retries", ("node", _NODES), ("", _NO_LABEL)),
    "attempt_results": (
        "jimu_retained_attempt_results",
        ("node", _NODES),
        ("status", frozenset(x.value for x in AttemptStatus)),
    ),
    "physical_dispatches": (
        "jimu_retained_admitted_dispatches",
        ("kind", _KINDS),
        ("", _NO_LABEL),
    ),
    "receipts": (
        "jimu_retained_dispatch_receipts",
        ("kind", _KINDS),
        ("boundary", _BOUNDARIES),
    ),
    "call_outcomes": (
        "jimu_retained_call_outcomes",
        ("kind", _KINDS),
        ("status", _CALL_STATUSES),
    ),
    "call_errors": (
        "jimu_retained_call_errors",
        ("kind", _KINDS),
        ("error", CALL_ERROR_CLASSES),
    ),
    "late_receipts": ("jimu_retained_late_receipts", ("kind", _KINDS), ("", _NO_LABEL)),
    "response_evidence_failures": (
        "jimu_retained_response_evidence_failures",
        ("", _NO_LABEL),
        ("", _NO_LABEL),
    ),
    "observation_slots": (
        "jimu_retained_observation_slots",
        ("status", frozenset(("invalid", "missing"))),
        ("", _NO_LABEL),
    ),
    "token_values": (
        "jimu_retained_reported_tokens",
        ("direction", _SIDES),
        ("", _NO_LABEL),
    ),
    "token_observations": (
        "jimu_retained_token_observations",
        ("direction", _SIDES),
        ("", _NO_LABEL),
    ),
    "tool_steps": (
        "jimu_retained_tool_step_completions",
        ("status", _TOOL_STATUSES),
        ("", _NO_LABEL),
    ),
}
_HISTOGRAMS = {
    "queue": ("jimu_retained_queue_wait_seconds", _NO_LABEL),
    "task": ("jimu_retained_task_duration_seconds", _NO_LABEL),
    "node": ("jimu_retained_node_duration_seconds", _NODES),
    "model": ("jimu_retained_model_duration_seconds", _NO_LABEL),
    "tool": ("jimu_retained_tool_duration_seconds", _NO_LABEL),
}


def _decimal(value: object, *, integer: bool = False) -> Decimal:
    if not isinstance(value, (int, Decimal)) or isinstance(value, bool):
        raise ValueError("unexpected database metric value")
    try:
        number = Decimal(value)
        if (
            not number.is_finite()
            or number < 0
            or number > Decimal(10**38)
            or (integer and number != number.to_integral_value())
        ):
            raise ValueError("out-of-range database metric value")
    except InvalidOperation:
        raise ValueError("invalid database metric value") from None
    return number


def _labels(*items: tuple[str, str]) -> str:
    entries = [f'{key}="{value}"' for key, value in items if key]
    return "{" + ",".join(entries) + "}" if entries else ""


def render_metrics(
    counts: Sequence[Mapping[str, object]],
    timings: Sequence[Mapping[str, object]],
) -> bytes:
    """Emit Prometheus 0.0.4 text; unknown source values cannot become labels."""
    if len(counts) > 256 or len(timings) > 16:
        raise ValueError("metrics aggregation exceeds the registered series limit")

    families: dict[str, tuple[str, list[str]]] = {}
    seen: set[tuple[str, str, str]] = set()
    for row in sorted(
        counts,
        key=lambda item: tuple(
            str(item[key]) for key in ("family", "first_label", "second_label")
        ),
    ):
        family, first, second = (
            str(row["family"]),
            str(row["first_label"]),
            str(row["second_label"]),
        )
        metric, (key_a, allowed_a), (key_b, allowed_b) = _COUNTERS[family]
        key = (family, first, second)
        if first not in allowed_a or second not in allowed_b or key in seen:
            raise ValueError("metric has an unknown or duplicate label")
        seen.add(key)
        value = _decimal(row["value"], integer=True)
        samples = families.setdefault(metric, ("gauge", []))[1]
        samples.append(f"{metric}{_labels((key_a, first), (key_b, second))} {value:f}")

    for row in sorted(
        timings, key=lambda item: (str(item["family"]), str(item["label"]))
    ):
        family, label = str(row["family"]), str(row["label"])
        metric, allowed = _HISTOGRAMS[family]
        key = (family, label, "")
        if label not in allowed or key in seen:
            raise ValueError("duration metric has an unknown or duplicate label")
        seen.add(key)
        count = _decimal(row["samples"], integer=True)
        total = _decimal(row["total"])
        raw = row["buckets"]
        if not isinstance(raw, list | tuple) or len(raw) != len(_BUCKETS):
            raise ValueError("duration bucket count differs from registered bounds")
        buckets = tuple(_decimal(item, integer=True) for item in raw)
        if buckets != tuple(sorted(buckets)) or buckets[-1] > count:
            raise ValueError("duration bucket counts must be cumulative")
        node = ("node", label) if label else ("", "")
        bucket_name = f"{metric}_bucket"
        samples = families.setdefault(bucket_name, ("gauge", []))[1]
        samples.extend(
            f"{bucket_name}{_labels(node, ('le', bound))} {value:f}"
            for bound, value in zip((*_BUCKETS, "+Inf"), (*buckets, count), strict=True)
        )
        labels = _labels(node)
        for suffix, value in (("sum", total), ("count", count)):
            gauge = f"{metric}_{suffix}"
            families.setdefault(gauge, ("gauge", []))[1].append(
                f"{gauge}{labels} {value:f}"
            )

    lines = [
        "# TYPE jimu_telemetry_available gauge",
        'jimu_telemetry_available{signal="gpu"} 0',
        'jimu_telemetry_available{signal="reviewed_quality"} 0',
    ]
    for metric, (kind, samples) in sorted(families.items()):
        lines.extend((f"# TYPE {metric} {kind}", *samples))
    output = ("\n".join(lines) + "\n").encode("ascii")
    if len(output) > 64 * 1024:
        raise ValueError("metrics response exceeds its byte limit")
    return output
