# immich-exporter: stdlib Python plus pg8000 (pure-Python Postgres driver) for the database size.
# exporter.py itself is bind-mounted by compose, so editing it only needs `docker compose restart immich-exporter`.
FROM python:3.13-alpine
RUN pip install --no-cache-dir pg8000==1.31.2
