import logging
import time
from abc import ABC
from slack_sdk import WebClient
from jinja2 import Template
from alerta.plugins import PluginBase
from alerta.models.alert import Alert
from dotmap import DotMap
from typing import Any, Optional

logger = logging.getLogger('alerta.plugins.slack')

DEFAULT_COLOR_MAP = {'security': '#000000',  # black
                     'critical': '#FF0000',  # red
                     'major': '#FFA500',  # orange
                     'minor': '#FFFF00',  # yellow
                     'warning': '#1E90FF',  # blue
                     'informational': '#808080',  # gray
                     'debug': '#808080',  # gray
                     'trace': '#808080',  # gray
                     'ok': '#00CC00'}  # green


def format_template(template_fmt: str, alert: Alert) -> Optional[str]:
    alert_dm = DotMap(alert.serialize)
    try:
        logger.debug(f"Generating template: {template_fmt}")
        template = Template(template_fmt)
    except Exception as e:
        logger.error(f"Template init failed: {repr(e)}")
        return None

    try:
        logger.debug(f"Rendering alert: {alert_dm}")
        raw_string = template.render(alert=alert_dm)
        return raw_string.replace('DotMap()', '')
    except Exception as e:
        logger.error(f"Template render failed: {repr(e)}")
        return None


class SlackThreadPlugin(PluginBase, ABC):

    def __init__(self, name=None):
        super().__init__(name)
        self.client = WebClient(token=self.get_config('SLACK_TOKEN', type=str))
        self.default_channel_id = self.get_config('SLACK_DEFAULT_CHANNEL_ID', type=str)
        self.default_fallback = "[{{alert.severity}}] {{alert.environment}}/{{alert.service}}/{{alert.resource}}/{{alert.event}}"
        self.default_thread_timeout = self.get_config('SLACK_DEFAULT_THREAD_TIMEOUT', type=int, default=24)
        self.channels = {}

    def generate_new_thread(self, alert: Alert, channel_id: str) -> bool:
        if alert.attributes.get('slack_ts', None) is None:
            logger.info(f"New slack thread being generated for {alert}, no existing thread found")
            return True

        if alert.attributes.get('slack_channel_id', None) != channel_id:
            logger.info(f"New slack thread being generated for {alert}, channel mismatch\n"
                        f"Current: {alert.attributes.get('slack_channel_id', None)}\n"
                        f"New: {channel_id}",)
            return True

        thread_age = time.time() - int(alert.attributes.get('slack_ts'))
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
        # If alert attribute 'slack_delay' is set to greater than 0, delay posting to slack until alert count exceeds
        # slack_delay
        if (alert.duplicate_count + 1) <= int(alert.attributes.get('slack_delay', 0)):
            return

        # Generate slack payload and channel_id
        slack_channel_id = self.get_channel_id(alert)
        slack_payload = [
            {
                "color": DEFAULT_COLOR_MAP.get(alert.severity),
                "mrkdwn_in": ["text"],
                "text": format_template(alert.text, alert),
                "fallback": format_template(alert.attributes.get('fallback', self.default_fallback), alert)
            }
        ]

        # Determine if thread needs to be created
        if self.generate_new_thread(alert, slack_channel_id):
            response = self.client.chat_postMessage(channel=slack_channel_id, attachments=slack_payload)
            if response['ok']:
                alert.update_attributes({'slack_ts': response['ts'], 'slack_channel_id': response['channel']})
            else:
                logger.error(f"Post to slack failed for {alert}\nReceived: {response}")
                return

        # Send reply to thread
        response = self.client.chat_postMessage(channel=slack_channel_id,
                                                attachments=slack_payload,
                                                thread_ts=alert.attributes.get('slack_ts'))
        if response['ok']:
            alert.update_attributes({'slack_ts': response['ts'], 'slack_channel_id': response['channel']})
        else:
            logger.error(f"Post to slack failed for {alert}\nReceived: {response}")

    def status_change(self, alert: Alert, status: str, text: str, **kwargs) -> Any:
        return
