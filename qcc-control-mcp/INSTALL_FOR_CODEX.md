# QCC Control MCP — Oracle install handoff

ChatGPT owns the MCP design/code. Codex is only the Oracle deployment hand.

## Source
Repository: `wyattsingapore-ux/wyattsingapore.github.io`
Branch: `qcc-chatgpt-ops`
Directory: `qcc-control-mcp/`

## Oracle target
Host: `134.185.86.240`
User: `ubuntu`
Ops checkout: `/home/ubuntu/openclaw_workspace/qcc-chatgpt-ops`
MCP venv: `/home/ubuntu/openclaw_workspace/qcc-chatgpt-ops/.mcp-venv`
Service: `qcc-control-mcp.service`
Listener: `127.0.0.1:8765` only

## Required actions
1. Pull latest `qcc-chatgpt-ops` branch with fast-forward only. Preserve local Oracle-only secret/env files.
2. Create the isolated MCP venv if absent:
   `python3 -m venv /home/ubuntu/openclaw_workspace/qcc-chatgpt-ops/.mcp-venv`
3. Upgrade pip in that venv and install only `qcc-control-mcp/requirements.txt`.
4. Syntax/import test:
   `/home/ubuntu/openclaw_workspace/qcc-chatgpt-ops/.mcp-venv/bin/python -m py_compile qcc-control-mcp/server.py`
5. Import test:
   `/home/ubuntu/openclaw_workspace/qcc-chatgpt-ops/.mcp-venv/bin/python -c "import sys; sys.path.insert(0,'qcc-control-mcp'); import server; print('import ok')"`
6. Install `qcc-control-mcp/qcc-control-mcp.service` as `/etc/systemd/system/qcc-control-mcp.service`.
7. `sudo systemctl daemon-reload && sudo systemctl enable --now qcc-control-mcp.service`
8. Confirm it binds ONLY localhost:
   `ss -lntp | grep 8765`
   Expected address must be `127.0.0.1:8765` (or equivalent localhost), never `0.0.0.0`.
9. Return service status and last 30 journal lines.
10. Do not add nginx/public ingress, firewall openings, tunnel, public DNS, OAuth, or external exposure yet.
11. Do not modify `server.py` to work around an error. If install/import/start fails, return the exact sanitized error and stop; ChatGPT will fix the code.
12. Do not print any secret values.

## Optional safe smoke tests
If the MCP package provides a local inspector/client already available on the host, list the tools and invoke only `get_qcc_status`. Do not install a separate browser or expose the port publicly solely for the smoke test.

## Report back
- Python/package versions relevant to MCP
- install success/failure
- py_compile result
- import result
- systemd status
- `ss` line for port 8765
- last 30 MCP journal lines
- whether any external port/firewall/nginx change was made (expected: NO)
