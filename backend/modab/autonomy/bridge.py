"""DSH can draft and inspect missions. Task approval is never a model tool."""
import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dualctx.contracts import ContractError
from modab.autonomy.contracts import exact, parse, read
from modab.autonomy.service import draft, public_state
from modab.autonomy.store import ResearchStore


def invoke(config, operation, body):
    exact(body, {'arguments', 'origin'})
    args = body['arguments']
    if operation == 'catalog':
        exact(args, set())
        state = public_state(config)
        result = {'profiles': state['profiles'], 'tools': state['catalog'],
            'user_control_url': f"http://127.0.0.1:{config['port']}/", 'approval_is_model_tool': False}
    elif operation == 'draft':
        exact(args, {'profile_id', 'objective', 'request_id'})
        result = draft(config, **args, origin=body['origin'])
    elif operation == 'query':
        exact(args, {'mission_id'})
        with ResearchStore(config['workspace']) as store:
            result = store.snapshot(args['mission_id'])
    else:
        raise ContractError('Unsupported DSH research task operation')
    return {'schema': 'modab_autonomy_response_v1', 'operation': operation, 'result': result}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--operation', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(invoke(read(args.config), args.operation, parse(sys.stdin.buffer.read(65537))), ensure_ascii=False))
    except (ContractError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        raise SystemExit(2)


if __name__ == '__main__':
    main()
