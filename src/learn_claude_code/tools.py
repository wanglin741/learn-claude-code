"""工具实现、工具的 JSON Schema，以及“工具名 -> 处理函数”的映射。

模型只会返回“我想调用 read_file，参数是 {...}”，真正干活的是这里的函数。
每个函数都遵循同一个约定：参数是模型给的值，返回值是字符串（会原样回传给模型），
出错也返回字符串而不是抛异常，这样模型能看到错误并自行调整。
"""

import glob
import json
import subprocess
from dataclasses import asdict

from .config import WORKDIR
from .skills import SKILL_LOADER
from .task_store import (claim_task, complete_task, create_task, list_tasks,
                         load_task, update_task)


def run_bash(command: str) -> str:
    """执行 shell 命令。Windows 下实际是 cmd.exe。"""
    try:
        result = subprocess.run(
            command, shell=True, cwd=WORKDIR,  # 统一在工作目录下执行
            capture_output=True, text=True, errors="replace",  # 编码有问题用替换字符，不崩
            timeout=120,  # 防止模型跑出死循环命令挂死整个会话
        )
        # stdout 和 stderr 合并返回，模型只关心“有没有报错”。
        output = (result.stdout + result.stderr).strip()
        # 截断超长输出，避免把上下文窗口撑爆。
        return output[:50000] if output else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None) -> str:
    """读取文件，limit 指定时只返回前 N 行，并在末尾注明还剩多少行。"""
    try:
        # 相对路径一律相对 WORKDIR 解析；resolve() 会顺便把 ../ 这样的路径规范化。
        lines = (WORKDIR / path).resolve().read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    """写入文件（会覆盖原内容）。父目录不存在时自动创建。"""
    try:
        file_path = (WORKDIR / path).resolve()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    """精确文本替换：只替换第一次出现的位置。"""
    try:
        file_path = (WORKDIR / path).resolve()
        text = file_path.read_text(encoding="utf-8")
        # 找不到就明确报错，不做模糊匹配——静默替换到错误位置比失败更糟。
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str) -> str:
    """按 glob 查文件，** 表示递归匹配。"""
    try:
        # root_dir=WORKDIR：让 pattern 相对工作目录，返回的也是相对路径，模型看着更短。
        matches = sorted({
            match for match in glob.glob(
                pattern, root_dir=WORKDIR, recursive=True)
            # 过滤掉解析后跑到工作目录外面的结果（比如 ../ 或软链接）。
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR)
        })
        # 限制返回条数，避免一次列出整个 node_modules 级别的目录。
        shown = matches[:200]
        if len(matches) > 200:
            shown.append("... (more matches omitted; narrow the pattern)")
        return "\n".join(shown) if shown else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


# 发给模型的工具声明：名字、描述、参数 JSON Schema。
# 描述写得好不好，直接决定模型会不会正确调用。
BASE_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern; ** matches recursively.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "load_skill", "description": "Load the full SKILL.md content by skill name.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
]
COMPACT_TOOL={
    "name": "compact",
    "description": "Summarize earlier conversation to free context space.",
    "input_schema": {"type": "object", "properties": {}},
}

# 注意要展开 BASE_TOOLS：API 要的是 [工具, 工具, ...]，
# 写成 [BASE_TOOLS, COMPACT_TOOL] 会变成嵌套列表，请求直接报错。
# ---- 任务工具 ----
# 任务系统真正的存储和依赖图在 task_store.py，这里只是"工具层"：
# 把领域操作包成模型能调用的函数，并把结果格式化成字符串。
# 返回值都会作为 tool_result 回给模型，所以要说人话、给 ID、给出下一步线索。


def run_create_task(subject: str, description: str = "") -> str:
    """新建一个任务节点。ID 由程序生成（模型不能自己编），返回给模型备用。"""
    task = create_task(subject, description)
    print(f"  [create] {task.subject}")
    return f"Created {task.id}: {task.subject}"


def run_update_task(task_id: str, addBlockedBy: list[str]) -> str:
    """给任务加依赖。只能加在还没开工的任务上，加的时候会检查成环。"""
    task = update_task(task_id, addBlockedBy)
    dependencies = ", ".join(task.blockedBy) or "(none)"
    print(f"  [update] {task.subject} blockedBy: {dependencies}")
    return f"Updated {task.id} blockedBy: {dependencies}"


def run_list_tasks() -> str:
    """把全部任务排成待办清单：一眼看出状态、认领者、依赖。"""
    tasks = list_tasks()
    if not tasks:
        return "No tasks. Use create_task to add some."
    lines = []
    for task in tasks:
        marker = {
            "pending": "[ ]",
            "in_progress": "[>]",
            "completed": "[x]",
        }.get(task.status, "[?]")
        dependencies = (
            f" (blockedBy: {', '.join(task.blockedBy)})"
            if task.blockedBy else ""
        )
        owner = f" [{task.owner}]" if task.owner else ""
        lines.append(
            f"{marker} {task.id}: {task.subject} "
            f"[{task.status}]{owner}{dependencies}"
        )
    return "\n".join(lines)


def run_get_task(task_id: str) -> str:
    """查看单条任务的完整 JSON（含 description）。"""
    return json.dumps(asdict(load_task(task_id)), indent=2)


TASK_TOOLS = [
    {"name": "create_task", "description": "Create a task and return its runtime-generated ID.",
     "input_schema": {"type": "object", "properties": {"subject": {"type": "string"}, "description": {"type": "string"}}, "required": ["subject"], "additionalProperties": False}},
    {"name": "update_task", "description": "Add dependencies using IDs returned by create_task.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string", "pattern": "^task_[0-9a-f]{8}$"}, "addBlockedBy": {"type": "array", "items": {"type": "string", "pattern": "^task_[0-9a-f]{8}$"}, "minItems": 1}}, "required": ["task_id", "addBlockedBy"], "additionalProperties": False}},
    {"name": "list_tasks", "description": "List tasks with status, owner, and dependencies.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_task", "description": "Get a task by ID.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    {"name": "claim_task", "description": "Claim a pending task whose dependencies are complete.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    {"name": "complete_task", "description": "Complete the task claimed by this agent.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
]

TOOLS = [*BASE_TOOLS, *TASK_TOOLS, COMPACT_TOOL]

# 分发用的映射表：agent 拿到 tool_use 后按 block.name 查这里。
# 参数 schema 里的字段名和函数形参名一一对应，所以可以直接 **block.input。
TOOL_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
    "load_skill": SKILL_LOADER.load,
    "create_task": run_create_task,
    "update_task": run_update_task,
    "list_tasks": run_list_tasks,
    "get_task": run_get_task,
    "claim_task": claim_task,
    "complete_task": complete_task,
}
