# Trae Agent upstream

- Project: `trae-agent`
- Version: `0.1.0`
- Source snapshot: `E:\PythonProject\WorkBranch\.tools\trae-agent-src`
- License: MIT, retained in `LICENSE`

This directory is a controlled source snapshot used by the Session Runner image. Platform code
does not modify Trae's agent loop directly; integration is performed through the documented
`BaseAgent._tool_caller` boundary.

## Local patches

- `trae_agent/utils/llm_clients/anthropic_client.py`: preserve `thinking` content blocks in the
  recorded assistant message so extended-thinking tool calls echo the blocks (with signatures)
  back to the API. Anthropic-compatible providers (e.g. DeepSeek) reject the follow-up message
  otherwise. Re-apply when syncing a newer snapshot.
