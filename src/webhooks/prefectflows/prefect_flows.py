from urllib import parse
from alerta.models.alert import Alert
from alerta.webhooks import WebhookBase
#deployment={{deployment.name}}&flow={{ flow.name }}flow_run={{ flow_run.name }}&state={{ flow_run.state.name }}&text={{flow_run.state.message}}&flow_run_url={{ flow_run|ui_url }}
# "deployment=hardwareswap&flow=reddit-new-submissionsflow_run=amaranth-hoatzin&state=Completed&text=All states completed.&flow_run_url=https://app.prefect.cloud


def _csv_list(value):
    """Parse a comma-separated string into a list, returning None for empty/missing.

    Alerta's ``service`` and ``tags`` columns are ``TEXT[]`` — passing a
    bare string trips the blackout plugin's ``<@`` array operator.

    Args:
        value: The raw value from the parsed querystring, or ``None``.

    Returns:
        ``None`` if the value is empty or absent, otherwise a list of
        non-empty trimmed strings.
    """
    if not value:
        return None
    parts = [p for p in value.split(',') if p]
    return parts or None


STATE_MAPPING = {
    'scheduled': 'informational',
    'pending': 'informational',
    'completed': 'ok',
    'failed': 'critical',
    'cancelled': 'warning',
    'crashed': 'critical',
    'paused': 'warning',
    'cancelling': 'major'
}


class PrefectFlowWebhook(WebhookBase):

    def incoming(self, query_string, payload, **kwargs):
        data = dict(parse.parse_qsl(payload['message']))

        # Default parameters
        environment = data.get('environment', 'Production')
        severity = STATE_MAPPING.get(data.get('state', 'Unknown'), 'major')

        return Alert(
            resource=data.get('resource', None),
            event=data.get('event', None),
            environment=environment,
            severity=severity,
            service=_csv_list(data.get('service')),
            group=data.get('group', None),
            value=data.get('value', None),
            text=data.get('text', None),
            tags=_csv_list(data.get('tags')),
            # Alert.__init__ rejects attributes=None (it pre-checks
            # .keys() before its own or-dict fallback). Default to
            # empty dict so the webhook accepts minimal payloads.
            attributes=data.get('attributes') or {}
        )

