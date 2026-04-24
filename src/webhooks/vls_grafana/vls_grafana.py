
"""Custom Alerta webhook for Grafana unified alerting.

Grafana's unified alerting webhook sends an Alertmanager-compatible
payload, so this webhook is structurally a fork of alerta-server's
built-in ``prometheus`` translator. It differs in three ways that
matter for the veloslab deployment:

1. ``event`` is built from the ``host`` and ``name`` labels (the
   telegraf metrics convention used everywhere else in this stack),
   making each firing distinct per host. ``resource`` defaults to
   the rule name (``alertname``), so the Alerta UI's primary column
   reads as the "what" rather than a composite string. The built-in
   translator puts ``alertname`` into ``event`` and ``instance`` into
   ``resource``; we invert both defaults to match this deployment's
   conventions. Rule-label overrides named ``resource`` and ``event``
   take precedence over the defaults.
2. A short allow-list of labels (see ``ATTRIBUTE_LABELS``) is lifted
   from labels into ``attributes`` before the leftover-labels-become-
   tags step. This lets us define defaults for those keys in
   Grafana's ``[unified_alerting.external_labels]`` (merged into every
   outgoing alert's labels) and have per-rule overrides via rule
   labels of the same name. slackthread reads ``attributes``, not
   labels, so we need the lift.
3. ``origin`` and ``event_type`` are tagged as ``grafana`` so these
   alerts are distinguishable from anything that might come via the
   built-in ``/api/webhooks/prometheus`` route.

Everything else — severity resolution, ``alertname`` override through
annotations, leftover-annotations-become-attributes, ``generatorURL``
footer, python-format templating on labels and annotations — matches
the built-in translator verbatim. When alerta-server changes its
prometheus translator, re-diff this file and pull in non-conflicting
improvements.
"""

from typing import Any, Dict

from flask import current_app

from alerta.app import alarm_model
from alerta.exceptions import ApiError
from alerta.models.alert import Alert
from alerta.webhooks import WebhookBase


JSON = Dict[str, Any]


# Labels in this tuple are lifted into attributes rather than ending
# up as k=v tags. Keep it tight — each entry ties to a downstream
# plugin that reads from attributes. slackthread reads slack_channel;
# team and runbook_url are reserved for future routing / UI use.
ATTRIBUTE_LABELS = ('slack_channel', 'team', 'runbook_url')


def parse_grafana(alert: JSON, external_url: str) -> Alert:
    """Map one Grafana (AM-format) alert to an Alerta ``Alert``.

    Args:
        alert: One element of ``payload['alerts']`` from Grafana's
            webhook body. Contains ``status``, ``labels``,
            ``annotations``, ``startsAt``, ``endsAt``, and optionally
            ``generatorURL``.
        external_url: The top-level ``externalURL`` from the payload
            (Grafana's own UI URL). Passed through as an
            ``externalUrl`` attribute for Alerta's bi-directional UI
            linking.

    Returns:
        A fully-populated ``Alert`` ready to hand to Alerta.
    """
    status = alert.get('status', 'firing')

    # Python-format templating: labels can reference other labels,
    # annotations can reference labels. Matches the prometheus
    # translator behavior (see prometheus/prometheus#2818).
    labels = {}
    for k, v in alert['labels'].items():
        try:
            labels[k] = v.format(**alert['labels'])
        except Exception:
            labels[k] = v

    annotations = {}
    for k, v in alert['annotations'].items():
        try:
            annotations[k] = v.format(**labels)
        except Exception:
            annotations[k] = v

    # Annotation alertname overrides the label-driven one.
    if 'alertname' in annotations and annotations['alertname']:
        labels['alertname'] = annotations['alertname']

    if status == 'firing':
        severity = labels.pop('severity', 'warning')
    elif status == 'resolved':
        severity = alarm_model.DEFAULT_NORMAL_SEVERITY
    else:
        severity = 'unknown'

    # Delta #1: event is host/name (distinct per firing instance),
    # resource defaults to the rule name. Peek at alertname for the
    # composite fallback — `resource` pops it below as its own fallback.
    host = labels.pop('host', 'grafana')
    name = labels.pop('name', None) or labels.get('alertname', 'unknown')
    event = labels.pop('event', None) or f'{host}/{name}'

    resource = labels.pop('resource', None) or labels.pop('alertname')
    resource = resource.lower().replace(' ', '_')
    environment = labels.pop('environment', current_app.config['DEFAULT_ENVIRONMENT'])
    customer = labels.pop('customer', None)
    correlate = labels.pop('correlate').split(',') if 'correlate' in labels else None
    service = labels.pop('service', '').split(',')
    group = labels.pop('group', None) or labels.pop('job', 'Grafana')

    try:
        timeout = int(labels.pop('timeout', 0)) or None
    except ValueError:
        timeout = None

    # Delta #2: lift attribute-destined labels before the remainder
    # becomes tags. Missing keys are simply absent from attributes —
    # Grafana external_labels ensures they're usually present in real
    # traffic, and downstream plugins (slackthread) handle absence via
    # their own env-var fallbacks.
    attribute_labels = {}
    for key in ATTRIBUTE_LABELS:
        if key in labels:
            attribute_labels[key] = labels.pop(key)

    tags = [f'{k}={v}' for k, v in labels.items()]

    value = annotations.pop('value', None)
    summary = annotations.pop('summary', None)
    description = annotations.pop('description', None)
    text = description or summary or f'{severity.upper()}: {resource} is {event}'

    if external_url:
        annotations['externalUrl'] = external_url

    if 'generatorURL' in alert:
        annotations['moreInfo'] = (
            f"<a href=\"{alert['generatorURL']}\" target=\"_blank\">Grafana</a>"
        )

    attributes = {
        'startsAt': alert['startsAt'],
        'endsAt': alert['endsAt'],
    }
    attributes.update(annotations)
    # Attribute-lifted labels applied last: a same-named leftover
    # annotation (unlikely) loses to the label, matching "rule label
    # wins" precedence when both are set.
    attributes.update(attribute_labels)

    return Alert(
        resource=resource,
        event=event,
        environment=environment,
        customer=customer,
        severity=severity,
        correlate=correlate,
        service=service,
        group=group,
        value=value,
        text=text,
        attributes=attributes,
        origin='grafana',
        event_type='grafanaAlert',
        timeout=timeout,
        raw_data=alert,
        tags=tags,
    )


class VlsGrafanaWebhook(WebhookBase):
    """Alerta webhook receiver for Grafana unified alerting.

    Registered via the ``alerta.webhooks`` entry point as
    ``vls-grafana`` → exposed at ``/api/webhooks/vls-grafana``.
    """

    def incoming(self, path, query_string, payload):
        """Parse a batch of Grafana alerts into ``Alert`` objects.

        Args:
            path: Trailing path (unused; present for API
                compatibility with ``WebhookBase``).
            query_string: Request query-string dict (unused).
            payload: Decoded JSON body sent by Grafana.

        Returns:
            A list of ``Alert`` objects, one per element of
            ``payload['alerts']``.

        Raises:
            ApiError: If the payload has no ``alerts`` key.
        """
        if payload and 'alerts' in payload:
            external_url = payload.get('externalURL')
            return [parse_grafana(alert, external_url) for alert in payload['alerts']]
        raise ApiError('no alerts in Grafana notification payload', 400)
