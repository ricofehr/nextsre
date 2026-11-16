# container-image Specification

## Purpose
Defines the single runtime image every NextSRE component runs from, so a real-cluster deployment needs no per-component build, and how that image is published.

## Requirements

### Requirement: One image runs every component in real mode
The runtime image SHALL contain every component entry point (the six MCP servers, both agents, and the ingestion gateway), the optional dependencies the real backends need, and a `kubectl` client of a pinned version whose download is verified against its published SHA-256 checksum, so that each component starts from the same image with `DEMO_MODE=0`. It SHALL run as a non-root user.

#### Scenario: Gateway starts from the image
- **WHEN** a container from the image runs `python -m services.gateway`
- **THEN** the gateway starts and answers `GET /health` with HTTP 200

#### Scenario: Real Kubernetes backend is importable
- **WHEN** a container from the image imports the Kubernetes client used by the real read backend
- **THEN** the import succeeds

#### Scenario: kubectl is present at the pinned version
- **WHEN** a container from the image runs `kubectl version --client`
- **THEN** it prints the pinned client version, and the image build fails if the downloaded binary does not match its checksum

#### Scenario: Image runs unprivileged
- **WHEN** a container from the image starts with no user override
- **THEN** its process runs with a non-zero UID

### Requirement: Image published to a public registry by CI
A CI workflow SHALL build and smoke-test the image on every pull request and push, and SHALL push it to GitHub Container Registry on every push to `main` and every `v*` tag, tagged with the full commit SHA plus `main` or the version. Only the publishing job SHALL hold `packages: write`, and a push to `main` SHALL never be cancelled by a later one. Third-party actions SHALL be pinned to a commit SHA.

#### Scenario: Push to main publishes a SHA-tagged image
- **WHEN** a commit lands on `main`
- **THEN** `ghcr.io/<owner>/nextsre:<sha>` and `ghcr.io/<owner>/nextsre:main` are published

#### Scenario: Pull request builds and smoke-tests without publishing
- **WHEN** a pull request is opened or updated
- **THEN** the image is built and run (non-root UID, Kubernetes client importable, `kubectl` present, gateway healthy), and nothing is pushed to the registry

#### Scenario: A broken image is never published
- **WHEN** the smoke test fails on a push to `main`
- **THEN** the publishing job does not run
