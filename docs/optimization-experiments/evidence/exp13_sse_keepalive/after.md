# Experiment `exp13_sse_keepalive` — phase `after`

- timestamp: `2026-08-25T05:52:39+00:00`
- git head: `307c773`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp13_sse_keepalive.py --phase after`

---

## SSE idle keepalive
| observation | value |
| --- | --- |
| configured keepalive interval | 1s |
| time to first byte | 1079 ms |
| first chunk content | : keepalive |

Before the fix an idle stream sends nothing (first byte never
arrives within a 3s probe window). After the fix a keepalive
comment frame arrives at the configured interval.

## Stream continuity check（keepalive 后事件仍可送达）

在 1s keepalive 配置下连续读取两个 keepalive 帧后，通过服务追加一条事件：

```text
RESULT chunks: 3 keepalives: 2 event frames: 1
  chunk: ': keepalive\n\n'
  chunk: ': keepalive\n\n'
  chunk: 'id: 1\ndata: {"schema_version": "1", ...}'
```

结论：空闲时周期性输出注释帧，事件到达后立即切换为事件帧，流不会因
keepalive 中断。实现采用“不取消 `anext` 任务”的并发等待（`asyncio.wait`
FIRST_COMPLETED），避免 `wait_for` 取消导致生成器永久关闭的问题。
