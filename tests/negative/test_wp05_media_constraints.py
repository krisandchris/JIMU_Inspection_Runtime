"""The application cannot mutate media bindings or attach bytes to unknown dispatches."""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from jimu_runtime.inspection.media import LocalMediaStore
from tests.contract.test_wp04_dispatch_journal import _ready
from tests.contract.test_wp05_media_postgres import publish
from tests.fixtures.wp03.jobs.support import database
from tests.unit.test_wp05_media_decoder import pixels


def test_only_append_and_exact_dispatch_foreign_key(tmp_path):
    async def run():
        async with database() as (engine, _):
            _, scope, execution = await _ready(engine)
            store = LocalMediaStore(tmp_path, engine)
            original = await publish(store, scope, pixels())
            with pytest.raises(DBAPIError) as denied:
                async with engine.begin() as connection:
                    await connection.execute(
                        text("DELETE FROM jimu_workflow.original_media")
                    )
            assert denied.value.orig.sqlstate == "42501"
            with pytest.raises(IntegrityError) as foreign:
                async with engine.begin() as connection:
                    await connection.execute(
                        text("""
                            INSERT INTO jimu_workflow.dispatch_media
                                (project_id, client_id, execution_id, dispatch_id, role,
                                 store_id, file_key, content_sha256, byte_size, wire_sha256, wire_size)
                            VALUES (:project, :client, :execution, 'never-dispatched',
                                'model_raw', 'media-store', :file_key, :hash, 5, :hash, 5)
                        """),
                        {
                            "project": scope.project_id,
                            "client": scope.client_id,
                            "execution": execution,
                            "hash": original.media_ref.sha256,
                            "file_key": "f" * 64,
                        },
                    )
            assert foreign.value.orig.sqlstate == "23503"

    asyncio.run(run())
