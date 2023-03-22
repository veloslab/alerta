from setuptools import find_packages, setup

version = '5.3.1'

setup(
    name='slack-thread',
    version=version,
    description='Alerta plugin for Slack that uses threading to display duplicate alerts',
    url='https://github.com/veloslab/alerta',
    license='MIT',
    author='Carlos Ramos',
    author_email='crqdev@gmail.com',
    packages=find_packages(),
    py_modules=['slack_thread.py'],
    install_requires=[
        'slack_sdk',
        'dotmap',
        'jinja2'
    ],
    include_package_data=True,
    zip_safe=True,
    entry_points={
        'alerta.plugins': [
            'slack-thread = slack_thread:SlackThreadPlugin'
        ]
    }
)