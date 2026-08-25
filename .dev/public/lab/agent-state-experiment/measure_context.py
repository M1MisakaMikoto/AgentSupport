import json
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def history(turns: int) -> list[dict[str, str]]:
    result = []
    for index in range(turns):
        result.append({"role": "user", "content": f"第 {index + 1} 轮用户消息：" + "需求背景。" * 40})
        result.append({"role": "assistant", "content": f"第 {index + 1} 轮助手消息：" + "分析与结果。" * 80})
    return result


def skill_manifests(count: int) -> list[dict[str, str]]:
    return [
        {
            "id": f"skill-{index + 1}",
            "name": f"skill-{index + 1}",
            "description": "处理特定领域任务。" * 10,
            "version": "1.0.0",
            "content_hash": f"sha256:{index + 1:064x}",
        }
        for index in range(count)
    ]


def skill_sources(count: int) -> list[dict[str, str]]:
    return [
        {
            "name": f"skill-{index + 1}",
            "description": "处理特定领域任务。" * 10,
            "source": "步骤：读取输入、验证约束、调用工具、检查结果。" * 35,
        }
        for index in range(count)
    ]


def tools(count: int) -> list[dict[str, object]]:
    return [
        {
            "name": f"tool_{index + 1}",
            "description": "执行一个受权限控制的平台动作。" * 8,
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "工作区相对路径"},
                    "query": {"type": "string", "description": "查询内容"},
                },
                "required": ["path"],
            },
        }
        for index in range(count)
    ]


def summarized_history(total_turns: int, recent_turns: int) -> dict[str, object]:
    assert total_turns >= recent_turns
    return {
        "older_turn_summary": "早期会话摘要：需求、约束、关键决定和未完成事项。" * 25,
        "recent_messages": history(recent_turns),
        "summarized_turns": total_turns - recent_turns,
    }


def measure(
    name: str,
    turn_count: int,
    skill_count: int,
    tool_count: int,
    include_skill_sources: bool = False,
    recent_turns: int | None = None,
) -> dict[str, object]:
    payload = {
        "conversation_history": (
            history(turn_count)
            if recent_turns is None
            else summarized_history(turn_count, recent_turns)
        ),
        "available_skills": skill_manifests(skill_count),
        "selected_skill_sources": skill_sources(skill_count) if include_skill_sources else [],
        "available_tools": tools(tool_count),
        "current_task": "继续完成当前任务。",
    }
    started = time.perf_counter()
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    serialization_ms = (time.perf_counter() - started) * 1000
    return {
        "name": name,
        "turns": turn_count,
        "skills": skill_count,
        "tools": tool_count,
        "skill_loading": "all_sources" if include_skill_sources else "manifest_only",
        "history_loading": "full" if recent_turns is None else f"summary_plus_{recent_turns}_recent",
        "characters": len(serialized),
        "utf8_bytes": len(serialized.encode("utf-8")),
        "estimated_tokens_chars_div_4": (len(serialized) + 3) // 4,
        "serialization_ms": round(serialization_ms, 3),
    }


def main() -> None:
    cases = [
        ("empty", 0, 0, 0, False, None),
        ("10_turns_20_tools", 10, 0, 20, False, None),
        ("50_turns_20_tools", 50, 0, 20, False, None),
        ("50_turns_10_skill_manifests_20_tools", 50, 10, 20, False, None),
        ("50_turns_10_full_skills_20_tools", 50, 10, 20, True, None),
        ("50_turns_50_skill_manifests_20_tools", 50, 50, 20, False, None),
        ("50_turns_50_full_skills_20_tools", 50, 50, 20, True, None),
        ("50_turns_10_skill_manifests_100_tools", 50, 10, 100, False, None),
        ("optimized_100_turns_50_skill_manifests_20_tools", 100, 50, 20, False, 5),
        ("eager_100_turns_50_full_skills_100_tools", 100, 50, 100, True, None),
    ]
    result = {
        "estimation_note": "Token 数为字符数除以 4 的粗略估算，不代表任何具体模型的账单用量。",
        "cases": [measure(*case) for case in cases],
    }
    output = ROOT / "results" / "context-cost.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
