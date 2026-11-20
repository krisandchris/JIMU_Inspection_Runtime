"""Run the isolated parking trial using the real API, worker, DB, and models."""
import asyncio
from pathlib import Path
from unittest.mock import patch
from sys import argv

from api_entry import use_task_api
from live_wire_bridge import start_bridge
from parking_live_tools import LiveGeometryInput, TOOL_ARTIFACT, build_source, entries_for
from runtime_harness import run
from parking_grouped_source import build_grouped_source
from jimu_runtime.inspection.geometry import node
from tests.fixtures.wp05 import perception

destination, image = map(Path,argv[1:3])
destination.mkdir(exist_ok=False)
with (
    patch.object(node, "GeometryInput", LiveGeometryInput),
    patch.object(perception, "CPU_ARTIFACT", TOOL_ARTIFACT),
    start_bridge(destination/"wire",detection_labels={"nonmotor_vehicle":"electric bicycle"}) as bridge,
    use_task_api(destination),
):
    asyncio.run(run(destination,bridge.url,image,build_grouped_source,make_tool_entries=entries_for(destination/"tool-wire")))
