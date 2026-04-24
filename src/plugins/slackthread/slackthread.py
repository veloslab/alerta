import logging
import time
from abc import ABC
from slack_sdk import WebClient
from jinja2 import Template
from alerta.plugins import PluginBase
from alerta.models.alert import Alert
from typing import Any, Optional

logger = logging.getLogger('alerta.plugins.slackthread')

DEFAULT_COLOR_MAP = {'security': '#000000',  # black
                     'critical': '#FF0000',  # red
                     'major': '#FFA500',  # orange
                     'minor': '#FFFF00',  # yellow
                     'warning': '#1E90FF',  # blue
                     'informational': '#808080',  # gray
                     'debug': '#808080',  # gray
                     'trace': '#808080',  # gray
                     'normal': '#00CC00',  # green
                     'ok': '#00CC00'}  # green

# Used when SLACK_DEFAULT_TEMPLATE is unset. `dashboard_url` is injected
# into the render context by SlackThreadPlugin.post_receive so operators
# don't have to duplicate the Alerta UI URL inside the template string.
DEFAULT_SLACK_TEMPLATE = (
    "*{{ alert.resource }}/{{ alert.event }}*\n```{{ alert.text }}```\n<{{ dashboard_url }}/alert/{{ alert.id }}|alert>"
)
DEFAULT_SLACK_TEMPLATE_OK = (
    "*{{ alert.resource }}/{{ alert.event }}* - Ok"
)

def format_template(template_fmt: str, alert: Alert, **context) -> Optional[str]:
    """Render a Jinja2 template against an alert plus extra context.

    Args:
        template_fmt: The Jinja2 template source. Typically comes from
            an alert attribute (``slack_template``, ``slack_fallback``)
            or a plugin-level default.
        alert: The Alerta alert the template renders against. Bound to
            the name ``alert`` inside the template.
        **context: Additional variables to expose to the template (e.g.
            ``dashboard_url``). Keyword names become Jinja2 variable
            names verbatim.

    Returns:
        The rendered string, or ``None`` if either the template failed
        to parse or the render raised. In either failure case the
        specific exception is already logged.
    """
    try:
        logger.debug(f"Generating template: {template_fmt}")
        template = Template(template_fmt)
    except Exception as e:
        logger.error(f"Template init failed: {repr(e)}")
        return None

    try:
        logger.debug(f"Rendering alert: {alert}")
        raw_string = template.render(alert=alert, **context)
        return raw_string
    except Exception as e:
        logger.error(f"Template render failed: {repr(e)}")
        return None


class SlackThreadPlugin(PluginBase, ABC):

    def __init__(self, name=None):
        super().__init__(name)
        # SLACK_BASE_URL is an override for the Slack API endpoint.
        # Default matches slack_sdk's own default; tests point this at
        # a mock service in docker-compose.test.yml to assert on the
        # traffic the plugin generates without hitting real Slack.
        self.client = WebClient(
            token=self.get_config('SLACK_TOKEN', type=str),
            base_url=self.get_config(
                'SLACK_BASE_URL', type=str, default='https://www.slack.com/api/'
            ),
        )
        self.default_channel_id = self.get_config('SLACK_DEFAULT_CHANNEL_ID', type=str)
        self.default_fallback = "[{{alert.severity}}] {{alert.environment}}/{{alert.service}}/{{alert.resource}}/{{alert.event}}"
        self.default_thread_timeout = self.get_config('SLACK_DEFAULT_THREAD_TIMEOUT', type=int, default=24)
        dashboard_url = self.get_config('DASHBOARD_URL', type=str, default='')
        if dashboard_url and not dashboard_url.startswith(('http://', 'https://')):
            dashboard_url = 'https://' + dashboard_url
        self.dashboard_url = dashboard_url
        # Two-step read so _select_template can tell whether an
        # operator actually configured SLACK_DEFAULT_TEMPLATE. When
        # set, it wins over the OK fallback (operator knows what they
        # want). When unset, DEFAULT_SLACK_TEMPLATE_OK can swap in
        # for resolved alerts. .env files can't carry a literal
        # newline, so operators write the template on one line and
        # use `\n` where they want breaks (Slack renders real
        # newlines). Unescape those here.
        configured_default = self.get_config('SLACK_DEFAULT_TEMPLATE', type=str, default=None)
        self._default_template_configured = configured_default is not None
        self.default_template = (configured_default or DEFAULT_SLACK_TEMPLATE).replace('\\n', '\n')
        self.channels = {}

    def _select_template(self, alert: Alert) -> str:
        """Pick the Jinja2 template for the alert's Slack attachment.

        Precedence (highest to lowest):
          1. ``alert.attributes['slack_template']`` — per-alert override.
          2. Operator-configured ``SLACK_DEFAULT_TEMPLATE`` env.
          3. ``DEFAULT_SLACK_TEMPLATE_OK`` when the alert's ``text``
             is any case of "ok" (vls_grafana sets this on resolved
             alerts).
          4. ``DEFAULT_SLACK_TEMPLATE`` — the firing default.

        Args:
            alert: The Alerta alert being rendered. Read for
                ``attributes`` and ``text`` only.

        Returns:
            The template source string to feed to ``format_template``.
        """
        per_alert = alert.attributes.get('slack_template')
        if per_alert is not None:
            return per_alert
        if self._default_template_configured:
            return self.default_template
        if alert.text and alert.text.lower() == 'ok':
            return DEFAULT_SLACK_TEMPLATE_OK
        return DEFAULT_SLACK_TEMPLATE

    def generate_new_thread(self, alert: Alert, channel_id: str) -> bool:
        if alert.attributes.get('slack_ts', None) is None:
            logger.info(f"New slack thread being generated for {alert}, no existing thread found")
            return True

        if alert.attributes.get('slack_channel_id', None) != channel_id:
            logger.info(f"New slack thread being generated for {alert}, channel mismatch\n"
                        f"Current: {alert.attributes.get('slack_channel_id', None)}\n"
                        f"New: {channel_id}",)
            return True

        thread_age = time.time() - float(alert.attributes.get('slack_ts'))
        if thread_age >= int(alert.attributes.get('slack_thread_timeout', self.default_thread_timeout)) * 3600:
            logger.info(f"New slack thread being generated for {alert}, existing thread has reached timeout")
            return True
        else:
            logger.info(f"Existing slack thread for {alert} will be used")
            return False

    def get_channel_id(self, alert: Alert) -> str:
        channel = alert.attributes.get('slack_channel', '@default')
        if channel == '@default':
            return self.default_channel_id

        if self.channels.get(channel, None) is None:
            data = self.client.conversations_list(types='public_channel,private_channel')
            channels = {i['name']: i['id'] for i in data['channels']}
            self.channels.update(**channels)

        if self.channels.get(channel, None) is None:
            logger.warning(f"Unable to find channel id for '{channel}', using default channel id")
            return self.default_channel_id

        return self.channels.get(channel)

    def pre_receive(self, alert: Alert, **kwargs) -> Alert:
        return alert

    def post_receive(self, alert: Alert, **kwargs) -> Optional[Alert]:

        # Don't post ok/normals more than once
        if alert.duplicate_count >= 1 and alert.severity.lower() in ['ok', 'normal']:
            return

        # Slack Notification Modulus
        slack_notification_modulus = int(alert.attributes.get('slack_notification_modulus', 1))

        if alert.duplicate_count % slack_notification_modulus != 0:
            logger.info(f"Slack message won't be send, slack_notification_modulus returned non-zero")
            return

        # Generate slack payload and channel_id
        slack_channel_id = self.get_channel_id(alert)
        slack_payload = [
            {
                "color": DEFAULT_COLOR_MAP.get(alert.severity),
                "mrkdwn_in": ["text"],
                "text": format_template(
                    self._select_template(alert),
                    alert,
                    dashboard_url=self.dashboard_url,
                ),
                "fallback": format_template(
                    alert.attributes.get('slack_fallback', self.default_fallback),
                    alert,
                    dashboard_url=self.dashboard_url,
                )
            }
        ]

        # Determine if thread needs to be created
        initial_thread_message = False
        if self.generate_new_thread(alert, slack_channel_id):
            response = self.client.chat_postMessage(channel=slack_channel_id, attachments=slack_payload)
            if response['ok']:
                alert.update_attributes({'slack_ts': response['ts'], 'slack_channel_id': response['channel']})
                initial_thread_message = True
                # Repost the same payload as the first thread reply so
                # the thread retains an immutable snapshot of the
                # original state even after chat_update later mutates
                # the parent message. A failure here logs but does not
                # abort — the thread is created and the alert state is
                # recorded; losing the history reply is strictly less
                # bad than losing the whole notification.
                history_reply = self.client.chat_postMessage(
                    channel=slack_channel_id,
                    attachments=slack_payload,
                    thread_ts=response['ts'],
                )
                if not history_reply['ok']:
                    logger.error(f"History reply to new thread failed for {alert}\nReceived: {history_reply}")
            else:
                logger.error(f"Initial post to slack failed for {alert}\nReceived: {response}")
                return

        # If not initial message for thread, send reply to thread and/or update parent message
        if initial_thread_message is False:
            # Send reply to thread
            response = self.client.chat_postMessage(channel=slack_channel_id,
                                                    attachments=slack_payload,
                                                    thread_ts=alert.attributes.get('slack_ts'))
            if not response['ok']:
                logger.error(f"Threaded reply to slack failed for {alert}\nReceived: {response}")

            response = self.client.chat_update(channel=slack_channel_id,
                                               ts=alert.attributes.get('slack_ts'),
                                               attachments=slack_payload)
            if not response['ok']:
                logger.error(f"Update to slack thread parent failed for {alert}\nReceived: {response}")

    def status_change(self, alert: Alert, status: str, text: str, **kwargs) -> Any:
        return
