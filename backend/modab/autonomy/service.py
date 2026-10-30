"""Loopback task service and user control UI, independent of the DSH chat turn."""
from __future__ import annotations

import argparse
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sys
import threading
from urllib.parse import urlsplit

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dualctx.contracts import ContractError
from modab.scientific_tools import scientific_tool_catalog
from modab.autonomy.adapters import Registry
from modab.autonomy.contracts import TERMINAL, atomic_json, parse, read
from modab.autonomy.controller import Controller
from modab.autonomy.planners import make_planner
from modab.autonomy.reports import render_html,preferred_report
from modab.autonomy.store import ResearchStore
from modab.autonomy.worker import exclusive_lock


def draft(config, profile_id, objective, request_id, origin):
    profile = config['profiles'].get(profile_id)
    if not profile:
        raise ContractError('Unknown task profile')
    commission = copy.deepcopy(profile['commission'])
    commission.update(objective=objective, request_id=request_id)
    with ResearchStore(config['workspace']) as store:
        return store.create(commission, origin)


def public_state(config):
    with ResearchStore(config['workspace']) as store:
        return {'profiles': {key: {'label': p['label'], 'description': p['description'],
            'activation_ready': p['activation_ready'], 'commission': p['commission']} for key, p in config['profiles'].items()},
            'missions': [store.snapshot(m['mission_id']) for m in store.missions()],
            'catalog': Registry(config).catalog(),
            'scientific_tool_candidates': scientific_tool_catalog()}


def run(config):
    directory = Path(config['workspace'])
    directory.mkdir(parents=True, exist_ok=True)
    host, port = '127.0.0.1', config['port']
    origin = f'http://{host}:{port}'
    token = secrets.token_urlsafe(32)
    stop = threading.Event()
    token_path = directory / 'service.token'
    token_path.write_text(token, encoding='ascii')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def send(self, code, data, content_type='application/json; charset=utf-8'):
            raw = data.encode('utf-8') if isinstance(data, str) else json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(raw)))
            try:
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionError):
                # Leaving the page cannot revoke a committed task operation.
                pass

        def valid_host(self):
            return self.headers.get('Host') == f'{host}:{port}'

        def do_GET(self):
            if not self.valid_host():
                return self.send(403, {'error': 'Local origin required'})
            route = urlsplit(self.path).path
            if route == '/':
                page = Path(__file__).with_name('console.html').read_text(encoding='utf-8')
                return self.send(200, page.replace('__CONTROL_TOKEN__', token), 'text/html; charset=utf-8')
            if route == '/api/state':
                return self.send(200, public_state(config))
            if route.startswith('/report/'):
                mid = route.split('/')[-1]
                try:
                    with ResearchStore(directory) as store:
                        reports = store.snapshot(mid)['reports']
                    if reports:
                        return self.send(200, render_html(preferred_report(reports)['document']), 'text/html; charset=utf-8')
                except (ContractError, ValueError):
                    pass
            self.send(404, {'error': 'Not found'})

        def do_POST(self):
            if not self.valid_host() or self.headers.get('Origin') != origin or self.headers.get('X-ModAb-Control') != token:
                return self.send(403, {'error': 'Direct local user control required'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 65536:
                    raise ContractError('Invalid request size')
                body = parse(self.rfile.read(size))
                route = urlsplit(self.path).path
                if route == '/api/draft':
                    result = draft(config, body['profile_id'], body['objective'], body['request_id'], {'kind': 'direct_web_user'})
                elif route == '/api/control':
                    with ResearchStore(directory) as store:
                        result = store.control(body['mission_id'], body['action'], {'kind': 'direct_web_user'})
                elif route == '/api/approve':
                    with ResearchStore(directory) as store:
                        mission = store.mission(body['mission_id'])
                        # A model can draft but cannot activate an unready resource profile.
                        ready = any(p['activation_ready'] and all(
                            mission['commission'][k] == v for k, v in p['commission'].items()
                            if k not in {'objective', 'request_id'}) for p in config['profiles'].values())
                        if not ready:
                            raise ContractError('Required runtime/resource commission is not activated')
                        result = store.approve(body['mission_id'], body['commission_sha256'], 'local_web_user',
                            'direct-user-click:' + body['commission_sha256'])
                else:
                    return self.send(404, {'error': 'Not found'})
                return self.send(200, result)
            except (ValueError, KeyError, TypeError, OSError, ContractError) as exc:
                self.send(400, {'error': str(exc)})

    def loop():
        while not stop.is_set():
            try:
                with ResearchStore(directory) as store:
                    for mission in store.missions():
                        if mission['approval'] and mission['status'] not in TERMINAL:
                            try:
                                planner = make_planner(config, mission['commission'])
                                Controller(store, config, planner).tick(mission['mission_id'])
                            except (ContractError, KeyError, ValueError, OSError) as exc:
                                store.status(mission['mission_id'], 'NEEDS_ATTENTION', str(exc))
            except Exception as exc:
                atomic_json(directory / 'service-error.json', {'type': type(exc).__name__, 'message': str(exc)})
            if (directory / 'STOP').exists():
                stop.set()
                threading.Thread(target=server.shutdown, daemon=True).start()
                break
            stop.wait(config.get('poll_seconds', 1))

    with exclusive_lock(directory / 'service.lock'):
        server = ThreadingHTTPServer((host, port), Handler)
        atomic_json(directory / 'service.json', {'status': 'RUNNING', 'url': origin,
            'process_id': __import__('os').getpid(), 'configuration_schema': config['schema']})
        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            stop.set()
            server.server_close()
            thread.join(timeout=2)
            atomic_json(directory / 'service.json', {'status': 'STOPPED', 'url': origin})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--migrate', action='store_true')
    args = parser.parse_args()
    config = read(args.config)
    if config['schema'] != 'modab_autonomy_service_v1':
        raise ContractError('Unsupported service config')
    if args.migrate:
        with ResearchStore(config['workspace'], migrate=True):
            pass
        print(json.dumps({'status': 'MIGRATED'}))
    else:
        run(config)


if __name__ == '__main__':
    main()
