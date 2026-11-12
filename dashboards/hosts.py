#!/usr/bin/env python3
"""Generates the host hardware dashboards into ../grafana/dashboards/.

    python3 dashboards/hosts.py

Two kinds of dashboard:

* ``hosts-overview``: one row of headline numbers per machine plus comparison graphs. It reads only
  the normalised ``host:*`` series from ``prometheus/rules/hosts.yml``, so any machine whose
  exporter is mapped there shows up without changes here.
* ``host-<name>``: one dashboard per machine, built from the raw exporter metrics and shaped to
  that machine's hardware (battery, GPU, Wi-Fi, ...). Add a machine by adding a ``Host`` to HOSTS.

Node Exporter Full (grafana.com #1860) is provisioned next to these as a catch-all.
"""
import json
import pathlib
from dataclasses import dataclass, field

OUT = pathlib.Path(__file__).resolve().parent.parent / "grafana" / "dashboards"
PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}

# Threshold steps: (value, color); the first value is always None (the base colour).
PCT = [(None, "green"), (0.7, "yellow"), (0.9, "red")]
TEMP = [(None, "green"), (70, "yellow"), (85, "red")]
DISK_TEMP = [(None, "green"), (55, "yellow"), (70, "red")]
BATTERY = [(None, "red"), (0.2, "yellow"), (0.4, "green")]
NEUTRAL = [(None, "blue")]

# Interfaces that are not real links: loopback, tailnet (already counted on the NIC), Docker.
VIRTUAL_NICS = "lo|tailscale.*|docker.*|br-.*|veth.*|virbr.*"
PSEUDO_FS = "tmpfs|devtmpfs|overlay|squashfs|fuse.*|nsfs|ramfs"


@dataclass
class Host:
    name: str               # host_name label
    title: str
    description: str
    battery: bool = False
    wifi: bool = False
    amdgpu: bool = False
    systemd: bool = False   # node_exporter runs --collector.systemd
    journal: bool = False   # journal is shipped to Loki
    extra_tags: list = field(default_factory=list)


HOSTS = [
    Host("fedora", "fedora · laptop", "Ryzen AI 7 350, Radeon 860M, 512 GB NVMe, battery",
         battery=True, wifi=True, amdgpu=True),
    Host("veronica", "veronica · homelab", "Asus NUC, always on: Immich, Cortex, telemetry hub",
         systemd=True, journal=True),
]


# ----------------------------------------------------------------- panels
def prom(expr, legend="", ref="A", instant=False):
    t = {"datasource": PROM, "expr": expr, "legendFormat": legend, "refId": ref, "editorMode": "code"}
    if instant:
        t.update(instant=True, range=False)
    return t


def steps(spec):
    return {"mode": "absolute", "steps": [{"value": v, "color": c} for v, c in spec]}


def ts(title, targets, unit="short", *, stack=False, fill=10, lo=None, hi=None, desc="",
       thresholds=None, overrides=None):
    defaults = {
        "unit": unit,
        "custom": {
            "lineWidth": 1, "fillOpacity": fill, "gradientMode": "opacity", "showPoints": "never",
            "spanNulls": 30_000, "stacking": {"mode": "normal" if stack else "none"},
        },
    }
    if lo is not None:
        defaults["min"] = lo
    if hi is not None:
        defaults["max"] = hi
    if thresholds:
        defaults["thresholds"] = steps(thresholds)
        defaults["custom"]["thresholdsStyle"] = {"mode": "dashed"}
    return {
        "type": "timeseries", "title": title, "description": desc, "datasource": PROM,
        "targets": targets,
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        "options": {"legend": {"displayMode": "table", "placement": "right", "calcs": ["lastNotNull", "max"]},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    }


def stat(title, expr, unit="short", thresholds=NEUTRAL, *, decimals=None, legend="", spark=True,
         text_mode="value", mappings=None, link=None, desc=""):
    defaults = {"unit": unit, "thresholds": steps(thresholds), "color": {"mode": "thresholds"},
                "mappings": mappings or []}
    if decimals is not None:
        defaults["decimals"] = decimals
    panel = {
        "type": "stat", "title": title, "description": desc, "datasource": PROM,
        "targets": [prom(expr, legend)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"graphMode": "area" if spark else "none", "colorMode": "background_solid" if not spark else "value",
                    "textMode": text_mode, "reduceOptions": {"calcs": ["lastNotNull"]}, "justifyMode": "center"},
    }
    if link:
        panel["links"] = [{"title": link[0], "url": link[1]}]
    return panel


def bars(title, expr, legend, unit, thresholds, *, lo=0, hi=None, desc=""):
    defaults = {"unit": unit, "min": lo, "thresholds": steps(thresholds), "color": {"mode": "thresholds"}}
    if hi is not None:
        defaults["max"] = hi
    return {
        "type": "bargauge", "title": title, "description": desc, "datasource": PROM,
        "targets": [prom(expr, legend, instant=True)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"displayMode": "lcd", "orientation": "horizontal", "showUnfilled": True,
                    "valueMode": "color", "namePlacement": "left", "sizing": "auto",
                    "reduceOptions": {"calcs": ["lastNotNull"]}},
    }


def state_timeline(title, expr, legend, mappings):
    return {
        "type": "state-timeline", "title": title, "datasource": PROM,
        "targets": [prom(expr, legend)],
        "fieldConfig": {"defaults": {"mappings": mappings, "color": {"mode": "thresholds"},
                                     "thresholds": steps(NEUTRAL)}, "overrides": []},
        "options": {"showValue": "never", "rowHeight": 0.8, "mergeValues": True},
    }


def logs(title, expr):
    return {
        "type": "logs", "title": title, "datasource": LOKI,
        "targets": [{"datasource": LOKI, "expr": expr, "refId": "A", "queryType": "range", "editorMode": "code"}],
        "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending", "enableLogDetails": True},
    }


def row(title, repeat=None):
    r = {"type": "row", "title": title, "collapsed": False, "panels": []}
    if repeat:
        r["repeat"] = repeat
    return r


def value_map(pairs):
    """[(value, text, color)] -> Grafana value mappings."""
    return [{"type": "value", "options": {str(v): {"text": t, "color": c, "index": i}}}
            for i, (v, t, c) in enumerate(pairs)]


def negate_series(regex):
    """Override that draws matching series below the axis (transmit/write under receive/read)."""
    return {"matcher": {"id": "byRegexp", "options": regex},
            "properties": [{"id": "custom.transform", "value": "negative-Y"}]}


class Grid:
    """Places panels left to right on Grafana's 24-column grid, wrapping onto new lines."""

    def __init__(self):
        self.panels, self.x, self.y, self.line_h, self.next_id = [], 0, 0, 0, 1

    def add(self, panel, w, h):
        if self.x + w > 24:
            self.newline()
        panel["id"] = self.next_id
        panel["gridPos"] = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.panels.append(panel)
        self.next_id += 1
        self.x += w
        self.line_h = max(self.line_h, h)

    def newline(self):
        if self.x:
            self.y += self.line_h
        self.x, self.line_h = 0, 0

    def section(self, title, **kw):
        self.newline()
        self.add(row(title, **kw), 24, 1)
        self.newline()


def dashboard(uid, title, panels, *, tags, description="", variables=(), links=(), refresh="5s"):
    return {
        "uid": uid, "title": title, "description": description, "tags": sorted(tags),
        "editable": True, "graphTooltip": 1, "schemaVersion": 41, "refresh": refresh,
        "time": {"from": "now-1h", "to": "now"},
        "timepicker": {"refresh_intervals": ["5s", "10s", "30s", "1m", "5m"]},
        "templating": {"list": list(variables)}, "links": list(links), "panels": panels,
        "annotations": {"list": []},
    }


# ---------------------------------------------------------------- overview
def overview():
    g = Grid()
    host_var = {
        "name": "host", "label": "Host", "type": "query", "datasource": PROM,
        "query": {"query": "label_values(host:online, host_name)", "refId": "host"},
        "definition": "label_values(host:online, host_name)",
        "multi": True, "includeAll": True, "current": {"text": "All", "value": "$__all"},
        "refresh": 2, "sort": 1,
    }
    h = 'host_name="$host"'
    details = ("Open ${host} details", "/d/host-${host}")
    online = value_map([(1, "ONLINE", "green"), (0, "OFFLINE", "red")])

    g.section("$host", repeat="host")
    for title, expr, unit, th, kw in [
        ("Status", f"host:online{{{h}}}", "none", [(None, "red"), (1, "green")],
         dict(spark=False, mappings=online, link=details)),
        ("Uptime", f"host:uptime_seconds{{{h}}}", "dtdurations", NEUTRAL, dict(spark=False, decimals=0)),
        ("CPU", f"host:cpu_usage:ratio{{{h}}}", "percentunit", PCT, dict(decimals=0)),
        ("CPU temp", f"host:cpu_temp_celsius{{{h}}}", "celsius", TEMP, dict(decimals=0)),
        ("CPU power", f"host:cpu_power_watts{{{h}}}", "watt", NEUTRAL, dict(decimals=1)),
        ("CPU clock (avg)", f"host:cpu_frequency_hertz:avg{{{h}}}", "hertz", NEUTRAL, dict(decimals=2)),
        ("Memory", f"host:memory_usage:ratio{{{h}}}", "percentunit", PCT, dict(decimals=0)),
        ("Swap", f"host:swap_usage:ratio{{{h}}}", "percentunit", PCT, dict(decimals=0)),
        ("GPU", f"host:gpu_busy:ratio{{{h}}}", "percentunit", PCT, dict(decimals=0)),
        ("GPU temp", f"host:gpu_temp_celsius{{{h}}}", "celsius", TEMP, dict(decimals=0)),
        ("Fan (max)", f"host:fan_rpm:max{{{h}}}", "rotrpm", NEUTRAL, dict(decimals=0)),
        ("Disk temp", f"host:disk_temp_celsius:max{{{h}}}", "celsius", DISK_TEMP, dict(decimals=0)),
        ("Root disk", f"host:root_fs_usage:ratio{{{h}}}", "percentunit", PCT, dict(decimals=0, spark=False)),
        ("Network ↓", f"host:network_receive_bytes:rate{{{h}}}", "Bps", NEUTRAL, dict(decimals=1)),
        ("Network ↑", f"host:network_transmit_bytes:rate{{{h}}}", "Bps", NEUTRAL, dict(decimals=1)),
        ("Battery", f"host:battery_capacity:ratio{{{h}}}", "percentunit", BATTERY, dict(decimals=0)),
    ]:
        g.add(stat(title, expr, unit, th, **kw), 3, 4)

    g.section("Compare hosts")
    for title, rec, unit, kw in [
        ("CPU usage", "host:cpu_usage:ratio", "percentunit", dict(lo=0, hi=1)),
        ("CPU temperature", "host:cpu_temp_celsius", "celsius", dict(thresholds=TEMP)),
        ("CPU package power", "host:cpu_power_watts", "watt", dict(lo=0)),
        ("Memory usage", "host:memory_usage:ratio", "percentunit", dict(lo=0, hi=1)),
        ("GPU busy", "host:gpu_busy:ratio", "percentunit", dict(lo=0, hi=1)),
        ("Average CPU clock", "host:cpu_frequency_hertz:avg", "hertz", dict(lo=0)),
    ]:
        g.add(ts(title, [prom(f'{rec}{{host_name=~"$host"}}', "{{host_name}}")], unit, **kw), 12, 8)
    g.add(ts("Network (↓ receive / ↑ transmit below)", [
        prom('host:network_receive_bytes:rate{host_name=~"$host"}', "{{host_name}} ↓", "A"),
        prom('host:network_transmit_bytes:rate{host_name=~"$host"}', "{{host_name}} ↑", "B"),
    ], "Bps", overrides=[negate_series(".*↑")]), 12, 8)
    g.add(ts("Disk I/O (read / write below)", [
        prom('host:disk_read_bytes:rate{host_name=~"$host"}', "{{host_name}} read", "A"),
        prom('host:disk_written_bytes:rate{host_name=~"$host"}', "{{host_name}} write", "B"),
    ], "Bps", overrides=[negate_series(".*write")]), 12, 8)

    links = [{"title": "Host details", "type": "dashboards", "tags": ["host"], "asDropdown": True},
             {"title": "Node Exporter Full", "type": "link", "url": "/d/node-exporter-full"}]
    return dashboard("hosts-overview", "Hosts", g.panels, tags={"hosts"}, variables=[host_var], links=links,
                     description="Headline numbers for every machine, from the host:* recording rules.")


# ------------------------------------------------------------ per host
def hwmon(metric, sel, chips=".*"):
    """A hwmon metric with `chip_name` and a readable `label` (the driver's label, else the sensor id)."""
    m = f"{metric}{{{sel}}}"
    labelled = f"({m} * on (host_name, chip, sensor) group_left (label) node_hwmon_sensor_label{{{sel}}})"
    bare = f'label_replace({m}, "label", "$1", "sensor", "(.*)")'
    return (f"(({labelled}) or on (host_name, chip, sensor) {bare})"
            f' * on (host_name, chip) group_left (chip_name) node_hwmon_chip_names{{{sel}, chip_name=~"{chips}"}}')


def host_dashboard(host: Host):
    g = Grid()
    s = f'host_name="{host.name}"'
    rate = "$__rate_interval"
    cores = f'scalar(count(node_cpu_seconds_total{{{s}, mode="idle"}}))'

    # ---- at a glance
    g.section("At a glance")
    g.add(stat("Status", f"host:online{{{s}}}", "none", [(None, "red"), (1, "green")], spark=False,
               mappings=value_map([(1, "ONLINE", "green"), (0, "OFFLINE", "red")])), 3, 4)
    g.add(stat("Uptime", f"host:uptime_seconds{{{s}}}", "dtdurations", spark=False, decimals=0), 3, 4)
    g.add(stat("CPU", f"count by (model_name) (node_cpu_info{{{s}}})", "none", legend="{{model_name}}",
               spark=False, text_mode="name", desc="CPU model; the value is the thread count."), 6, 4)
    g.add(stat("Kernel", f"node_uname_info{{{s}}}", "none", legend="{{release}}", spark=False,
               text_mode="name"), 4, 4)
    g.add(stat("Processes", f'sum(node_processes_state{{{s}}})', "none", spark=True), 3, 4)
    g.add(stat("Load (1m)", f"node_load1{{{s}}}", "none", [(None, "green")], decimals=2), 3, 4)
    g.newline()
    g.add(stat("CPU usage", f"host:cpu_usage:ratio{{{s}}}", "percentunit", PCT, decimals=0), 3, 4)
    g.add(stat("CPU temp", f"host:cpu_temp_celsius{{{s}}}", "celsius", TEMP, decimals=0), 3, 4)
    g.add(stat("CPU power", f"host:cpu_power_watts{{{s}}}", "watt", decimals=1), 3, 4)
    g.add(stat("Memory", f"host:memory_usage:ratio{{{s}}}", "percentunit", PCT, decimals=0), 3, 4)
    if host.amdgpu:
        g.add(stat("GPU", f"host:gpu_busy:ratio{{{s}}}", "percentunit", PCT, decimals=0), 3, 4)
        g.add(stat("GPU temp", f"host:gpu_temp_celsius{{{s}}}", "celsius", TEMP, decimals=0), 3, 4)
    g.add(stat("Disk temp", f"host:disk_temp_celsius:max{{{s}}}", "celsius", DISK_TEMP, decimals=0), 3, 4)
    if host.battery:
        g.add(stat("Battery", f"host:battery_capacity:ratio{{{s}}}", "percentunit", BATTERY, decimals=0), 3, 4)
    else:
        g.add(stat("Root disk", f"host:root_fs_usage:ratio{{{s}}}", "percentunit", PCT, decimals=0,
                   spark=False), 3, 4)

    # ---- CPU
    g.section("CPU")
    g.add(ts("CPU usage by mode", [prom(
        f'sum by (mode) (rate(node_cpu_seconds_total{{{s}, mode!="idle"}}[{rate}])) / {cores}', "{{mode}}")],
        "percentunit", stack=True, fill=40, lo=0, hi=1), 12, 8)
    g.add(bars("Per-thread usage (now)", f'1 - rate(node_cpu_seconds_total{{{s}, mode="idle"}}[1m])',
               "cpu {{cpu}}", "percentunit", PCT, hi=1), 12, 8)
    g.add(ts("Per-thread clock", [prom(f"node_cpu_scaling_frequency_hertz{{{s}}}", "cpu {{cpu}}")],
             "hertz", fill=0), 12, 8)
    g.add(ts("Load vs threads", [
        prom(f"node_load1{{{s}}}", "1m", "A"), prom(f"node_load5{{{s}}}", "5m", "B"),
        prom(f"node_load15{{{s}}}", "15m", "C"), prom(cores, "threads", "D"),
    ], "short", fill=0, lo=0, overrides=[{"matcher": {"id": "byName", "options": "threads"}, "properties": [
        {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}},
        {"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}]}]), 12, 8)
    power = [prom(f"rate(node_rapl_package_joules_total{{{s}}}[{rate}])", "package (RAPL)", "A"),
             prom(f"rate(node_rapl_core_joules_total{{{s}}}[{rate}])", "cores (RAPL)", "B")]
    if host.amdgpu:
        power.append(prom(hwmon("node_hwmon_power_average_watt", s, "amdgpu"), "SoC PPT (amdgpu)", "C"))
    g.add(ts("Power", power, "watt", lo=0), 12, 8)
    g.add(ts("CPU temperatures", [prom(hwmon("node_hwmon_temp_celsius", s, "k10temp|zenpower|coretemp"),
                                       "{{chip_name}} {{label}}")], "celsius", fill=0, thresholds=TEMP), 12, 8)
    g.add(ts("Pressure (time stalled)", [
        prom(f"rate(node_pressure_cpu_waiting_seconds_total{{{s}}}[{rate}])", "cpu waiting", "A"),
        prom(f"rate(node_pressure_memory_waiting_seconds_total{{{s}}}[{rate}])", "memory waiting", "B"),
        prom(f"rate(node_pressure_io_waiting_seconds_total{{{s}}}[{rate}])", "io waiting", "C"),
    ], "percentunit", fill=0, lo=0,
        desc="Share of time some task was stalled waiting on CPU, memory or I/O (Linux PSI)."), 12, 8)
    g.add(ts("Context switches and interrupts", [
        prom(f"rate(node_context_switches_total{{{s}}}[{rate}])", "context switches", "A"),
        prom(f"rate(node_intr_total{{{s}}}[{rate}])", "interrupts", "B"),
    ], "ops", fill=0), 12, 8)

    # ---- GPU
    if host.amdgpu:
        g.section("GPU")
        g.add(ts("GPU busy", [prom(f"node_drm_gpu_busy_percent{{{s}}} / 100", "{{card}}")],
                 "percentunit", lo=0, hi=1), 8, 8)
        g.add(ts("GPU clock", [prom(hwmon("node_hwmon_freq_freq_mhz", s, "amdgpu") + " * 1e6", "{{label}}")],
                 "hertz", lo=0), 8, 8)
        g.add(ts("GPU temperature and power", [
            prom(hwmon("node_hwmon_temp_celsius", s, "amdgpu"), "{{label}} temp", "A"),
            prom(hwmon("node_hwmon_power_average_watt", s, "amdgpu"), "{{label}} power", "B"),
        ], "celsius", fill=0, overrides=[{"matcher": {"id": "byRegexp", "options": ".*power"}, "properties": [
            {"id": "unit", "value": "watt"}, {"id": "custom.axisPlacement", "value": "right"}]}]), 8, 8)
        for kind, label in [("vram", "VRAM (carve-out)"), ("gtt", "GTT (shared system RAM)")]:
            g.add(ts(label, [
                prom(f"node_drm_memory_{kind}_used_bytes{{{s}}}", "used", "A"),
                prom(f"node_drm_memory_{kind}_size_bytes{{{s}}}", "size", "B"),
            ], "bytes", lo=0, overrides=[{"matcher": {"id": "byName", "options": "size"}, "properties": [
                {"id": "custom.fillOpacity", "value": 0},
                {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}}]}]), 12, 7)

    # ---- memory
    g.section("Memory")
    mem = f"node_memory_{{}}_bytes{{{{{s}}}}}"
    g.add(ts("Memory", [
        prom(f"{mem.format('MemTotal')} - {mem.format('MemAvailable')}", "used", "A"),
        prom(f"{mem.format('Cached')} + {mem.format('Buffers')}", "cache + buffers", "B"),
        prom(mem.format("MemAvailable") + " - " + mem.format("Cached") + " - " + mem.format("Buffers"),
             "free", "C"),
    ], "bytes", stack=True, fill=40, lo=0), 12, 8)
    g.add(ts("Swap and zram", [
        prom(f"{mem.format('SwapTotal')} - {mem.format('SwapFree')}", "swap used", "A"),
        prom(f"{mem.format('SwapTotal')}", "swap size", "B"),
        prom(f"rate(node_vmstat_pswpout{{{s}}}[{rate}]) * 4096", "swapped out /s", "C"),
    ], "bytes", lo=0, overrides=[{"matcher": {"id": "byName", "options": "swapped out /s"}, "properties": [
        {"id": "unit", "value": "Bps"}, {"id": "custom.axisPlacement", "value": "right"}]}]), 12, 8)

    # ---- battery
    if host.battery:
        g.section("Battery and power")
        bat = f'{s}, power_supply=~"BAT.*"'
        # BAT0 reports "Not charging" rather than "Discharging" on some firmware, so go by the charger.
        discharging = f"and on (host_name) host:on_ac_power{{{s}}} == 0"
        g.add(stat("Status", f'node_power_supply_info{{{bat}}}', "none", legend="{{status}}", spark=False,
                   text_mode="name"), 4, 4)
        g.add(stat("Charger", f"host:on_ac_power{{{s}}}", "none", spark=False,
                   mappings=value_map([(1, "PLUGGED IN", "green"), (0, "ON BATTERY", "orange")])), 4, 4)
        g.add(stat("Draw", f"host:battery_power_watts{{{s}}}", "watt", decimals=1), 4, 4)
        g.add(stat("Time left", f"node_power_supply_charge_ampere{{{bat}}} / node_power_supply_current_ampere{{{bat}}}"
                   f" {discharging}", "h", decimals=1, spark=False,
                   desc="Charge left ÷ current draw, while unplugged."), 4, 4)
        g.add(stat("Health", f"node_power_supply_charge_full{{{bat}}} / node_power_supply_charge_full_design{{{bat}}}",
                   "percentunit", BATTERY, decimals=0, spark=False,
                   desc="Full charge today vs. design capacity."), 4, 4)
        g.add(stat("Cycles", f"node_power_supply_cyclecount{{{bat}}}", "none", spark=False), 4, 4)
        g.add(ts("Charge", [prom(f"host:battery_capacity:ratio{{{s}}}", "charge")], "percentunit",
                 lo=0, hi=1, fill=30), 8, 8)
        g.add(ts("Battery power", [prom(f"host:battery_power_watts{{{s}}}", "watts")], "watt", lo=0,
                 desc="Current × voltage. When unplugged this is the whole laptop's draw."), 8, 8)
        g.add(ts("Battery voltage", [prom(f"node_power_supply_voltage_volt{{{bat}}}", "voltage"),
                                     prom(f"node_power_supply_voltage_min_design{{{bat}}}", "design minimum", "B")],
                 "volt", fill=0), 8, 8)
        g.add(state_timeline("Charger", f"host:on_ac_power{{{s}}}", "AC",
                             value_map([(1, "plugged in", "green"), (0, "battery", "orange")])), 24, 3)

    # ---- thermals and fans
    g.section("Thermals and fans")
    g.add(ts("All temperatures", [prom(hwmon("node_hwmon_temp_celsius", s), "{{chip_name}} {{label}}")],
             "celsius", fill=0, thresholds=TEMP), 12, 9)
    g.add(ts("Fans", [prom(hwmon("node_hwmon_fan_rpm", s), "{{chip_name}} {{label}}")], "rotrpm", lo=0,
             fill=0), 12, 9)
    g.add(ts("Voltages", [prom(hwmon("node_hwmon_in_volts", s) + " > 0", "{{chip_name}} {{label}}")],
             "volt", fill=0), 24, 7)

    # ---- storage
    g.section("Storage")
    disks = f'{s}, device!~"dm-.*|zram.*|loop.*"'
    g.add(ts("Throughput (read / write below)", [
        prom(f"rate(node_disk_read_bytes_total{{{disks}}}[{rate}])", "{{device}} read", "A"),
        prom(f"rate(node_disk_written_bytes_total{{{disks}}}[{rate}])", "{{device}} write", "B"),
    ], "Bps", overrides=[negate_series(".*write")]), 8, 8)
    g.add(ts("IOPS (read / write below)", [
        prom(f"rate(node_disk_reads_completed_total{{{disks}}}[{rate}])", "{{device}} read", "A"),
        prom(f"rate(node_disk_writes_completed_total{{{disks}}}[{rate}])", "{{device}} write", "B"),
    ], "iops", overrides=[negate_series(".*write")]), 8, 8)
    g.add(ts("Busy time", [prom(f"rate(node_disk_io_time_seconds_total{{{disks}}}[{rate}])", "{{device}}")],
             "percentunit", lo=0, hi=1), 8, 8)
    g.add(bars("Filesystems (used)",
               f'1 - node_filesystem_avail_bytes{{{s}, fstype!~"{PSEUDO_FS}"}}'
               f' / node_filesystem_size_bytes{{{s}, fstype!~"{PSEUDO_FS}"}}',
               "{{mountpoint}}", "percentunit", PCT, hi=1), 12, 7)
    g.add(stat("Drives", f"node_disk_info{{{s}}}", "none", legend="{{device}} · {{model}}", spark=False,
               text_mode="name"), 12, 7)

    # ---- network
    g.section("Network")
    nics = f'{s}, device!~"{VIRTUAL_NICS}"'
    g.add(ts("Throughput (receive / transmit below)", [
        prom(f"rate(node_network_receive_bytes_total{{{nics}}}[{rate}])", "{{device}} ↓", "A"),
        prom(f"rate(node_network_transmit_bytes_total{{{nics}}}[{rate}])", "{{device}} ↑", "B"),
        prom(f'rate(node_network_receive_bytes_total{{{s}, device=~"tailscale.*"}}[{rate}])', "tailnet ↓", "C"),
        prom(f'rate(node_network_transmit_bytes_total{{{s}, device=~"tailscale.*"}}[{rate}])', "tailnet ↑", "D"),
    ], "Bps", overrides=[negate_series(".*↑")]), 12 if host.wifi else 24, 8)
    if host.wifi:
        g.add(ts("Wi-Fi link", [
            prom(f"node_wifi_station_signal_dbm{{{s}}}", "signal", "A"),
            prom(f"node_wifi_station_receive_bits_per_second{{{s}}}", "rx bitrate", "B"),
            prom(f"node_wifi_station_transmit_bits_per_second{{{s}}}", "tx bitrate", "C"),
        ], "dBm", fill=0, overrides=[{"matcher": {"id": "byRegexp", "options": ".*bitrate"}, "properties": [
            {"id": "unit", "value": "bps"}, {"id": "custom.axisPlacement", "value": "right"}]}]), 12, 8)
    g.add(ts("Errors and drops", [
        prom(f"rate(node_network_receive_errs_total{{{nics}}}[{rate}])", "{{device}} rx errors", "A"),
        prom(f"rate(node_network_transmit_errs_total{{{nics}}}[{rate}])", "{{device}} tx errors", "B"),
        prom(f"rate(node_network_receive_drop_total{{{nics}}}[{rate}])", "{{device}} rx drops", "C"),
    ], "pps", fill=0, lo=0), 24, 6)

    # ---- system
    g.section("System")
    g.add(ts("Processes by state", [prom(f"node_processes_state{{{s}}}", "{{state}}")], "short",
             stack=True, fill=30), 12 if host.systemd else 24, 8)
    if host.systemd:
        g.add(bars("Failed systemd units", f'node_systemd_unit_state{{{s}, state="failed"}} == 1',
                   "{{name}}", "none", [(None, "red")], hi=1,
                   desc="Empty is good."), 12, 8)
    if host.journal:
        g.add(logs("Journal (warnings and errors)",
                   f'{{host_name="{host.name}", level=~"warning|err|crit|alert|emerg"}}'), 24, 10)

    node_full = f"/d/node-exporter-full?var-job=node&var-nodename={host.name}&var-node={host.name}"
    links = [{"title": "Hosts", "type": "link", "url": "/d/hosts-overview"},
             {"title": "Node Exporter Full", "type": "link", "url": node_full}]
    return dashboard(f"host-{host.name}", host.title, g.panels, tags={"hosts", "host", *host.extra_tags},
                     description=host.description, links=links)


def main():
    dashboards = [overview()] + [host_dashboard(h) for h in HOSTS]
    for d in dashboards:
        path = OUT / f"{d['uid']}.json"
        path.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {path.relative_to(OUT.parent.parent)} ({len(d['panels'])} panels)")


if __name__ == "__main__":
    main()
