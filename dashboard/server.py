#!/usr/bin/env python3
"""
Aurabase Server Live Monitoring Dashboard
Zero-dependency Python 3 telemetry daemon (< 20 MB RAM, read-only /proc & /sys).
Serves accurate hardware vitals, storage/memory math matching df/free, wireless link
metrics, systemd health, and per-container resource usage on port 8088.
"""

import glob
import json
import math
import os
import platform
import re
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8088
CACHE_LOCK = threading.Lock()
SLOW_LOCK = threading.Lock()
CURRENT_STATS = {}
SLOW_CACHE = {
    "containers": [],
    "services": [],
    "wifi": {},
}


def read_file(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().strip()
    except Exception:
        return default


def read_int(path: str, default: int | None = None) -> int | None:
    raw = read_file(path)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def get_static_host_info() -> dict:
    model = "Unknown CPU"
    cores = 0
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("model name") and model == "Unknown CPU":
                    model = line.split(":", 1)[1].strip()
                elif line.startswith("processor"):
                    cores += 1
    except Exception:
        pass

    os_name = "Debian Linux"
    os_release = read_file("/etc/os-release")
    for line in os_release.splitlines():
        if line.startswith("PRETTY_NAME="):
            os_name = line.split("=", 1)[1].strip().strip('"')
            break

    product = read_file("/sys/devices/virtual/dmi/id/product_name", "Linux Host")
    vendor = read_file("/sys/devices/virtual/dmi/id/sys_vendor", "")
    hw_model = f"{vendor} {product}".strip() if vendor and vendor not in product else product

    disk_model = "Primary Disk"
    disk_size_gb = None
    for dev in ("sda", "nvme0n1", "vda"):
        m = read_file(f"/sys/block/{dev}/device/model")
        sectors = read_int(f"/sys/block/{dev}/size")
        if m or sectors:
            if m:
                disk_model = m
            if sectors:
                disk_size_gb = round((sectors * 512) / 1e9, 1)
            break

    return {
        "cpu_model": model,
        "cores": cores or (os.cpu_count() or 1),
        "os_name": os_name,
        "kernel": platform.release(),
        "arch": platform.machine(),
        "hw_model": hw_model,
        "disk_model": disk_model,
        "disk_physical_gb": disk_size_gb,
    }


HOST_STATIC = get_static_host_info()


def get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def get_cpu_freq_mhz() -> float | None:
    freqs = []
    for p in sorted(glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")):
        val = read_int(p)
        if val:
            freqs.append(val / 1000.0)
    if freqs:
        return round(sum(freqs) / len(freqs), 0)
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as f:
            mhz = [
                float(line.split(":", 1)[1].strip())
                for line in f
                if line.startswith("cpu MHz")
            ]
        if mhz:
            return round(sum(mhz) / len(mhz), 0)
    except Exception:
        pass
    return None


def get_temps_and_sensors() -> dict:
    cpu_temp = None
    cpu_high = 70.0
    cpu_crit = 100.0
    gpu_temp = None
    gpu_sclk_mhz = None
    gpu_vdd_v = None
    apu_power_mw = None
    fan_rpm = None

    for p in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        name = read_file(f"{p}/name")
        if "k10temp" in name or "coretemp" in name:
            val = read_int(f"{p}/temp1_input")
            if val is not None:
                cpu_temp = round(val / 1000.0, 1)
            hi = read_int(f"{p}/temp1_max")
            cr = read_int(f"{p}/temp1_crit")
            if hi:
                cpu_high = round(hi / 1000.0, 1)
            if cr:
                cpu_crit = round(cr / 1000.0, 1)
        elif "amdgpu" in name:
            val = read_int(f"{p}/temp1_input")
            if val is not None:
                gpu_temp = round(val / 1000.0, 1)
            sclk = read_int(f"{p}/freq1_input")
            if sclk is not None:
                gpu_sclk_mhz = round(sclk / 1e6, 0)
            vdd = read_int(f"{p}/in0_input")
            if vdd is not None:
                gpu_vdd_v = round(vdd / 1000.0, 2)
        elif "fam15h_power" in name:
            avg_uw = read_int(f"{p}/power1_average")
            cur_uw = read_int(f"{p}/power1_input")
            val_uw = avg_uw if avg_uw is not None else cur_uw
            if val_uw is not None:
                apu_power_mw = round(val_uw / 1000.0, 1)

        for fan_file in glob.glob(f"{p}/fan*_input"):
            f_val = read_int(fan_file)
            if f_val is not None:
                fan_rpm = f_val

    return {
        "cpu_temp": cpu_temp,
        "cpu_high": cpu_high,
        "cpu_crit": cpu_crit,
        "gpu_temp": gpu_temp,
        "gpu_sclk_mhz": gpu_sclk_mhz,
        "gpu_vdd_v": gpu_vdd_v,
        "apu_power_mw": apu_power_mw,
        "fan_rpm": fan_rpm,
        "fan_status": f"{fan_rpm} RPM" if fan_rpm is not None else "EC Auto (No tach sensor)",
    }


def get_power_and_battery() -> dict:
    bat_path = "/sys/class/power_supply/BAT1"
    if not os.path.isdir(bat_path):
        cands = [p for p in glob.glob("/sys/class/power_supply/BAT*")]
        if cands:
            bat_path = cands[0]

    status = read_file(f"{bat_path}/status", "Unknown")
    capacity = read_int(f"{bat_path}/capacity", 0) or 0
    volt_uv = read_int(f"{bat_path}/voltage_now")
    pwr_uw = read_int(f"{bat_path}/power_now")
    e_now_uwh = read_int(f"{bat_path}/energy_now")
    e_full_uwh = read_int(f"{bat_path}/energy_full")
    e_design_uwh = read_int(f"{bat_path}/energy_full_design")

    volts = round(volt_uv / 1e6, 2) if volt_uv is not None else None
    bat_watts = round(pwr_uw / 1e6, 2) if pwr_uw is not None else None
    energy_now_wh = round(e_now_uwh / 1e6, 2) if e_now_uwh is not None else None
    energy_full_wh = round(e_full_uwh / 1e6, 2) if e_full_uwh is not None else None
    energy_design_wh = round(e_design_uwh / 1e6, 2) if e_design_uwh is not None else None
    health_pct = (
        round((e_full_uwh / e_design_uwh) * 100.0, 1)
        if e_full_uwh and e_design_uwh
        else None
    )

    ac_online = False
    for ac_path in glob.glob("/sys/class/power_supply/*/online"):
        if "AC" in ac_path or read_file(os.path.join(os.path.dirname(ac_path), "type")) == "Mains":
            ac_online = read_file(ac_path) == "1"
            break

    cm_path = "/sys/bus/platform/drivers/ideapad_acpi/VPC2004:00/conservation_mode"
    conservation_mode = read_file(cm_path) == "1"

    if ac_online:
        if conservation_mode and (status in ("Not charging", "Full") or (bat_watts is not None and bat_watts <= 0.05 and capacity >= 55)):
            mode_desc = "AC Connected · Conservation Bypass"
        else:
            mode_desc = f"AC Connected · {status}"
    else:
        mode_desc = f"Unplugged · On Battery ({status})"

    return {
        "status": status,
        "capacity": capacity,
        "bat_watts": bat_watts,
        "volts": volts,
        "energy_now_wh": energy_now_wh,
        "energy_full_wh": energy_full_wh,
        "energy_design_wh": energy_design_wh,
        "health_pct": health_pct,
        "ac_connected": ac_online,
        "conservation_mode": conservation_mode,
        "mode_desc": mode_desc,
    }


def get_memory_info() -> dict:
    mem = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if ":" in line:
                    k, v = line.split(":", 1)
                    mem[k.strip()] = int(v.strip().split()[0])
    except Exception:
        pass

    total_kb = mem.get("MemTotal", 1)
    avail_kb = mem.get("MemAvailable", 0)
    free_kb = mem.get("MemFree", 0)
    buffers_kb = mem.get("Buffers", 0)
    cached_kb = mem.get("Cached", 0)
    reclaim_kb = mem.get("SReclaimable", 0)

    # Match procps-ng `free` formula: used = MemTotal - MemAvailable
    used_kb = max(0, total_kb - avail_kb)
    buff_cache_kb = buffers_kb + cached_kb + reclaim_kb
    ram_pct = round((used_kb / total_kb) * 100.0, 1)

    swap_total = mem.get("SwapTotal", 0)
    swap_free = mem.get("SwapFree", 0)
    swap_used = max(0, swap_total - swap_free)
    swap_pct = round((swap_used / swap_total) * 100.0, 1) if swap_total > 0 else 0.0

    return {
        "ram_total_mb": round(total_kb / 1024.0, 1),
        "ram_used_mb": round(used_kb / 1024.0, 1),
        "ram_free_mb": round(free_kb / 1024.0, 1),
        "ram_avail_mb": round(avail_kb / 1024.0, 1),
        "ram_buff_cache_mb": round(buff_cache_kb / 1024.0, 1),
        "ram_pct": ram_pct,
        "swap_total_mb": round(swap_total / 1024.0, 1),
        "swap_used_mb": round(swap_used / 1024.0, 1),
        "swap_pct": swap_pct,
    }


def get_disk_info() -> dict:
    st = os.statvfs("/")
    total_b = st.f_blocks * st.f_frsize
    free_b = st.f_bfree * st.f_frsize
    avail_b = st.f_bavail * st.f_frsize
    # Match GNU df: Used = Total - Free (does not count ext4 5% root-reserved blocks as used data)
    used_b = max(0, total_b - free_b)
    nonroot_total = used_b + avail_b
    df_pct = math.ceil((used_b / nonroot_total) * 100.0) if nonroot_total > 0 else 0

    mount_opts = "ext4"
    fs_dev = "/"
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[1] == "/":
                    fs_dev = parts[0]
                    mount_opts = f"{parts[2]} ({parts[3]})"
                    break
    except Exception:
        pass

    ra_kb = read_int("/sys/block/sda/queue/read_ahead_kb")
    return {
        "device": fs_dev,
        "model": HOST_STATIC["disk_model"],
        "physical_gb": HOST_STATIC["disk_physical_gb"],
        "total_gib": round(total_b / (1024**3), 1),
        "used_gib": round(used_b / (1024**3), 2),
        "avail_gib": round(avail_b / (1024**3), 1),
        "used_pct": df_pct,
        "mount_opts": mount_opts,
        "readahead_kb": ra_kb,
    }


def get_uptime_str() -> str:
    try:
        up_sec = float(read_file("/proc/uptime").split()[0])
        days = int(up_sec // 86400)
        hours = int((up_sec % 86400) // 3600)
        mins = int((up_sec % 3600) // 60)
        secs = int(up_sec % 60)
        if days > 0:
            return f"{days}d {hours}h {mins}m"
        if hours > 0:
            return f"{hours}h {mins}m {secs}s"
        return f"{mins}m {secs}s"
    except Exception:
        return "Unknown"


def get_load_avg() -> list[float]:
    try:
        parts = read_file("/proc/loadavg").split()
        return [float(parts[0]), float(parts[1]), float(parts[2])]
    except Exception:
        return [0.0, 0.0, 0.0]


def collect_wifi_link(iface: str) -> dict:
    info = {"band": None, "standard": None, "freq_mhz": None, "signal_dbm": None, "bitrate": None}
    iw_bin = "/sbin/iw" if os.path.exists("/sbin/iw") else "iw"
    try:
        res = subprocess.run(
            [iw_bin, "dev", iface, "link"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
        )
        out = res.stdout
        if "Connected to" not in out:
            return info
        m_freq = re.search(r"freq:\s*([0-9.]+)", out)
        if m_freq:
            freq = float(m_freq.group(1))
            info["freq_mhz"] = round(freq, 0)
            info["band"] = "5 GHz" if freq >= 4900 else "2.4 GHz"
        m_sig = re.search(r"signal:\s*(-?[0-9]+)\s*dBm", out)
        if m_sig:
            info["signal_dbm"] = int(m_sig.group(1))
        m_rate = re.search(r"rx bitrate:\s*([0-9.]+\s*MBit/s[^\n]*)", out)
        if m_rate:
            raw_rate = m_rate.group(1)
            m_speed = re.match(r"([0-9.]+\s*MBit/s)", raw_rate)
            info["bitrate"] = m_speed.group(1) if m_speed else raw_rate
            if "HE-" in raw_rate:
                info["standard"] = "Wi-Fi 6 (802.11ax)"
            elif "VHT-" in raw_rate:
                info["standard"] = "Wi-Fi 5 (802.11ac)"
            else:
                info["standard"] = "Wi-Fi 4 (802.11n)"
    except Exception:
        pass
    return info


def collect_docker_and_services() -> tuple[list[dict], list[dict]]:
    ps_map = {}
    try:
        res = subprocess.run(
            ["docker", "ps", "--format", "{{json .}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3.0,
        )
        for line in res.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                c = json.loads(line)
                name = c.get("Names", "unknown")
                ps_map[name] = {
                    "name": name,
                    "image": c.get("Image", "unknown"),
                    "status": c.get("Status", "unknown"),
                    "state": c.get("State", "unknown"),
                    "ports": c.get("Ports", ""),
                    "cpu_pct": "--",
                    "mem_usage": "--",
                    "mem_pct": "--",
                    "pids": "--",
                }
            except Exception:
                pass
    except Exception:
        pass

    if ps_map:
        try:
            st = subprocess.run(
                ["docker", "stats", "--no-stream", "--format", "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}|{{.PIDs}}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=6.0,
            )
            for line in st.stdout.strip().splitlines():
                parts = line.split("|")
                if len(parts) == 5 and parts[0] in ps_map:
                    ps_map[parts[0]]["cpu_pct"] = parts[1]
                    ps_map[parts[0]]["mem_usage"] = parts[2].split(" / ")[0]
                    ps_map[parts[0]]["mem_pct"] = parts[3]
                    ps_map[parts[0]]["pids"] = parts[4]
        except Exception:
            pass

    services = []
    unit_list = [
        ("cloudflared", "Cloudflare Tunnel"),
        ("docker", "Docker Engine"),
        ("multi-channel-extractor.service", "TubeSync Stack"),
        ("n8n.service", "n8n Automation"),
        ("aurabase-dashboard.service", "Health Dashboard"),
        ("tlp", "TLP Power Care"),
    ]
    for unit, label in unit_list:
        state = "unknown"
        try:
            r = subprocess.run(
                ["systemctl", "is-active", unit],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1.5,
            )
            state = r.stdout.strip() or "inactive"
        except Exception:
            pass
        services.append({"unit": unit, "label": label, "state": state})

    return list(ps_map.values()), services


class SlowCollector(threading.Thread):
    """Polls docker stats, systemd units, and iw every 8s off the 1s hot path."""

    def __init__(self, iface_getter):
        super().__init__(daemon=True)
        self.iface_getter = iface_getter

    def run(self):
        while True:
            containers, services = collect_docker_and_services()
            wifi = collect_wifi_link(self.iface_getter())
            with SLOW_LOCK:
                SLOW_CACHE["containers"] = containers
                SLOW_CACHE["services"] = services
                SLOW_CACHE["wifi"] = wifi
            time.sleep(8.0)


class MetricCollector(threading.Thread):
    """Fast 1-second kernel telemetry reader (/proc and /sys only)."""

    def __init__(self):
        super().__init__(daemon=True)
        self.last_cpu_total = 0
        self.last_cpu_idle = 0
        self.last_net_time = 0.0
        self.last_rx_bytes = 0
        self.last_tx_bytes = 0
        self.cpu_pct = 0.0
        self.rx_rate_kbs = 0.0
        self.tx_rate_kbs = 0.0
        self.net_iface = "wlp2s0"

    def read_cpu_raw(self) -> tuple[float, float]:
        try:
            with open("/proc/stat", "r", encoding="utf-8", errors="ignore") as f:
                parts = [float(x) for x in f.readline().split()[1:8]]
                idle = parts[3] + parts[4]
                return sum(parts), idle
        except Exception:
            return 0.0, 0.0

    def read_net_raw(self) -> tuple[int, int]:
        rx_total = 0
        tx_total = 0
        try:
            with open("/proc/net/dev", "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if ":" not in line:
                        continue
                    iface, data = line.split(":", 1)
                    iface = iface.strip()
                    if iface == "lo" or iface.startswith(("docker", "br-", "veth", "tun")):
                        continue
                    cols = data.split()
                    rx = int(cols[0])
                    tx = int(cols[8])
                    if iface == self.net_iface:
                        rx_total, tx_total = rx, tx
                        break
                    if rx_total == 0:
                        rx_total, tx_total = rx, tx
                        self.net_iface = iface
        except Exception:
            pass
        return rx_total, tx_total

    def run(self):
        self.last_cpu_total, self.last_cpu_idle = self.read_cpu_raw()
        self.last_rx_bytes, self.last_tx_bytes = self.read_net_raw()
        self.last_net_time = time.time()

        while True:
            time.sleep(1.0)
            now = time.time()

            tot, idl = self.read_cpu_raw()
            d_tot = tot - self.last_cpu_total
            d_idl = idl - self.last_cpu_idle
            if d_tot > 0:
                self.cpu_pct = round(max(0.0, min(100.0, (1.0 - d_idl / d_tot) * 100.0)), 1)
            self.last_cpu_total = tot
            self.last_cpu_idle = idl

            rx, tx = self.read_net_raw()
            dt = now - self.last_net_time
            if dt > 0:
                self.rx_rate_kbs = round(max(0.0, (rx - self.last_rx_bytes) / dt / 1024.0), 1)
                self.tx_rate_kbs = round(max(0.0, (tx - self.last_tx_bytes) / dt / 1024.0), 1)
            self.last_rx_bytes = rx
            self.last_tx_bytes = tx
            self.last_net_time = now

            temps = get_temps_and_sensors()
            power = get_power_and_battery()
            mem = get_memory_info()
            disk = get_disk_info()

            with SLOW_LOCK:
                containers = list(SLOW_CACHE["containers"])
                services = list(SLOW_CACHE["services"])
                wifi = dict(SLOW_CACHE["wifi"])

            payload = {
                "server_time": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "hostname": socket.gethostname(),
                "os_name": HOST_STATIC["os_name"],
                "kernel": HOST_STATIC["kernel"],
                "hw_model": HOST_STATIC["hw_model"],
                "uptime": get_uptime_str(),
                "cpu": {
                    "model": HOST_STATIC["cpu_model"],
                    "cores": HOST_STATIC["cores"],
                    "usage_pct": self.cpu_pct,
                    "freq_mhz": get_cpu_freq_mhz(),
                    "load_avg": get_load_avg(),
                    "temp_c": temps["cpu_temp"],
                    "temp_high_c": temps["cpu_high"],
                    "temp_crit_c": temps["cpu_crit"],
                },
                "gpu": {
                    "name": "AMD Radeon R3 Graphics",
                    "temp_c": temps["gpu_temp"],
                    "sclk_mhz": temps["gpu_sclk_mhz"],
                    "vdd_v": temps["gpu_vdd_v"],
                },
                "cooling": {
                    "fan_rpm": temps["fan_rpm"],
                    "fan_status": temps["fan_status"],
                },
                "power": {
                    **power,
                    "apu_power_mw": temps["apu_power_mw"],
                },
                "memory": mem,
                "disk": disk,
                "network": {
                    "interface": self.net_iface,
                    "local_ip": get_local_ip(),
                    "rx_kbs": self.rx_rate_kbs,
                    "tx_kbs": self.tx_rate_kbs,
                    "rx_mb_total": round(rx / (1024.0 * 1024.0), 2),
                    "tx_mb_total": round(tx / (1024.0 * 1024.0), 2),
                    "wifi": wifi,
                },
                "services": services,
                "containers": containers,
            }

            with CACHE_LOCK:
                global CURRENT_STATS
                CURRENT_STATS = payload


HTML_DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Aurabase Home Server Monitor</title>
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>⚡</text></svg>">
  <style>
    :root {
      --bg: #07090e;
      --surface: #0e121b;
      --surface-hover: #131926;
      --border: #1e2638;
      --text: #eef2ff;
      --muted: #8b96b0;
      --blue: #3b82f6;
      --cyan: #06b6d4;
      --green: #10b981;
      --amber: #f59e0b;
      --red: #ef4444;
      --violet: #8b5cf6;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: radial-gradient(circle at top right, rgba(37, 99, 235, 0.08), transparent 45%),
                  radial-gradient(circle at bottom left, rgba(16, 185, 129, 0.06), transparent 45%),
                  var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
      padding: 1.5rem;
      min-height: 100vh;
    }
    .container { max-width: 1260px; margin: 0 auto; }
    header {
      display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center;
      padding-bottom: 1.25rem; border-bottom: 1px solid var(--border); margin-bottom: 1.25rem; gap: 1rem;
    }
    .brand { display: flex; align-items: center; gap: 0.85rem; }
    .logo {
      width: 42px; height: 42px; border-radius: 11px;
      background: linear-gradient(135deg, #2563eb, #06b6d4);
      display: flex; align-items: center; justify-content: center;
      font-size: 1.25rem; font-weight: 800; color: #fff;
      box-shadow: 0 4px 20px rgba(37, 99, 235, 0.35);
    }
    h1 { font-size: 1.3rem; font-weight: 700; letter-spacing: -0.02em; }
    .subtitle { font-size: 0.82rem; color: var(--muted); margin-top: 0.15rem; }
    .nav-group { display: flex; align-items: center; gap: 0.55rem; flex-wrap: wrap; }
    .pill-link {
      text-decoration: none; font-size: 0.78rem; font-weight: 600;
      padding: 0.35rem 0.72rem; border-radius: 8px;
      border: 1px solid var(--border); background: var(--surface); color: var(--text);
      transition: border-color 0.2s, background 0.2s;
    }
    .pill-link:hover { border-color: #334155; background: var(--surface-hover); }
    .live-pill {
      display: inline-flex; align-items: center; gap: 0.45rem;
      padding: 0.35rem 0.8rem; border-radius: 999px; font-size: 0.78rem; font-weight: 700;
      background: rgba(16, 185, 129, 0.12); border: 1px solid rgba(16, 185, 129, 0.35); color: #34d399;
    }
    .live-pill.offline {
      background: rgba(239, 68, 68, 0.12); border-color: rgba(239, 68, 68, 0.35); color: #f87171;
    }
    .dot {
      width: 8px; height: 8px; border-radius: 50%; background: var(--green);
      box-shadow: 0 0 8px var(--green); animation: pulse 1.8s infinite;
    }
    .live-pill.offline .dot { background: var(--red); box-shadow: 0 0 8px var(--red); }
    @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }

    /* Services strip */
    .svc-strip {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(175px, 1fr));
      gap: 0.75rem; margin-bottom: 1.25rem;
    }
    .svc-chip {
      background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
      padding: 0.6rem 0.85rem; display: flex; justify-content: space-between; align-items: center;
      font-size: 0.8rem;
    }
    .svc-name { font-weight: 600; color: var(--text); }
    .svc-state { font-size: 0.7rem; font-weight: 700; text-transform: uppercase; padding: 0.15rem 0.45rem; border-radius: 5px; }
    .st-ok { background: rgba(16, 185, 129, 0.14); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }
    .st-bad { background: rgba(239, 68, 68, 0.14); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); }

    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1rem; margin-bottom: 1rem; }
    .card {
      background: var(--surface); border: 1px solid var(--border); border-radius: 13px; padding: 1.15rem;
      transition: border-color 0.2s;
    }
    .card:hover { border-color: #2f3b56; }
    .card-head {
      display: flex; justify-content: space-between; align-items: center; gap: 0.5rem;
      font-size: 0.75rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em;
      color: var(--muted); margin-bottom: 0.75rem;
    }
    .badge {
      font-size: 0.7rem; font-weight: 600; padding: 0.18rem 0.5rem; border-radius: 6px;
      text-transform: none; letter-spacing: 0; white-space: nowrap;
    }
    .b-blue { background: rgba(59, 130, 246, 0.14); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.3); }
    .b-green { background: rgba(16, 185, 129, 0.14); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }
    .b-violet { background: rgba(139, 92, 246, 0.14); color: #a78bfa; border: 1px solid rgba(139, 92, 246, 0.3); }
    .b-amber { background: rgba(245, 158, 11, 0.14); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3); }
    .b-red { background: rgba(239, 68, 68, 0.14); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); }

    .big-num { font-size: 2.05rem; font-weight: 800; letter-spacing: -0.03em; line-height: 1.1; }
    .unit { font-size: 0.95rem; font-weight: 500; color: var(--muted); margin-left: 0.2rem; }
    .bar-bg { background: #1a2132; height: 7px; border-radius: 999px; overflow: hidden; margin: 0.65rem 0; }
    .bar-fill { height: 100%; border-radius: 999px; transition: width 0.35s ease, background 0.35s ease; background: var(--blue); }

    .kv { display: flex; justify-content: space-between; align-items: center; padding: 0.34rem 0; font-size: 0.82rem; }
    .kv + .kv { border-top: 1px solid rgba(30, 38, 56, 0.65); }
    .k { color: var(--muted); }
    .v { font-weight: 600; font-variant-numeric: tabular-nums; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.78rem; }

    .table-wrap { overflow-x: auto; margin-top: 0.4rem; }
    table { width: 100%; border-collapse: collapse; text-align: left; font-size: 0.83rem; }
    th { color: var(--muted); font-weight: 600; padding: 0.55rem 0.7rem; border-bottom: 1px solid var(--border); font-size: 0.75rem; text-transform: uppercase; }
    td { padding: 0.68rem 0.7rem; border-bottom: 1px solid rgba(30, 38, 56, 0.6); vertical-align: middle; }
    tr:last-child td { border-bottom: none; }

    footer {
      margin-top: 1.5rem; padding-top: 1rem; border-top: 1px solid var(--border);
      display: flex; justify-content: space-between; flex-wrap: wrap; gap: 0.5rem;
      font-size: 0.78rem; color: var(--muted);
    }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="brand">
        <div class="logo">⚡</div>
        <div>
          <h1>Aurabase Home Server Monitor</h1>
          <div class="subtitle" id="host-info">Connecting to live kernel telemetry...</div>
        </div>
      </div>
      <div class="nav-group">
        <a href="https://app.aurabase.systems" target="_blank" rel="noopener" class="pill-link">🌐 TubeSync App</a>
        <a href="https://n8n.aurabase.systems" target="_blank" rel="noopener" class="pill-link">⚡ n8n Automation</a>
        <a href="https://github.com/vigusri/Home-Lab" target="_blank" rel="noopener" class="pill-link">📦 GitHub Home-Lab</a>
        <span class="live-pill" id="sync-badge"><span class="dot"></span><span id="sync-status">SYNCING</span></span>
      </div>
    </header>

    <div class="svc-strip" id="services-strip"></div>

    <!-- Row 1: CPU, Thermals, Battery & Power -->
    <div class="grid">
      <div class="card">
        <div class="card-head">
          <span>CPU Utilization</span>
          <span class="badge b-blue" id="cpu-cores">-- Cores</span>
        </div>
        <div class="big-num" id="cpu-usage">--<span class="unit">%</span></div>
        <div class="bar-bg"><div class="bar-fill" id="cpu-bar" style="width:0%"></div></div>
        <div class="kv"><span class="k">Processor</span><span class="v" id="cpu-model" style="font-size:0.75rem;text-align:right;">--</span></div>
        <div class="kv"><span class="k">Clock / Load (1m,5m,15m)</span><span class="v mono" id="cpu-clock-load">--</span></div>
      </div>

      <div class="card">
        <div class="card-head">
          <span>Thermals &amp; GPU</span>
          <span class="badge b-green" id="temp-badge">--</span>
        </div>
        <div class="big-num" id="cpu-temp">--<span class="unit">°C</span></div>
        <div class="bar-bg"><div class="bar-fill" id="cpu-temp-bar" style="width:0%;background:var(--green)"></div></div>
        <div class="kv"><span class="k">Radeon GPU Edge Temp</span><span class="v" id="gpu-temp">-- °C</span></div>
        <div class="kv"><span class="k">GPU Clock / Voltage</span><span class="v mono" id="gpu-clocks">--</span></div>
        <div class="kv"><span class="k">Cooling / Threshold</span><span class="v" id="fan-status">--</span></div>
      </div>

      <div class="card">
        <div class="card-head">
          <span>Battery &amp; AC Power Care</span>
          <span class="badge b-amber" id="power-badge">--</span>
        </div>
        <div class="big-num" id="bat-pct">--<span class="unit">%</span></div>
        <div class="bar-bg"><div class="bar-fill" id="bat-bar" style="width:0%;background:var(--cyan)"></div></div>
        <div class="kv"><span class="k">Power State</span><span class="v" id="bat-mode">--</span></div>
        <div class="kv"><span class="k">Energy (Now / Full Cap)</span><span class="v mono" id="bat-energy">--</span></div>
        <div class="kv"><span class="k">Battery Wear Health</span><span class="v" id="bat-health">--</span></div>
      </div>

      <div class="card">
        <div class="card-head">
          <span>Electrical Telemetry</span>
          <span class="badge b-violet" id="cons-badge">TLP --</span>
        </div>
        <div class="big-num" id="bat-watts">--<span class="unit">W</span></div>
        <div class="bar-bg"><div class="bar-fill" id="watts-bar" style="width:0%;background:var(--violet)"></div></div>
        <div class="kv"><span class="k">BAT1 Bus Voltage</span><span class="v mono" id="bat-volts">-- V</span></div>
        <div class="kv"><span class="k">APU Package (fam15h_power)</span><span class="v mono" id="apu-power">-- mW</span></div>
        <div class="kv"><span class="k">Telemetry Note</span><span class="v" style="font-size:0.75rem;color:var(--muted);">BAT1 charge/discharge rate</span></div>
      </div>
    </div>

    <!-- Row 2: Memory, Storage, Wireless -->
    <div class="grid">
      <div class="card">
        <div class="card-head">
          <span>Memory &amp; Swap (free -m)</span>
          <span class="badge b-violet" id="ram-used-total">-- MiB</span>
        </div>
        <div class="big-num" id="ram-pct">--<span class="unit">%</span></div>
        <div class="bar-bg"><div class="bar-fill" id="ram-bar" style="width:0%;background:linear-gradient(90deg,#8b5cf6,#c084fc)"></div></div>
        <div class="kv"><span class="k">Available / Buff+Cache</span><span class="v mono" id="ram-avail">--</span></div>
        <div class="kv"><span class="k">Swap Used / Total</span><span class="v mono" id="swap-used">--</span></div>
      </div>

      <div class="card">
        <div class="card-head">
          <span id="disk-title">Storage (/)</span>
          <span class="badge b-green" id="disk-model-badge">--</span>
        </div>
        <div class="big-num" id="disk-used-pct">--<span class="unit">%</span></div>
        <div class="bar-bg"><div class="bar-fill" id="disk-bar" style="width:0%;background:var(--green)"></div></div>
        <div class="kv"><span class="k">Used / Available (df /)</span><span class="v mono" id="disk-used-gb">--</span></div>
        <div class="kv"><span class="k">Mount / Read-Ahead</span><span class="v mono" id="disk-opts">--</span></div>
      </div>

      <div class="card">
        <div class="card-head">
          <span id="net-title">Network</span>
          <span class="badge b-blue" id="net-iface">--</span>
        </div>
        <div class="kv"><span class="k">Local IPv4</span><span class="v mono" id="local-ip">--</span></div>
        <div class="kv"><span class="k">Wireless Link</span><span class="v mono" id="wifi-link">--</span></div>
        <div class="kv"><span class="k">Live Throughput (RX / TX)</span><span class="v mono" id="net-rates">--</span></div>
        <div class="kv"><span class="k">Session Totals (RX / TX)</span><span class="v mono" id="net-totals">--</span></div>
      </div>
    </div>

    <!-- Row 3: Live Containers -->
    <div class="card">
      <div class="card-head">
        <span>Running Docker Containers (Live Stats)</span>
        <span class="badge b-green" id="docker-count">--</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Container</th>
              <th>CPU</th>
              <th>Memory</th>
              <th>PIDs</th>
              <th>Status</th>
              <th>Port Bindings</th>
            </tr>
          </thead>
          <tbody id="containers-table">
            <tr><td colspan="6" style="text-align:center;color:var(--muted);">Loading container telemetry...</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <footer>
      <div>Host: <strong id="footer-host">--</strong> · <span id="footer-hw">--</span> · Kernel <span id="footer-kernel">--</span></div>
      <div>Server Time: <span id="server-time">--</span> · Zero-write in-memory /proc &amp; /sys telemetry</div>
    </footer>
  </div>

  <script>
    function esc(s) {
      return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }

    async function updateDashboard() {
      const badge = document.getElementById('sync-badge');
      try {
        const res = await fetch('/api/stats', { cache: 'no-store' });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const d = await res.json();

        document.getElementById('host-info').innerText =
          `${d.hostname} \u2022 ${d.os_name} \u2022 Up ${d.uptime}`;
        document.getElementById('footer-host').innerText = d.hostname;
        document.getElementById('footer-hw').innerText = d.hw_model;
        document.getElementById('footer-kernel').innerText = d.kernel;
        document.getElementById('server-time').innerText = d.server_time;

        // Services strip
        const strip = document.getElementById('services-strip');
        if (d.services && d.services.length) {
          strip.innerHTML = d.services.map(s => `
            <div class="svc-chip">
              <span class="svc-name">${esc(s.label)}</span>
              <span class="svc-state ${s.state === 'active' ? 'st-ok' : 'st-bad'}">${esc(s.state)}</span>
            </div>
          `).join('');
        }

        // CPU
        document.getElementById('cpu-cores').innerText = `${d.cpu.cores} Cores`;
        document.getElementById('cpu-usage').innerHTML = `${d.cpu.usage_pct}<span class="unit">%</span>`;
        document.getElementById('cpu-bar').style.width = `${Math.min(100, d.cpu.usage_pct)}%`;
        document.getElementById('cpu-model').innerText = d.cpu.model;
        const mhzTxt = d.cpu.freq_mhz ? `${d.cpu.freq_mhz} MHz` : 'N/A';
        document.getElementById('cpu-clock-load').innerText = `${mhzTxt} · ${d.cpu.load_avg.join(', ')}`;

        // Thermals & GPU
        const t = d.cpu.temp_c;
        document.getElementById('cpu-temp').innerHTML = t !== null ? `${t}<span class="unit">°C</span>` : `N/A`;
        const tBar = document.getElementById('cpu-temp-bar');
        const tBadge = document.getElementById('temp-badge');
        if (t !== null) {
          tBar.style.width = `${Math.min(100, (t / d.cpu.temp_crit_c) * 100)}%`;
          if (t >= d.cpu.temp_high_c) {
            tBar.style.background = 'var(--red)';
            tBadge.className = 'badge b-red';
            tBadge.innerText = 'High Temp';
          } else if (t >= 55) {
            tBar.style.background = 'var(--amber)';
            tBadge.className = 'badge b-amber';
            tBadge.innerText = 'Warm (55–70°C)';
          } else {
            tBar.style.background = 'var(--green)';
            tBadge.className = 'badge b-green';
            tBadge.innerText = 'Cool (< 55°C)';
          }
        }
        document.getElementById('gpu-temp').innerText = d.gpu.temp_c !== null ? `${d.gpu.temp_c} °C` : 'N/A';
        document.getElementById('gpu-clocks').innerText =
          `${d.gpu.sclk_mhz !== null ? d.gpu.sclk_mhz + ' MHz' : '--'} · ${d.gpu.vdd_v !== null ? d.gpu.vdd_v + ' V' : '--'}`;
        document.getElementById('fan-status').innerText = `${d.cooling.fan_status} · High ${d.cpu.temp_high_c}°C`;

        // Battery & AC
        const p = d.power;
        document.getElementById('bat-pct').innerHTML = `${p.capacity}<span class="unit">%</span>`;
        document.getElementById('bat-bar').style.width = `${Math.min(100, p.capacity)}%`;
        document.getElementById('bat-mode').innerText = p.mode_desc;
        const pBadge = document.getElementById('power-badge');
        if (p.ac_connected) {
          pBadge.className = 'badge b-green';
          pBadge.innerText = p.conservation_mode ? 'AC · Conservation' : 'AC Connected';
        } else {
          pBadge.className = 'badge b-amber';
          pBadge.innerText = 'On Battery';
        }
        document.getElementById('bat-energy').innerText =
          (p.energy_now_wh !== null && p.energy_full_wh !== null)
            ? `${p.energy_now_wh} / ${p.energy_full_wh} Wh`
            : '--';
        document.getElementById('bat-health').innerText =
          (p.health_pct !== null && p.energy_design_wh !== null)
            ? `${p.health_pct}% (${p.energy_full_wh} / ${p.energy_design_wh} Wh)`
            : '--';

        // Electrical telemetry
        const consBadge = document.getElementById('cons-badge');
        consBadge.innerText = p.conservation_mode ? 'TLP Conservation ON' : 'TLP Standard';
        consBadge.className = p.conservation_mode ? 'badge b-green' : 'badge b-amber';
        document.getElementById('bat-watts').innerHTML =
          p.bat_watts !== null ? `${p.bat_watts.toFixed(2)}<span class="unit">W</span>` : 'N/A';
        document.getElementById('watts-bar').style.width =
          p.bat_watts !== null ? `${Math.min(100, (p.bat_watts / 35.0) * 100)}%` : '0%';
        document.getElementById('bat-volts').innerText = p.volts !== null ? `${p.volts.toFixed(2)} V` : 'N/A';
        document.getElementById('apu-power').innerText = p.apu_power_mw !== null ? `${p.apu_power_mw} mW` : 'N/A';

        // Memory
        const m = d.memory;
        document.getElementById('ram-pct').innerHTML = `${m.ram_pct}<span class="unit">%</span>`;
        document.getElementById('ram-bar').style.width = `${Math.min(100, m.ram_pct)}%`;
        document.getElementById('ram-used-total').innerText = `${m.ram_used_mb} / ${m.ram_total_mb} MiB`;
        document.getElementById('ram-avail').innerText = `${m.ram_avail_mb} MiB / ${m.ram_buff_cache_mb} MiB`;
        document.getElementById('swap-used').innerText = `${m.swap_used_mb} / ${m.swap_total_mb} MiB (${m.swap_pct}%)`;

        // Storage
        const dk = d.disk;
        document.getElementById('disk-title').innerText = `Storage (${dk.physical_gb ? dk.physical_gb + ' GB Disk' : dk.device})`;
        document.getElementById('disk-model-badge').innerText = dk.model;
        document.getElementById('disk-used-pct').innerHTML = `${dk.used_pct}<span class="unit">%</span>`;
        document.getElementById('disk-bar').style.width = `${Math.min(100, dk.used_pct)}%`;
        document.getElementById('disk-used-gb').innerText = `${dk.used_gib} / ${dk.avail_gib} GiB free (${dk.total_gib} GiB)`;
        document.getElementById('disk-opts').innerText = `${dk.mount_opts}${dk.readahead_kb ? ' · RA ' + dk.readahead_kb + 'K' : ''}`;

        // Network
        const n = d.network;
        const w = n.wifi || {};
        document.getElementById('net-iface').innerText = n.interface;
        document.getElementById('net-title').innerText = w.standard ? `Network (${w.standard})` : 'Network';
        document.getElementById('local-ip').innerText = n.local_ip;
        document.getElementById('wifi-link').innerText =
          w.bitrate ? `${w.band || ''} · ${w.bitrate} · ${w.signal_dbm ?? '--'} dBm` : 'Link active';
        document.getElementById('net-rates').innerText = `↓ ${n.rx_kbs} KB/s · ↑ ${n.tx_kbs} KB/s`;
        document.getElementById('net-totals').innerText = `${n.rx_mb_total} MiB / ${n.tx_mb_total} MiB`;

        // Containers
        const tbody = document.getElementById('containers-table');
        if (d.containers && d.containers.length) {
          document.getElementById('docker-count').innerText = `${d.containers.length} Running`;
          tbody.innerHTML = d.containers.map(c => `
            <tr>
              <td><strong>${esc(c.name)}</strong><br><span style="font-size:0.74rem;color:var(--muted);">${esc(c.image)}</span></td>
              <td class="mono">${esc(c.cpu_pct)}</td>
              <td class="mono">${esc(c.mem_usage)} <span style="color:var(--muted)">(${esc(c.mem_pct)})</span></td>
              <td class="mono">${esc(c.pids)}</td>
              <td><span class="badge ${c.state === 'running' ? 'b-green' : 'b-red'}">${esc(c.status)}</span></td>
              <td class="mono">${esc(c.ports || 'internal')}</td>
            </tr>
          `).join('');
        } else {
          document.getElementById('docker-count').innerText = '0 Running';
          tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--muted);">No running containers detected.</td></tr>';
        }

        badge.classList.remove('offline');
        document.getElementById('sync-status').innerText = 'LIVE · 1s';
      } catch (err) {
        badge.classList.add('offline');
        document.getElementById('sync-status').innerText = 'RECONNECTING';
      }
    }

    updateDashboard();
    setInterval(updateDashboard, 1200);
  </script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(HTML_DASHBOARD.encode("utf-8"))
        elif self.path == "/api/stats":
            with CACHE_LOCK:
                payload = json.dumps(CURRENT_STATS)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))
        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

    def log_message(self, format, *args):
        return


def main():
    fast = MetricCollector()
    slow = SlowCollector(lambda: fast.net_iface)
    slow.start()
    fast.start()
    time.sleep(1.2)

    httpd = HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    print(f"Aurabase Dashboard listening on http://0.0.0.0:{PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
