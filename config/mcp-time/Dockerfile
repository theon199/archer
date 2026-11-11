FROM python@sha256:c3729439afc4ec3f3e159e0c86aeabfadac8030ef46838a0256383d0511c5f5e
COPY requirements.txt /build/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r /build/requirements.txt
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONTZPATH=""
LABEL org.opencontainers.image.source="https://github.com/modelcontextprotocol/servers/tree/main/src/time" archer.connector="official-mcp-time-2026.10.19"
ENTRYPOINT ["python", "-m", "mcp_server_time", "--local-timezone", "UTC"]
