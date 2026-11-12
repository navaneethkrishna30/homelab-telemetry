#!/usr/bin/env python3
"""Prometheus exporter for Intel integrated GPUs (i915).

Runs `intel_gpu_top -J` and republishes each sample on :9102/metrics, together with the
throttle reasons the kernel exposes in sysfs. Standard library only.

    intel_gpu_engine_busy_ratio{engine}          share of the period each engine was busy
    intel_gpu_frequency_hertz{type}              actual / requested GPU clock
    intel_gpu_rc6_ratio                          share of the period the GPU was asleep (RC6)
    intel_gpu_power_watts{domain}                gpu / package power (RAPL)
    intel_gpu_imc_bandwidth_bytes_per_second{direction}   memory controller reads / writes
    intel_gpu_interrupts_per_second
    intel_gpu_throttle{reason}                   1 while the GPU is throttled for that reason
    intel_gpu_up                                 1 while intel_gpu_top is producing samples

Needs /dev/dri and CAP_PERFMON (the i915 PMU is a system-wide perf event).
"""
import glob
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "9102"))
PERIOD_MS = int(os.environ.get("PERIOD_MS", "5000"))
DEVICE = os.environ.get("DEVICE", "")  # intel_gpu_top -d filter; empty = first Intel GPU
THROTTLE_GLOB = "/sys/class/drm/card*/gt/gt*/throttle_reason_*"
STALE_AFTER = 3 * PERIOD_MS / 1000

latest = {}          # last sample from intel_gpu_top
latest_at = 0.0
lock = threading.Lock()


def samples(stream):
    """Yield JSON objects from intel_gpu_top's never-closing JSON array."""
    buf, depth = [], 0
    while chunk := stream.read(1):
        if depth == 0 and chunk != "{":
            continue                        # '[', ',', whitespace between samples
        buf.append(chunk)
        depth += {"{": 1, "}": -1}.get(chunk, 0)
        if depth == 0:
            yield json.loads("".join(buf))
            buf = []


def reader():
    global latest, latest_at
    cmd = ["intel_gpu_top", "-J", "-s", str(PERIOD_MS)] + (["-d", DEVICE] if DEVICE else [])
    while True:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
        try:
            for sample in samples(proc.stdout):
                with lock:
                    latest, latest_at = sample, time.time()
        finally:
            proc.kill()
        print(f"intel_gpu_top exited ({proc.wait()}); restarting in 10s", file=sys.stderr, flush=True)
        time.sleep(10)


def metric_lines(sample):
    lines = []

    def add(name, value, labels=None, help_=None):
        if value is None:
            return
        if help_:
            lines.append(f"# HELP {name} {help_}")
            lines.append(f"# TYPE {name} gauge")
        label = ",".join(f'{k}="{v}"' for k, v in (labels or {}).items())
        lines.append(f"{name}{{{label}}} {value}")

    for i, (engine, stats) in enumerate(sorted(sample.get("engines", {}).items())):
        # Names vary by version: "Render/3D", "Render/3D/0", ... Keep the class, drop the instance.
        name = engine.rstrip("/0123456789") or engine
        add("intel_gpu_engine_busy_ratio", stats.get("busy", 0) / 100, {"engine": name},
            "Share of the period the engine was busy." if i == 0 else None)

    freq = sample.get("frequency", {})
    add("intel_gpu_frequency_hertz", freq.get("actual", 0) * 1e6, {"type": "actual"}, "GPU clock.")
    add("intel_gpu_frequency_hertz", freq.get("requested", 0) * 1e6, {"type": "requested"})

    if "rc6" in sample:
        add("intel_gpu_rc6_ratio", sample["rc6"].get("value", 0) / 100, help_="Share of the period in RC6 (asleep).")

    power = sample.get("power", {})
    for i, domain in enumerate(k for k in power if k != "unit"):
        add("intel_gpu_power_watts", power[domain], {"domain": domain.lower()},
            "RAPL power by domain." if i == 0 else None)

    imc = sample.get("imc-bandwidth", {})
    for i, direction in enumerate(("reads", "writes")):
        if direction in imc:
            add("intel_gpu_imc_bandwidth_bytes_per_second", imc[direction] * 1024 * 1024,
                {"direction": direction.rstrip("s")}, "Memory controller bandwidth." if i == 0 else None)

    add("intel_gpu_interrupts_per_second", sample.get("interrupts", {}).get("count"),
        help_="GPU interrupts per second.")
    return lines


def throttle_lines():
    lines = ["# HELP intel_gpu_throttle 1 while the GPU is throttled for this reason.",
             "# TYPE intel_gpu_throttle gauge"]
    for path in sorted(glob.glob(THROTTLE_GLOB)):
        reason = path.rsplit("throttle_reason_", 1)[1]
        if reason == "status":
            continue
        try:
            value = int(open(path).read().strip())
        except (OSError, ValueError):
            continue
        lines.append(f'intel_gpu_throttle{{reason="{reason}"}} {value}')
    return lines


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_error(404)
            return
        with lock:
            sample, fresh = latest, time.time() - latest_at < STALE_AFTER
        lines = ["# HELP intel_gpu_up 1 while intel_gpu_top is producing samples.", "# TYPE intel_gpu_up gauge",
                 f"intel_gpu_up {int(fresh)}"]
        if fresh:
            lines += metric_lines(sample)
        lines += throttle_lines()
        body = ("\n".join(lines) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=reader, daemon=True).start()
    print(f"intel-gpu-exporter on :{PORT}, sampling every {PERIOD_MS} ms", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
