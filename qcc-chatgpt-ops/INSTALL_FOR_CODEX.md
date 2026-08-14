# Oracle install handoff — access/deployment only

ChatGPT owns the monitor implementation. Codex should only install the files from this branch onto the Oracle QCC host and wire them to the EXISTING QCC environment/secrets.

## Source
Repository: wyattsingapore-ux/wyattsingapore.github.io
Branch: qcc-chatgpt-ops
Directory: qcc-chatgpt-ops/

## Oracle target
Host: 134.185.86.240
User: ubuntu
QCC root: /home/ubuntu/openclaw_workspace/quant_command_centre
Deployment directory: /home/ubuntu/openclaw_workspace/qcc-chatgpt-ops
Python: /home/ubuntu/openclaw_workspace/quant_command_centre/venv/bin/python

## Required actions
1. Clone/fetch ONLY the qcc-chatgpt-ops branch into /home/ubuntu/openclaw_workspace/qcc-chatgpt-ops. Do not modify QCC trading logic or existing Claude alert engines.
2. Locate the existing environment file/config that currently supplies TWELVEDATA_API_KEY, TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID. Do not print or copy secret values into GitHub or chat.
3. Edit the local copy of qcc-chatgpt-trigger.service only as needed to add an EnvironmentFile line pointing to that existing QCC env file. Do not embed secret values in the unit.
4. Run the unit tests: `cd /home/ubuntu/openclaw_workspace/qcc-chatgpt-ops && /home/ubuntu/openclaw_workspace/quant_command_centre/venv/bin/python -m unittest -v test_trigger_monitor.py`
5. Run status: `/home/ubuntu/openclaw_workspace/quant_command_centre/venv/bin/python /home/ubuntu/openclaw_workspace/qcc-chatgpt-ops/chatgpt_trigger_monitor.py --status`
6. Run one live scan without alerts: `/home/ubuntu/openclaw_workspace/quant_command_centre/venv/bin/python /home/ubuntu/openclaw_workspace/qcc-chatgpt-ops/chatgpt_trigger_monitor.py --once --force --no-alerts`
7. Send one clearly marked Telegram test: `/home/ubuntu/openclaw_workspace/quant_command_centre/venv/bin/python /home/ubuntu/openclaw_workspace/qcc-chatgpt-ops/chatgpt_trigger_monitor.py --test-telegram`
8. Install the service as `/etc/systemd/system/qcc-chatgpt-trigger.service`, run daemon-reload, enable and start it.
9. Do not add broker/order permissions or imports. This service is alert-only.
10. Report back only: test results, status output, one-scan output with secrets omitted, Telegram test success/failure, service status, and last 20 monitor log lines. Do not alter the monitor algorithm; ChatGPT will handle any code changes via this branch.

## Optional deployment lane for later ChatGPT updates
If safe, add a simple root-owned or ubuntu-owned update script that fetches this branch and restarts qcc-chatgpt-trigger.service only when HEAD changes. Do not auto-merge or modify main branch.
