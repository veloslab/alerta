"""Unit-tier fixtures: lightweight Flask app for plugin/webhook code.

The production plugins and webhooks read configuration through
``current_app.config`` (webhooks) or ``PluginBase.get_config`` (plugins),
which itself falls through to ``current_app.config``. Full
``alerta.app.create_app`` boots a DB connection we don't want in unit
tests, so we stand up a bare Flask app with just the config keys these
modules actually read.

Mark every test in this tree with ``@pytest.mark.unit`` (or configure
auto-marking in the module) so ``pytest -m unit`` skips docker tiers.
"""
import pytest


# Heavy imports (flask, alerta.*) are deferred into fixture bodies so
# pytest can collect this conftest even when those packages aren't
# installed — the smoke CI job runs `pytest -m smoke` with only the
# test-runner deps, and pytest imports every conftest under
# `testpaths` at collection time regardless of the marker filter.


def _build_default_config():
    """Assemble the default test config, pulling alerta defaults lazily.

    Alerta's ``Alert`` constructor and ``alarm_model`` read ~100 config
    keys directly off ``current_app.config``; rather than reimplement
    them, load alerta's own settings module and layer test-specific
    overrides on top.

    Returns:
        Dict of config keys ready to push onto a Flask app.
    """
    from alerta import settings as alerta_settings

    alerta_defaults = {
        k: getattr(alerta_settings, k) for k in dir(alerta_settings) if k.isupper()
    }
    return {
        **alerta_defaults,
        'TESTING': True,
        'DEFAULT_ENVIRONMENT': 'Production',
        'SLACK_TOKEN': 'xoxb-test-token',
        'SLACK_DEFAULT_CHANNEL_ID': 'C_TEST_DEFAULT',
        'SLACK_DEFAULT_THREAD_TIMEOUT': 24,
        'SLACK_BASE_URL': 'http://mock-slack.test/api/',
        'DASHBOARD_URL': 'https://alerta.test',
    }


@pytest.fixture
def alerta_app():
    """Flask app + alerta's ``FakeApp`` config, synced for the test's lifetime.

    Two config surfaces need the test values:

    * ``current_app.config`` — read by webhook code and by
      ``PluginBase.get_config``.
    * ``alerta.plugins.app.config`` — a module-level ``FakeApp`` that
      the plugins system instantiates at import time; plugins like
      ``OverridePlugin`` read this directly instead of going through
      ``current_app``.

    Mutating ``alerta.plugins.app.config`` leaks across tests, so we
    snapshot it on entry and restore it on teardown.

    Returns:
        A ``flask.Flask`` instance with ``DEFAULT_CONFIG`` applied.
        Use ``.config.update(...)`` on it inside a test — the
        ``_sync_plugins_app_config`` fixture mirrors the keys into
        the FakeApp automatically when ``app_context`` is used.
    """
    from flask import Flask

    app = Flask('alerta-test')
    app.config.update(_build_default_config())
    return app


@pytest.fixture
def app_context(alerta_app):
    """Push an application context AND sync config into alerta's FakeApp.

    Most plugin/webhook code calls ``current_app.config[...]`` or
    ``flask.current_app`` during ``__init__``. The override plugin
    (and any future plugin that does ``from alerta.plugins import app``)
    needs its keys on the FakeApp too.

    Yields:
        The active app. Yielding rather than returning ensures the
        context is popped and the FakeApp config is restored on teardown.
    """
    from alerta.plugins import app as plugins_app

    snapshot = dict(plugins_app.config)
    plugins_app.config.update(alerta_app.config)
    try:
        with alerta_app.app_context():
            yield alerta_app
    finally:
        plugins_app.config.clear()
        plugins_app.config.update(snapshot)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Strip SLACK_*/OVERRIDE_* env vars so env precedence doesn't leak.

    ``PluginBase.get_config`` checks ``os.environ`` before ``app.config``,
    so a stray ``SLACK_TOKEN`` in the developer's shell would silently
    override the test config. Same story for ``OverridePlugin`` which
    reads every ``OVERRIDE_*`` env var at init time.

    Individual tests can add their own env vars back with
    ``monkeypatch.setenv``; this autouse fixture only removes, so it
    composes cleanly.
    """
    import os
    for key in list(os.environ):
        if key.startswith(('SLACK_', 'OVERRIDE_', 'ALERTA_', 'DASHBOARD_')):
            monkeypatch.delenv(key, raising=False)
