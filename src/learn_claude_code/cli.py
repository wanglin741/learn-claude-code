"""交互式 REPL 入口：读一行输入，丢给 agent，打印最终回复。"""

# readline 是标准库，作用是让 input() 支持方向键、历史记录。
# 下面几行是针对中文/多字节输入的配置，否则退格会删半个汉字。
try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    # Windows 上可能没有 readline，没有也能正常运行，只是少了行编辑功能。
    pass

from .agent import agent_loop
from .hooks import trigger_hooks


def main() -> None:
    print("s07: Skill Loading - catalog first, full content on demand")
    print("Enter a question, press Enter to send. Type q to quit.\n")

    # 对话历史。agent_loop 会往里追加 assistant / tool_result 消息，
    # 所以同一个进程内的多轮输入共享上下文。
    history = []
    while True:
        try:
            # \001/\002 告诉 readline：中间的 ANSI 颜色码不占显示宽度，
            # 否则光标位置会算错，输入中文时尤其明显。
            query = input("\001\033[36m\002s07 >> \001\033[0m\002")
        except (EOFError, KeyboardInterrupt):
            # Ctrl+C / Ctrl+D 直接退出，不打印堆栈。
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        # agent_loop 内部会一直循环到模型不再请求工具为止。
        # active_request 传本轮原始输入，压缩上下文时要用它记住"当前要做什么"。
        agent_loop(history, query)
        # 循环结束后，history 最后一条是 assistant 消息，把其中的文本块打印出来。
        # tool_use 块没有 .text 属性，所以用 getattr 过滤掉。
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
