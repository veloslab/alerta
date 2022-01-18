# Alerta Heartbeat

### An additional image layer to generate alerts from heartbeat expirations

Information
------------

By default, expired heartbeats do not generate an alert. Instead the following command must be run:

    $ alerta heartbeats --alert

The additional image layer generates a cron entry that runs above command every 30 seconds
