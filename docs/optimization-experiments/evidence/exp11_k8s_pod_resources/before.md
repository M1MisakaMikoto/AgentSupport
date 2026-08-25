# Experiment `exp11_k8s_pod_resources` — phase `before`

- timestamp: `2026-08-25T05:40:38+00:00`
- git head: `307c773`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp11_k8s_pod_resources.py --phase before`

---

## Kubernetes runner Pod resources
| observation | value |
| --- | --- |
| container name | runner |
| resources block present | False |
| resources.requests | {} |
| resources.limits | {} |
| readOnlyRootFilesystem | False |

### rendered container spec (resources section)
```json
{
  "name": "runner",
  "resources": null,
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
