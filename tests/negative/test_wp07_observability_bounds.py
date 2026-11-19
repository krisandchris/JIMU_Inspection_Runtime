import asyncio
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.observability.contracts import EventCursor
from jimu_runtime.observability.postgres import PostgresObservability

_SCOPE = IdempotencyScope(project_id="fixture.project", client_id="fixture.client")


def test_request_bounds_apply_before_database_io():
    async def run():
        engine = Mock()
        reader = PostgresObservability(engine)
        with pytest.raises(ValidationError):
            await reader.events(_SCOPE, "execution", limit=101)
        with pytest.raises(ValidationError):
            await reader.calls(_SCOPE, "execution", attempt_id="'; select pg_sleep(10)")
        with pytest.raises(ValidationError):
            await reader.tool_steps(_SCOPE, "' illegal identifier")
        with pytest.raises(ValidationError):
            await reader.events(
                _SCOPE,
                "execution",
                cursor=EventCursor.model_construct(after=6, through=5),
            )
        engine.connect.assert_not_called()

    asyncio.run(run())
