"""Small dependency-free host telemetry sampler for kiosk dashboard agents."""
from __future__ import annotations

import os
import platform
import shutil
import time
from pathlib import Path
from typing import Any


def _human_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


def _read_cpu() -> tuple[int, int] | None:
    try:
        fields = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
        values = [int(value) for value in fields]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return sum(values), idle
    except (OSError, ValueError, IndexError):
        return None


def _read_memory() -> tuple[int, int] | None:
    try:
        values = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            values[key] = int(value.split()[0]) * 1024
        total = values["MemTotal"]
        available = values.get("MemAvailable", values.get("MemFree", 0))
        return total, max(0, total - available)
    except (OSError, ValueError, KeyError):
        return None


def _read_network() -> tuple[int, int] | None:
    try:
        received = sent = 0
        for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
            interface, values = line.split(":", 1)
            if interface.strip() == "lo":
                continue
            numbers = values.split()
            received += int(numbers[0])
            sent += int(numbers[8])
        return received, sent
    except (OSError, ValueError, IndexError):
        return None


def _read_temperature() -> float | None:
    for source in sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp")):
        try:
            value = float(source.read_text().strip())
            return round(value / 1000 if value > 1000 else value, 1)
        except (OSError, ValueError):
            continue
    return None


def _os_name() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.system()


class HostTelemetryCollector:
    """Calculates CPU and network rates from consecutive local samples."""

    def __init__(self) -> None:
        self._cpu: tuple[int, int] | None = None
        self._network: tuple[float, int, int] | None = None

    def snapshot(self) -> dict[str, Any]:
        sampled_at = time.time()
        cpu_pct = 0.0
        cpu = _read_cpu()
        if cpu and self._cpu:
            total_delta = cpu[0] - self._cpu[0]
            idle_delta = cpu[1] - self._cpu[1]
            if total_delta > 0:
                cpu_pct = round(max(0, min(100, (1 - idle_delta / total_delta) * 100)), 1)
        if cpu:
            self._cpu = cpu

        memory = _read_memory()
        disk = shutil.disk_usage("/")
        network = _read_network()
        rx_rate = tx_rate = 0.0
        if network and self._network:
            previous_at, previous_rx, previous_tx = self._network
            elapsed = max(sampled_at - previous_at, .001)
            rx_rate = max(0, (network[0] - previous_rx) / elapsed)
            tx_rate = max(0, (network[1] - previous_tx) / elapsed)
        if network:
            self._network = (sampled_at, *network)

        uptime = 0.0
        try:
            uptime = float(Path("/proc/uptime").read_text().split()[0])
        except (OSError, ValueError, IndexError):
            pass
        days, remaining = divmod(int(uptime), 86400)
        hours, remaining = divmod(remaining, 3600)
        minutes, _ = divmod(remaining, 60)
        memory_total, memory_used = memory or (0, 0)
        return {
            "hostname": platform.node(),
            "os": _os_name(),
            "cpu_pct": cpu_pct,
            "ram_pct": round(memory_used / memory_total * 100, 1) if memory_total else None,
            "ram_used": _human_bytes(memory_used),
            "ram_total": _human_bytes(memory_total),
            "disk_pct": round(disk.used / disk.total * 100, 1) if disk.total else None,
            "disk_used": _human_bytes(disk.used),
            "disk_total": _human_bytes(disk.total),
            "temperature_c": _read_temperature(),
            "load": [round(value, 2) for value in os.getloadavg()],
            "uptime": f"{days}d {hours}h {minutes}m",
            "net_rx_rate": round(rx_rate, 1),
            "net_tx_rate": round(tx_rate, 1),
            "sampled_at": sampled_at,
        }
