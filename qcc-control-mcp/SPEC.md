# QCC Control MCP — Specification

## Purpose
Provide ChatGPT-compatible MCP control over the Oracle-hosted Quant Command Centre without exposing a generic shell or broker-order capability.

## Security boundaries
- No arbitrary command execution tool.
- No IBKR order placement, cancellation, modification, or position-changing tools.
- No reading of `.env`, private keys, SSH material, token files, or other secret-bearing files.
- File reads/writes are restricted to explicit allowed roots.
- Service operations are restricted to explicit allowlisted QCC ChatGPT services.
- Git operations are fixed to the dedicated `qcc-chatgpt-ops` deployment checkout and branch.
- All subprocesses use fixed argv lists, timeouts, and no shell interpolation.
- Streamable HTTP should bind to localhost by default; external exposure must be through an authenticated TLS/tunnel layer.

## Allowed roots
- `/home/ubuntu/openclaw_workspace/qcc-chatgpt-ops`
- `/home/ubuntu/openclaw_workspace/quant_command_centre`

## Blocked file classes
- `.env` and `*.env`
- files containing `secret`, `token`, `password`, `credential`, `private`, `id_rsa`, `id_ed25519`, `.pem`, `.key`
- anything under `.ssh`

## UX flows
1. Inspect QCC status and service health.
2. Run the fixed QCC ChatGPT monitor test suites.
3. Read recent logs from the ChatGPT monitor/scanner services.
4. Read non-secret source/configuration files inside the allowed roots.
5. Write non-secret source/configuration files inside the ChatGPT deployment root only.
6. Pull the dedicated deployment branch with fast-forward-only semantics.
7. Restart only allowlisted ChatGPT QCC services.

## MCP tools
- `get_qcc_status()`
- `run_qcc_tests()`
- `tail_qcc_logs(source, lines)`
- `read_qcc_file(path, max_bytes)`
- `write_chatgpt_file(path, content)`
- `pull_chatgpt_ops()`
- `restart_qcc_service(service)`
- `get_qcc_service_status(service)`

## Transport
- `stdio` for local MCP clients.
- `streamable-http` for remote MCP clients when supported.
- HTTP binds to `127.0.0.1:8765` by default.

## Explicit non-goals
- Generic SSH replacement.
- Generic shell execution.
- Root filesystem browsing.
- Secrets retrieval.
- Trading execution.
