"""Unit tests for ``vls_grafana.parse_grafana``.

Exercises the translator in isolation — no Flask routes, no DB, just
dict-in / ``Alert``-out. Every assertion here encodes a behavior
documented in the module's docstring; if one of these flips, the
docstring probably needs updating too.
"""
import pytest

from vls_grafana import parse_grafana


pytestmark = pytest.mark.unit


class TestResourceEventSwap:
    """Pins the post-swap defaults: resource = rule name, event = host/name.

    Resource is also normalized to ``lower().replace(' ', '_')`` so
    it's stable across case-insensitive systems and URL-safe.
    """

    def test_defaults_put_rule_name_in_resource(self, app_context, payload):
        """Basic payload: resource is the alertname, event is host/name."""
        body = payload('grafana.firing_basic')
        alert = parse_grafana(body['alerts'][0], body['externalURL'])

        assert alert.resource == 'highcpuusage'
        assert alert.event == 'server01/HighCpuUsage'

    def test_resource_label_override_wins(self, app_context, payload):
        """A rule-level ``resource`` label takes precedence over alertname."""
        body = payload('grafana.firing_with_overrides')
        alert = parse_grafana(body['alerts'][0], body['externalURL'])

        assert alert.resource == 'db-primary-disk'

    def test_event_label_override_wins(self, app_context, payload):
        """A rule-level ``event`` label takes precedence over host/name."""
        body = payload('grafana.firing_with_overrides')
        alert = parse_grafana(body['alerts'][0], body['externalURL'])

        assert alert.event == 'disk_exhaustion'

    def test_resource_is_normalized(self, app_context, make_grafana_alert):
        """Spaces become underscores, everything lowercased."""
        alert = parse_grafana(
            make_grafana_alert(labels={'alertname': 'High CPU Usage'}),
            'https://grafana.test',
        )
        assert alert.resource == 'high_cpu_usage'

    def test_missing_host_falls_back_to_grafana(self, app_context, payload):
        """No ``host`` label: event prefix defaults to the literal 'grafana'."""
        body = payload('grafana.firing_no_host')
        alert = parse_grafana(body['alerts'][0], body['externalURL'])

        # No host, no name label: name falls through to alertname.
        assert alert.event == 'grafana/SyntheticCheckFailed'
        assert alert.resource == 'syntheticcheckfailed'

    def test_alertname_not_leaked_to_tags(self, app_context, make_grafana_alert):
        """``alertname`` is popped, not peeked, so it shouldn't land in tags."""
        alert = parse_grafana(make_grafana_alert(), 'https://grafana.test')

        tag_keys = {t.split('=', 1)[0] for t in alert.tags}
        assert 'alertname' not in tag_keys
        assert 'host' not in tag_keys
        assert 'name' not in tag_keys


class TestSeverityAndStatus:

    def test_firing_reads_severity_label(self, app_context, make_grafana_alert):
        alert = parse_grafana(
            make_grafana_alert(labels={'severity': 'critical'}),
            'https://grafana.test',
        )
        assert alert.severity == 'critical'

    def test_firing_without_severity_defaults_warning(self, app_context, make_grafana_alert):
        g = make_grafana_alert()
        g['labels'].pop('severity')
        alert = parse_grafana(g, 'https://grafana.test')
        assert alert.severity == 'warning'

    def test_resolved_uses_default_normal_severity(self, app_context, payload):
        body = payload('grafana.resolved')
        alert = parse_grafana(body['alerts'][0], body['externalURL'])
        # DEFAULT_NORMAL_SEVERITY is resolved via alarm_model at import
        # time, not our test config; just assert it's not the firing severity.
        assert alert.severity != 'warning'

    def test_resolved_text_is_overridden_to_ok(self, app_context, payload):
        """Resolved alerts get text="OK" regardless of incoming annotations."""
        body = payload('grafana.resolved')
        alert = parse_grafana(body['alerts'][0], body['externalURL'])
        assert alert.text == 'OK'

    def test_unknown_status_maps_to_unknown_severity(self, app_context, make_grafana_alert):
        alert = parse_grafana(
            make_grafana_alert(status='weird'),
            'https://grafana.test',
        )
        assert alert.severity == 'unknown'


class TestAttributeLifting:
    """ATTRIBUTE_LABELS allow-list should move out of tags into attributes."""

    def test_allowed_labels_lifted_into_attributes(self, app_context, payload):
        body = payload('grafana.firing_with_overrides')
        alert = parse_grafana(body['alerts'][0], body['externalURL'])

        assert alert.attributes.get('slack_channel') == '#db-alerts'
        assert alert.attributes.get('team') == 'dba'
        assert alert.attributes.get('runbook_url') == 'https://runbooks.test/disk-full'

    def test_lifted_labels_not_in_tags(self, app_context, payload):
        body = payload('grafana.firing_with_overrides')
        alert = parse_grafana(body['alerts'][0], body['externalURL'])

        tag_keys = {t.split('=', 1)[0] for t in alert.tags}
        assert 'slack_channel' not in tag_keys
        assert 'team' not in tag_keys
        assert 'runbook_url' not in tag_keys

    def test_non_allowlisted_labels_become_tags(self, app_context, payload):
        body = payload('grafana.firing_with_overrides')
        alert = parse_grafana(body['alerts'][0], body['externalURL'])

        assert 'custom_tag=fleet=core' in alert.tags


class TestAnnotationsAndUrls:

    def test_annotation_alertname_overrides_label(self, app_context, payload):
        """When annotations.alertname is set, it wins over the label."""
        body = payload('grafana.annotation_alertname_override')
        alert = parse_grafana(body['alerts'][0], body['externalURL'])

        # resource now reflects the annotation-provided alertname,
        # lowercased per the normalization step.
        assert alert.resource == 'p95latencydegraded'

    def test_external_url_lands_in_attributes(self, app_context, payload):
        body = payload('grafana.firing_basic')
        alert = parse_grafana(body['alerts'][0], body['externalURL'])

        assert alert.attributes.get('externalUrl') == 'https://grafana.test'

    def test_generator_url_renders_as_html_link(self, app_context, payload):
        body = payload('grafana.firing_basic')
        alert = parse_grafana(body['alerts'][0], body['externalURL'])

        more_info = alert.attributes.get('moreInfo', '')
        assert 'https://grafana.test/alerting/rule/abc' in more_info
        assert '<a href=' in more_info

    def test_missing_generator_url_omits_more_info(self, app_context, payload):
        body = payload('grafana.firing_no_host')
        alert = parse_grafana(body['alerts'][0], body['externalURL'])

        assert 'moreInfo' not in alert.attributes


class TestOriginAndEventType:

    def test_origin_tagged_grafana(self, app_context, make_grafana_alert):
        alert = parse_grafana(make_grafana_alert(), 'https://grafana.test')
        assert alert.origin == 'grafana'

    def test_event_type_tagged_grafana_alert(self, app_context, make_grafana_alert):
        alert = parse_grafana(make_grafana_alert(), 'https://grafana.test')
        assert alert.event_type == 'grafanaAlert'


class TestOptionalMetadata:

    def test_correlate_splits_comma_list(self, app_context, make_grafana_alert):
        alert = parse_grafana(
            make_grafana_alert(labels={'correlate': 'HighMem,DiskFull'}),
            'https://grafana.test',
        )
        # Alert.__init__ auto-appends event to correlate if not present.
        # We assert the parsed values are preserved, not exact equality.
        assert 'HighMem' in alert.correlate
        assert 'DiskFull' in alert.correlate

    def test_timeout_invalid_int_falls_through_to_alerta_default(
        self, app_context, make_grafana_alert,
    ):
        """parse_grafana passes None on ValueError; Alert applies ALERT_TIMEOUT."""
        alert = parse_grafana(
            make_grafana_alert(labels={'timeout': 'not-a-number'}),
            'https://grafana.test',
        )
        # ALERT_TIMEOUT default from alerta.settings (86400s / 24h).
        assert alert.timeout == 86400

    def test_timeout_zero_falls_through_to_alerta_default(
        self, app_context, make_grafana_alert,
    ):
        alert = parse_grafana(
            make_grafana_alert(labels={'timeout': '0'}),
            'https://grafana.test',
        )
        assert alert.timeout == 86400

    def test_timeout_positive_int_preserved(self, app_context, make_grafana_alert):
        alert = parse_grafana(
            make_grafana_alert(labels={'timeout': '300'}),
            'https://grafana.test',
        )
        assert alert.timeout == 300

    def test_group_defaults_to_grafana(self, app_context, make_grafana_alert):
        alert = parse_grafana(make_grafana_alert(), 'https://grafana.test')
        assert alert.group == 'Grafana'

    def test_group_falls_back_to_job(self, app_context, make_grafana_alert):
        alert = parse_grafana(
            make_grafana_alert(labels={'job': 'node-exporter'}),
            'https://grafana.test',
        )
        assert alert.group == 'node-exporter'

    def test_service_missing_yields_empty_list(self, app_context, make_grafana_alert):
        """No ``service`` label should produce ``[]``, not ``['']``.

        The old ``''.split(',')`` returned a one-element list containing an
        empty string, which renders as ``['']`` in Slack template fallbacks.
        """
        alert = parse_grafana(make_grafana_alert(), 'https://grafana.test')
        assert alert.service == []

    def test_service_single_value(self, app_context, make_grafana_alert):
        """A single ``service`` label value is returned as a one-element list."""
        alert = parse_grafana(
            make_grafana_alert(labels={'service': 'payments'}),
            'https://grafana.test',
        )
        assert alert.service == ['payments']

    def test_service_csv_splits_into_list(self, app_context, make_grafana_alert):
        """Comma-separated ``service`` labels are split into individual entries."""
        alert = parse_grafana(
            make_grafana_alert(labels={'service': 'payments,auth'}),
            'https://grafana.test',
        )
        assert alert.service == ['payments', 'auth']

    def test_service_trailing_comma_filtered(self, app_context, make_grafana_alert):
        """A trailing comma in the ``service`` label does not produce an empty entry."""
        alert = parse_grafana(
            make_grafana_alert(labels={'service': 'payments,'}),
            'https://grafana.test',
        )
        assert alert.service == ['payments']
