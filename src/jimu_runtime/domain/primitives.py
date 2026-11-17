"""Exact version, identity and SHA-256 textual types; no hash implementation."""

from typing import Annotated

from pydantic import Field, StringConstraints

Version = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=5,
        max_length=128,
        pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$",
    ),
    # JSON Schema regex engines may match $ before a final newline. The source
    # constraint already forbids whitespace; state it explicitly in the schema.
    Field(json_schema_extra={"not": {"pattern": r"\s"}}),
]
Digest = Annotated[
    str,
    StringConstraints(
        strict=True, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    ),
]
ArtifactId = Annotated[
    str,
    StringConstraints(
        strict=True, min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]{0,127}$"
    ),
    Field(json_schema_extra={"not": {"pattern": r"\s"}}),
]
RecordId = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
    ),
    Field(json_schema_extra={"not": {"pattern": r"\s"}}),
]
