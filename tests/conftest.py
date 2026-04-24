"""Shared fixtures for all test tiers.

Layout conventions — keep these stable so new webhooks/plugins drop
in without touching existing tests:

* JSON payloads live under ``tests/fixtures/payloads/<service>/<scenario>.json``
  and are loaded via the ``payload`` fixture by dotted name
  (e.g. ``payload('grafana.firing_basic')``).
* Per-tier extras live in ``tests/<tier>/conftest.py``.
* Adding a new webhook: drop payload JSON + one ``test_<name>.py`` in
  ``tests/unit/webhooks/``. Adding a plugin: same under ``plugins/``.

Fixtures here are tier-agnostic. App-context / docker fixtures live in
the per-tier conftests.
"""
import json
from pathlib import Path

import pytest


FIXTURES_DIR = Path(__file__).parent / 'fixtures'
PAYLOADS_DIR = FIXTURES_DIR / 'payloads'


@pytest.fixture
def payload():
    """Load a JSON payload fixture by dotted name.

    Args:
        Called with a single string like ``'grafana.firing_basic'``
        which resolves to ``tests/fixtures/payloads/grafana/firing_basic.json``.

    Returns:
        The parsed JSON as a ``dict``. Returns a fresh copy per call
        so tests can mutate without leaking into other tests.

    Example:
        >>> body = payload('grafana.firing_basic')
        >>> body['alerts'][0]['labels']['alertname']
        'HighCpuUsage'
    """
    def _load(name: str) -> dict:
        rel = name.replace('.', '/') + '.json'
        path = PAYLOADS_DIR / rel
        if not path.exists():
            raise FileNotFoundError(
                f"No payload fixture at {path}. "
                f"Drop a JSON file there to create it."
            )
        return json.loads(path.read_text())
    return _load


@pytest.fixture
def make_grafana_alert():
    """Factory for a single-alert Grafana webhook element.

    Returns a callable that produces the dict shape ``parse_grafana``
    expects (one element of ``payload['alerts']``). Any keyword
    overrides update the defaults — use this for table-driven tests
    where only one or two fields differ between cases.

    Returns:
        Callable producing a Grafana alert dict.

    Example:
        >>> alert = make_grafana_alert(labels={'alertname': 'X', 'host': 'h1', 'name': 'X'})
        >>> alert['status']
        'firing'
    """
    def _build(**overrides) -> dict:
        base = {
            'status': 'firing',
            'labels': {
                'alertname': 'HighCpuUsage',
                'host': 'server01',
                'name': 'HighCpuUsage',
                'severity': 'warning',
            },
            'annotations': {},
            'startsAt': '2026-04-24T12:00:00Z',
            'endsAt': '0001-01-01T00:00:00Z',
            'generatorURL': 'https://grafana.test/alerting/rule/abc',
        }
        # Shallow-merge labels/annotations so callers can override a
        # single key without having to re-specify the whole dict.
        for key in ('labels', 'annotations'):
            if key in overrides:
                base[key] = {**base[key], **overrides.pop(key)}
        base.update(overrides)
        return base
    return _build
