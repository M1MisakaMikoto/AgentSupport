# Experiment `exp11_k8s_pod_resources` — phase `after`

- timestamp: `2026-08-25T05:40:50+00:00`
- git head: `307c773`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp11_k8s_pod_resources.py --phase after`

---

## Kubernetes runner Pod resources
| observation | value |
| --- | --- |
| container name | runner |
| resources block present | True |
| resources.requests | {"cpu": "500m", "memory": "1Gi"} |
| resources.limits | {"cpu": "2", "memory": "2Gi"} |
| readOnlyRootFilesystem | False |

### rendered container spec (resources section)
```json
{
  "name": "runner",
  "resources": {
    "requests": {
      "cpu": "500m",
      "memory": "1Gi"
    },
    "limits": {
      "cpu": "2",
      "memory": "2Gi"
    }
  },
  "securityContext": {
    "allowPrivilegeEscalation": false,
    "readOnlyRootFilesystem": false,
    "runAsNonRoot": true,
    "capabilities": {
      "drop": [
        "ALL"
      ]
    }
  }
}
```
