"""Backend selection: demo substrate or real infrastructure.

Selection happens per call rather than at import. FastMCP registers tools at
import time, so reading ``DEMO_MODE`` there would freeze the choice before a test
(or a container entrypoint) had a chance to set the environment - and would make
the servers importable only under the right env.

All eight adapters are stateless (no fields, no held connections), so there is
nothing to cache and no lifecycle to manage. Tests override a selector with
``monkeypatch.setattr(backends, "k8s_read", ...)``.
"""

from __future__ import annotations

from common.env import env_flag

from .demo import DemoAlerts, DemoK8sRead, DemoK8sWrite, DemoLogs
from .ports import AlertsPort, K8sReadPort, K8sWritePort, LogsPort
from .real import RealAlerts, RealK8sRead, RealK8sWrite, RealLogs


def _demo() -> bool:
    return env_flag("DEMO_MODE", default=True)


def k8s_read() -> K8sReadPort:
    return DemoK8sRead() if _demo() else RealK8sRead()


def k8s_write() -> K8sWritePort:
    return DemoK8sWrite() if _demo() else RealK8sWrite()


def alerts() -> AlertsPort:
    return DemoAlerts() if _demo() else RealAlerts()


def logs() -> LogsPort:
    return DemoLogs() if _demo() else RealLogs()
