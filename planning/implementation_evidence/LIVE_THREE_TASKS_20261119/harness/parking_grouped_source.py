"""Group overlapping genuine WeVis detections in the signed trial Profile."""
import json
from dataclasses import replace

from examples.nonmotor_vehicle_illegal_parking.compilable.scene import with_source_provenance
from jimu_runtime.domain.enums import ArtifactKind
from parking_live_tools import build_source


def build_grouped_source():
    source = build_source()
    artifacts = []
    for row in source.artifacts:
        if row.kind is ArtifactKind.PROFILE and row.artifact_id == "nonmotor_vehicle":
            document = json.loads(row.content)
            document["grounding_grouping"] = {
                "mode": "connected_boxes",
                "max_gap_pixels": 8,
            }
            row = replace(row, content=json.dumps(document).encode())
        artifacts.append(row)
    return with_source_provenance(replace(source, artifacts=tuple(artifacts)))
