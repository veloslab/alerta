"""Integration-tier fixtures: compose stack up, reset between tests.

Strategy:
* ``compose_stack`` (session): ``docker compose up --build -d``, wait
  for alerta + slack_mock health, tear down at end.
* ``slack_mock`` (function): yields a helper object scoped to one
  test. Calls ``/_reset`` on entry so captured requests start clean.
* ``alerta_client`` (function): prebuilt ``requests.Session`` with the
  admin API key header set.
* ``post_webhook`` (function): small wrapper so a new test reads like
  ``post_webhook('vls-grafana', payload_fixture)``.

Adding a test for a new webhook:
1. Drop a JSON payload in ``tests/fixtures/payloads/<service>/``.
2. Write ``test_<service>_flow.py`` in ``tests/integration/`` using
   the fixtures above.
3. If the plugin calls an external HTTP API, add a mock service in
   ``tests/integration/mocks/`` following ``slack_mock`` as a template.
"""
import os
import subprocess
import time
from pathlib import Path

import pytest
import requests


pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = REPO_ROOT / 'docker-compose.yml'
TEST_COMPOSE = REPO_ROOT / 'tests' / 'integration' / 'docker-compose.test.yml'

ALERTA_URL = 'http://localhost:8080'
SLACK_MOCK_URL = 'http://localhost:18080'
ADMIN_API_KEY = 'demo-key'  # matches docker-compose.yml ADMIN_KEY


def _compose(*args, check=True):
    """Run ``docker compose`` with both files layered.

    Args:
        *args: Subcommand and its flags (e.g. ``'up', '-d', '--build'``).
        check: Raise on non-zero exit when ``True``.

    Returns:
        ``CompletedProcess`` from ``subprocess.run``.
    """
    cmd = [
        'docker', 'compose',
        '-f', str(BASE_COMPOSE),
        '-f', str(TEST_COMPOSE),
        *args,
    ]
    return subprocess.run(cmd, check=check, cwd=REPO_ROOT,
                          capture_output=True, text=True)


def _wait_for(url: str, timeout: float = 60.0, probe_interval: float = 1.0):
    """Poll ``url`` until HTTP 200 or ``timeout`` elapses.

    Alerta takes ~15–30s to boot (wsgi + DB schema init); slack_mock is
    near-instant. Polling is simpler than parsing docker healthchecks
    and fails with a useful message if the service never comes up.

    Raises:
        TimeoutError: If the service never returns 200 within ``timeout``.
    """
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            r = requests.get(url, timeout=2)
            if r.status_code == 200:
                return
            last_error = f'status {r.status_code}'
        except requests.RequestException as e:
            last_error = str(e)
        time.sleep(probe_interval)
    raise TimeoutError(f'{url} never became ready: {last_error}')


@pytest.fixture(scope='session')
def compose_stack():
    """Bring up the full integration stack once per test session.

    Honors ``ALERTA_TESTS_KEEP_STACK=1`` — useful when iterating
    locally ("pytest --pdb" on a failure, poke at the live stack).

    Yields:
        ``None``. The fixture's job is startup/teardown, not exposing
        handles; use ``alerta_client`` / ``slack_mock`` fixtures for
        that.
    """
    if subprocess.run(['which', 'docker'], capture_output=True).returncode != 0:
        pytest.skip('docker CLI not available')

    up = _compose('up', '-d', '--build', check=False)
    if up.returncode != 0:
        pytest.fail(f'compose up failed:\n{up.stderr[-4000:]}')

    try:
        # Alerta 9.x exposes its health endpoint as /management/healthcheck
        # on the mgmt blueprint; nginx in the alerta-web image strips
        # the /api/ prefix before proxying, so externally it lives at
        # /api/management/healthcheck. /api/health returns 404.
        _wait_for(f'{ALERTA_URL}/api/management/healthcheck', timeout=90)
        _wait_for(f'{SLACK_MOCK_URL}/_health', timeout=30)
    except TimeoutError as e:
        logs = _compose('logs', '--tail=200', check=False)
        pytest.fail(f'{e}\n\n--- compose logs ---\n{logs.stdout[-6000:]}')

    yield

    if os.environ.get('ALERTA_TESTS_KEEP_STACK') != '1':
        _compose('down', '-v', check=False)


@pytest.fixture
def slack_mock(compose_stack):
    """Per-test handle to the slack mock; resets capture state on entry."""
    requests.post(f'{SLACK_MOCK_URL}/_reset', timeout=5)

    class _Handle:
        """Thin wrapper so tests read like ``slack_mock.captured('chat.postMessage')``."""

        url = SLACK_MOCK_URL

        def captured(self, endpoint: str | None = None) -> list:
            """All requests recorded since the fixture was entered.

            Args:
                endpoint: Optional endpoint filter (``'chat.postMessage'``).

            Returns:
                List of request records. Each record has ``endpoint``,
                ``body``, ``headers``, and ``received_at`` keys.
            """
            params = {'endpoint': endpoint} if endpoint else {}
            return requests.get(f'{SLACK_MOCK_URL}/_captured',
                                params=params, timeout=5).json()

        def queue(self, endpoint: str, response: dict) -> None:
            """Queue a canned response for the next call to ``endpoint``."""
            requests.post(
                f'{SLACK_MOCK_URL}/_next_response',
                json={'endpoint': endpoint, 'response': response},
                timeout=5,
            )

    return _Handle()


@pytest.fixture
def alerta_client(compose_stack):
    """``requests.Session`` pre-auth'd with the admin API key."""
    s = requests.Session()
    s.headers.update({'Authorization': f'Key {ADMIN_API_KEY}'})
    # Purge any alerts left by earlier tests so counts are deterministic.
    s.delete(f'{ALERTA_URL}/api/alerts', timeout=10)
    return s


@pytest.fixture
def post_webhook(alerta_client):
    """Post a webhook payload to ``/api/webhooks/<name>``.

    Args:
        Called with ``post_webhook(name, body)`` where ``name`` is the
        webhook entry-point name (e.g. ``'vls-grafana'``) and ``body``
        is the decoded JSON dict.

    Returns:
        The ``requests.Response``. Caller asserts on status code.
    """
    def _post(name: str, body: dict) -> requests.Response:
        return alerta_client.post(
            f'{ALERTA_URL}/api/webhooks/{name}',
            json=body,
            timeout=10,
        )
    return _post
