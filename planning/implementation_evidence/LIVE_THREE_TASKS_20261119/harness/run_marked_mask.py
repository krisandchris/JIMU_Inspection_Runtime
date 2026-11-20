import asyncio,json,sys
from dataclasses import replace
from pathlib import Path
from api_entry import use_task_api
from live_wire_bridge import start_bridge
from runtime_harness import run
from trial_scenes import build_mask_compile_input, capability_snapshot
from examples.nonmotor_vehicle_illegal_parking.compilable.scene import with_source_provenance
from jimu_runtime.domain.enums import ArtifactKind
def marked_mask_source():
    source=build_mask_compile_input()
    entries=[]
    for entry in source.artifacts:
        if entry.kind is ArtifactKind.PROFILE:
            profile=json.loads(entry.content)
            profile['grounding_grouping']={'mode':'per_box'}
            entry=replace(entry,content=json.dumps(profile).encode())
        entries.append(entry)
    return with_source_provenance(replace(source,artifacts=tuple(entries)))
if __name__=='__main__':
    out=Path(sys.argv[1]);out.mkdir(exist_ok=False)
    img=Path('planning/implementation_evidence/LIVE_THREE_TASKS_20261119/images/mask.png')
    with use_task_api(out),start_bridge(out/'provider') as bridge:
        asyncio.run(run(out,bridge.url,img,marked_mask_source,region='isolated_food_service',caps=capability_snapshot))
