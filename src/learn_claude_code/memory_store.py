"""记忆存储层：一个目录、一条记忆一个文件、一个自动生成的索引。

    .memory/
    ├── MEMORY.md                 <- 索引，代码生成，不手改
    ├── user-prefers-pytest.md    <- 一条记忆一个文件
    └── build-uses-uv.md

单条记忆的格式固定：YAML frontmatter 放元数据，正文放内容。

    ---
    name: user prefers pytest
    description: 用户所有 Python 项目都用 pytest
    type: user
    ---

    用户明确说过两次：所有 Python 项目用 pytest ...

这一层只管"怎么存、怎么读、怎么防越界"，不涉及 LLM。
要不要存（抽取）、存哪条（召回）、什么时候合并（整理）在 memory.py 里。

本模块做的三件事：

    1. 统一的路径入口 memory_path()，挡住模型生成的恶意文件名；
    2. 写入前的准入判断 should_store_memory()，挡住临时信息和重复信息；
    3. 每次写完自动重建 MEMORY.md，保证索引和文件永远一致。
"""

import re
from pathlib import Path

import yaml

from .config import WORKDIR

MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"

# 四种记忆类型，决定了"这条记的是谁的事"。
MEMORY_TYPES = ("user", "feedback", "project", "reference")

# 出现这些词就说明说的是"当前这次"，不是长久事实，一律不存。
# 中英日都列上，因为模型可能用任意语言复述用户原话。
TEMPORARY_MEMORY_MARKERS = (
    "this session",
    "current session",
    "this turn",
    "current turn",
    "this task",
    "current task",
    "for now",
    "just this time",
    "today only",
    # 中文
    "本次会话",
    "当前会话",
    "这一轮",
    "当前轮次",
    "本次任务",
    "当前任务",
    "暂时",
    # 日文
    "今回だけ",
    "このセッション",
    "現在のタスク",
)


# ---- 文件格式 ----


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """把文件拆成 (元数据, 正文)。格式不规范时退回 ({}, 全文)，不让程序崩。"""
    if not text.startswith("---\n"):
        return {}, text
    # 最多切 3 段：第 0 段是空串，第 1 段是 YAML，第 2 段是正文（正文里可能有 ---）。
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        metadata = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(metadata, dict):
        return {}, text
    return metadata, parts[2].lstrip()


def memory_slug(name: str) -> str:
    """把记忆名字变成安全的文件名（不含路径分隔符、空格等）。"""
    slug = re.sub(r"[^\w]+", "-", name.lower()).strip("-_")
    return slug or "memory"


def memory_path(filename: str, allow_index: bool = False) -> Path:
    """所有记忆读写的唯一入口：校验文件名，并确保解析后仍在 .memory/ 内。

    名字来自模型输出，所以必须假设它可能是 "../x" 或带斜杠的东西：
      - 带路径的分隔符直接拒绝；
      - resolve() 之后必须仍是 .memory 的子路径（防软链接）。
    """
    if Path(filename).name != filename:
        raise ValueError(f"Invalid memory filename: {filename}")
    if filename == MEMORY_INDEX.name and not allow_index:
        raise ValueError("The memory index is not a memory record")

    root = MEMORY_DIR.resolve()
    if not root.is_relative_to(WORKDIR.resolve()):
        raise ValueError("Memory directory escapes the workspace")
    path = (root / filename).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Memory path escapes the store: {filename}")
    return path


def _normalized_memory_text(value: str) -> str:
    """去重用的小工具：忽略大小写和多余空白，只比较"意思"。"""
    return " ".join(value.lower().split())


# ---- 准入判断 ----


def should_store_memory(candidate: dict, existing: list[dict]) -> bool:
    """候选记忆能不能落盘？依次过五道门，任何一道不过就丢弃。

    模型已经按提示词筛过一遍了，这里再筛一遍是因为模型会犯错：
      - 把"这次用 Python 3.13 跑"当成长久事实；
      - 换个名字把同一条记忆再说一遍。
    每道门都很便宜，所以宁可多检查几次。
    """
    if not isinstance(candidate, dict):
        return False
    # 门 1：只收持久记忆。模型把临时信息标成 current_task 就到此为止。
    if candidate.get("scope") != "persistent":
        return False
    # 门 2：类型必须是四种之一，其他类型（比如 "summary"）一律不收。
    if candidate.get("type") not in MEMORY_TYPES:
        return False

    name = str(candidate.get("name", "")).strip()
    description = str(candidate.get("description", "")).strip()
    body = str(candidate.get("body", "")).strip()
    # 门 3：三个字段都不能空，否则写出来的文件没有意义。
    if not name or not description or not body:
        return False

    # 门 4：正文里出现"本次/暂时"之类的词，说明说的是当前会话。
    candidate_text = _normalized_memory_text(f"{name}\n{description}\n{body}")
    if any(marker in candidate_text for marker in TEMPORARY_MEMORY_MARKERS):
        return False

    # 门 5：名字 slug、描述、正文任意一项和已有记忆重复，就不重复存。
    slug = memory_slug(name)
    normalized_description = _normalized_memory_text(description)
    normalized_body = _normalized_memory_text(body)
    for memory in existing:
        if memory_slug(str(memory.get("name", ""))) == slug:
            return False
        if _normalized_memory_text(
            str(memory.get("description", ""))
        ) == normalized_description:
            return False
        if _normalized_memory_text(str(memory.get("body", ""))) == normalized_body:
            return False
    return True


# ---- 写 ----


def memory_document(name: str, mem_type: str, description: str, body: str) -> str:
    """拼出文件全文：frontmatter + 空行 + 正文。"""
    metadata = yaml.safe_dump(
        {"name": name, "description": description, "type": mem_type},
        sort_keys=False,
        allow_unicode=True,   # 中文原样写，不要转成 \uXXXX
    ).strip()
    return f"---\n{metadata}\n---\n\n{body.strip()}\n"


def write_memory_file(name: str, mem_type: str,
                      description: str, body: str) -> Path:
    """写一条记忆并重建索引。同名文件会被覆盖（更新即覆盖）。"""
    if not name.strip():
        raise ValueError("Memory name cannot be empty")
    if mem_type not in MEMORY_TYPES:
        raise ValueError(f"Unknown memory type: {mem_type}")
    if not description.strip() or not body.strip():
        raise ValueError("Memory description and body cannot be empty")

    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    path = memory_path(f"{memory_slug(name)}.md")
    path.write_text(
        memory_document(name, mem_type, description, body), encoding="utf-8"
    )
    rebuild_memory_index()
    return path


def rebuild_memory_index() -> None:
    """扫描 .memory/*.md 重新生成 MEMORY.md。

    索引永远由代码生成：只要有一条记忆变了，就整份重写，
    这样索引和文件不可能对不上。每行格式：
        - [名字](文件名) - 描述
    """
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    lines = []
    for path in sorted(MEMORY_DIR.glob("*.md")):
        if path.name == MEMORY_INDEX.name:   # 索引自己不算记忆
            continue
        try:
            path = memory_path(path.name)    # 再校验一次（防软链接混进来）
        except ValueError:
            continue
        metadata, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        name = " ".join(str(metadata.get("name") or path.stem).split())
        # 没写 description 就用正文第一行兜底。
        first_line = next((line for line in body.splitlines() if line.strip()), "")
        description = " ".join(
            str(metadata.get("description") or first_line).split()
        )
        lines.append(f"- [{name}]({path.name}) - {description}")
    memory_path(MEMORY_INDEX.name, allow_index=True).write_text(
        "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
    )


# ---- 读 ----


def read_memory_index() -> str:
    """索引全文（几十行），拼进 system prompt 做"目录页"。"""
    try:
        path = memory_path(MEMORY_INDEX.name, allow_index=True)
    except ValueError:
        return ""
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def read_memory_file(filename: str) -> str | None:
    """读一条记忆的完整内容；文件名非法或文件不存在返回 None。"""
    try:
        path = memory_path(filename)
    except ValueError:
        return None
    return path.read_text(encoding="utf-8") if path.is_file() else None


def list_memory_files() -> list[dict]:
    """列出所有记忆的结构化信息（不含索引），供召回和去重使用。"""
    records = []
    if not MEMORY_DIR.exists():
        return records
    for path in sorted(MEMORY_DIR.glob("*.md")):
        if path.name == MEMORY_INDEX.name:
            continue
        try:
            path = memory_path(path.name)
        except ValueError:
            continue
        metadata, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        records.append({
            "filename": path.name,
            "name": str(metadata.get("name") or path.stem),
            "description": str(metadata.get("description") or ""),
            "type": str(metadata.get("type") or "project"),
            "body": body.strip(),
        })
    return records


if __name__ == "__main__":
    # 自检：五道门和路径校验——写错一条会被"记住"，很难回头查。
    existing = [{
        "name": "User prefers pytest",
        "description": "所有 Python 项目都用 pytest",
        "body": "用 pytest，不要 unittest",
    }]
    ok = {"name": "Build uses uv", "type": "project", "scope": "persistent",
          "description": "用 uv 管理依赖", "body": "不要用 pip install"}
    assert should_store_memory(ok, existing)
    # scope 不是 persistent
    assert not should_store_memory({**ok, "scope": "current_task"}, existing)
    # 临时标记
    assert not should_store_memory(
        {**ok, "body": "这次会话暂时不用缓存"}, existing)
    # 名字重复（slug 相同、大小写不同）
    assert not should_store_memory(
        {**ok, "name": "user prefers pytest"}, existing)
    # 正文重复
    assert not should_store_memory(
        {**ok, "body": "用 pytest，不要 unittest"}, existing)
    # 类型非法
    assert not should_store_memory({**ok, "type": "summary"}, existing)
    # 文件名带路径必须被拒
    for bad in ("../x.md", "a/b.md", "MEMORY.md"):
        try:
            memory_path(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted invalid filename: {bad}")
    print("memory_store self-check OK")
