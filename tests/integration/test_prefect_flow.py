"""Tier 3: Prefect webhook end-to-end.

Doubles as the integration test for the override plugin — the compose
override sets ``OVERRIDE_prefect_severity=major`` so any alert from
this webhook (which tags ``service=prefect``) gets its severity
rewritten. Tests assert the resulting alert reflects the override.
"""
import pytest

from tests.integration.conftest import ALERTA_URL


pytestmark = pytest.mark.integration


def _get_alerts(alerta_client, **params):
    r = alerta_client.get(f'{ALERTA_URL}/api/alerts', params=params, timeout=10)
    r.raise_for_status()
    return r.json()['alerts']


def test_prefect_failed_flow_ingested(post_webhook, alerta_client, payload):
    r = post_webhook('prefectflows', payload('prefect.failed'))
    assert r.status_code in (200, 201), r.text

    alerts = _get_alerts(alerta_client, resource='etl-users')
    assert len(alerts) == 1
    a = alerts[0]
    assert a['event'] == 'flow_run_failed'
    # Raw mapping says 'critical', override says 'major' — override wins
    # because it runs in pre_receive.
    assert a['severity'] == 'major'


def test_prefect_completed_flow_also_overridden(post_webhook, alerta_client, payload):
    """Every alert with service=prefect is overridden, not just failures."""
    post_webhook('prefectflows', payload('prefect.completed'))

    alerts = _get_alerts(alerta_client, resource='reddit-new-submissions')
    assert len(alerts) == 1
    assert alerts[0]['severity'] == 'major'
