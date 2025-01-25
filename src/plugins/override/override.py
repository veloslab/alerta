import logging
from abc import ABC
from alerta.plugins import PluginBase
from alerta.models.alert import Alert
from typing import Any, Optional
import os
try:
    from alerta.plugins import app  # alerta >= 5.0
except ImportError:
    from alerta.app import app  # alerta < 5.0

logger = logging.getLogger('alerta.plugins.override')

"""
Plugin will override alerts that match service in env/app variables
OVERRIDE_{service}_{alert_attribute}=XXX
or to modify an alerts attributes
OVERRIDE_{service}_attributes_{key_in_attributes}=XXX
"""
class OverridePlugin(PluginBase, ABC):
    def __init__(self, name=None):
        super().__init__(name)
        service_overrides = {}
        for key, value in {**os.environ, **app.config}.items():
            key = key.lower()
            if key.startswith('override_'):
                service, override = [i.strip() for i in key.replace('override_', '').split('_', 1)]
                if service not in service_overrides:
                    service_overrides[service] = {}
                if override.startswith('attributes_'):
                    if 'attributes' not in service_overrides:
                        service_overrides[service]['attributes'] = {}
                    service_overrides[service]['attributes'][override.replace('attributes_', '')] = value
                else:
                    service_overrides[service][override] = value
        self.service_override = service_overrides

    def pre_receive(self, alert: Alert, **kwargs) -> Alert:
        service = alert.service.lower()
        if service in self.service_override:
            logger.info(f"{alert} will be modified")
            for key, value in self.service_override[service].items():
                if key == 'attributes':
                    logger.info(f"{alert}'s attributes override with {value}")
                    alert.attributes = {**alert.attributes, **value}
                else:
                    logger.info(f"{alert}'s {key} override with {value}")
                    setattr(alert, key, value)
        return alert

    def post_receive(self, alert: Alert, **kwargs) -> Optional[Alert]:
        pass

    def status_change(self, alert: Alert, status: str, text: str, **kwargs) -> Any:
        return
