"""运行时配置：环境变量、工作目录、API 客户端。

这是整个项目的"地基"，其他模块只从这里取配置，不自己读环境变量。
"""

import os
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

# 读取项目根目录的 .env；override=True 表示 .env 里的值覆盖进程中的同名变量，
# 避免 shell 里残留的旧 KEY 干扰调试。
load_dotenv(override=True)

# 用自定义网关（ANTHROPIC_BASE_URL）时，SDK 会优先用 AUTH_TOKEN 认证；
# 这里把 AUTH_TOKEN 清掉，强制走 ANTHROPIC_API_KEY，防止两套凭证互相打架。
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 所有工具的相对路径都以“启动进程时的当前目录”为根。
# 想换工作目录，就在启动 Python 前 cd 过去，而不是改这里。
WORKDIR = Path.cwd()

# 技能目录约定：<工作目录>/skills/<技能名>/SKILL.md
SKILLS_DIR = WORKDIR / "skills"

# base_url 为 None 时 SDK 用官方地址；有值则走自定义网关。
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))

# 模型名必须由 .env 提供。缺失时启动就抛 KeyError，
# 好过静默用错模型跑出一堆奇怪结果。
MODEL = os.environ["MODEL_ID"]

# 把操作系统的差异写进系统提示词：Windows 下 subprocess 的 shell=True 走的是
# cmd.exe，而不是 bash，模型必须知道这一点才不会写出 `ls -la` 这类命令。
ENVIRONMENT_PROMPT = (
    "Windows: the bash tool runs through cmd.exe; use cmd.exe syntax, not Unix "
    "Bash or PowerShell syntax, and prefer dedicated file tools for file operations"
    if os.name == "nt"
    else "Unix-like: the bash tool runs the system shell"
)
