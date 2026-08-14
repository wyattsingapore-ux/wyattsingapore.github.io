#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
from typing import Any

from mcp.server.fastmcp import FastMCP

MCP_NAME = "QCC Control MCP"
QCC_ROOT = Path("/home/ubuntu/openclaw_workspace/quant_command_centre").resolve()
OPS_ROOT = Path("/home/ubuntu/openclaw_workspace/qcc-chatgpt-ops").resolve()
ALLOWED_READ_ROOTS = (QCC_ROOT, OPS_ROOT)
ALLOWED_WRITE_ROOT = OPS_ROOT
ALLOWED_SERVICES = {
    "qcc-chatgpt-trigger.service",
    "qcc-chatgpt-opportunity.service",
    "qcc-control-mcp.service",
}
BLOCKED_MARKERS = (
    ".env", "secret", "token", "password", "credential", "private",
    "id_rsa", "id_ed25519", ".pem", ".key", ".ssh",
)
MAX_WRITE_BYTES = 512_000
DEFAULT_TIMEOUT = 45

mcp = FastMCP(MCP_NAME)


def _run(argv: list[str], cwd: Path | None = None, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    proc = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
        env=os.environ.copy(),
    )
    return {
        "argv": argv,
        "returncode": proc.returncode,
        "stdout": proc.stdout[-20000:],
        "stderr": proc.stderr[-10000:],
    }


def _inside(path: Path, roots: tuple[Path, ...]) -> bool:
    try:
        resolved = path.resolve(strict=False)
        return any(resolved == r or r in resolved.parents for r in roots)
    except OSError:
        return False


def _blocked(path: Path) -> bool:
    text = str(path).lower()
    return any(marker in text for marker in BLOCKED_MARKERS)


def _validate_read_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = OPS_ROOT / path
    path = path.resolve(strict=False)
    if not _inside(path, ALLOWED_READ_ROOTS):
        raise ValueError("Path outside allowed QCC roots")
    if _blocked(path):
        raise ValueError("Secret-bearing or sensitive path is blocked")
    return path


def _validate_write_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = OPS_ROOT / path
    path = path.resolve(strict=False)
    if not _inside(path, (ALLOWED_WRITE_ROOT,)):
        raise ValueError("Writes are restricted to qcc-chatgpt-ops")
    if _blocked(path):
        raise ValueError("Secret-bearing or sensitive path is blocked")
    return path


def _validate_service(service: str) -> str:
    if service not in ALLOWED_SERVICES:
        raise ValueError(f"Service is not allowlisted: {service}")
    return service


@mcp.tool()
def get_qcc_status() -> dict[str, Any]:
    """Return host, QCC paths, git status, and allowlisted service states without exposing secrets."""
    services = {}
    for service in sorted(ALLOWED_SERVICES):
        services[service] = _run(["systemctl", "is-active", service], timeout=10)
    return {
        "host": _run(["hostname"], timeout=5),
        "user": _run(["whoami"], timeout=5),
        "qcc_root": str(QCC_ROOT),
        "ops_root": str(OPS_ROOT),
        "ops_git": _run(["git", "status", "--short", "--branch"], cwd=OPS_ROOT, timeout=15),
        "services": services,
    }


@mcp.tool()
def run_qcc_tests() -> dict[str, Any]:
    """Run only the fixed ChatGPT monitor/scanner unittest suites in qcc-chatgpt-ops."""
    py = QCC_ROOT / "venv" / "bin" / "python"
    return _run(
        [str(py), "-m", "unittest", "-v", "test_trigger_monitor.py", "test_opportunity_scanner.py"],
        cwd=OPS_ROOT,
        timeout=120,
    )


@mcp.tool()
def tail_qcc_logs(source: str = "trigger-file", lines: int = 50) -> dict[str, Any]:
    """Read recent non-secret monitor logs. Sources: trigger-file, trigger-journal, opportunity-journal, mcp-journal."""
    lines = max(1, min(int(lines), 500))
    if source == "trigger-file":
        path = OPS_ROOT / "logs" / "chatgpt_trigger_monitor.log"
        return _run(["tail", "-n", str(lines), str(path)], timeout=10)
    mapping = {
        "trigger-journal": "qcc-chatgpt-trigger.service",
        "opportunity-journal": "qcc-chatgpt-opportunity.service",
        "mcp-journal": "qcc-control-mcp.service",
    }
    if source not in mapping:
        raise ValueError("Unsupported log source")
    return _run(["journalctl", "-u", mapping[source], "-n", str(lines), "--no-pager"], timeout=15)


@mcp.tool()
def read_qcc_file(path: str, max_bytes: int = 120_000) -> dict[str, Any]:
    """Read a non-secret UTF-8 file inside the allowlisted QCC roots."""
    p = _validate_read_path(path)
    if not p.exists() or not p.is_file():
        raise ValueError("File not found")
    if p.is_symlink():
        raise ValueError("Symlink reads are blocked")
    max_bytes = max(1, min(int(max_bytes), 500_000))
    data = p.read_bytes()[:max_bytes]
    text = data.decode("utf-8", errors="replace")
    return {"path": str(p), "bytes_returned": len(data), "content": text}


@mcp.tool()
def write_chatgpt_file(path: str, content: str) -> dict[str, Any]:
    """Write a non-secret UTF-8 file under qcc-chatgpt-ops only. Secret/env/key paths are blocked."""
    p = _validate_write_path(path)
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        raise ValueError("Content exceeds write limit")
    if p.exists() and p.is_symlink():
        raise ValueError("Symlink writes are blocked")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(p)
    return {"path": str(p), "bytes_written": len(content.encode("utf-8"))}


@mcp.tool()
def pull_chatgpt_ops() -> dict[str, Any]:
    """Fast-forward the dedicated qcc-chatgpt-ops branch only; no arbitrary repo/ref input is accepted."""
    fetch = _run(["git", "fetch", "origin", "qcc-chatgpt-ops"], cwd=OPS_ROOT, timeout=60)
    if fetch["returncode"] != 0:
        return {"fetch": fetch, "merge": None}
    merge = _run(["git", "merge", "--ff-only", "origin/qcc-chatgpt-ops"], cwd=OPS_ROOT, timeout=60)
    return {"fetch": fetch, "merge": merge}


@mcp.tool()
def restart_qcc_service(service: str) -> dict[str, Any]:
    """Restart one explicitly allowlisted ChatGPT QCC service via passwordless sudo."""
    service = _validate_service(service)
    restart = _run(["sudo", "-n", "systemctl", "restart", service], timeout=30)
    status = _run(["systemctl", "is-active", service], timeout=10)
    return {"restart": restart, "status": status}


@mcp.tool()
def get_qcc_service_status(service: str) -> dict[str, Any]:
    """Return status for one explicitly allowlisted ChatGPT QCC service."""
    service = _validate_service(service)
    active = _run(["systemctl", "is-active", service], timeout=10)
    show = _run(
        ["systemctl", "show", service, "-p", "MainPID", "-p", "ActiveEnterTimestamp", "-p", "SubState"],
        timeout=10,
    )
    return {"active": active, "show": show}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default=os.getenv("QCC_MCP_TRANSPORT", "stdio"))
    parser.add_argument("--host", default=os.getenv("QCC_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("QCC_MCP_PORT", "8765")))
    args = parser.parse_args()

    if args.transport == "streamable-http":
        # FastMCP uses these settings for its HTTP transport.
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
