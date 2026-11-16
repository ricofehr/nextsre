# Single image shared by every service (6 MCP servers, 2 agents, the gateway);
# the compose file picks the command.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# kubectl for shell_exec's read-only diagnostics. Keep the minor within one of
# the target cluster's (kubectl's supported skew); bump the pinned checksums
# with the version, from https://dl.k8s.io/release/<version>/bin/linux/<arch>/kubectl.sha256
ARG KUBECTL_VERSION=v1.35.10
ARG KUBECTL_SHA256_AMD64=c49a28f1f4efc4d475ed05f47f2ce8e563bf7bef69ed77fe1b924ea6d83b6327
ARG KUBECTL_SHA256_ARM64=283f1be6a46b3695b625e25030106b8ffe18bd0d3a900f5b65c259ca407f04f5
# Supplied by BuildKit; a default here would override it. The classic builder
# leaves it empty, so fall back to the base image's own architecture.
ARG TARGETARCH
RUN set -eu; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
      amd64) sum="$KUBECTL_SHA256_AMD64" ;; \
      arm64) sum="$KUBECTL_SHA256_ARM64" ;; \
      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], '/usr/local/bin/kubectl')" \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl"; \
    echo "$sum  /usr/local/bin/kubectl" | sha256sum -c -; \
    chmod 0755 /usr/local/bin/kubectl

# Install dependencies first for better layer caching. The real extra carries
# the Kubernetes client the DEMO_MODE=0 backends need.
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --upgrade --upgrade-strategy eager ".[real]"

# Now the source.
COPY common ./common
COPY mcp_servers ./mcp_servers
COPY agents ./agents
COPY services ./services
COPY scripts ./scripts

# Re-install so the package metadata matches the copied source.
RUN pip install --no-cache-dir --no-deps .

# Drop root before anything runs. This image also hosts shell_exec, which
# executes commands on behalf of an LLM - running that as uid 0 would make the
# command harness the only thing standing between the model and the container.
#
# /data (the shared demo-state volume mount point) is the only path the runtime
# user may write: leaving /app root-owned means no write primitive reachable
# through the agents can modify the source it will run next boot.
RUN groupadd --gid 10001 sre \
    && useradd --create-home --uid 10001 --gid 10001 sre \
    && mkdir -p /data \
    && chown sre:sre /data
# Numeric, so Kubernetes' runAsNonRoot check can verify it (it cannot resolve a name).
USER 10001:10001

# Each service overrides this in docker-compose (e.g. python -m mcp_servers.k8s_readonly).
CMD ["python", "-m", "agents.observer.server"]
