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

# Drop root before anything runs. This image also hosts shell_exec, which
# executes commands on behalf of an LLM - running that as uid 0 would make the
# command harness the only thing standing between the model and the container.
#
# /data (the shared demo-state volume mount point) is the only path the runtime
# user may write: leaving /app root-owned means no write primitive reachable
# through the agents can modify the source it will run next boot.
RUN useradd --create-home --uid 10001 sre \
    && mkdir -p /data \
    && chown sre:sre /data
USER sre

# Each service overrides this in docker-compose (e.g. python -m mcp_servers.k8s_readonly).
CMD ["python", "-m", "agents.observer.server"]
