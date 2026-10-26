"""Hook 注册表 + 内置的权限校验 / 日志 Hook。

Hook 就是在固定时机插入的自定义逻辑：
    UserPromptSubmit  用户输入后、发给模型前
    PreToolUse        工具执行前（可以拦截）
    PostToolUse       工具执行后（可以告警 / 记录）
    Stop              模型不再调用工具、准备结束时

关键约定：回调返回 None = 放行；返回非 None（通常是字符串）= 打断流程。
"""

import re

from .config import WORKDIR

# 每个事件挂一串回调，按注册顺序执行。
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}


def register_hook(event: str, callback):
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    """依次执行某事件的所有回调，遇到第一个非 None 返回值就立刻停止并返回。"""
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


# 一票否决的硬黑名单：命令里出现这些子串就直接拒绝，连询问都省了。
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]

# 为什么不用简单的 `"rm" in command`？
# 那样会把 "format"、"firm" 误判成危险命令。这里用正则要求 rm/del
# 出现在命令开头或 ; & | ( ) 换行之后，才算一个独立的命令词。
DESTRUCTIVE_COMMAND_WORD = re.compile(
    r"(?i)(?:^|[;&|()\n])\s*(?:rm|del)(?=\s|$|[;&|()])"
)

# 需要弹窗询问的其它危险特征。
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]


def contains_destructive_command(command: str) -> bool:
    return bool(DESTRUCTIVE_COMMAND_WORD.search(command))


def permission_hook(block):
    """PreToolUse：拦下黑名单命令，危险命令和越界路径则问用户。"""
    if block.name == "bash":
        command = block.input.get("command", "")
        for pattern in DENY_LIST:
            if pattern in command:
                print(f"\n\033[31m[blocked] '{pattern}'\033[0m")
                # 返回字符串 = 拒绝，这句话会作为 tool_result 回给模型。
                return "Permission denied by deny list"
        if contains_destructive_command(command) or any(
            keyword in command for keyword in DESTRUCTIVE
        ):
            print("\n\033[33m[permission] Potentially destructive command\033[0m")
            print(f"   Tool: {block.name}({block.input})")
            # 停在这里等用户按回车，这是唯一的人工确认环节。
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"

    # 文件工具只允许访问工作目录内部，防止模型读写到项目外面的文件。
    if block.name in ("read_file", "write_file", "edit_file"):
        path = block.input.get("path", "")
        if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
            print("\n\033[33m[permission] Access outside workspace\033[0m")
            print(f"   Tool: {block.name}({block.input})")
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
    return None  # 返回 None 表示放行


def log_hook(block):
    """PreToolUse：每次工具调用打一行日志。"""
    # 只取前两个参数、截断到 60 字符，日志才不会刷屏。
    args_preview = str(list(block.input.values())[:2])[:60]
    print(f"\033[90m[HOOK] {block.name}({args_preview})\033[0m")
    return None


def large_output_hook(block, output):
    """PostToolUse：输出过大时提醒一句（不影响结果）。"""
    if len(str(output)) > 100000:
        print(f"\033[33m[HOOK] Large output from {block.name}: {len(str(output))} chars\033[0m")
    return None


def context_inject_hook(query: str):
    """UserPromptSubmit：示例性地打印当前工作目录。

    真实项目可以在这里检索相关文件、拼接上下文，再把结果返回给模型。
    """
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None


def summary_hook(messages: list):
    """Stop：统计本次会话用了多少次工具，作为结束前的收尾日志。"""
    tool_count = sum(
        1
        for message in messages
        # content 可能是字符串（普通消息）或 block 列表（assistant/tool_result）
        for block in (
            message.get("content")
            if isinstance(message.get("content"), list)
            else []
        )
        if isinstance(block, dict) and block.get("type") == "tool_result"
    )
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None


# 注册顺序有讲究：permission_hook 在 log_hook 之前，
# 被拒绝的命令就不会再打一条“已执行”的日志。
register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)
