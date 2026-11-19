"""Preserve the WP-01B public Tool mapping contract during source reconstruction."""

import pytest
from pydantic import ValidationError

from jimu_runtime.domain.tool_mappings import MappingTypeContext


def test_public_mapping_types_still_require_explicit_views():
    schema = MappingTypeContext.model_json_schema()
    assert {"object_schema_ref", "image_schema_ref"} <= set(schema["required"])
    with pytest.raises(ValidationError) as error:
        MappingTypeContext.model_validate(
            {"current_step_id": "step", "context_registrations": (), "descriptors": ()}
        )
    missing = {row["loc"][0] for row in error.value.errors()}
    assert {"object_schema_ref", "image_schema_ref"} <= missing
