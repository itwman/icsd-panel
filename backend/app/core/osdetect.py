"""OS / distribution detection — abstraction layer.

لایهٔ تشخیص توزیع و مدیر بسته. کل کد بالادست از این انتزاع استفاده می‌کند
تا تفاوت‌های بین خانوادهٔ Debian (apt) و RHEL (dnf) پنهان بماند.
"""
from __future__ import annotations

import platform
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass
class OSInfo:
    id: str            # ubuntu, debian, almalinux, rocky, centos ...
    family: str        # debian | rhel | unknown
    version_id: str
    pretty_name: str
    package_manager: str   # apt | dnf | yum | unknown
    service_manager: str   # systemd | unknown
    webserver_conf_dir: str
    supported: bool
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


_DEBIAN = {"ubuntu", "debian", "raspbian", "linuxmint"}
_RHEL = {"almalinux", "rocky", "centos", "rhel", "fedora", "ol"}

# CentOS Linux 7/8 are EOL (June 2024). We warn and recommend AlmaLinux/Rocky.
_EOL_HINT = (
    "CentOS Linux به پایان عمر رسیده — مهاجرت به AlmaLinux 9 یا Rocky Linux 9 توصیه می‌شود. "
    "(CentOS Linux is EOL — migrate to AlmaLinux 9 or Rocky Linux 9.)"
)


def _parse_os_release(path: str = "/etc/os-release") -> dict:
    data: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return data
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        if "=" in line:
            key, _, val = line.partition("=")
            data[key.strip()] = val.strip().strip('"')
    return data


def detect_os() -> OSInfo:
    """Detect the running distribution and return a normalized OSInfo."""
    rel = _parse_os_release()
    os_id = (rel.get("ID") or "").lower()
    id_like = (rel.get("ID_LIKE") or "").lower()
    version_id = rel.get("VERSION_ID") or ""
    pretty = rel.get("PRETTY_NAME") or platform.platform()

    # Determine family
    if os_id in _DEBIAN or "debian" in id_like:
        family = "debian"
        pkg = "apt"
        conf_dir = "/etc/nginx/sites-available"
    elif os_id in _RHEL or "rhel" in id_like or "fedora" in id_like:
        family = "rhel"
        pkg = "dnf"
        conf_dir = "/etc/nginx/conf.d"
    else:
        family = "unknown"
        pkg = "unknown"
        conf_dir = "/etc/nginx/conf.d"

    supported = family in ("debian", "rhel")
    note = ""

    # EOL handling for CentOS Linux 7/8
    if os_id == "centos":
        major = version_id.split(".")[0] if version_id else ""
        if major in ("7", "8"):
            supported = False
            note = _EOL_HINT
        else:
            note = "CentOS Stream — برای پروداکشن پایدار AlmaLinux/Rocky توصیه می‌شود."

    if not supported and not note:
        note = "توزیع پشتیبانی‌نشده — Ubuntu/Debian یا AlmaLinux/Rocky توصیه می‌شود."

    return OSInfo(
        id=os_id or "unknown",
        family=family,
        version_id=version_id,
        pretty_name=pretty,
        package_manager=pkg,
        service_manager="systemd" if Path("/run/systemd/system").exists() else "unknown",
        webserver_conf_dir=conf_dir,
        supported=supported,
        note=note,
    )


if __name__ == "__main__":
    import json
    print(json.dumps(detect_os().to_dict(), ensure_ascii=False, indent=2))
