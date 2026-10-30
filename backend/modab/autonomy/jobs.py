"""Typed local job transport; submission is idempotent and UNKNOWN is sticky."""
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import threading

from dualctx.contracts import ContractError
from . import worker
from .contracts import atomic_json, identifier, read


class LocalJobs:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def directory(self, job_id):
        return self.root / identifier(job_id)

    def submit(self, manifest):
        directory = self.directory(manifest['job_id'])
        directory.mkdir(exist_ok=True)
        atomic_json(directory / 'manifest.json', manifest, immutable=True)
        state = self.status(manifest['job_id'])
        if state['status'] not in {'NOT_FOUND', 'PREPARED'}:
            return state
        # Several launchers may be created after response loss. The durable
        # worker lock and execution-once marker prevent a second tool process.
        with (directory / 'supervisor.log').open('ab') as log:
            flags = {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
            child = subprocess.Popen([sys.executable, '-B', '-I', '-S', '-X', 'utf8', str(Path(worker.__file__).resolve()), str(directory)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, **flags)
            # Retain/reap the local supervisor handle without tying its lifetime
            # to the browser or to this service process.
            threading.Thread(target=child.wait, daemon=True).start()
        return {'status': 'SUBMITTED', 'job_id': manifest['job_id'], 'manifest_sha256': worker.identity(manifest)}

    def status(self, job_id):
        directory = self.directory(job_id)
        if not (directory / 'manifest.json').exists():
            return {'status': 'NOT_FOUND', 'job_id': job_id}
        manifest = read(directory / 'manifest.json')
        if manifest['job_id'] != job_id:
            raise ContractError('Job manifest identity changed')
        expected = worker.identity(manifest)
        if (directory / 'receipt.json').exists():
            result = read(directory / 'receipt.json')
            body = {k: v for k, v in result.items() if k != 'receipt_sha256'}
            if worker.identity(body) != result['receipt_sha256'] or result['manifest_sha256'] != expected:
                raise ContractError('Job receipt identity mismatch')
            return result
        if (directory / 'state.json').exists():
            result = read(directory / 'state.json')
            if result['manifest_sha256'] != expected or result['job_id'] != job_id:
                raise ContractError('Job state identity mismatch')
            if result['status'] in {'STARTING','RUNNING'}:
                heartbeat = datetime.fromisoformat(result['heartbeat_utc'])
                if (datetime.now(timezone.utc) - heartbeat).total_seconds() > 15:
                    return {**result, 'status': 'UNKNOWN', 'reason': 'Worker heartbeat stale; do not resubmit'}
            return result
        if (directory / 'execution-once.json').exists():
            # The worker may have published STARTING after the first existence
            # check above; reread the same job instead of declaring it lost.
            if (directory / 'state.json').exists():
                return self.status(job_id)
            return {'status': 'UNKNOWN', 'job_id': job_id, 'manifest_sha256': expected,
                'reason': 'Execution claimed without current state or final receipt'}
        return {'status': 'PREPARED', 'job_id': job_id, 'manifest_sha256': expected}

    def cancel(self, job_id):
        directory = self.directory(job_id)
        if not (directory / 'manifest.json').exists():
            return {'status': 'NOT_FOUND', 'job_id': job_id}
        (directory / 'CANCEL').touch(exist_ok=True)
        return self.status(job_id)


def inherited_environment(extra=None):
    names = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATH', 'PATHEXT', 'TEMP', 'TMP',
        'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'HOME', 'LANG', 'LC_ALL', 'LD_LIBRARY_PATH', 'SSL_CERT_FILE', 'SSL_CERT_DIR'}
    result = {key: value for key, value in os.environ.items() if key.upper() in names}
    result.update({'PYTHONUNBUFFERED': '1', 'PYTHONDONTWRITEBYTECODE': '1'})
    result.update(extra or {})
    return result
