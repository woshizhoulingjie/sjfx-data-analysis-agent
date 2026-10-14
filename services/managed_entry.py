"""Wait for supervisor ownership before starting an SJFX role."""
import os
from pathlib import Path
import runpy
import signal
import sys
import threading
import time

ROLES = {
    'web': 'app.py', 'worker': 'worker.py',
    'large-worker': 'large_package_worker.py', 'qa-worker': 'scripts/large_qa_worker.py',
    'embedding': 'services/mail/embedding_server.py',
}

def mark_role_ready():
    """Acknowledge successful role initialization only for a managed launch."""
    name = os.environ.get('SJFX_ROLE_READY_FILE')
    token = os.environ.get('SJFX_ROLE_TOKEN')
    if name and token:
        Path(name).write_text(token, encoding='ascii')

def _watch_parent():
    """Reap the owned POSIX session even if role handlers replace SIGTERM."""
    import psutil
    pid = int(os.environ['SJFX_CONTROLLER_PID'])
    created = float(os.environ['SJFX_CONTROLLER_CREATED'])
    def watch():
        while True:
            try:
                parent = psutil.Process(pid)
                alive = abs(parent.create_time() - created) < .001 and parent.status() != psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess:
                alive = False
            except psutil.AccessDenied:
                alive = True
            if not alive:
                if os.getpgrp() == os.getpid():
                    os.killpg(os.getpgrp(), signal.SIGKILL)
                os._exit(1)
            time.sleep(.25)
    threading.Thread(target=watch, daemon=True, name='sjfx-parent-watch').start()

def main():
    role = sys.argv[1] if len(sys.argv) == 2 else ''
    if role not in ROLES:
        raise SystemExit('Unknown SJFX role')
    if os.name != 'nt':
        _watch_parent()
    gate = Path(os.environ['SJFX_PROCESS_GATE'])
    token = os.environ['SJFX_PROCESS_TOKEN']
    deadline = time.monotonic() + 30
    while True:
        try:
            if gate.read_text(encoding='ascii') == token:
                break
        except FileNotFoundError:
            pass
        if time.monotonic() >= deadline:
            raise SystemExit('Supervisor did not acquire process ownership')
        time.sleep(.05)
    os.environ.pop('SJFX_PROCESS_GATE', None)
    os.environ.pop('SJFX_PROCESS_TOKEN', None)
    source = Path(__file__).resolve().parents[1] / ROLES[role]
    sys.argv = [str(source)]
    runpy.run_path(str(source), run_name='__main__')

if __name__ == '__main__':
    main()
