"""Tier 3: end-to-end Grafana → alerta → Slack.

These tests drive the real container stack:

1. POST a Grafana webhook payload to alerta.
2. Read the resulting alert back via alerta's REST API.
3. Read the mock Slack's captured requests.

They're deliberately chatty — the point is to catch plugin-ordering,
template-rendering, and HTTP-wiring issues that unit tests can't see.
"""
import pytest

from tests.integration.conftest import ALERTA_URL


pytestmark = pytest.mark.integration


def _get_alerts(alerta_client, **params):
    """Fetch alerts matching query params, returning the JSON list."""
    r = alerta_client.get(f'{ALERTA_URL}/api/alerts', params=params, timeout=10)
    r.raise_for_status()
    return r.json()['alerts']


class TestGrafanaToAlerta:
    """The webhook side — payload lands, fields are populated correctly."""

    def test_firing_alert_ingested(self, post_webhook, alerta_client, payload):
        r = post_webhook('vls-grafana', payload('grafana.firing_basic'))
        assert r.status_code in (200, 201), r.text

        alerts = _get_alerts(alerta_client, event='server01/HighCpuUsage')
        assert len(alerts) == 1
        a = alerts[0]
        # Resource is normalized (.lower().replace(' ', '_')) in the
        # vls_grafana webhook before handing to Alerta.
        assert a['resource'] == 'highcpuusage'
        assert a['event'] == 'server01/HighCpuUsage'
        assert a['severity'] == 'warning'
        assert a['origin'] == 'grafana'

    def test_overrides_applied(self, post_webhook, alerta_client, payload):
        post_webhook('vls-grafana', payload('grafana.firing_with_overrides'))

        alerts = _get_alerts(alerta_client, resource='db-primary-disk')
        assert len(alerts) == 1
        a = alerts[0]
        assert a['event'] == 'disk_exhaustion'
        assert a['severity'] == 'critical'
        # slack_channel lifted into attributes, not tags.
        assert a['attributes'].get('slack_channel') == '#db-alerts'

    def test_dedup_on_repeat_fire(self, post_webhook, alerta_client, payload):
        body = payload('grafana.firing_basic')
        post_webhook('vls-grafana', body)
        post_webhook('vls-grafana', body)

        alerts = _get_alerts(alerta_client, event='server01/HighCpuUsage')
        assert len(alerts) == 1
        assert alerts[0]['duplicateCount'] >= 1


class TestGrafanaToSlack:
    """slackthread plugin posts through the mock — assert the traffic."""

    def test_initial_fire_posts_new_message(
        self, post_webhook, slack_mock, payload,
    ):
        """Initial fire posts the parent AND a history reply in the thread."""
        post_webhook('vls-grafana', payload('grafana.firing_basic'))

        calls = slack_mock.captured('chat.postMessage')
        # Parent (no thread_ts) + history reply (thread_ts = parent's ts).
        assert len(calls) == 2

        parent, reply = calls
        # Default channel (no slack_channel attribute set).
        assert parent['body'].get('channel') == 'C_ALERTS_DEFAULT'
        # Attachment payload is a JSON string in form-encoded submit.
        assert 'HighCpuUsage' in parent['body'].get('attachments', '')
        # History reply threads under the parent.
        assert 'thread_ts' not in parent['body']
        assert reply['body'].get('thread_ts') is not None

    def test_duplicate_triggers_thread_reply_and_parent_update(
        self, post_webhook, slack_mock, payload,
    ):
        body = payload('grafana.firing_basic')
        post_webhook('vls-grafana', body)  # initial (parent + history reply)
        post_webhook('vls-grafana', body)  # dup (reply + parent update)

        posts = slack_mock.captured('chat.postMessage')
        updates = slack_mock.captured('chat.update')

        # 3 postMessage calls: initial parent + history reply + duplicate reply.
        assert len(posts) == 3
        # Initial parent has no thread_ts; the other two do.
        assert 'thread_ts' not in posts[0]['body']
        assert posts[1]['body'].get('thread_ts') is not None  # history reply
        assert posts[2]['body'].get('thread_ts') is not None  # duplicate reply
        # One parent-update call from the duplicate path.
        assert len(updates) == 1

    def test_slack_channel_label_routes_alert(
        self, post_webhook, slack_mock, payload,
    ):
        """``slack_channel`` attribute resolves via conversations.list."""
        post_webhook('vls-grafana', payload('grafana.firing_with_overrides'))

        posts = slack_mock.captured('chat.postMessage')
        assert len(posts) == 1
        # #db-alerts → C_ALERTS_DB per the mock's default channel roster.
        assert posts[0]['body'].get('channel') == 'C_ALERTS_DB'

    def test_slack_failure_does_not_break_ingest(
        self, post_webhook, alerta_client, slack_mock, payload,
    ):
        """Even when Slack returns an error, the alert still lands in alerta."""
        slack_mock.queue('chat.postMessage', {'ok': False, 'error': 'channel_not_found'})

        r = post_webhook('vls-grafana', payload('grafana.firing_basic'))
        assert r.status_code in (200, 201)

        alerts = _get_alerts(alerta_client, event='server01/HighCpuUsage')
        assert len(alerts) == 1
