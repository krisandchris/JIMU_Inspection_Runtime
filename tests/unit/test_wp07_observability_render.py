from decimal import Decimal

import pytest

from jimu_runtime.observability.prometheus import render_metrics


def test_export_never_invents_token_or_gpu_readings():
    body = render_metrics(
        [
            {
                "family": "tasks",
                "first_label": "completed",
                "second_label": "",
                "value": 3,
            }
        ],
        [
            {
                "family": "model",
                "label": "",
                "samples": 2,
                "total": Decimal("1.55"),
                "buckets": [0, 0, 0, 1, 1, 2, 2, 2, 2, 2],
            }
        ],
    ).decode()
    assert 'jimu_telemetry_available{signal="gpu"} 0' in body
    assert "jimu_retained_reported_tokens" not in body
    assert 'jimu_retained_model_duration_seconds_bucket{le="+Inf"} 2' in body
    assert "jimu_retained_model_duration_seconds_sum 1.55" in body
    assert 'jimu_retained_tasks{status="completed"} 3' in body


@pytest.mark.parametrize("label", ("project-id", '\nprivate="credential', ""))
def test_unregistered_metric_labels_cannot_enter_exposition(label):
    with pytest.raises(ValueError, match="unknown or duplicate label"):
        render_metrics(
            [{"family": "tasks", "first_label": label, "second_label": "", "value": 1}],
            [],
        )


def test_histogram_rejects_non_cumulative_counts():
    with pytest.raises(ValueError, match="cumulative"):
        render_metrics(
            [],
            [
                {
                    "family": "task",
                    "label": "",
                    "samples": 2,
                    "total": Decimal("1"),
                    "buckets": [1, 0, 0, 1, 1, 2, 2, 2, 2, 2],
                }
            ],
        )


def test_retained_distributions_are_stable_snapshot_gauges():
    counts = [
        {"family": "tasks", "first_label": phase, "second_label": "", "value": 2}
        for phase in ("failed", "completed")
    ]
    timed = [
        {
            "family": "node",
            "label": node,
            "samples": 1,
            "total": Decimal("0.1"),
            "buckets": [0, 1, 1, 1, 1, 1, 1, 1, 1, 1],
        }
        for node in ("N06", "N01")
    ]
    body = render_metrics(counts, timed)
    assert body == render_metrics(tuple(reversed(counts)), tuple(reversed(timed)))
    assert b"# TYPE jimu_retained_node_duration_seconds_bucket gauge\n" in body
    assert b"# TYPE jimu_retained_node_duration_seconds_count gauge\n" in body
    assert b"# TYPE jimu_retained_node_duration_seconds_sum gauge\n" in body
    assert b" histogram\n" not in body
    assert body.index(b'node="N01",le="0.1"') < body.index(b'node="N01",le="+Inf"')
    assert body.index(b'node="N01",le="+Inf"') < body.index(b'node="N06",le="0.05"')


def test_reported_zero_tokens_is_different_from_absent_tokens():
    observed = [
        {
            "family": "token_values",
            "first_label": "input",
            "second_label": "",
            "value": 0,
        }
    ]
    assert b'jimu_retained_reported_tokens{direction="input"} 0\n' in render_metrics(
        observed, ()
    )
    assert b"jimu_retained_reported_tokens" not in render_metrics((), ())
