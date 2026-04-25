"""Tier 2: container smoke tests.

Answers one question per test: "is this piece wired up in the image?"
No network, no DB, no HTTP — just ``docker run`` invoking Python
inside the built image.

These tests run in CI on every commit; they're the cheapest
regression net for:

* A ``setup.py`` that stopped declaring its entry point.
* A missing runtime dep (pinned in ``requirements.txt`` but not
  actually installed by the plugin's own ``install_requires``).
* A syntax error that landed in a module the unit tests don't import.
* A base-image upgrade that changed the venv path.

When you add a new webhook or plugin, add its name to the
``EXPECTED_PLUGINS`` / ``EXPECTED_WEBHOOKS`` list below and, if it
imports at a non-obvious module name, extend the import smoke test.
"""
import json

import pytest

from tests.smoke.conftest import run_in_image


pytestmark = pytest.mark.smoke


# Entry-point names declared by the setup.py in each src/ package.
# Keep these synced when packages are added or renamed.
EXPECTED_PLUGINS = ['slackthread', 'override']
EXPECTED_WEBHOOKS = ['vls-grafana', 'prefectflows']


def test_image_builds(alerta_image):
    """The fixture raises on build failure; reaching here means success."""
    assert alerta_image


def test_plugin_modules_import(alerta_image):
    """Each custom plugin/webhook module imports without error."""
    result = run_in_image(
        alerta_image,
        '/venv/bin/python', '-c',
        'import slackthread, override, vls_grafana, prefect_flows; print("ok")',
    )
    assert result.returncode == 0, result.stderr
    assert 'ok' in result.stdout


def test_plugin_entry_points_registered(alerta_image):
    """``alerta.plugins`` entry-point group advertises every plugin."""
    result = run_in_image(
        alerta_image,
        '/venv/bin/python', '-c',
        'import json; from importlib.metadata import entry_points; '
        'print(json.dumps([ep.name for ep in entry_points(group="alerta.plugins")]))',
    )
    assert result.returncode == 0, result.stderr

    # Take the last stdout line defensively in case the interpreter
    # emits warnings before our print().
    registered = json.loads(result.stdout.strip().splitlines()[-1])
    for name in EXPECTED_PLUGINS:
        assert name in registered, f'plugin {name!r} not registered, saw {registered}'


def test_webhook_entry_points_registered(alerta_image):
    """``alerta.webhooks`` entry-point group advertises every webhook."""
    result = run_in_image(
        alerta_image,
        '/venv/bin/python', '-c',
        'import json; from importlib.metadata import entry_points; '
        'print(json.dumps([ep.name for ep in entry_points(group="alerta.webhooks")]))',
    )
    assert result.returncode == 0, result.stderr

    registered = json.loads(result.stdout.strip().splitlines()[-1])
    for name in EXPECTED_WEBHOOKS:
        assert name in registered, f'webhook {name!r} not registered, saw {registered}'


def test_alertad_cli_runs(alerta_image):
    """``alertad --help`` exits 0 — catches broken app-factory imports."""
    result = run_in_image(alerta_image, '/venv/bin/alertad', '--help')
    assert result.returncode == 0, result.stderr
    assert 'Usage' in result.stdout or 'Commands' in result.stdout


def test_slack_base_url_knob_wired(alerta_image):
    """New SLACK_BASE_URL config is readable from the installed plugin."""
    result = run_in_image(
        alerta_image,
        '/venv/bin/python', '-c',
        'import inspect, slackthread; '
        'src = inspect.getsource(slackthread.SlackThreadPlugin.__init__); '
        'assert "SLACK_BASE_URL" in src, src; print("ok")',
    )
    assert result.returncode == 0, result.stderr
