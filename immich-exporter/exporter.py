#!/usr/bin/env python3
"""Prometheus exporter for Immich, built on its public HTTP API.

Immich's own /metrics endpoints need IMMICH_TELEMETRY_INCLUDE set on the apps-owned server
container, so this polls the API instead. Liveness and latency work without a key; the storage,
usage and job-queue metrics need an admin API key (IMMICH_API_KEY), otherwise they are skipped.
The database size needs a Postgres login (IMMICH_DB_PASSWORD, a read-only pg_monitor role), otherwise it is skipped.

Env:
  IMMICH_URL      base URL, default http://172.30.0.1:2283 (host gateway seen from the compose net)
  IMMICH_API_KEY  optional admin API key (Immich: Account Settings > API Keys)
  HOST_NAME       host_name label
  POLL_SECONDS    default 30
  LISTEN          default 0.0.0.0:9101 (only reachable on the compose network, never published)
  IMMICH_DB_HOST      default immich_postgres (the exporter joins the `immich` network)
  IMMICH_DB_PORT      default 5432
  IMMICH_DB_NAME      default immich
  IMMICH_DB_USER      default immich_metrics
  IMMICH_DB_PASSWORD  optional; enables immich_database_size_bytes and the immich_library_* history
  LIBRARY_SECONDS     how often to recompute the library history, default 900
  ADDED_DAYS          days of per-day upload counts to publish, default 90
  LIBRARY_TZ          time zone for "per day" (IANA name), default UTC
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
DB = {
    "host": os.environ.get("IMMICH_DB_HOST", "immich_postgres"),
    "port": int(os.environ.get("IMMICH_DB_PORT", "5432")),
    "database": os.environ.get("IMMICH_DB_NAME", "immich"),
    "user": os.environ.get("IMMICH_DB_USER", "immich_metrics"),
    "password": os.environ.get("IMMICH_DB_PASSWORD", "").strip(),
}

BASE_LABELS = {"host_name": HOST_NAME, "deployment_environment": "homelab", "service_name": "immich"}

lock = threading.Lock()
state = {"lines": [], "errors": {}}
LIBRARY_SECONDS = float(os.environ.get("LIBRARY_SECONDS", "900"))
ADDED_DAYS = int(os.environ.get("ADDED_DAYS", "90"))
LIBRARY_TZ = os.environ.get("LIBRARY_TZ", "UTC")
IMPORT_GAP = "6 hours"
library = {"lines": [], "at": 0.0}  # cached: these queries scan every asset

# Assets the way Immich's own statistics count them: not deleted, not hidden (live-photo motion parts).
LIBRARY_FROM = """FROM asset a LEFT JOIN asset_exif e ON e."assetId" = a.id
                  WHERE a."deletedAt" IS NULL AND a.visibility <> 'hidden'"""
TYPES = {"IMAGE": "photo", "VIDEO": "video"}


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

    if DB["password"]:
        size = database_size()
        if size is None:
            errors["database"] = errors.get("database", 0) + 1
        else:
            out.append(fmt("immich_database_size_bytes", {"database": DB["database"]}, size))
        if time.time() - library["at"] >= LIBRARY_SECONDS:
            lines = library_history()
            if lines is None:
                errors["library"] = errors.get("library", 0) + 1
            else:
                library.update(lines=lines, at=time.time())
        out.extend(library["lines"])

    out.append(fmt("immich_exporter_last_poll_timestamp_seconds", {}, f"{time.time():.3f}"))
    state["errors"] = errors  # counters persist across polls
    for ep, n in sorted(errors.items()):
        out.append(fmt("immich_exporter_errors_total", {"endpoint": ep}, n))
    return out


def database_size():
    """pg_database_size of the Immich database, or None if Postgres can't be reached."""
    import pg8000.native  # only needed when IMMICH_DB_PASSWORD is set
    try:
        conn = pg8000.native.Connection(timeout=10, application_name="immich-exporter", **DB)
        try:
            return int(conn.run("SELECT pg_database_size(current_database())")[0][0])
        finally:
            conn.close()
    except Exception as e:  # pg8000 raises its own error types plus OSError
        print(f"database size: {e}", flush=True)
        return None


def library_history():
    """Precomputed library history, from Immich's tables (needs pg_read_all_data):
      immich_library_year_{assets,bytes}{year,type}    everything, by the year it was taken
      immich_library_added_{assets,bytes}{day,type}    uploads per day for the last ADDED_DAYS, after the import
      immich_library_import_assets                     size of the initial bulk import (see below)
      immich_library_import_end_timestamp_seconds      when it ended (the first later upload)
    """
    import pg8000.native
    try:
        conn = pg8000.native.Connection(timeout=30, application_name="immich-exporter", **DB)
    except Exception as e:
        print(f"library history: {e}", flush=True)
        return None
    out = []
    try:
        years = conn.run(f"""SELECT extract(year FROM a."localDateTime")::int, a.type, count(*),
                                    coalesce(sum(e."fileSizeInByte"), 0)
                             {LIBRARY_FROM} GROUP BY 1, 2""")
        for year, typ, n, size in years:
            labels = {"year": year, "type": TYPES.get(typ, typ.lower())}
            out.append(fmt("immich_library_year_assets", labels, n))
            out.append(fmt("immich_library_year_bytes", labels, size))

        conn.run("SELECT set_config('TimeZone', :tz, false)", tz=LIBRARY_TZ)  # days = LIBRARY_TZ calendar days
        # The initial bulk import: uploads from the very first one until the first gap of more than
        # IMPORT_GAP. "Recently added" starts after it, so one huge import doesn't flatten every later day.
        rows = conn.run(f"""WITH t AS (SELECT a."createdAt" c, a."createdAt" - lag(a."createdAt") OVER (ORDER BY a."createdAt") gap
                                       {LIBRARY_FROM})
                            SELECT (SELECT min(c) FROM t WHERE gap > CAST(:gap AS interval)) AS import_end,
                                   count(*) FILTER (WHERE c < coalesce((SELECT min(c) FROM t WHERE gap > CAST(:gap AS interval)),
                                                                       'infinity'))
                            FROM t""", gap=IMPORT_GAP)
        import_end, n_import = rows[0] if rows else (None, 0)
        out.append(fmt("immich_library_import_assets", {}, n_import))
        if import_end is not None:
            out.append(fmt("immich_library_import_end_timestamp_seconds", {}, f"{import_end.timestamp():.0f}"))
            added = conn.run(f"""SELECT to_char(date_trunc('day', a."createdAt"), 'YYYY-MM-DD'), a.type, count(*),
                                        coalesce(sum(e."fileSizeInByte"), 0)
                                 {LIBRARY_FROM} AND a."createdAt" >= greatest(CAST(:start AS timestamptz),
                                                                               now() - make_interval(days => CAST(:n AS int)))
                                 GROUP BY 1, 2""", start=import_end, n=ADDED_DAYS)
            for day, typ, n, size in added:
                labels = {"day": day, "type": TYPES.get(typ, typ.lower())}
                out.append(fmt("immich_library_added_assets", labels, n))
                out.append(fmt("immich_library_added_bytes", labels, size))
        out.append(fmt("immich_library_history_timestamp_seconds", {}, f"{time.time():.0f}"))
        return out
    except Exception as e:  # e.g. a future Immich release renames a table
        print(f"library history: {e}", flush=True)
        return None
    finally:
        conn.close()


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
    print(f"immich exporter: {IMMICH_URL}, admin metrics {'on' if API_KEY else 'off (no IMMICH_API_KEY)'}, "
          f"database size {'on' if DB['password'] else 'off (no IMMICH_DB_PASSWORD)'}", flush=True)
    threading.Thread(target=poll_forever, daemon=True).start()
    ThreadingHTTPServer((LISTEN_HOST, int(LISTEN_PORT)), Handler).serve_forever()
