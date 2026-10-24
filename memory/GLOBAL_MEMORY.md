# 全局记忆（跨会话）

与 `memory/groups/<群号>.json` 的**群级**记忆不同，`memory/global.json` 里的内容会注入到**每一次**请求：私聊、所有群聊，以及图搜图等由提示词驱动的功能。

适合放：身份、固定称呼/别名、跨所有会话都成立的偏好。
不适合放：只在某个群成立的背景（那是群记忆）、说话风格（那是 `personality/style.txt`）、可被成员单方面写入的声明。

## 格式

```json
{
  "schema_version": 1,
  "scope": "global",
  "entries": [
    {
      "id": "example-alias",
      "topic": "称呼",
      "content": "示例：A 是 B 的简称，两者指同一人。",
      "source": "人工确认",
      "updated_at": "2026-10-21"
    }
  ]
}
```

- `topic` 可选，渲染成 `- 称呼：…`；省略则渲染成 `- …`。
- 单条最多 300 字；整体块最多 1000 字（`memory/global_memory.py` 的 `MAX_PROMPT_CHARS`），超出部分截断。
- 文件缺失或 JSON 损坏时**静默跳过**（只记一条 warning），不会影响机器人启动。

## 生效方式

`ai/prompt.py` 的 `build_system_prompt()` 每次生成回复都会重新读取，所以**改完立即生效，无需重启**。
（改的是 `memory/global_memory.py` 这个**代码**才需要重启。）

运行数据 `memory/global.json` 不会被提交：已在 `.gitignore` 中排除，与群记忆一致。
