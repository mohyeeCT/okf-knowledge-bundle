"""Shared robots-aware pacing for sitemap, collection and availability requests."""
import math
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from .common import digest, normalize_url, public_url, utc_now
from .network import HTML_REQUEST_HEADERS, REQUEST_USER_AGENT


class AvailabilityPolicyError(ValueError):
    """Safe local policy explanation; never a remote response or credential."""


class RobotsDisallowedError(AvailabilityPolicyError):
    """A robots access rule prohibits this destination."""


def _crawl_delay(text):
    """Conservatively honor matching groups, including fractional delays.

    RobotFileParser handles access rules and integer Request-rate. Its
    crawl_delay ignores fractions, so retain/validate those separately.
    """
    groups, agents, rules = [], [], []
    for line in text.splitlines() + ["User-agent: end-of-input"]:
        key, sep, value = line.split("#", 1)[0].partition(":")
        if not sep:
            continue
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
        else:
            rules.append((key, value))
    delays = [0]
    for agents, rules in groups:
        if not any(agent == "*" or agent and agent in REQUEST_USER_AGENT.lower() for agent in agents):
            continue
        for key, value in rules:
            if key == "crawl-delay":
                try:
                    delay = float(value)
                except ValueError as error:
                    raise AvailabilityPolicyError("Invalid robots Crawl-delay; no page request sent") from error
                if not math.isfinite(delay) or delay < 0:
                    raise AvailabilityPolicyError("Invalid robots Crawl-delay; no page request sent")
                delays.append(delay)
    return max(delays)


class DirectFetcher:
    def __init__(self, state, transport, clock=None, sleep=None):
        self.state = state
        self.config = {**state["config"], "collection_authorized": True}
        self.transport = transport
        self.clock = clock or time.time
        self.sleep = sleep or time.sleep
        self.interval = self.config.get("direct_interval_seconds", 0.5)
        if (type(self.interval) not in (int, float) or not math.isfinite(self.interval)
                or not 0 <= self.interval <= 30):
            raise ValueError("Direct request interval must be bounded between zero and thirty seconds")
        self.policies = {}
        self.requests = []

    def _origin(self, url):
        if not public_url(url, self.config["domains"]):
            raise ValueError("Direct destination is outside permitted public scope")
        parts = urlsplit(normalize_url(url))
        return parts.scheme + "://" + parts.netloc

    def _pace(self, url):
        origin = self._origin(url)
        prior_policy = self.state.get("direct_robots_checks", {}).get(origin,
            self.state.get("availability_robots_checks", {}).get(origin, {}))
        interval = max(self.interval, prior_policy.get("effective_interval_seconds", self.interval))
        times = [table[origin] for table in (self.state.get("direct_request_times", {}),
                 self.state.get("availability_request_times", {})) if origin in table]
        last = max(times) if times else None
        now = self.clock()
        wait = max(0, interval - (now - last)) if last is not None else 0
        # Long robots delays stop for a later invocation rather than block the host.
        if wait > 60:
            raise AvailabilityPolicyError("Robots pacing requires a later check; no request sent")
        if wait:
            self.sleep(wait)
            now = self.clock()
            if now - last < interval - 0.001:
                raise AvailabilityPolicyError("Pacing wait did not elapse; no request sent")
        self.state.setdefault("direct_request_times", {})[origin] = now
        self.requests.append({"url": url, "at_epoch": now, "wait_seconds": wait,
                              "interval_seconds": interval})

    def _policy(self, url):
        origin = self._origin(url)
        if origin in self.policies:
            policy = self.policies[origin]
            if policy is None:
                raise AvailabilityPolicyError("Robots policy could not be checked; no page request sent")
            return policy
        # Remember a failed policy too: no automatic metadata retries in a batch.
        self.policies[origin] = None
        try:
            response = self.transport(origin + "/robots.txt", self.config, max_bytes=256 * 1024,
                                      headers={"Accept": "text/plain", "User-Agent": REQUEST_USER_AGENT},
                                      before_request=self._pace)
        except AvailabilityPolicyError:
            raise
        except (OSError, TimeoutError, ValueError) as error:
            raise AvailabilityPolicyError("Robots policy could not be checked; no page request sent") from error
        status, body = response.get("status"), response.get("body", b"")
        self._origin(response.get("url") or origin + "/robots.txt")
        if not isinstance(body, bytes) or len(body) > 256 * 1024:
            raise AvailabilityPolicyError("Malformed robots response; no page request sent")
        prior = self.state.get("direct_robots_checks", {}).get(origin,
            self.state.get("availability_robots_checks", {}).get(origin, {}))
        record = {"at": utc_now(), "status": status, "response_hash": digest(body),
                  "observed_url": response.get("url") or origin + "/robots.txt",
                  "body": body.decode("utf-8", errors="replace"),
                  "configured_interval_seconds": self.interval,
                  "effective_interval_seconds": max(self.interval, prior.get("effective_interval_seconds", 0)),
                  "checked": False}
        self.state.setdefault("direct_robots_checks", {})[origin] = record
        parser = RobotFileParser()
        if status == 200 and b"<html" not in body.lower() and b"<!doctype html" not in body.lower():
            parser.parse(record["body"].splitlines())
        elif status in (404, 410):
            parser.parse([])
        else:
            raise AvailabilityPolicyError("Robots policy could not be checked; no page request sent")
        delay = _crawl_delay(record["body"]) if status == 200 else 0
        rate = parser.request_rate(REQUEST_USER_AGENT)
        rate_interval = rate.seconds / rate.requests if rate and rate.requests > 0 else 0
        record.update(checked=True, crawl_delay_seconds=delay,
                      request_rate_interval_seconds=rate_interval,
                      effective_interval_seconds=max(self.interval, delay, rate_interval))
        self.policies[origin] = (parser, record)
        return parser, record

    def _before_page(self, url):
        parser, _ = self._policy(url)
        if not parser.can_fetch(REQUEST_USER_AGENT, url):
            raise RobotsDisallowedError("Robots policy disallows this URL; no request sent; do not bypass through another provider")
        self._pace(url)

    def __call__(self, url, *, max_bytes=20 * 1024 * 1024, headers=None, decode_content_encoding=True):
        # Guarded direct_fetch invokes the callback for every redirect hop too.
        return self.transport(url, self.config, max_bytes=max_bytes,
                              headers=HTML_REQUEST_HEADERS if headers is None else headers,
                              decode_content_encoding=decode_content_encoding, before_request=self._before_page)

    def receipt(self):
        return {"mode": "robots-aware-direct", "configured_interval_seconds": self.interval,
                "requests": self.requests,
                "robots": {origin: record for origin, record in self.state.get("direct_robots_checks", {}).items()
                           if origin in self.policies}}


# Public name retained for callers from 0.2.10. All paths share the same clocks.
AvailabilityFetcher = DirectFetcher
