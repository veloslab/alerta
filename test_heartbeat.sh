curl -XPOST localhost:8080/api/heartbeat \
-H 'Authorization: Key demo-key' \
-H 'Content-type: application/json' \
-d '{
      "origin": "cluster05",
      "timeout": 45,
      "tags": ["db05", "dc2"],
      "attributes": {
        "environment": "Production",
        "service": [
          "Core",
          "HA"
        ],
        "group": "Network",
        "severity": "major"
      }
    }'
