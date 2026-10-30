"""Owned, detached subprocess supervisor. Importable and deployable without deps.

A durable execution marker is written BEFORE process creation. A lost worker
after that marker is UNKNOWN, never permission to execute the command again.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


def utc():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1048576), b''):
            h.update(chunk)
    return h.hexdigest()


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def replace_file(source,destination):
    # Windows readers may briefly deny replacement. Retry only this atomic
    # metadata write, never the scientific process; permanent errors propagate.
    for attempt in range(21):
        try:
            os.replace(source,destination)
            return
        except PermissionError as exc:
            if getattr(exc,'winerror',None) not in {5,32} or attempt==20:raise
            time.sleep(.05)


def write(path, value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode('utf-8')
    fd, temp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        replace_file(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class LockHeld(RuntimeError):
    pass


@contextmanager
def exclusive_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as lock:
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise LockHeld('Another owner holds the task lock') from exc
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def stop_owned_child(child, stderr):
    # Popen retains the process handle; do not discover or kill arbitrary PIDs.
    if child.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill.exe', '/PID', str(child.pid), '/T', '/F'],
            stdin=subprocess.DEVNULL, stdout=stderr, stderr=stderr, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW, check=False)
    else:
        os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
    child.wait(timeout=15)


def group_memory_bytes(group):
    """Linux session RSS includes owned child descendants; conservative shared pages."""
    total = 0
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry/'stat').read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) == group:
                total += int(fields[21]) * os.sysconf('SC_PAGE_SIZE')
        except (FileNotFoundError, ProcessLookupError):
            continue
    return total


def run(directory):
    directory = Path(directory).resolve()
    manifest = json.loads((directory / 'manifest.json').read_bytes())
    if manifest['schema'] != 'modab_owned_job_v1' or directory.name != manifest['job_id']:
        raise ValueError('Worker manifest identity mismatch')
    bound = identity(manifest)
    expected = {'job_id': manifest['job_id'], 'mission_id': manifest['mission_id'],
        'step_id': manifest['step_id'], 'manifest_sha256': bound}
    with exclusive_lock(directory / 'worker.lock'):
        if (directory / 'receipt.json').exists():
            return 0
        if (directory / 'execution-once.json').exists():
            write(directory / 'state.json', {**expected, 'status': 'UNKNOWN',
                'reason': 'Prior execution marker has no receipt; reconcile original execution', 'observed_utc': utc()})
            return 3
        try:
            for path, expected_digest in manifest['inputs'].items():
                if sha(path) != expected_digest:
                    raise ValueError('Worker input identity changed')
        except (OSError, ValueError) as exc:
            failed = {**expected, 'status': 'FAILED', 'process_exit_code': None,
                'started_utc': utc(), 'finished_utc': utc(), 'elapsed_seconds': 0,
                'reason': 'Preflight failed: ' + str(exc), 'files': {}}
            failed['receipt_sha256'] = identity(failed)
            write(directory / 'receipt.json', failed)
            return 2
        timeout = manifest['timeout_seconds']
        if type(timeout) is not int or timeout < 1:
            raise ValueError('Explicit positive execution timeout required')
        if not isinstance(manifest['argv'], list) or not manifest['argv'] or not Path(manifest['argv'][0]).is_absolute():
            raise ValueError('Pinned absolute executable required')
        begin = time.monotonic()
        started = utc()
        receipt = {**expected, 'status': 'FAILED', 'process_exit_code': None,
            'started_utc': started, 'finished_utc': None, 'elapsed_seconds': 0,
            'reason': None, 'files': {}}
        # Publish ownership before the once-only marker. A controller observing
        # this short process-creation window must wait, not infer response loss.
        write(directory / 'state.json', {**expected, 'status': 'STARTING', 'worker_pid': os.getpid(),
            'started_utc': started, 'heartbeat_utc': utc(), 'elapsed_seconds': 0})
        with (directory / 'execution-once.json').open('x', encoding='utf-8') as f:
            json.dump({**expected, 'worker_pid': os.getpid(), 'created_utc': started}, f)
            f.flush()
            os.fsync(f.fileno())
        child = None
        try:
            with (directory / 'stdout.log').open('xb') as out, (directory / 'stderr.log').open('xb') as err:
                if (directory / 'CANCEL').exists():
                    receipt.update(status='CANCELLED', reason='Cancelled before process creation')
                else:
                    flags = {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
                    limits = manifest.get('resource_limits', {})
                    if limits and os.name != 'nt':
                        cpus = sorted(os.sched_getaffinity(0))[:limits['cpu_threads']]
                        flags['preexec_fn'] = lambda: os.sched_setaffinity(0, cpus)
                    child = subprocess.Popen(manifest['argv'], cwd=manifest['cwd'], env=manifest['env'],
                        stdin=subprocess.DEVNULL, stdout=out, stderr=err, shell=False, **flags)
                    last_resource_check = -100
                    last_disk_check = -100
                    receipt['peak_monitored_memory_bytes'] = 0
                    while child.poll() is None:
                        elapsed = time.monotonic() - begin
                        if limits and os.name != 'nt' and elapsed-last_resource_check >= 1:
                            last_resource_check = elapsed
                            rss = group_memory_bytes(child.pid)
                            receipt['peak_monitored_memory_bytes'] = max(receipt['peak_monitored_memory_bytes'], rss)
                            if rss > limits['memory_bytes']:
                                receipt.update(status='FAILED', reason='Commissioned process-tree RSS limit exceeded')
                                stop_owned_child(child, err)
                                break
                            if 'storage_root' in limits and elapsed-last_disk_check>=10:
                                import shutil
                                last_disk_check=elapsed
                                root_used=int(subprocess.check_output(['du','-s','-B1',limits['storage_root']],text=True).split()[0])
                                free=shutil.disk_usage(limits['storage_root']).free
                                if free<limits['min_free_bytes'] or root_used>limits['max_root_bytes']:
                                    receipt.update(status='FAILED',reason='Commissioned disk limit reached')
                                    stop_owned_child(child,err)
                                    break
                        write(directory / 'state.json', {**expected, 'status': 'RUNNING', 'worker_pid': os.getpid(),
                            'child_pid': child.pid, 'started_utc': started, 'heartbeat_utc': utc(), 'elapsed_seconds': elapsed})
                        if (directory / 'CANCEL').exists() or elapsed >= timeout:
                            receipt.update(status='CANCELLED' if (directory / 'CANCEL').exists() else 'TIMED_OUT',
                                reason='User cancellation' if (directory / 'CANCEL').exists() else 'Committed step time limit reached')
                            stop_owned_child(child, err)
                            break
                        time.sleep(0.2)
                    receipt['process_exit_code'] = child.wait()
                    if receipt['reason'] is None:
                        receipt['status'] = 'SUCCEEDED' if child.returncode == 0 else 'FAILED'
                        receipt['reason'] = None if child.returncode == 0 else 'Tool process returned nonzero exit code'
        except BaseException as exc:
            if child and child.poll() is None:
                with (directory / 'stderr.log').open('ab') as err:
                    stop_owned_child(child, err)
            receipt.update(status='FAILED', reason=type(exc).__name__ + ': ' + str(exc),
                process_exit_code=child.returncode if child else None)
        finally:
            receipt.update(finished_utc=utc(), elapsed_seconds=time.monotonic() - begin)
            for name in ('stdout.log', 'stderr.log'):
                if (directory / name).exists():
                    receipt['files'][name] = {'sha256': sha(directory / name), 'bytes': (directory / name).stat().st_size}
            receipt['receipt_sha256'] = identity(receipt)
            write(directory / 'receipt.json', receipt)
            write(directory / 'state.json', {**expected, 'status': receipt['status'], 'receipt_sha256': receipt['receipt_sha256']})
        return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    try:
        return run(args.directory)
    except LockHeld:
        # A duplicate launcher has no authority to interrupt the current owner.
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
