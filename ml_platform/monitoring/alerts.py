"""
ML Platform — Alert Manager
=============================
Evaluates monitoring thresholds and dispatches alerts.

Design:
  - AlertRule defines the condition
  - AlertDispatcher sends notifications (log, webhook, Slack, PagerDuty)
  - AlertManager orchestrates: evaluates rules, dispatches on trigger

Future integrations: PagerDuty, Opsgenie, Slack webhooks, email.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from ml_platform.monitoring.monitor import ModelHealthSnapshot


class AlertSeverity(str, Enum):
    INFO     = "info"
    WARNING  = "warning"
    CRITICAL = "critical"


@dataclass
class AlertRule:
    name:          str
    model_id:      str
    metric:        str           # attribute name on ModelHealthSnapshot
    threshold:     float
    operator:      str           # "gt", "lt", "gte", "lte"
    severity:      AlertSeverity = AlertSeverity.WARNING
    message_tmpl:  str           = ""
    cooldown_s:    int           = 300    # don't re-alert within this window


@dataclass
class Alert:
    alert_id:      str
    rule_name:     str
    model_id:      str
    severity:      AlertSeverity
    metric:        str
    actual_value:  float
    threshold:     float
    message:       str
    fired_at:      str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    resolved:      bool = False


class BaseAlertDispatcher(ABC):

    @abstractmethod
    def dispatch(self, alert: Alert) -> None: ...


class LogAlertDispatcher(BaseAlertDispatcher):
    """Writes alerts to the platform logger."""

    def dispatch(self, alert: Alert) -> None:
        # TODO: wire to platform logger
        print(f"[ALERT][{alert.severity.upper()}] {alert.rule_name}: {alert.message}")


class WebhookAlertDispatcher(BaseAlertDispatcher):
    """Posts alert payload to an HTTP webhook."""

    def __init__(self, webhook_url: str) -> None:
        self._url = webhook_url

    def dispatch(self, alert: Alert) -> None:
        # TODO: implement HTTP POST with retry
        pass


class AlertManager:
    """
    Evaluates a set of AlertRules against a ModelHealthSnapshot.

    Usage:
        manager = AlertManager(dispatchers=[LogAlertDispatcher()])
        manager.add_rule(AlertRule(
            name="high_failure_rate",
            model_id="abc123",
            metric="failure_rate",
            threshold=0.05,
            operator="gt",
            severity=AlertSeverity.CRITICAL,
        ))
        alerts = manager.evaluate(snapshot)
    """

    def __init__(self, dispatchers: list[BaseAlertDispatcher] | None = None) -> None:
        self._rules: list[AlertRule] = []
        self._dispatchers = dispatchers or [LogAlertDispatcher()]
        self._last_fired: dict[str, float] = {}

    def add_rule(self, rule: AlertRule) -> None:
        self._rules.append(rule)

    def evaluate(self, snapshot: ModelHealthSnapshot) -> list[Alert]:
        import time
        alerts: list[Alert] = []
        now = time.monotonic()

        for rule in self._rules:
            if rule.model_id != snapshot.model_id:
                continue
            actual = getattr(snapshot, rule.metric, None)
            if actual is None:
                continue
            if not self._check(actual, rule.operator, rule.threshold):
                continue
            # Cooldown check
            last = self._last_fired.get(rule.name, 0)
            if now - last < rule.cooldown_s:
                continue

            alert = Alert(
                alert_id=f"{rule.name}_{snapshot.model_id}",
                rule_name=rule.name,
                model_id=rule.model_id,
                severity=rule.severity,
                metric=rule.metric,
                actual_value=actual,
                threshold=rule.threshold,
                message=rule.message_tmpl or
                    f"{rule.metric}={actual:.4f} {rule.operator} threshold={rule.threshold}",
            )
            alerts.append(alert)
            self._last_fired[rule.name] = now

            for dispatcher in self._dispatchers:
                try:
                    dispatcher.dispatch(alert)
                except Exception:
                    pass

        return alerts

    @staticmethod
    def _check(actual: float, operator: str, threshold: float) -> bool:
        return {
            "gt":  actual >  threshold,
            "lt":  actual <  threshold,
            "gte": actual >= threshold,
            "lte": actual <= threshold,
            "eq":  actual == threshold,
        }.get(operator, False)
