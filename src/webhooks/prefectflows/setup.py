from setuptools import find_packages, setup

version = '1.0.0'

setup(
    name='prefect-flows',
    version=version,
    description='Alerta Webhook for Prefect Flows',
    url='https://github.com/veloslab/alerta',
    license='MIT',
    author='Carlos Ramos',
    author_email='crqdev@gmail.com',
    packages=find_packages(),
    py_modules=['prefect_flows'],
    include_package_data=True,
    zip_safe=True,
    entry_points={
        'alerta.webhooks': [
            'prefectflows = prefect_flows:PrefectFlowWebhook'
        ]
    }
)