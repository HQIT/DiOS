"""Explicit human-operated business commands; no implicit approval or messages."""
import argparse
import asyncio
import json

from dios_sdk import WorkloadError
from .cli import Client, load
from .registry import Registry, RegistryError
from .workitems import WorkItems


async def run(args):
    registry=Registry(args.state_dir)
    try:
        work=WorkItems(registry)
        if args.action=='show': return work.history(args.id)
        if args.action=='bind': return work.bind(args.id,args.employee,expected_version=args.expected_version)
        config=json.loads(load(args.config,private=True))
        if args.action=='dispatch':
            employee=work.get(args.id)['employee']
            return await work.dispatch(args.id,Client(config['employees'][employee]),expected_version=args.expected_version)
        client=Client(config['reviewers'][args.reviewer])
        if args.action=='rework':
            employee=work.get(args.id)['employee']
            return await work.rework(args.id,client,Client(config['employees'][employee]),args.goal,args.key,
                expected_version=args.expected_version)
        if args.action=='create':
            return await work.create(args.id,args.title,args.work_type,args.sample,args.goal,client)
        if args.action=='observe': return await work.observe(args.id,client,expected_version=args.expected_version)
        return await work.confirm(args.id,client,args.decision,args.note,expected_version=args.expected_version)
    finally: registry.close()


def main():
    parser=argparse.ArgumentParser(description='Upper-platform WorkItem and human confirmation')
    parser.add_argument('--state-dir',required=True)
    commands=parser.add_subparsers(dest='action',required=True)
    for action in ('create','bind','dispatch','observe','confirm','rework','show'):
        command=commands.add_parser(action);command.add_argument('--id',required=True)
        if action not in ('create','show'): command.add_argument('--expected-version',required=True,type=int)
        if action not in ('bind','show'): command.add_argument('--config',required=True)
        if action in ('create','observe','confirm','rework'): command.add_argument('--reviewer',required=True)
        if action=='create':
            for flag in ('title','work-type','sample','goal'):command.add_argument('--'+flag,required=True)
        if action=='bind':command.add_argument('--employee',required=True)
        if action=='confirm':
            command.add_argument('--decision',choices=('accept','reject'),required=True)
            command.add_argument('--note',required=True)
        if action=='rework':
            command.add_argument('--goal',required=True)
            command.add_argument('--key',required=True,help='stable key for this rework request; reuse after an uncertain receipt')
    try:
        print(json.dumps(asyncio.run(run(parser.parse_args())),ensure_ascii=False,indent=2));return 0
    except (OSError,ValueError,KeyError,TypeError,TimeoutError,WorkloadError) as exc:
        code=str(exc) if isinstance(exc,RegistryError) else getattr(exc,'code','WORK_ITEM_OPERATION_UNCONFIRMED')
        print(json.dumps({'error':code}));return 2


if __name__=='__main__':raise SystemExit(main())
