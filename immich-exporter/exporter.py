#!/usr/bin/env python3
"""Prometheus exporter for Immich, built on its public HTTP API.

Immich's own /metrics endpoints need IMMICH_TELEMETRY_INCLUDE set on the apps-owned server
container, so this polls the API instead. Liveness and latency work without a key; the storage,
usage and job-queue metrics need an admin API key (IMMICH_API_KEY), otherwise they are skipped.

Env:
  IMMICH_URL      base URL, default http://172.30.0.1:2283 (host gateway seen from the compose net)
  IMMICH_API_KEY  optional admin API key (Immich: Account Settings > API Keys)
  HOST_NAME       host_name label
  POLL_SECONDS    default 30
  LISTEN          default 0.0.0.0:9101 (only reachable on the compose network, never published)
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IMMICH_URL = os.environ.get("IMMICH_URL", "http://172.30.0.1:2283").rstrip("/")
API_KEY = os.environ.get("IMMICH_API_KEY", "").strip()
HOST_NAME = os.environ.get("HOST_NAME", "unknown")
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "30"))
LISTEN_HOST, LISTEN_PORT = (os.environ.get("LISTEN", "0.0.0.0:9101").rsplit(":", 1))

BASE_LABELS = {"host_name": HOST_NAME, "deployment_environment": "homelab", "service_name": "immich"}

lock = threading.Lock()
state = {"lines": [], "errors": {}}


def esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def fmt(name, labels, value):
    all_labels = {**BASE_LABELS, **labels}
    inner = ",".join(f'{k}="{esc(v)}"' for k, v in all_labels.items())
    return f"{name}{{{inner}}} {value}"


def get(path, timeout=10, auth=False):
    req = urllib.request.Request(IMMICH_URL + path)
    if auth:
        req.add_header("x-api-key", API_KEY)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"null")


def collect():
    out = []
    errors = dict(state["errors"])

    # Liveness and latency. Always available.
    t0 = time.monotonic()
    try:
        get("/api/server/ping", timeout=5)
        up = 1
    except (urllib.error.URLError, OSError, ValueError):
        up = 0
        errors["ping"] = errors.get("ping", 0) + 1
    ping_s = time.monotonic() - t0
    out.append(fmt("immich_up", {}, up))
    out.append(fmt("immich_ping_duration_seconds", {}, f"{ping_s:.6f}"))

    if up:
        try:
            v = get("/api/server/version", timeout=5)
            out.append(fmt("immich_version_info", {"version": f'{v["major"]}.{v["minor"]}.{v["patch"]}'}, 1))
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            errors["version"] = errors.get("version", 0) + 1

    if up and API_KEY:
        out.extend(admin_metrics(errors))

    out.append(fmt("immich_exporter_last_poll_timestamp_seconds", {}, f"{time.time():.3f}"))
    state["errors"] = errors  # counters persist across polls
    for ep, n in sorted(errors.items()):
        out.append(fmt("immich_exporter_errors_total", {"endpoint": ep}, n))
    return out


def admin_metrics(errors):
    out = []

    def call(key, path):
        try:
            return get(path, auth=True)
        except (urllib.error.URLError, OSError, ValueError) as e:
            errors[key] = errors.get(key, 0) + 1
            print(f"{path}: {e}", flush=True)
            return None

    stats = call("statistics", "/api/server/statistics")
    if stats:
        out.append(fmt("immich_photos_total", {}, stats.get("photos", 0)))
        out.append(fmt("immich_videos_total", {}, stats.get("videos", 0)))
        out.append(fmt("immich_usage_bytes", {"type": "photos"}, stats.get("usagePhotos", 0)))
        out.append(fmt("immich_usage_bytes", {"type": "videos"}, stats.get("usageVideos", 0)))
        out.append(fmt("immich_usage_bytes", {"type": "total"}, stats.get("usage", 0)))
        for u in stats.get("usageByUser", []) or []:
            who = {"user": u.get("userName", "unknown")}
            out.append(fmt("immich_user_photos", who, u.get("photos", 0)))
            out.append(fmt("immich_user_videos", who, u.get("videos", 0)))
            out.append(fmt("immich_user_usage_bytes", who, u.get("usage", 0)))
            if u.get("quotaSizeInBytes") is not None:
                out.append(fmt("immich_user_quota_bytes", who, u["quotaSizeInBytes"]))

    storage = call("storage", "/api/server/storage")
    if storage:
        out.append(fmt("immich_disk_size_bytes", {}, storage.get("diskSizeRaw", 0)))
        out.append(fmt("immich_disk_used_bytes", {}, storage.get("diskUseRaw", 0)))
        out.append(fmt("immich_disk_available_bytes", {}, storage.get("diskAvailableRaw", 0)))

    jobs = call("jobs", "/api/jobs")
    if jobs:
        for name, j in jobs.items():
            counts = j.get("jobCounts", {}) or {}
            for state_name, n in counts.items():
                out.append(fmt("immich_jobs_count", {"job": name, "state": state_name}, n))
            qs = j.get("queueStatus", {}) or {}
            out.append(fmt("immich_job_active", {"job": name}, int(bool(qs.get("isActive")))))
            out.append(fmt("immich_job_paused", {"job": name}, int(bool(qs.get("isPaused")))))
    return out


def poll_forever():
    while True:
        try:
            lines = collect()
        except Exception as e:  # keep polling whatever happens
            print(f"collect failed: {e}", flush=True)
            lines = [fmt("immich_up", {}, 0)]
        with lock:
            state["lines"] = lines
        time.sleep(POLL_SECONDS)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        with lock:
            body = "\n".join(state["lines"]) + "\n"
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"immich exporter: {IMMICH_URL}, admin metrics {'on' if API_KEY else 'off (no IMMICH_API_KEY)'}",
          flush=True)
    threading.Thread(target=poll_forever, daemon=True).start()
    ThreadingHTTPServer((LISTEN_HOST, int(LISTEN_PORT)), Handler).serve_forever()
