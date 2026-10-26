"""技能目录：扫描 skills/*/SKILL.md，生成提示词里的目录，按需加载全文。

核心思想（也是这一课的重点）：
    启动时只把每个技能的“名字 + 一句话描述”放进系统提示词，省 token；
    模型真正需要某个技能时，再调用 load_skill 工具读取完整正文。
"""

from pathlib import Path

import yaml

from .config import ENVIRONMENT_PROMPT, SKILLS_DIR, WORKDIR


class SkillLoader:
    def __init__(self, skills_dir: Path):
        self.skills_dir:dict = skills_dir
        # 结构：{技能名: {"name": ..., "description": ..., "content": 原始全文}}
        self.skills: dict[str, dict[str, str]] = {}
        self.scan()

    @staticmethod
    def parse_frontmatter(text: str) -> tuple[dict, str]:
        """拆出 SKILL.md 顶部的 YAML frontmatter，返回 (元数据, 正文)。

        约定格式：
            ---
            name: pdf
            description: 读取 PDF 文件
            ---
            正文...
        """
        lines = text.splitlines(keepends=True)
        # 第一行不是 --- 就当作没有 frontmatter，整段都是正文。
        if not lines or lines[0].rstrip("\r\n") != "---":
            return {}, text

        # 从第二行开始找下一个 --- 作为结束标记；找不到说明格式不完整，同样退回整段正文。
        closing_index = next(
            (index for index, line in enumerate(lines[1:], start=1)
             if line.rstrip("\r\n") == "---"),
            None,
        )
        if closing_index is None:
            return {}, text

        frontmatter = "".join(lines[1:closing_index])
        body = "".join(lines[closing_index + 1:]).strip()
        try:
            metadata = yaml.safe_load(frontmatter) or {}
        except yaml.YAMLError:
            # frontmatter 写坏了不让整个技能消失，降级成“无元数据”。
            metadata = {}
        # YAML 可能解析出字符串/列表等，不是 dict 就当没有元数据。
        if not isinstance(metadata, dict):
            metadata = {}
        return metadata, body

    def scan(self):
        """重新扫描磁盘上的技能目录（进程内只会在启动时调用一次）。"""
        self.skills.clear()
        if not self.skills_dir.exists():
            return

        skills_root = self.skills_dir.resolve()
        for manifest in sorted(self.skills_dir.glob("*/SKILL.md")):
            # 两道保险：必须是普通文件，且解析后仍在 skills 目录内。
            # 后者防止软链接指向目录外的文件。
            if (not manifest.is_file()
                    or not manifest.resolve().is_relative_to(skills_root)):
                continue
            content = manifest.read_text(encoding="utf-8")
            metadata, body = self.parse_frontmatter(content)

            # 名字优先级：frontmatter 的 name > 目录名
            raw_name = metadata.get("name")
            name = raw_name.strip() if isinstance(raw_name, str) else ""
            name = name or manifest.parent.name

            # 描述优先级：frontmatter 的 description > 正文第一行。
            # 描述会被拼进系统提示词，所以还要把换行、多余空白压成一行。
            raw_description = metadata.get("description")
            description = (raw_description.strip()
                           if isinstance(raw_description, str) else "")
            description = description or body.split("\n", 1)[0]
            description = " ".join(str(description).lstrip("# ").split())

            self.skills[name] = {
                "name": name,
                "description": description,
                "content": content,  # 全文先放内存，等 load() 时才交给模型
            }

    def catalog(self) -> str:
        """给系统提示词用的目录：每行一个“名字: 描述”，不含正文。"""
        if not self.skills:
            return "(no skills found)"
        return "\n".join(
            f"- {skill['name']}: {skill['description']}"
            for skill in self.skills.values()
        )

    def load(self, name: str) -> str:
        """load_skill 工具的实际处理函数：返回完整 SKILL.md。"""
        skill = self.skills.get(name)
        if skill:
            return skill["content"]
        # 名字写错时把可选列表告诉模型，方便它自己纠正后重试。
        available = ", ".join(self.skills) or "none"
        return f"Error: Unknown skill '{name}'. Available: {available}"


# 全局单例：模块被 import 时就扫描一次磁盘。
# 注意这是一份快照，运行期间新增的技能不会自动出现在目录里。
SKILL_LOADER = SkillLoader(SKILLS_DIR)


def build_system_prompt() -> str:
    """拼系统提示词：身份 + 环境 + 技能目录 + 使用说明。"""
    return (
        f"You are a coding agent at {WORKDIR}. Environment: {ENVIRONMENT_PROMPT}. "
        "Use tools to solve tasks. "
        "Act, don't explain.\n\n"
        f"Skills available:\n{SKILL_LOADER.catalog()}\n\n"
        "Use load_skill to read the full instructions when a skill applies."
    )


# 系统提示词在导入时构建一次，之后每轮对话复用同一个字符串。
SYSTEM = build_system_prompt()
