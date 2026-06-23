# Single image shared by all 7 services; the compose file picks the command.
FROM python:3.12-slim

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# Now the source.
COPY common ./common
COPY mcp_servers ./mcp_servers
COPY agents ./agents
COPY scripts ./scripts
COPY tests ./tests

# Re-install so the package metadata matches the copied source.
RUN pip install --no-cache-dir --no-deps .

# Each service overrides this in docker-compose (e.g. python -m mcp_servers.k8s_readonly).
CMD ["python", "-m", "agents.observer.server"]
