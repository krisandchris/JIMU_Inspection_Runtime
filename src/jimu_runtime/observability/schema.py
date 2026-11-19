"""Generate the observability schema library from its authoritative models."""

from json import dumps

from pydantic.json_schema import models_json_schema

from jimu_runtime.observability.contracts import (
    CallPage,
    EventCursor,
    EventPage,
    Readiness,
    TimeCursor,
    ToolStepCursor,
    ToolStepPage,
)


def render() -> str:
    roots, library = models_json_schema(
        [
            (model, "serialization")
            for model in (
                EventCursor,
                EventPage,
                TimeCursor,
                CallPage,
                ToolStepCursor,
                ToolStepPage,
                Readiness,
            )
        ],
        title="JIMU Observability v1",
    )
    return (
        dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                **library,
                "x-exports": {
                    model.__name__: schema for (model, _), schema in roots.items()
                },
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


if __name__ == "__main__":
    print(render(), end="")
