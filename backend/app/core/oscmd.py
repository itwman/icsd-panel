"""Safe system-command execution helper.

اجرای امن دستورات سیستمی. تمام دستورات به‌صورت لیست آرگومان (بدون shell=True)
اجرا می‌شوند تا از تزریق فرمان جلوگیری شود. در معماری نهایی، این فراخوانی‌ها از طریق
«عامل سیستمی privileged» انجام می‌شوند؛ این ماژول واسط آن است.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class CmdResult:
    ok: bool
    code: int
    stdout: str
    stderr: str


def run(args: list[str], timeout: int = 30, check: bool = False) -> CmdResult:
    """Run a command given as an argument list (never a shell string)."""
    if not args:
        raise ValueError("empty command")
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout
        )
        res = CmdResult(
            ok=(proc.returncode == 0),
            code=proc.returncode,
            stdout=proc.stdout.strip(),
            stderr=proc.stderr.strip(),
        )
    except FileNotFoundError:
        res = CmdResult(False, 127, "", f"command not found: {args[0]}")
    except subprocess.TimeoutExpired:
        res = CmdResult(False, 124, "", f"timeout after {timeout}s")
    if check and not res.ok:
        raise RuntimeError(f"command failed ({res.code}): {res.stderr or res.stdout}")
    return res


def has(binary: str) -> bool:
    """Whether a binary exists on PATH."""
    return shutil.which(binary) is not None


# --- Privileged execution (via sudo when not already root) ---

def sudo_prefix() -> list[str]:
    """Return ['sudo','-n'] when the panel runs unprivileged and sudo exists.

    The installer grants this user passwordless sudo for the management binaries
    it needs (systemctl, useradd, crontab, mysql, git, …). When already root
    (e.g. dev), the prefix is empty.
    """
    try:
        if os.geteuid() != 0 and has("sudo"):
            return ["sudo", "-n"]
    except AttributeError:  # non-POSIX (e.g. Windows dev) — no geteuid
        pass
    return []


def run_priv(args: list[str], timeout: int = 60, check: bool = False) -> CmdResult:
    """Run a command with root privileges (prepends sudo when needed)."""
    if not args:
        raise ValueError("empty command")
    return run(sudo_prefix() + args, timeout=timeout, check=check)


# --- Web server helpers (Nginx) ---

def nginx_test() -> CmdResult:
    """Validate Nginx configuration (nginx -t)."""
    return run(["nginx", "-t"])


def nginx_reload() -> CmdResult:
    """Reload Nginx. Prefers systemctl, falls back to `nginx -s reload`."""
    if has("systemctl"):
        return run(["systemctl", "reload", "nginx"])
    return run(["nginx", "-s", "reload"])


def systemctl(action: str, service: str) -> CmdResult:
    return run(["systemctl", action, service])
