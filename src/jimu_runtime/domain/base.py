"""Shared validation configuration for nested foundation contracts."""

import json

from pydantic import BaseModel, ConfigDict


class ContractModel(BaseModel):
    """Strict immutable model; nested containers must also be immutable.

    Foundation refs/values are nested types, not independently persisted records.
    The future owning persisted root supplies its explicit schema_version.
    """

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
        validate_default=True,
        allow_inf_nan=False,
    )

    def stable_json(self) -> str:
        """Stable JSON for comparison, NOT JCS, artifact hashing or Bundle integrity."""
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
