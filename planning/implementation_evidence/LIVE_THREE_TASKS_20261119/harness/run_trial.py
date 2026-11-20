import argparse,asyncio
from pathlib import Path
from api_entry import use_task_api
import trial_scenes
from live_wire_bridge import start_bridge
from runtime_harness import run
cases={'goods':(trial_scenes.build_sidewalk_goods_compile_input,'isolated_sidewalk'),'mask':(trial_scenes.build_mask_compile_input,'isolated_food_service')}
parser=argparse.ArgumentParser();parser.add_argument('case',choices=cases);parser.add_argument('out',type=Path);opts=parser.parse_args()
opts.out.mkdir(parents=True,exist_ok=False)
img=Path.cwd()/'planning/implementation_evidence/LIVE_THREE_TASKS_20261119/images'/(opts.case+'.png')
source,region=cases[opts.case]
with use_task_api(opts.out),start_bridge(opts.out/'provider') as bridge:
 asyncio.run(run(opts.out,bridge.url,img,source,region=region,caps=trial_scenes.capability_snapshot))
