"""Explicit, opt-in request blocking policies for traffic experiments.

The policy is intentionally independent from the Phase 1 request classifier.
Only static, reviewable rules are eligible for abort; every non-match falls
back to the previously registered Signed Request route.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urlparse


EXPERIMENT_TELEMETRY_V1 = "telemetry-v1"
BLOCKING_TYPE = "ABORT_ONLY"
PRESERVED_RESOURCE_TYPES = frozenset({"image", "font", "media"})


@dataclass(frozen=True)
class TrafficReductionRule:
    """One static host/path telemetry rule."""

    name: str
    hosts: tuple[str, ...] = ()
    host_suffixes: tuple[str, ...] = ()
    exact_paths: tuple[str, ...] = ()
    path_prefixes: tuple[str, ...] = ()

    def matches(self, host: str, path: str) -> bool:
        host_matches = host in self.hosts or any(
            host == suffix.lstrip(".") or host.endswith(suffix)
            for suffix in self.host_suffixes
        )
        path_matches = path in self.exact_paths or any(
            path.startswith(prefix) for prefix in self.path_prefixes
        )
        return host_matches and path_matches


TELEMETRY_V1_RULES = (
    TrafficReductionRule(
        name="google_analytics_collect",
        hosts=("analytics.google.com", "stats.g.doubleclick.net"),
        exact_paths=("/g/collect",),
    ),
    TrafficReductionRule(
        name="google_ads_collect",
        hosts=("ad.doubleclick.net",),
        exact_paths=("/ccm/s/collect",),
    ),
    TrafficReductionRule(
        name="facebook_pixel",
        hosts=("www.facebook.com",),
        path_prefixes=("/tr/",),
    ),
    TrafficReductionRule(
        name="microsoft_clarity_collect",
        host_suffixes=(".clarity.ms",),
        exact_paths=("/collect",),
    ),
    TrafficReductionRule(
        name="tiktok_pixel",
        hosts=("analytics.tiktok.com",),
        path_prefixes=("/i18n/pixel/",),
    ),
)


class TrafficReductionPolicy:
    """Playwright routing policy for one named, explicitly enabled experiment."""

    def __init__(
        self,
        experiment_name: Optional[str] = None,
        *,
        rules: Optional[Iterable[TrafficReductionRule]] = None,
    ) -> None:
        self.experiment_name = experiment_name
        self.enabled = experiment_name is not None
        if self.enabled and experiment_name != EXPERIMENT_TELEMETRY_V1:
            raise ValueError(f"unsupported traffic reduction experiment: {experiment_name}")
        self.rules = tuple(rules if rules is not None else TELEMETRY_V1_RULES)
        self.matched_requests = 0
        self.blocked_requests = 0
        self.by_host: Counter[str] = Counter()
        self.by_rule: Counter[str] = Counter()
        self.by_resource_type: Counter[str] = Counter()
        self.attached_contexts = 0

    def match_rule(self, url: str, resource_type: str) -> Optional[TrafficReductionRule]:
        """Return the matching static rule, never consulting runtime classification."""

        if not self.enabled or str(resource_type).lower() in PRESERVED_RESOURCE_TYPES:
            return None
        parsed = urlparse(str(url or ""))
        host = (parsed.hostname or "").lower()
        path = parsed.path or "/"
        return next((rule for rule in self.rules if rule.matches(host, path)), None)

    def _handle_route(self, route) -> None:
        request = route.request
        resource_type = str(request.resource_type or "other").lower()
        rule = self.match_rule(request.url, resource_type)
        if rule is None:
            route.fallback()
            return

        host = (urlparse(request.url).hostname or "unknown").lower()
        self.matched_requests += 1
        self.by_host[host] += 1
        self.by_rule[rule.name] += 1
        self.by_resource_type[resource_type] += 1
        route.abort()
        self.blocked_requests += 1

    def attach(self, context) -> None:
        """Register after Signed Request; non-target fallback preserves its handler."""

        if not self.enabled:
            return
        context.route("**/*", self._handle_route)
        self.attached_contexts += 1

    def runtime_summary(self) -> dict:
        return {
            "enabled": self.enabled,
            "experiment": self.experiment_name,
            "blocking_type": BLOCKING_TYPE if self.enabled else None,
        }

    def build_summary(self) -> dict:
        """Return a secret-free aggregate; URLs, queries, and headers are excluded."""

        return {
            "schema_version": "1.0",
            "enabled": self.enabled,
            "experiment": self.experiment_name,
            "blocking_type": BLOCKING_TYPE if self.enabled else None,
            "rules": [rule.name for rule in self.rules] if self.enabled else [],
            "attached_contexts": self.attached_contexts,
            "matched_requests": self.matched_requests,
            "blocked_requests": self.blocked_requests,
            "by_host": dict(sorted(self.by_host.items())),
            "by_rule": dict(sorted(self.by_rule.items())),
            "by_resource_type": dict(sorted(self.by_resource_type.items())),
        }


def create_traffic_reduction_policy(
    experiment_name: Optional[str],
) -> TrafficReductionPolicy:
    """Create an OFF policy or the named static experiment."""

    return TrafficReductionPolicy(experiment_name)
