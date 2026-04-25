# CLAUDE.md — veloslab alerta

Instructions for Claude Code sessions working in this repo. Short by
design; extend in place as the code grows.

## What this is

A custom Alerta build for the veloslab stack. The `src/` tree holds
pip-installable plugin and webhook packages that extend `alerta-server`;
the `Dockerfile` layers them onto `alerta/alerta-web:9.1.0`.

## Layout

```
alerta/
├── src/
│   ├── plugins/
│   │   ├── slackthread/     # Slack notifier with thread reuse + channel routing
│   │   └── override/        # Per-service field/attribute overrides via OVERRIDE_* config
│   └── webhooks/
│       ├── vls_grafana/     # Grafana unified-alerting → Alerta (fork of built-in prometheus)
│       └── prefectflows/    # Prefect flow-run status → Alerta
├── Dockerfile                # pip-installs each src/ package on top of alerta-web
├── docker-compose.yml        # production-ish stack (alerta + postgres)
├── tests/                    # see tests/README.md for the full recipe
├── .venv/                    # repo-local venv (gitignored)
└── requirements-dev.txt
```

Every package under `src/` has its own `setup.py` with an
`alerta.plugins` or `alerta.webhooks` entry point. The entry-point name
(not the module name) is what shows up in `PLUGINS=` env and in
`/api/webhooks/<name>` routes.

## Dev environment — always use the repo-local venv

All Python commands in this repo run through `.venv/bin/python`. Do
not use system Python or `pyenv` — the venv carries editable installs
of every `src/` package, and tests depend on them.

First-time setup:

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/pip install -e src/plugins/slackthread \
                      -e src/plugins/override \
                      -e src/webhooks/vls_grafana \
                      -e src/webhooks/prefectflows
```

Running anything:

```bash
.venv/bin/python -m pytest          # all tiers
.venv/bin/python -m pytest -m unit  # fast path
```

If `.venv/` doesn't exist when you start work, create it before doing
anything else.

## Testing framework — three tiers

See `tests/README.md` for the operating manual. Short version:

| Tier | What it catches | Runs on |
|------|-----------------|---------|
| **unit** (`tests/unit/`) | Translator logic, template rendering, plugin branch matrix | Every commit |
| **smoke** (`tests/smoke/`) | Image builds, packages import, entry points register | Every commit |
| **integration** (`tests/integration/`) | Full compose stack, HTTP wiring, dedup, plugin ordering | PR merge |

Tier 3 uses a `slack_mock` Flask service (in
`tests/integration/mocks/slack_mock/`) that captures every request the
plugin makes, so tests can assert on real traffic without hitting
Slack. Future plugins that do outbound HTTP should follow the same
pattern — expose a `*_BASE_URL` config knob and add a sibling mock.

## Invariant: new plugin/webhook ⇒ new tests in all three tiers

When a user adds a `src/plugins/<name>/` or `src/webhooks/<name>/`
package, the work is not done until:

1. **Payload fixtures** exist under `tests/fixtures/payloads/<name>/`
   (at least a happy-path case; more for edge cases).
2. **Unit tests** exist at `tests/unit/{plugins,webhooks}/test_<name>.py`
   covering the core translator/hook logic.
3. **Smoke coverage**: the entry-point name is added to
   `EXPECTED_PLUGINS` or `EXPECTED_WEBHOOKS` in
   `tests/smoke/test_container.py`.
4. **Integration test**: at least one `test_<name>_flow.py` in
   `tests/integration/` that POSTs a payload end-to-end and asserts
   on the resulting alert (and any downstream mock traffic).
5. **Docker build**: add the editable-install line to the `Dockerfile`
   and, if the plugin ships, add it to the `PLUGINS=` env in
   `docker-compose.yml`.

Do these in the same change as the plugin/webhook itself. A PR that
adds a new plugin without tests should be rejected.

## CI workflows

Two workflows live in `.github/workflows/`:

* **`ci.yml`** — runs the three test tiers on every push and PR.
  Unit + smoke gate every change; integration gates PRs and pushes to
  `main`. Jobs are chained with `needs:` so a unit failure short-
  circuits the expensive tiers.
* **`docker-image.yml`** — publishes the container image to ghcr.io
  on version tags (`v*`). Keep it release-only; don't retrigger it
  from CI.

Each tier job invokes pytest with both an explicit path AND its
marker — `pytest tests/<tier> -m <tier>`. The path scoping is
load-bearing: smoke and integration jobs don't install the editable
`src/` packages, and pytest collects every conftest under the path it
walks. If a job runs `pytest -m smoke` from the repo root, pytest
also collects `tests/unit/conftest.py` (which imports `flask` and
`alerta.*`), which fails with ImportError. Keep the path scope; don't
trust marker filtering alone. The integration job also sets
`ALERTA_TESTS_KEEP_STACK=1` so the failure-log-dump step has a stack
to read from.

When you add a new test tier, fixture pattern, or runtime dependency
that CI needs, update `ci.yml` in the same change. The tier markers
(`-m unit`, `-m smoke`, `-m integration`) mean *test additions* don't
need workflow edits — but adding a dep, bumping Python, or adding a
new marker does.

## Invariant: READMEs stay current with code

`README.md` (repo root) documents each plugin and webhook — what it
does, its entry point, its config knobs, and its notable behaviors.
`tests/README.md` documents the test framework's operating contract.
Treat them as part of the code, not separate artifacts:

* When you add a plugin/webhook, add its section to `README.md` in
  the same change.
* When you add, rename, or remove a config knob (env var, attribute,
  label), update the relevant `README.md` entry to match.
* When you add a fixture pattern, a mock service, or a new test
  convention, update `tests/README.md`.
* When you change behavior that the README describes (severity
  mapping, normalization rules, routing logic, etc.), update the
  README alongside the code edit — not in a follow-up pass.

A PR that changes documented behavior without touching the README
should be rejected the same way one without tests would be.

## Key gotchas (load-bearing findings)

* **`PluginBase.get_config` is env-only.** It's a `@staticmethod` that
  reads `os.environ` and an optional `config=` kwarg — *not*
  `current_app.config`. Unit tests for plugins must set config via
  `monkeypatch.setenv(...)`, not via Flask app config. The
  `slack_env` fixture in `tests/unit/plugins/test_slackthread.py` is
  the reference pattern.
* **`alerta.plugins.app` is a `FakeApp`**, separate from the Flask
  app. Plugins that do `from alerta.plugins import app` (like
  `override`) read from this object, not `current_app`. The
  `app_context` fixture in `tests/unit/conftest.py` snapshots and
  mirrors config into this FakeApp; see the `load_plugin` fixture in
  `test_override.py` for the pattern.
* **`Alert.__init__` applies defaults and mutations.**
  `ALERT_TIMEOUT` is filled in when `timeout=None`. `correlate` gets
  the event appended when not already present. Resources are NOT
  lowercased by Alerta (any normalization is our job).
* **vls_grafana normalizes resources** — `resource =
  resource.lower().replace(' ', '_')`. Tests must assert on the
  normalized form.
* **vls_grafana resolved alerts** get `text = "OK"` regardless of
  incoming annotations.
* **Resource/event are inverted** from the stock Prometheus
  translator. `resource` = rule name (from `alertname` or `resource`
  label). `event` = `host/name` composite (or the `event` label).
  This is deliberate — see the docstring in `vls_grafana.py`.
* **SLACK_BASE_URL** is how integration tests redirect the real
  `slack_sdk.WebClient` at the mock. If you add a new plugin that
  wraps an HTTP SDK, follow the same pattern — expose a `*_BASE_URL`
  knob with a sensible default.

## Commits

Match the existing repo style — short lowercase subject, no long body
unless there's real context to record. No `Co-Authored-By` lines on
commits (global preference).
