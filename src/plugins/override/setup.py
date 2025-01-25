from setuptools import find_packages, setup

version = '1.0.0'

setup(
    name='override',
    version=version,
    description='Alerta plugin to further formats Override Alerts configs',
    url='https://github.com/veloslab/alerta',
    license='MIT',
    author='Carlos Ramos',
    author_email='crqdev@gmail.com',
    packages=find_packages(),
    py_modules=['override'],
    install_requires=[
    ],
    include_package_data=True,
    zip_safe=True,
    entry_points={
        'alerta.plugins': [
            'override = override:OverridePlugin'
        ]
    }
)