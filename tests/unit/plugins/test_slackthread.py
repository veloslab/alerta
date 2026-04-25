"""Unit tests for ``slackthread.SlackThreadPlugin``.

The plugin's real side effect is HTTP via ``slack_sdk.WebClient``. We
replace the client with a ``MagicMock`` configured to return
Slack-shaped ``{"ok": true, "ts": "...", "channel": "..."}`` responses
so ``post_receive`` can walk its full logic path without network.

Tests cover:
* Template rendering (``format_template`` pure function).
* ``generate_new_thread`` branch matrix (new / channel switch / timeout / reuse).
* ``get_channel_id`` with cache hits, cache misses, and unknown channels.
* ``post_receive`` gating logic: ok/normal dedup, notification modulus.
* ``post_receive`` happy path: initial post vs. thread reply + parent update.
"""
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from slack_sdk.errors import SlackApiError

from slackthread import (
    DEFAULT_COLOR_MAP,
    DEFAULT_SLACK_TEMPLATE,
    DEFAULT_SLACK_TEMPLATE_OK,
    SlackThreadPlugin,
    format_template,
)


pytestmark = pytest.mark.unit


def _fake_alert(**overrides):
    """Build a minimal object exposing the attributes the plugin reads.

    The real ``Alert`` has dozens of fields and mongo/postgres plumbing
    we don't need here. A ``SimpleNamespace`` lets tests pin exactly
    which attributes matter without booting alerta-server.
    """
    defaults = dict(
        id='abc-123',
        resource='HighCpuUsage',
        event='server01/HighCpuUsage',
        severity='warning',
        environment='Production',
        service=['infra'],
        text='CPU above 90%',
        duplicate_count=0,
        attributes={},
    )
    defaults.update(overrides)
    alert = SimpleNamespace(**defaults)
    # ``Alert.update_attributes`` is called by post_receive; stub with a
    # mutator that mirrors the real method's observable behavior.
    alert.update_attributes = lambda new: alert.attributes.update(new)
    return alert


@pytest.fixture
def slack_env(monkeypatch):
    """Set SLACK_* env vars that ``PluginBase.get_config`` reads.

    ``PluginBase.get_config`` is a staticmethod that only consults
    ``os.environ`` (not ``current_app.config``). The unit conftest's
    autouse ``_isolate_env`` fixture strips any pre-existing SLACK_
    vars from the developer's shell; this fixture puts the test
    values back.
    """
    monkeypatch.setenv('SLACK_TOKEN', 'xoxb-test-token')
    monkeypatch.setenv('SLACK_DEFAULT_CHANNEL_ID', 'C_TEST_DEFAULT')
    monkeypatch.setenv('SLACK_DEFAULT_THREAD_TIMEOUT', '24')
    monkeypatch.setenv('SLACK_BASE_URL', 'http://mock-slack.test/api/')
    monkeypatch.setenv('DASHBOARD_URL', 'https://alerta.test')


@pytest.fixture
def plugin(app_context, slack_env, mocker):
    """A ``SlackThreadPlugin`` with its WebClient mocked.

    Uses ``mocker.patch`` on the ``WebClient`` symbol as imported into
    the ``slackthread`` module — patching at import site, not at
    definition site, is what makes this stick.
    """
    mocker.patch('slackthread.WebClient', autospec=True)
    p = SlackThreadPlugin()
    # Replace the instantiated client with a plain MagicMock so tests
    # can configure per-call return values without autospec friction.
    p.client = MagicMock()
    return p


class TestFormatTemplate:

    def test_renders_alert_fields(self, app_context):
        alert = _fake_alert(resource='R', event='E', text='T')
        out = format_template('{{ alert.resource }}/{{ alert.event }} :: {{ alert.text }}', alert)
        assert out == 'R/E :: T'

    def test_extra_context_available(self, app_context):
        alert = _fake_alert()
        out = format_template('{{ dashboard_url }}/alert/{{ alert.id }}',
                              alert, dashboard_url='https://d')
        assert out == 'https://d/alert/abc-123'

    def test_bad_template_returns_none(self, app_context):
        # Unclosed tag triggers a parse error, which format_template swallows.
        out = format_template('{% if alert.x', _fake_alert())
        assert out is None

    def test_render_error_returns_none(self, app_context):
        # Accessing an undefined attribute raises at render time.
        out = format_template('{{ alert.does_not_exist.boom }}', _fake_alert())
        assert out is None


class TestGenerateNewThread:
    """Four branches: no ts, channel mismatch, timeout reached, reuse."""

    def test_no_existing_ts_creates_new(self, plugin):
        alert = _fake_alert(attributes={})
        assert plugin.generate_new_thread(alert, 'C123') is True

    def test_channel_mismatch_creates_new(self, plugin):
        alert = _fake_alert(attributes={
            'slack_ts': str(time.time()),
            'slack_channel_id': 'Cold',
        })
        assert plugin.generate_new_thread(alert, 'Cnew') is True

    def test_timeout_exceeded_creates_new(self, plugin):
        # 25 hours ago, default timeout is 24 hours.
        alert = _fake_alert(attributes={
            'slack_ts': str(time.time() - 25 * 3600),
            'slack_channel_id': 'C123',
        })
        assert plugin.generate_new_thread(alert, 'C123') is True

    def test_fresh_thread_is_reused(self, plugin):
        alert = _fake_alert(attributes={
            'slack_ts': str(time.time() - 60),  # 1 minute old
            'slack_channel_id': 'C123',
        })
        assert plugin.generate_new_thread(alert, 'C123') is False

    def test_per_alert_timeout_override(self, plugin):
        # Override is 1 hour; thread is 2 hours old → new thread.
        alert = _fake_alert(attributes={
            'slack_ts': str(time.time() - 2 * 3600),
            'slack_channel_id': 'C123',
            'slack_thread_timeout': '1',
        })
        assert plugin.generate_new_thread(alert, 'C123') is True


class TestGetChannelId:

    def test_default_channel_when_unspecified(self, plugin):
        alert = _fake_alert(attributes={})
        assert plugin.get_channel_id(alert) == 'C_TEST_DEFAULT'

    def test_named_channel_looked_up_via_api(self, plugin):
        plugin.client.conversations_list.return_value = {
            'ok': True,
            'channels': [{'name': 'alerts-db', 'id': 'C_DB'},
                         {'name': 'alerts-infra', 'id': 'C_INFRA'}],
        }
        alert = _fake_alert(attributes={'slack_channel': '#alerts-db'})
        assert plugin.get_channel_id(alert) == 'C_DB'
        assert plugin.channels['alerts-db'] == 'C_DB'

    def test_named_channel_cache_prevents_second_call(self, plugin):
        plugin.channels['alerts-db'] = 'C_DB'
        alert = _fake_alert(attributes={'slack_channel': '#alerts-db'})
        assert plugin.get_channel_id(alert) == 'C_DB'
        plugin.client.conversations_list.assert_not_called()

    def test_unknown_channel_falls_back_to_default(self, plugin):
        plugin.client.conversations_list.return_value = {
            'ok': True, 'channels': [],
        }
        alert = _fake_alert(attributes={'slack_channel': '#nonexistent'})
        assert plugin.get_channel_id(alert) == 'C_TEST_DEFAULT'


class TestPostReceiveGating:
    """Early-return paths that suppress Slack traffic."""

    def test_ok_duplicate_is_suppressed(self, plugin):
        alert = _fake_alert(severity='ok', duplicate_count=1)
        assert plugin.post_receive(alert) is None
        plugin.client.chat_postMessage.assert_not_called()

    def test_normal_duplicate_is_suppressed(self, plugin):
        alert = _fake_alert(severity='Normal', duplicate_count=3)
        assert plugin.post_receive(alert) is None
        plugin.client.chat_postMessage.assert_not_called()

    def test_ok_first_fire_still_posts(self, plugin):
        """First-time OK alert: parent + history reply, no suppression."""
        plugin.client.chat_postMessage.side_effect = [
            {'ok': True, 'ts': '1.0', 'channel': 'C_TEST_DEFAULT'},
            {'ok': True, 'ts': '1.1', 'channel': 'C_TEST_DEFAULT'},
        ]
        alert = _fake_alert(severity='ok', duplicate_count=0)
        plugin.post_receive(alert)
        # Parent + history reply = 2 calls (see TestPostReceiveHappyPath).
        assert plugin.client.chat_postMessage.call_count == 2

    def test_notification_modulus_skips_non_matching(self, plugin):
        alert = _fake_alert(
            duplicate_count=1,
            attributes={'slack_notification_modulus': '5'},
        )
        assert plugin.post_receive(alert) is None
        plugin.client.chat_postMessage.assert_not_called()

    def test_notification_modulus_allows_matching(self, plugin):
        plugin.client.chat_postMessage.return_value = {
            'ok': True, 'ts': '1.0', 'channel': 'C_TEST_DEFAULT',
        }
        alert = _fake_alert(
            duplicate_count=5,  # 5 % 5 == 0
            attributes={'slack_notification_modulus': '5'},
        )
        plugin.post_receive(alert)
        plugin.client.chat_postMessage.assert_called()


class TestPostReceiveHappyPath:

    def test_initial_post_records_ts_and_channel(self, plugin):
        # Parent post first, then a history reply — both return ok.
        # chat_postMessage is called twice; side_effect feeds both.
        plugin.client.chat_postMessage.side_effect = [
            {'ok': True, 'ts': '1700000000.000100', 'channel': 'C_TEST_DEFAULT'},
            {'ok': True, 'ts': '1700000000.000200', 'channel': 'C_TEST_DEFAULT'},
        ]
        alert = _fake_alert()
        plugin.post_receive(alert)

        # Parent ts is the one recorded; the reply ts is not used for
        # threading on subsequent fires.
        assert alert.attributes['slack_ts'] == '1700000000.000100'
        assert alert.attributes['slack_channel_id'] == 'C_TEST_DEFAULT'

    def test_initial_post_reposts_as_thread_reply(self, plugin):
        """Parent post is immediately followed by a thread reply with the same payload."""
        plugin.client.chat_postMessage.side_effect = [
            {'ok': True, 'ts': '1700000000.000100', 'channel': 'C_TEST_DEFAULT'},
            {'ok': True, 'ts': '1700000000.000200', 'channel': 'C_TEST_DEFAULT'},
        ]
        alert = _fake_alert()
        plugin.post_receive(alert)

        assert plugin.client.chat_postMessage.call_count == 2

        parent_call, reply_call = plugin.client.chat_postMessage.call_args_list
        # Parent has no thread_ts.
        assert 'thread_ts' not in parent_call.kwargs
        # Reply threads under the parent's ts with the same attachments.
        assert reply_call.kwargs['thread_ts'] == '1700000000.000100'
        assert reply_call.kwargs['attachments'] == parent_call.kwargs['attachments']

    def test_history_reply_failure_does_not_abort(self, plugin):
        """Failed history reply is logged but state is still recorded.

        ``slack_sdk.WebClient`` raises ``SlackApiError`` on any non-ok
        response, so the second call must raise rather than return an
        error dict. The plugin catches it and continues.
        """
        plugin.client.chat_postMessage.side_effect = [
            {'ok': True, 'ts': '1700000000.000100', 'channel': 'C_TEST_DEFAULT'},
            SlackApiError('rate_limited', {'ok': False, 'error': 'rate_limited'}),
        ]
        alert = _fake_alert()
        plugin.post_receive(alert)  # must not raise

        # Parent thread ts still recorded despite reply failure.
        assert alert.attributes['slack_ts'] == '1700000000.000100'

    def test_duplicate_fires_thread_reply_and_parent_update(self, plugin):
        plugin.client.chat_postMessage.return_value = {
            'ok': True, 'ts': '1.1', 'channel': 'C_TEST_DEFAULT',
        }
        plugin.client.chat_update.return_value = {'ok': True, 'ts': '1.0'}

        alert = _fake_alert(
            duplicate_count=1,
            attributes={
                'slack_ts': str(time.time() - 60),  # fresh thread
                'slack_channel_id': 'C_TEST_DEFAULT',
            },
        )
        plugin.post_receive(alert)

        # Duplicate path skips thread creation entirely: one reply, one update.
        assert plugin.client.chat_postMessage.call_count == 1
        reply_call = plugin.client.chat_postMessage.call_args
        assert reply_call.kwargs.get('thread_ts') is not None
        plugin.client.chat_update.assert_called_once()

    def test_severity_color_applied_to_attachment(self, plugin):
        plugin.client.chat_postMessage.side_effect = [
            {'ok': True, 'ts': '1.0', 'channel': 'C_TEST_DEFAULT'},
            {'ok': True, 'ts': '1.1', 'channel': 'C_TEST_DEFAULT'},
        ]
        alert = _fake_alert(severity='critical')
        plugin.post_receive(alert)

        # Parent is call_args_list[0]; reply is [1] with the same attachments.
        attachments = plugin.client.chat_postMessage.call_args_list[0].kwargs['attachments']
        assert attachments[0]['color'] == DEFAULT_COLOR_MAP['critical']

    def test_failed_initial_post_short_circuits(self, plugin):
        """A ``SlackApiError`` on the parent post causes an early return.

        ``slack_sdk.WebClient`` raises on non-ok responses, so the mock
        must raise rather than return an error dict.
        """
        plugin.client.chat_postMessage.side_effect = SlackApiError(
            'channel_not_found', {'ok': False, 'error': 'channel_not_found'}
        )
        alert = _fake_alert()

        result = plugin.post_receive(alert)

        assert result is None
        # No thread state was recorded on failure.
        assert 'slack_ts' not in alert.attributes
        # Exactly one attempt (the failing parent post) — no reply, no update.
        assert plugin.client.chat_postMessage.call_count == 1
        plugin.client.chat_update.assert_not_called()


class TestSelectTemplate:
    """Template precedence: per-alert > env-default > OK-fallback > base default."""

    def test_per_alert_override_wins(self, plugin):
        """slack_template attribute wins even when text is OK and env is set."""
        plugin._default_template_configured = True
        plugin.default_template = 'env-template'
        alert = _fake_alert(
            text='OK',
            attributes={'slack_template': 'per-alert-template'},
        )
        assert plugin._select_template(alert) == 'per-alert-template'

    def test_env_default_wins_over_ok_fallback(self, plugin):
        """SLACK_DEFAULT_TEMPLATE operator override suppresses the OK auto-swap."""
        plugin._default_template_configured = True
        plugin.default_template = 'env-template'
        alert = _fake_alert(text='OK', attributes={})

        assert plugin._select_template(alert) == 'env-template'

    @pytest.mark.parametrize('ok_text', ['OK', 'ok', 'Ok', 'oK'])
    def test_ok_fallback_on_resolved_text(self, plugin, ok_text):
        """Any case of 'ok' triggers the OK template when nothing else overrides."""
        plugin._default_template_configured = False
        alert = _fake_alert(text=ok_text, attributes={})

        assert plugin._select_template(alert) == DEFAULT_SLACK_TEMPLATE_OK

    def test_base_default_for_non_ok_text(self, plugin):
        """Firing text → firing default template."""
        plugin._default_template_configured = False
        alert = _fake_alert(text='CPU above 90%', attributes={})

        assert plugin._select_template(alert) == DEFAULT_SLACK_TEMPLATE

    def test_empty_text_does_not_trigger_ok(self, plugin):
        """Empty / None text shouldn't match the OK branch."""
        plugin._default_template_configured = False
        for text in ('', None):
            alert = _fake_alert(text=text, attributes={})
            assert plugin._select_template(alert) == DEFAULT_SLACK_TEMPLATE

    def test_text_containing_ok_is_not_ok(self, plugin):
        """Exact-match only — 'OK for now' should not trigger OK template."""
        plugin._default_template_configured = False
        alert = _fake_alert(text='OK for now', attributes={})

        assert plugin._select_template(alert) == DEFAULT_SLACK_TEMPLATE


class TestDefaultTemplate:

    def test_default_template_constant_reads_expected_fields(self, app_context):
        """Sanity check: the default template renders the swapped fields."""
        alert = _fake_alert(resource='HighCpuUsage', event='server01/HighCpuUsage',
                            text='90%', id='a1')
        out = format_template(DEFAULT_SLACK_TEMPLATE, alert, dashboard_url='https://d')

        assert 'HighCpuUsage/server01/HighCpuUsage' in out
        assert 'https://d/alert/a1' in out

    def test_escaped_newlines_unescaped_on_load(self, app_context, slack_env, monkeypatch, mocker):
        """Operators write \\n in .env; plugin converts to real newlines."""
        monkeypatch.setenv('SLACK_DEFAULT_TEMPLATE', 'line1\\nline2')
        mocker.patch('slackthread.WebClient', autospec=True)

        p = SlackThreadPlugin()
        assert p.default_template == 'line1\nline2'
