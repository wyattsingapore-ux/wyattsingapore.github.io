#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path('/home/ubuntu/openclaw_workspace/qcc-chatgpt-ops')
QCC = Path('/home/ubuntu/openclaw_workspace/quant_command_centre')
PY = QCC / 'venv/bin/python'

ALLOWED = {
    'status': ['systemctl','status','--no-pager','qcc-chatgpt-trigger.service','qcc-control-mcp.service'],
    'trigger-logs': ['journalctl','-u','qcc-chatgpt-trigger.service','--no-pager','-n','80'],
    'mcp-logs': ['journalctl','-u','qcc-control-mcp.service','--no-pager','-n','80'],
    'tests': [str(PY), '-m', 'unittest', '-v', 'test_trigger_monitor.py', 'test_opportunity_scanner.py'],
    'scanner-status': [str(PY), 'chatgpt_opportunity_scanner.py', '--status'],
    'scanner-smoke': [str(PY), 'chatgpt_opportunity_scanner.py', '--once', '--force', '--no-alerts'],
    'restart-trigger': ['sudo','systemctl','restart','qcc-chatgpt-trigger.service'],
    'restart-mcp': ['sudo','systemctl','restart','qcc-control-mcp.service'],
}


def run(cmd: list[str], cwd: Path | None = None) -> int:
    print('$', ' '.join(cmd), flush=True)
    p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
    print(p.stdout[-30000:], end='' if p.stdout.endswith('\n') else '\n')
    return p.returncode


def pull_ops() -> int:
    cmds = [
        ['git','fetch','origin','qcc-chatgpt-ops'],
        ['git','merge','--ff-only','origin/qcc-chatgpt-ops'],
    ]
    rc = 0
    for cmd in cmds:
        rc = run(cmd, ROOT)
        if rc:
            break
    return rc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('command')
    args = ap.parse_args()
    command = args.command.strip()
    if command == 'pull-ops':
        return pull_ops()
    cmd = ALLOWED.get(command)
    if not cmd:
        print(f'REJECTED unknown command: {command}', file=sys.stderr)
        return 64
    cwd = ROOT if command in {'tests','scanner-status','scanner-smoke'} else None
    return run(cmd, cwd)

if __name__ == '__main__':
    raise SystemExit(main())
