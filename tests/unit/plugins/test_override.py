"""Unit tests for ``override.OverridePlugin``.

The plugin inspects ``OVERRIDE_<service>_<field>`` env vars (and the
same keys in ``app.config``) at init time and rewrites matching alerts
in ``pre_receive``. Tests cover key parsing, field vs. attribute
targets, and the "no matching service, no mutation" path.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest


pytestmark = pytest.mark.unit


def _fake_alert(**overrides):
    defaults = dict(
        id='a1',
        resource='r',
        event='e',
        service=['infra'],
        severity='warning',
        environment='Production',
        attributes={},
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture
def load_plugin(app_context):
    """Import-and-instantiate fixture.

    ``OverridePlugin`` reads from ``alerta.plugins.app.config`` (a
    module-level ``FakeApp``) directly, not through Flask's
    ``current_app``. Tests configure the Flask app via
    ``alerta_app.config[...]`` before calling this; we mirror those
    keys into the FakeApp at call time so the plugin sees them.

    The parent ``app_context`` fixture snapshots and restores FakeApp
    state around each test, so the mirror doesn't leak.
    """
    from alerta.plugins import app as plugins_app

    def _load():
        plugins_app.config.update(app_context.config)
        from override import OverridePlugin
        return OverridePlugin()
    return _load


class TestConfigParsing:

    def test_top_level_field_override(self, alerta_app, load_plugin):
        alerta_app.config['OVERRIDE_infra_severity'] = 'critical'
        plugin = load_plugin()

        assert plugin.service_override == {'infra': {'severity': 'critical'}}

    def test_attributes_override_nested(self, alerta_app, load_plugin):
        alerta_app.config['OVERRIDE_infra_attributes_runbook_url'] = 'https://r/infra'
        plugin = load_plugin()

        assert plugin.service_override == {
            'infra': {'attributes': {'runbook_url': 'https://r/infra'}},
        }

    def test_multiple_services_and_keys(self, alerta_app, load_plugin):
        alerta_app.config.update({
            'OVERRIDE_infra_severity': 'critical',
            'OVERRIDE_infra_attributes_team': 'sre',
            'OVERRIDE_dba_environment': 'Production-DB',
        })
        plugin = load_plugin()

        assert plugin.service_override['infra'] == {
            'severity': 'critical',
            'attributes': {'team': 'sre'},
        }
        assert plugin.service_override['dba'] == {'environment': 'Production-DB'}

    def test_env_var_also_picked_up(self, alerta_app, load_plugin, monkeypatch):
        monkeypatch.setenv('OVERRIDE_infra_severity', 'major')
        plugin = load_plugin()

        assert plugin.service_override == {'infra': {'severity': 'major'}}


class TestPreReceive:

    def test_matching_service_mutates_field(self, alerta_app, load_plugin):
        alerta_app.config['OVERRIDE_infra_severity'] = 'critical'
        plugin = load_plugin()

        alert = _fake_alert(service=['infra'], severity='warning')
        out = plugin.pre_receive(alert)

        assert out.severity == 'critical'

    def test_matching_service_merges_attributes(self, alerta_app, load_plugin):
        alerta_app.config['OVERRIDE_infra_attributes_team'] = 'sre'
        plugin = load_plugin()

        alert = _fake_alert(service=['infra'], attributes={'existing': 'keep'})
        out = plugin.pre_receive(alert)

        assert out.attributes == {'existing': 'keep', 'team': 'sre'}

    def test_non_matching_service_untouched(self, alerta_app, load_plugin):
        alerta_app.config['OVERRIDE_infra_severity'] = 'critical'
        plugin = load_plugin()

        alert = _fake_alert(service=['other'], severity='warning')
        out = plugin.pre_receive(alert)

        assert out.severity == 'warning'

    def test_service_matching_is_case_insensitive(self, alerta_app, load_plugin):
        alerta_app.config['OVERRIDE_infra_severity'] = 'critical'
        plugin = load_plugin()

        alert = _fake_alert(service=['INFRA'])
        out = plugin.pre_receive(alert)

        assert out.severity == 'critical'

    def test_multi_service_uses_first_and_warns(self, alerta_app, load_plugin, caplog):
        alerta_app.config['OVERRIDE_infra_severity'] = 'critical'
        plugin = load_plugin()

        alert = _fake_alert(service=['infra', 'dba'])
        plugin.pre_receive(alert)

        assert any('More than one service' in r.message for r in caplog.records)


class TestHooksAreNoOps:
    """post_receive and status_change are explicitly no-ops today."""

    def test_post_receive_returns_none(self, alerta_app, load_plugin):
        plugin = load_plugin()
        assert plugin.post_receive(_fake_alert()) is None

    def test_status_change_returns_none(self, alerta_app, load_plugin):
        plugin = load_plugin()
        assert plugin.status_change(_fake_alert(), 'open', 'text') is None
