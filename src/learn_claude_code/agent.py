"""Agent 循环：调用模型 -> 执行工具 -> 把结果喂回去 -> 直到模型不再要工具。

整个项目的核心就是 agent_loop 里那个 while True。
"""

from .context_compact import COMPACTOR, MAX_REACTIVE_RETRIES

from .config import MODEL, client
from .hooks import trigger_hooks
from .skills import SYSTEM
from .tools import TOOL_HANDLERS, TOOLS


def execute_tool(block) -> str:
    """执行单个 tool_use 块，并跑完它前后的 Hook。"""
    # PreToolUse 返回非空字符串 = 被拒绝，直接把这个原因当作工具结果返回。
    blocked = trigger_hooks("PreToolUse", block)
    if blocked:
        return str(blocked)

    handler = TOOL_HANDLERS.get(block.name)
    try:
        # 模型给的参数直接按关键字展开传给函数，字段名与函数形参名一致。
        output = handler(**block.input) if handler else f"Unknown: {block.name}"
    except Exception as e:
        # 工具内部抛异常也要变成文本回给模型，不能让它把整个循环打断。
        output = f"Error: {e}"

    # PostToolUse 只做观察/告警，返回值被忽略，不影响工具结果。
    trigger_hooks("PostToolUse", block, output)
    return str(output)


def agent_loop(messages: list, active_request: str):
    """带上下文管理的 agent 循环。

    与基础版的区别只有三处，其余流程一模一样：
      1. 每次请求前调 COMPACTOR.prepare 主动压缩；
      2. 请求真超长时兜底 reactive_compact 并重试；
      3. 支持模型主动调用 compact 工具。
    """
    # active_request：本轮用户原始输入。历史被摘要压缩后，模型还能靠它记住"现在要干什么"。
    # reactive_retries：本轮已经因"超长"重试过几次，用完就放弃，避免死循环。
    reactive_retries = 0
    while True:
        # 请求前的"事前压缩"：按代价从低到高逐级尝试，
        #   截断最新工具结果 -> 归档中间消息 -> 替换旧结果 -> 再截断 -> 整段摘要。
        # 具体每一级见 context_compact.py 的 prepare()。
        # 用 messages[:] = ... 原地替换，保证 cli.py 持有的 history 还是同一个列表对象。
        messages[:] = COMPACTOR.prepare(messages, active_request)
        try:
            response = client.messages.create(
                model=MODEL, system=SYSTEM, messages=messages,
                tools=TOOLS, max_tokens=8000,
            )
            # 请求成功说明长度已回到安全区，重试计数归零，
            # 这样以后某轮再超长时，还能再获得一次补救机会。
            reactive_retries = 0
        except Exception as error:
            # prepare 里的字符数是"粗估"，不同模型的 token 换算也不一样；
            # 真超长时 API 会明确报 prompt_too_long / too many tokens。
            too_long = any(text in str(error).lower()
                           for text in ("prompt_too_long", "too many tokens"))
            if too_long and reactive_retries < MAX_REACTIVE_RETRIES:
                # "事后补救"：保留最近几条消息，把更早的历史压成一条摘要，然后重试。
                print("[reactive compact]")
                messages[:] = COMPACTOR.reactive_compact(messages, active_request)
                reactive_retries += 1
                continue
            # 不是长度问题，或者已经补救过一次：继续抛，别把真正的错误吞掉。
            raise

        # 原样保存 assistant 响应，tool_use_id 才能和下面的 tool_result 对上。
        messages.append({"role": "assistant", "content": response.content})
        tool_calls = [
            block for block in response.content if block.type == "tool_use"
        ]
        if not tool_calls:
            # 模型不请求工具，默认结束；Stop Hook 返回内容时可强制继续。
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return

        results = []
        compact_requested = False
        for block in tool_calls:
            # 在终端显示正在调用哪个工具，长任务时能看出进度。
            print(f"\033[36m> {block.name}\033[0m")
            # compact 是"伪工具"：没有真正的处理函数，
            # 这里只回一句确认，等这批工具全部执行完再真正压缩。
            if block.name == "compact":
                output = "Compaction requested after this tool batch."
                compact_requested = True
            else:
                output = execute_tool(block)
                # 只打印前 200 字符预览，避免一条命令刷满整个终端。
                print(output[:200])
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": output})

        messages.append({"role": "user", "content": results})
        # 关键顺序：先写入 tool_result，再压缩。
        # 如果反过来，assistant 里的 tool_use 可能被摘要掉，
        # 剩下的 tool_result 就成了没有配对的"孤儿"，API 会直接报错。
        if compact_requested:
            messages[:] = COMPACTOR.compact_history(messages, active_request)