# veloslab alerta

Custom Alerta build for the veloslab stack. Layers four pip-installed
packages (two plugins, two webhooks) onto the upstream
`alerta/alerta-web:9.1.0` image.

For Claude Code guidelines and repo conventions see `CLAUDE.md`.
For the test framework see `tests/README.md`.

## Layout

```
src/
├── plugins/
│   ├── slackthread/
│   └── override/
└── webhooks/
    ├── vls_grafana/
    └── prefectflows/
Dockerfile              # alerta/alerta-web + pip install src/*
docker-compose.yml      # alerta + postgres
tests/                  # unit + smoke + integration
```

## Plugins

### `slackthread` — Slack notifier with thread reuse

Sends each incoming alert to Slack as a new thread, then posts
duplicates as replies to that thread and updates the parent message in
place so the thread title always reflects the latest state. Each alert
gets its own thread, routed to a configurable channel.

- **Entry point**: `slackthread` (register in `PLUGINS=`)
- **Hooks**: `post_receive` (after-ingest side effect only)
- **Channel routing**: `slack_channel` attribute on the alert
  overrides the default; `#channel-name` is resolved via
  `conversations.list` and cached in-process. Absent or empty →
  `SLACK_DEFAULT_CHANNEL_ID`. The leading `#` is optional —
  `#alerts-db` and `alerts-db` resolve identically.
- **Thread rotation**: a new thread is started when the existing
  `slack_ts` is older than `SLACK_DEFAULT_THREAD_TIMEOUT` hours (or
  the per-alert `slack_thread_timeout` attribute), or when the
  resolved channel doesn't match the attached `slack_channel_id`.
- **Notification gating**: `slack_notification_modulus` on the alert
  suppresses traffic — only every Nth duplicate posts. OK/normal
  duplicates are suppressed unconditionally.
- **Templating**: Jinja2 over the alert. Defaults live in
  `DEFAULT_SLACK_TEMPLATE`; override per-alert with the
  `slack_template` / `slack_fallback` attributes. `.env` files can't
  hold literal newlines, so `\n` in a configured template is
  unescaped to real newlines on load.
- **OK auto-swap**: when the alert's `text` is any case of "ok" and
  no other template is configured, `DEFAULT_SLACK_TEMPLATE_OK`
  (`*{{ resource }}/{{ event }}* - Ok`) is used instead of the
  firing default. Pairs with the `vls_grafana` webhook's
  text-on-resolve behavior to give resolved alerts a short one-liner
  at the end of their thread. Precedence: per-alert `slack_template`
  > `SLACK_DEFAULT_TEMPLATE` env > OK auto-swap > base firing default.
- **Thread history**: when a new thread is created, the plugin
  reposts the same payload as the first reply in that thread. This
  gives the thread an immutable snapshot of the original firing
  state, so later `chat_update` calls that mutate the parent message
  don't erase the original content from the channel's scrollback.
- **Config** (all env vars — `PluginBase.get_config` is env-only):
  - `SLACK_TOKEN` — bot token (`xoxb-…`)
  - `SLACK_DEFAULT_CHANNEL_ID` — channel ID for unspecified routes
  - `SLACK_DEFAULT_THREAD_TIMEOUT` — hours before a thread rotates
    (default 24)
  - `SLACK_BASE_URL` — override Slack's API URL. Default is the real
    Slack; point at a mock in tests (see `tests/integration/`)
  - `SLACK_DEFAULT_TEMPLATE` — optional override for the Jinja2
    template
  - `DASHBOARD_URL` — used by the default template to link back to
    the Alerta UI

### `override` — per-service field rewrites

Reads `OVERRIDE_<service>_<field>` keys from env and `app.config` at
init time and rewrites matching alerts' fields in `pre_receive`.
Lets operators force a severity, attach a runbook, or pin any other
Alert attribute for every alert coming from a given service without
touching the source producer.

- **Entry point**: `override`
- **Hooks**: `pre_receive` (mutates the alert before it's stored)
- **Key shape**: `OVERRIDE_<service>_<field>=<value>`, e.g.
  `OVERRIDE_infra_severity=critical`. For nested attributes use
  `OVERRIDE_<service>_attributes_<key>=<value>`, e.g.
  `OVERRIDE_dba_attributes_runbook_url=https://…`.
- **Matching**: case-insensitive on the first entry of the alert's
  `service` list. Multi-service alerts log a warning and use the
  first entry only.
- **Config**: any number of `OVERRIDE_*` keys in env or Flask config.
  No dedicated config beyond that.

## Webhooks

### `vls_grafana` — Grafana unified alerting receiver

Fork of alerta-server's built-in `prometheus` translator, tuned for
the veloslab metrics conventions. Grafana rules post to
`/api/webhooks/vls-grafana`; each element of `alerts[]` is translated
to one `Alert`.

- **Entry point**: `vls-grafana`
- **Route**: `POST /api/webhooks/vls-grafana`
- **Resource / event semantics** (inverted from the built-in
  translator):
  - `resource` defaults to the rule name (`alertname` label or
    annotation). Normalized via `.lower().replace(' ', '_')` so it's
    stable across case-insensitive dedup and URL-safe.
  - `event` defaults to `{host}/{name}` — distinct per firing
    instance.
  - Rule-label overrides named `resource` and `event` win over the
    defaults.
- **Severity**: `severity` label on firing alerts; resolved alerts
  use `alarm_model.DEFAULT_NORMAL_SEVERITY` and force `text = "OK"`.
- **Attribute lifting**: labels in the `ATTRIBUTE_LABELS` allow-list
  (`slack_channel`, `team`, `runbook_url`) move from tags into
  `attributes` so downstream plugins can read them via
  `alert.attributes` instead of parsing tag strings. Configure
  defaults via Grafana's `[unified_alerting.external_labels]`;
  per-rule labels of the same name override.
- **Templating**: label values are Python `str.format`'d against the
  full labels dict, and annotations are formatted against the
  processed labels — matches upstream prometheus translator behavior.
- **Tagging**: `origin=grafana`, `event_type=grafanaAlert`. Leftover
  labels become `k=v` tag strings.
- **Config**: none of its own (reads `DEFAULT_ENVIRONMENT` and
  `ALERT_TIMEOUT` from alerta's standard config).

### `prefectflows` — Prefect flow-run status receiver

Accepts Prefect's automation webhook (form-encoded key/value pairs in
a `message` field) and maps flow-run state to an alert severity. Used
to surface failed and crashed deployments in Alerta.

- **Entry point**: `prefectflows`
- **Route**: `POST /api/webhooks/prefectflows`
- **Expected payload**: `{"message": "state=…&resource=…&event=…&…"}`
  — a URL-encoded string in a JSON envelope. Prefect's automation
  template produces this shape out of the box.
- **State → severity**:

  | Prefect state | Alert severity |
  |---------------|----------------|
  | `scheduled`, `pending` | `informational` |
  | `completed` | `ok` |
  | `failed`, `crashed` | `critical` |
  | `cancelled`, `paused` | `warning` |
  | `cancelling` | `major` |
  | anything else | `major` |

- **Fields**: `resource`, `event`, `service`, `group`, `value`,
  `text`, `tags` are passed through from the URL-encoded body.
  `environment` defaults to `Production`.
- **Config**: none. Behavior is fully driven by the posted payload.

## Development

See `CLAUDE.md` for the full workflow. The short version:

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/pip install -e src/plugins/slackthread \
                      -e src/plugins/override \
                      -e src/webhooks/vls_grafana \
                      -e src/webhooks/prefectflows

.venv/bin/python -m pytest -m unit     # unit tier, fast
.venv/bin/python -m pytest             # all three tiers (needs docker)
```

## Running the container

```bash
docker compose up --build -d
```

`docker-compose.yml` wires alerta + postgres and enables the plugins
via the `PLUGINS=` env var. Adjust credentials, the admin key, and
the plugin list as needed before running against anything real.

## Adding a new plugin or webhook

Every new package under `src/plugins/` or `src/webhooks/` ships with
fixtures + unit/smoke/integration tests in the same change — see
`CLAUDE.md` § "Invariant" for the checklist and `tests/README.md` for
the recipe.
