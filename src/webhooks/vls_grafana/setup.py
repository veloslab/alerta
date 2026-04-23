from setuptools import find_packages, setup

version = '1.0.0'

setup(
    name='vls-grafana',
    version=version,
    description='Alerta Webhook for Grafana unified alerting (veloslab)',
    url='https://github.com/veloslab/alerta',
    license='MIT',
    author='Carlos Ramos',
    author_email='crqdev@gmail.com',
    packages=find_packages(),
    py_modules=['vls_grafana'],
    include_package_data=True,
    zip_safe=True,
    entry_points={
        'alerta.webhooks': [
            'vls-grafana = vls_grafana:VlsGrafanaWebhook'
        ]
    }
)
