curl -XPOST localhost:8080/api/alert \
-H 'Authorization: Key demo-key' \
-H 'Content-type: application/json' \
-d '{
      "environment": "Production",
      "event": "HttpServerError",
      "group": "Web",
      "origin": "curl",
      "resource": "web04",
      "service": [
        "example.com"
      ],
      "severity": "major",
      "tags": [
        "dc1"
      ],
      "text": "Site is down.",
      "type": "exceptionAlert",
      "value": "Bad Gateway (501)"
    }'