"""Unit tests for ``prefect_flows.PrefectFlowWebhook``.

The webhook expects a Prefect automation action posting a form-encoded
``message`` field; ``incoming`` parses that URL-encoded string into
alert fields. Tests drive it with the same JSON shape alerta actually
delivers to ``WebhookBase.incoming``.
"""
import pytest

from prefect_flows import PrefectFlowWebhook, STATE_MAPPING


pytestmark = pytest.mark.unit


@pytest.fixture
def webhook(app_context):
    """Prefect webhook instance inside an app context.

    ``Alert.__init__`` reads ``ALERT_TIMEOUT`` from ``current_app.config``,
    so every test that calls ``incoming`` needs the context active.
    """
    return PrefectFlowWebhook()


class TestIncomingBasics:

    def test_completed_flow_maps_to_ok(self, webhook, payload):
        alert = webhook.incoming(query_string={}, payload=payload('prefect.completed'))

        assert alert.severity == 'ok'
        assert alert.resource == 'reddit-new-submissions'
        assert alert.event == 'flow_run_completed'
        assert alert.environment == 'Production'

    def test_failed_flow_maps_to_critical(self, webhook, payload):
        alert = webhook.incoming(query_string={}, payload=payload('prefect.failed'))

        assert alert.severity == 'critical'
        assert alert.resource == 'etl-users'
        assert alert.event == 'flow_run_failed'

    def test_unknown_state_defaults_to_major(self, webhook):
        body = {'message': 'state=quantum&resource=r1&event=e1'}
        alert = webhook.incoming(query_string={}, payload=body)

        assert alert.severity == 'major'


class TestStateMapping:
    """Each documented state maps to a specific severity."""

    @pytest.mark.parametrize('state,expected', list(STATE_MAPPING.items()))
    def test_state_mapping(self, webhook, state, expected):
        body = {'message': f'state={state}&resource=r&event=e'}
        alert = webhook.incoming(query_string={}, payload=body)
        assert alert.severity == expected


class TestDefaults:

    def test_missing_environment_defaults_to_production(self, webhook):
        body = {'message': 'state=failed&resource=r&event=e'}
        alert = webhook.incoming(query_string={}, payload=body)
        assert alert.environment == 'Production'

    def test_message_text_passes_through(self, webhook, payload):
        alert = webhook.incoming(query_string={}, payload=payload('prefect.failed'))
        assert 'Task X raised ValueError' in alert.text
