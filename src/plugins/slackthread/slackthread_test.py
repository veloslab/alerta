import contextlib
import json
import os
import unittest
import slackthread
from slack_sdk import WebClient
from uuid import uuid4
from alerta.app import create_app, plugins
from typing import Dict


class SlackPluginTestCase(unittest.TestCase):
    def setUp(self):
        test_config = {
            'TESTING': True,
            'AUTH_REQUIRED': False,
            'DATABASE_URL': os.environ['DATABASE_URL'],
            'SLACK_TOKEN': os.environ['SLACK_TOKEN'],
            'SLACK_DEFAULT_CHANNEL_ID': os.environ['SLACK_DEFAULT_CHANNEL_ID']
        }
        self.app = create_app(test_config)
        self.client = self.app.test_client()
        self.slack_client = WebClient(token=test_config['SLACK_TOKEN'])
        plugins.plugins['slack'] = slackthread.SlackThreadPlugin()

    def generate_alert(self, event: str, severity: str, text: str, attributes: Dict = None):
        alert = {
            'event': event,
            'resource': 'UnitTest',
            'environment': 'Development',
            'service': ['SlackPlugin'],
            'severity': severity,
            'text': text,
        }
        if attributes:
            alert['attributes'] = attributes
        response = self.client.post(
            '/alert', data=json.dumps(alert), headers={'Content-type': 'application/json'})
        self.assertEqual(response.status_code, 201)
        data = json.loads(response.data.decode('utf-8'))
        self.assertEqual(data['status'], 'ok')
        self.assertRegex(
            data['id'], '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
        return data['alert']

    def test_slack_plugin_threading(self):
        event = uuid4().hex
        # Generate Initial Alert
        self.generate_alert(event, 'critical', 'Testing - Threading')
        # Clear Alert
        alert = self.generate_alert(event, 'ok', 'Testing - Threading')
        # Slack Confirmation
        channel_id = alert['attributes']['slack_channel_id']
        thread_ts = alert['attributes']['slack_ts']
        thread_info = self.slack_client.conversations_replies(channel=channel_id, ts=thread_ts)
        self.assertEqual(len(thread_info.data['messages']), 2)

    def test_slack_plugin_modulus(self):
        event = uuid4().hex
        for i in range(0, 4):
            self.generate_alert(event, 'critical', 'Testing - Threading',
                                attributes={
                                    'slack_notification_modulus': 2
                                })
        # Clear Alert
        alert = self.generate_alert(event, 'ok', 'Testing - Threading')
        # Slack Confirmation
        channel_id = alert['attributes']['slack_channel_id']
        thread_ts = alert['attributes']['slack_ts']
        thread_info = self.slack_client.conversations_replies(channel=channel_id, ts=thread_ts)
        self.assertEqual(len(thread_info.data['messages']), 2)


