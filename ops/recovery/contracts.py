"""Private recovery package contract."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.primitives import ArtifactId, Digest

MANIFEST = "manifest.json"
DUMP = "database.dump"
BUNDLE = "bundle-store.sqlite3"
RESOURCES = "resource.sqlite3"
MEDIA = "media-v1"
IDENTITY = f"{MEDIA}/.database"


class RecoveryError(RuntimeError):
    """A contextual, credential-free recovery failure."""


class FileWitness(ContractModel):
    sha256: Digest
    size: Annotated[int, Field(ge=0)]


class RowWitness(ContractModel):
    sha256: Digest
    rows: Annotated[int, Field(ge=0)]


class Manifest(ContractModel):
    schema_version: Literal["1.0.0"]
    logical_database_id: UUID
    source_database: str
    postgres_version: str
    dump_version: str
    host_config_sha256: Digest
    bundle_version: int
    resource_version: int
    inventory_id: str
    inventory_sha256: Digest
    postgres_tables: dict[str, RowWitness]
    files: dict[str, FileWitness]


class HostBinding(ContractModel):
    schema_version: Literal["1.0.0"]
    deployment_config_sha256: Digest
    inventory_id: ArtifactId
    inventory_sha256: Digest
