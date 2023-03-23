FROM alerta/alerta-web:0.7.0
COPY src/ /src
RUN /venv/bin/python /src/plugins/slackthread/setup.py install
