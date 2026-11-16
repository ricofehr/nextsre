# Spec Delta

## ADDED Requirements

### Requirement: Deployment-configurable narrowing of the shell binary allowlist
The shell execution harness SHALL read `SHELL_BINARIES` (comma-separated) at call time and allow only binaries in both that set and the built-in allowlist. When unset, the built-in allowlist SHALL apply unchanged; when the intersection is empty, every command SHALL be refused. Verb, flag and denylist checks SHALL still apply to every binary that remains allowed.

#### Scenario: Narrowed to kubectl refuses host tools
- **WHEN** `SHELL_BINARIES=kubectl` and a caller submits `df -h` or `systemctl status nginx`
- **THEN** the harness raises `PermissionError` naming the binary and nothing is executed

#### Scenario: Narrowed kubectl keeps its read-only verb policy
- **WHEN** `SHELL_BINARIES=kubectl` and a caller submits `kubectl get pods -n web`, then `kubectl delete pod x -n web`
- **THEN** the first command passes the harness and the second raises `PermissionError`

#### Scenario: Narrowing cannot widen the allowlist
- **WHEN** `SHELL_BINARIES=kubectl,bash,curl` and a caller submits `bash -c id` or `curl http://example.com`
- **THEN** the harness raises `PermissionError` and only `kubectl` is executable

#### Scenario: Empty narrowing refuses everything
- **WHEN** `SHELL_BINARIES` is set to `bash` or to `,`
- **THEN** every command is refused by the harness

#### Scenario: Unset keeps the built-in allowlist
- **WHEN** `SHELL_BINARIES` is unset and a caller submits `uptime`
- **THEN** the command passes the binary check as before this change
