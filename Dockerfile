FROM alerta/alerta-web:9.0.0
USER root
COPY src/ /src
RUN /venv/bin/pip install /src/plugins/slackthread/
USER alerta