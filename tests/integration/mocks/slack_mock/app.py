"""Minimal Slack Web API mock for integration tests.

Stands in for ``https://www.slack.com/api/`` so the slackthread plugin
in the alerta container can make its normal calls without touching the
real Slack. Every request is recorded in-memory; tests fetch the log
via ``GET /_captured`` and assert on it.

Endpoints intentionally return the happy-path shape only. If a test
needs a specific error, set it via ``POST /_next_response`` before the
alerta container is triggered.

Why not pre-built (wiremock, mountebank)?
* Zero extra image dependencies — plain stdlib + Flask.
* Assertions are Pythonic (the ``/_captured`` endpoint returns JSON
  tests can iterate).
* The Slack API shape we care about is tiny; a generic mock is
  overkill.
"""
import itertools
import logging
import os
import threading
import time

from flask import Flask, jsonify, request


app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger('slack_mock')


# Monotonic-ish ts generator so threading logic sees distinct values.
# Slack's real ts format is "1700000000.000100"; we preserve that shape.
_ts_counter = itertools.count(start=int(time.time() * 1_000_000))
_ts_lock = threading.Lock()


def _next_ts() -> str:
    """Return a Slack-shaped timestamp string, guaranteed unique per call."""
    with _ts_lock:
        n = next(_ts_counter)
    return f'{n // 1_000_000}.{n % 1_000_000:06d}'


# Captured requests. Reset between tests via POST /_reset.
_captured: list = []
_captured_lock = threading.Lock()


# Per-endpoint response overrides queued by POST /_next_response.
# Keyed by endpoint path ("chat.postMessage"); value is the JSON body
# to return on the next matching request, then cleared.
_queued_responses: dict = {}
_queued_lock = threading.Lock()


def _record(endpoint: str, body: dict) -> None:
    """Append a request record for later inspection."""
    with _captured_lock:
        _captured.append({
            'endpoint': endpoint,
            'body': body,
            'headers': {k: v for k, v in request.headers.items()},
            'received_at': time.time(),
        })


def _pop_override(endpoint: str):
    """Return a queued override response for ``endpoint`` if one exists."""
    with _queued_lock:
        return _queued_responses.pop(endpoint, None)


# ---- Slack-compatible endpoints -------------------------------------------
# slack_sdk posts form-encoded bodies (default) or JSON when configured.
# Accept both; the captured body is whichever came in.


def _parse_body() -> dict:
    """Accept either form-encoded or JSON bodies from slack_sdk."""
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form.to_dict(flat=True)


@app.route('/api/chat.postMessage', methods=['POST'])
def chat_post_message():
    body = _parse_body()
    _record('chat.postMessage', body)

    override = _pop_override('chat.postMessage')
    if override is not None:
        return jsonify(override)

    return jsonify({
        'ok': True,
        'ts': _next_ts(),
        'channel': body.get('channel', 'C_MOCK_DEFAULT'),
        'message': {'text': body.get('text', ''), 'user': 'U_MOCK'},
    })


@app.route('/api/chat.update', methods=['POST'])
def chat_update():
    body = _parse_body()
    _record('chat.update', body)

    override = _pop_override('chat.update')
    if override is not None:
        return jsonify(override)

    return jsonify({
        'ok': True,
        'ts': body.get('ts', _next_ts()),
        'channel': body.get('channel', 'C_MOCK_DEFAULT'),
        'text': body.get('text', ''),
    })


@app.route('/api/conversations.list', methods=['GET', 'POST'])
def conversations_list():
    # slack_sdk sends this as a GET when paginated, POST when not.
    body = request.args.to_dict(flat=True) if request.method == 'GET' else _parse_body()
    _record('conversations.list', body)

    override = _pop_override('conversations.list')
    if override is not None:
        return jsonify(override)

    # Default seed matches integration test fixtures. Extend via
    # SLACK_MOCK_CHANNELS env var (comma-separated "name:id" pairs)
    # if a specific test needs a custom roster.
    # Names match the test fixtures' slack_channel labels with the
    # leading '#' stripped (Slack's real API returns names without '#';
    # the plugin normalizes both sides).
    default_channels = [
        {'id': 'C_ALERTS_DEFAULT', 'name': 'alerts'},
        {'id': 'C_ALERTS_DB',      'name': 'db-alerts'},
        {'id': 'C_ALERTS_INFRA',   'name': 'alerts-infra'},
    ]
    extra = os.environ.get('SLACK_MOCK_CHANNELS', '')
    for pair in filter(None, (p.strip() for p in extra.split(','))):
        name, _, cid = pair.partition(':')
        if name and cid:
            default_channels.append({'id': cid, 'name': name})

    return jsonify({
        'ok': True,
        'channels': default_channels,
        'response_metadata': {'next_cursor': ''},
    })


# ---- Test-only control surface --------------------------------------------


@app.route('/_captured', methods=['GET'])
def captured():
    """Return every request received since the last ``/_reset``.

    Filter with ``?endpoint=chat.postMessage`` to narrow. Tests use
    this to assert on payload content, call count, ordering, etc.
    """
    endpoint = request.args.get('endpoint')
    with _captured_lock:
        items = list(_captured)
    if endpoint:
        items = [i for i in items if i['endpoint'] == endpoint]
    return jsonify(items)


@app.route('/_reset', methods=['POST'])
def reset():
    """Clear captured history and any queued response overrides."""
    with _captured_lock:
        _captured.clear()
    with _queued_lock:
        _queued_responses.clear()
    return jsonify({'ok': True})


@app.route('/_next_response', methods=['POST'])
def queue_response():
    """Queue a canned response for the next call to ``endpoint``.

    Body shape: ``{"endpoint": "chat.postMessage", "response": {...}}``.
    The queued dict is returned verbatim; use it to simulate Slack
    errors (``{"ok": false, "error": "channel_not_found"}``) or rate
    limits in tests that need to exercise failure paths.
    """
    data = request.get_json(force=True)
    endpoint = data['endpoint']
    response = data['response']
    with _queued_lock:
        _queued_responses[endpoint] = response
    return jsonify({'ok': True, 'queued_for': endpoint})


@app.route('/_health', methods=['GET'])
def health():
    return jsonify({'ok': True})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
