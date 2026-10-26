# learn-claude-code

一个最小的 Claude Code 类 Agent 学习项目：工具调用 + Hook 机制 + 技能按需加载。

## 目录结构

```
src/learn_claude_code/
├── __init__.py   # 包入口，导出 main
├── config.py     # 环境变量、工作目录、Anthropic 客户端、模型名
├── skills.py     # 扫描 skills/*/SKILL.md，生成目录清单，按需加载全文
├── tools.py      # 工具实现 + JSON Schema + 名称到处理函数的映射
├── hooks.py      # Hook 注册表 + 权限校验/日志等内置 Hook
├── agent.py      # Agent 循环：调用模型 → 执行工具 → 回填结果
└── cli.py        # 交互式 REPL
```

数据流：

```
用户输入 → cli.main → agent_loop
                         │  client.messages.create(system, tools)
                         ↓
                    tool_use ──> execute_tool
                                   ├─ PreToolUse hooks（权限、日志）
                                   ├─ TOOL_HANDLERS[name](**input)
                                   └─ PostToolUse hooks（大输出告警）
                         ↓
                    tool_result 回填 messages，继续循环
```

## 运行

```bash
uv run learn-claude-code
```

需要在项目根目录的 `.env` 中提供 `ANTHROPIC_API_KEY`、`MODEL_ID`，
可选 `ANTHROPIC_BASE_URL`。

技能放在工作目录下的 `skills/<name>/SKILL.md`，frontmatter 写
`name` 与 `description`。

## 深入阅读

- [上下文压缩：其实就是在"整理一张越来越满的书桌"](docs/context-compaction.md)
