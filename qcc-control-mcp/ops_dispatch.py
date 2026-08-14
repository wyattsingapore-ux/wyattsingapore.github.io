#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path('/home/ubuntu/openclaw_workspace/qcc-chatgpt-ops')
SOURCE = ROOT / 'qcc-chatgpt-ops'
QCC = Path('/home/ubuntu/openclaw_workspace/quant_command_centre')
PY = QCC / 'venv/bin/python'

# Only these non-secret source files may be promoted from the Git checkout into
# the Oracle runtime root. Environment files, tokens and keys are never copied.
RUNTIME_FILES = (
    'chatgpt_trigger_monitor.py',
    'test_trigger_monitor.py',
    'chatgpt_opportunity_scanner.py',
    'test_opportunity_scanner.py',
    'qcc-chatgpt-opportunity.service',
)

ALLOWED = {
    'status': ['systemctl','status','--no-pager','qcc-chatgpt-trigger.service','qcc-chatgpt-opportunity.service','qcc-control-mcp.service'],
    'trigger-logs': ['journalctl','-u','qcc-chatgpt-trigger.service','--no-pager','-n','80'],
    'opportunity-logs': ['journalctl','-u','qcc-chatgpt-opportunity.service','--no-pager','-n','80'],
    'mcp-logs': ['journalctl','-u','qcc-control-mcp.service','--no-pager','-n','80'],
    'tests': [str(PY), '-m', 'unittest', '-v', 'test_trigger_monitor.py', 'test_opportunity_scanner.py'],
    'scanner-status': [str(PY), 'chatgpt_opportunity_scanner.py', '--status'],
    'scanner-smoke': [str(PY), 'chatgpt_opportunity_scanner.py', '--once', '--force', '--no-alerts'],
    'restart-trigger': ['sudo','systemctl','restart','qcc-chatgpt-trigger.service'],
    'restart-mcp': ['sudo','systemctl','restart','qcc-control-mcp.service'],
    'restart-opportunity': ['sudo','systemctl','restart','qcc-chatgpt-opportunity.service'],
    'stop-trigger': ['sudo','systemctl','disable','--now','qcc-chatgpt-trigger.service'],
}


def run(cmd: list[str], cwd: Path | None = None) -> int:
    print('$', ' '.join(cmd), flush=True)
    p = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
    )
    print(p.stdout[-30000:], end='' if p.stdout.endswith('\n') else '\n')
    return p.returncode


def sync_runtime_files() -> int:
    for name in RUNTIME_FILES:
        src = SOURCE / name
        dst = ROOT / name
        if not src.is_file():
            print(f'SYNC FAILED missing source: {src}', file=sys.stderr)
            return 66
        shutil.copy2(src, dst)
        print(f'SYNC {src.relative_to(ROOT)} -> {dst.name}')
    # Compile only Python runtime/test files; this catches a bad promotion before
    # a service restart while avoiding any secret-bearing files.
    py_files = [str(ROOT / name) for name in RUNTIME_FILES if name.endswith('.py')]
    return run([str(PY), '-m', 'py_compile', *py_files], ROOT)


def pull_ops() -> int:
    cmds = [
        ['git','fetch','origin','qcc-chatgpt-ops'],
        ['git','merge','--ff-only','origin/qcc-chatgpt-ops'],
    ]
    for cmd in cmds:
        rc = run(cmd, ROOT)
        if rc:
            return rc
    return sync_runtime_files()


def install_opportunity_service() -> int:
    src = ROOT / 'qcc-chatgpt-opportunity.service'
    if not src.is_file():
        print(f'INSTALL FAILED missing service file: {src}', file=sys.stderr)
        return 66
    cmds = [
        ['sudo','install','-m','0644',str(src),'/etc/systemd/system/qcc-chatgpt-opportunity.service'],
        ['sudo','systemctl','daemon-reload'],
        ['sudo','systemctl','enable','--now','qcc-chatgpt-opportunity.service'],
    ]
    for cmd in cmds:
        rc = run(cmd, ROOT)
        if rc:
            return rc
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('command')
    args = ap.parse_args()
    command = args.command.strip()
    if command == 'pull-ops':
        return pull_ops()
    if command == 'install-opportunity':
        return install_opportunity_service()
    cmd = ALLOWED.get(command)
    if not cmd:
        print(f'REJECTED unknown command: {command}', file=sys.stderr)
        return 64
    cwd = ROOT if command in {'tests','scanner-status','scanner-smoke'} else None
    return run(cmd, cwd)

if __name__ == '__main__':
    raise SystemExit(main())
