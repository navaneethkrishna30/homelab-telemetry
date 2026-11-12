# intel-gpu-exporter: intel_gpu_top (Debian's intel-gpu-tools) plus a stdlib Python wrapper.
# exporter.py is bind-mounted by compose, so editing it only needs `docker compose restart intel-gpu-exporter`.
FROM python:3.13-slim-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends intel-gpu-tools \
 && rm -rf /var/lib/apt/lists/*
