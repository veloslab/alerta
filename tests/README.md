# Alerta test framework

Three tiers, each catching a different class of regression:

| Tier | Scope | Speed | When it runs |
|------|-------|-------|--------------|
| **unit** | Pure Python, one plugin/webhook at a time | <1s | Every commit |
| **smoke** | `docker build` + one-shot container introspection | ~30s | Every commit |
| **integration** | Full `docker compose` stack, real HTTP, mock Slack | ~1–2 min | PR merge |

## Layout

```
tests/
├── conftest.py                 # tier-agnostic fixtures (payload loader, factories)
├── fixtures/payloads/          # JSON bodies, one dir per source
│   ├── grafana/
│   └── prefect/
├── unit/
│   ├── conftest.py             # Flask app + app_context, env-var isolation
│   ├── webhooks/
│   └── plugins/
├── smoke/
│   ├── conftest.py             # builds image once per session
│   └── test_container.py
└── integration/
    ├── conftest.py             # compose up/down, slack_mock handle, post_webhook
    ├── docker-compose.test.yml # overrides base compose with test config
    ├── mocks/slack_mock/       # tiny Flask service that records every request
    ├── test_vls_grafana_flow.py
    └── test_prefect_flow.py
```

## Running

All development happens inside the repo-local venv at `.venv/`. It
holds `alerta-server`, the test deps, and editable installs of every
plugin/webhook in `src/` — tests can then import them by their
entry-point name.

```bash
# one-time setup (from the alerta/ repo root)
python3 -m venv .venv
.venv/bin/pip install --upgrade pip 'setuptools<81'
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/pip install alerta-server==9.0.3 strenum
.venv/bin/pip install -e src/plugins/slackthread \
                      -e src/plugins/override \
                      -e src/webhooks/vls_grafana \
                      -e src/webhooks/prefectflows

# each tier (always via .venv/bin/python to keep deps consistent)
.venv/bin/python -m pytest -m unit          # fast, no docker
.venv/bin/python -m pytest -m smoke         # builds image; needs docker
.venv/bin/python -m pytest -m integration   # brings up full stack; needs compose

# everything
.venv/bin/python -m pytest
```

Notes on pinning:
* `setuptools<81` — alerta-server 9.0.3 uses `pkg_resources`, which
  setuptools 81 finally dropped. Pin until alerta moves to
  `importlib.metadata`.
* `strenum` — alerta-server 9.0.3 requires it but doesn't list it in
  install_requires on Python 3.12+.

### Debugging a failing integration test

The stack is torn down on session exit. To keep it up for inspection:

```bash
ALERTA_TESTS_KEEP_STACK=1 pytest -m integration -x --pdb
# when done
docker compose -f docker-compose.yml -f tests/integration/docker-compose.test.yml down -v
```

Once the stack is up you can hit it directly:

- Alerta: `http://localhost:8080` (admin key `demo-key`)
- Slack mock: `http://localhost:18080/_captured` (JSON dump of every call)

## Adding a new webhook

1. **Drop payload fixtures** in `tests/fixtures/payloads/<service>/<scenario>.json`.
   One file per meaningful case (firing, resolved, with-overrides, edge-case).
2. **Write unit tests** in `tests/unit/webhooks/test_<service>.py`:
   - Import the translator / `WebhookBase` subclass directly.
   - Use the `payload` fixture: `body = payload('<service>.<scenario>')`.
   - Use `app_context` if the code touches `current_app.config`.
   - Mark with `pytestmark = pytest.mark.unit`.
3. **Register in smoke tests** by adding the entry-point name to
   `EXPECTED_WEBHOOKS` in `tests/smoke/test_container.py`.
4. **Add an integration test** in `tests/integration/test_<service>_flow.py`:
   - Use `post_webhook('<entry-point-name>', payload(...))`.
   - Read alerts back via the `alerta_client` fixture.
   - If the webhook drives a downstream plugin that hits an external
     service, assert on the mock via the plugin's mock-handle fixture
     (e.g. `slack_mock.captured('chat.postMessage')`).

## Adding a new plugin

1. **Unit test in `tests/unit/plugins/test_<plugin>.py`**:
   - Patch any external SDK at its import site in the plugin module
     (`mocker.patch('<plugin>.<SDK>', autospec=True)`).
   - Use `app_context` + config-override on `alerta_app.config` for
     plugin config keys.
   - Mark with `pytestmark = pytest.mark.unit`.
2. **Register in smoke tests** by adding the entry-point name to
   `EXPECTED_PLUGINS` in `tests/smoke/test_container.py`.
3. **Integration path depends on the plugin's side effect.** If it
   makes outbound HTTP, add a mock service following the slack_mock
   template (see below). If it only mutates the alert, add assertions
   to an existing integration test that drives an alert through the
   plugin.

## Adding a new mock service

Pattern lives in `tests/integration/mocks/slack_mock/`. Copy it and:

1. Implement the upstream API's happy-path endpoints in `app.py`.
   Keep a `_captured` list and a `_queued_responses` override map —
   same pattern works for any HTTP-based service.
2. Expose `/_captured`, `/_reset`, `/_next_response`, `/_health`. The
   test fixtures depend on this contract.
3. Add the service to `docker-compose.test.yml`, exposing it on a
   host port (5-digit range, no conflicts).
4. Add an env var on the `web` service pointing the plugin at the
   mock (`<UPSTREAM>_BASE_URL=http://<service>:8080/...`).
5. Add a fixture in `tests/integration/conftest.py` that returns a
   handle with `captured()` and `queue()` methods.

## Why the plugin needed `SLACK_BASE_URL`

`slack_sdk.WebClient` defaults to `https://www.slack.com/api/`. To hit
our mock without monkey-patching inside a running container, the
plugin reads `SLACK_BASE_URL` from config and passes it as the
client's `base_url`. The same pattern works for any future plugin that
wraps an HTTP SDK — expose the base URL as a config key, default to
the real endpoint, override in `docker-compose.test.yml`.

## What each tier catches (and doesn't)

- **Unit**: translator logic, template rendering, branch coverage.
  Misses: entry-point registration, dep pinning, inter-plugin ordering.
- **Smoke**: image builds, every package imports, entry points are
  registered, CLI loads. Misses: anything involving HTTP or DB.
- **Integration**: full dedup behavior, plugin ordering (`pre_receive`
  vs. `post_receive`), HTTP wiring, template rendering against real
  alerta `Alert` objects, cross-plugin interactions. Misses: real
  Slack quirks — the mock only covers the happy-path API surface.
