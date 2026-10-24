"""CPU/latency stress and fixture comparison. No network; flat task-owned tmp files."""
import argparse
import asyncio
import json
import hashlib
import os
import importlib.util
from pathlib import Path
import statistics
import sys
import time
from unittest.mock import patch

sys.path.insert(0, os.environ.get('CARWASH_MEASURE_SOURCE', str(Path.cwd())))
from ai import Agent, ChatUser
from tests.fakes import fixture
spec=importlib.util.spec_from_file_location('phase2_measure_memory',Path(__file__).with_name('measure_memory.py'))
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
measure,retained_bytes=module.measure,module.retained_bytes


async def stress():
    store, _ = fixture(Path.cwd()/'tmp', sheets_mapping_file=str(Path.cwd()/'tmp/never-used-stress.json'))
    with patch('ai.AsyncOpenAI'):
        agent = Agent(store.cfg, store)
    async def reply(*args):
        return 'Подскажу услуги.'
    agent._run = reply
    started = time.perf_counter()
    for i in range(2000):
        await agent.reply(ChatUser(i % 1000,None,'Synthetic'), 'Какие услуги?')
    elapsed = (time.perf_counter()-started)*1000
    return {'replies':2000,'users':1000,'elapsed_ms':elapsed,
            'retained_container_bytes_approx':retained_bytes([agent._state,agent._history,agent._pending,
                agent._locks,agent._generation,agent._last_seen,agent._active,agent._turn_text,
                getattr(agent,'_turn_sources',{})]),
            'sessions':len(agent._last_seen),'generations':len(agent._generation)}


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--label',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--runs',type=int,default=5)
    args=parser.parse_args()
    source = Path(sys.modules['ai'].__file__)
    result={'source':str(source), 'ai_sha256':hashlib.sha256(source.read_bytes()).hexdigest()}
    for workload in ('memory','memory50','invalid_loop','booking','repeat_confirmation'):
        samples=[await measure(f'{args.label}-{i}',workload,rpc_delay_ms=2) for i in range(args.runs)]
        result[workload]={'sample':samples[0], 'latency_ms_samples':[s['synthetic_elapsed_ms'] for s in samples],
                          'latency_ms_median':statistics.median(s['synthetic_elapsed_ms'] for s in samples)}
    result['session_stress']=[await stress() for _ in range(args.runs)]
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')

if __name__=='__main__':
    asyncio.run(main())
