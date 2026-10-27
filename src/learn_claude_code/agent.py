"""Agent 循环：调用模型 -> 执行工具 -> 把结果喂回去 -> 直到模型不再要工具。

整个项目的核心就是 agent_loop 里那个 while True。这里把它拆成五个编号步骤，
读的时候从上往下顺着看就行：

    每个用户请求进来
        │
        ▼
    ┌─────────────────────────────────────────────┐
    │ ① 压缩上下文   COMPACTOR.prepare()           │  装得下就直接过
    │ ② 召回记忆     memory.augment_system()       │  拼进 system prompt
    │ ③ 请求模型     client.messages.create()      │  超长则兜底重试
    │ ④ 判断出口     没有 tool_use 了？            │  有 Stop Hook 可拦
    │ ⑤ 执行工具     执行 -> 结果塞回 messages     │  然后回到 ①
    └─────────────────────────────────────────────┘

三个外部模块各管一摊，agent.py 只负责"串联"：
    context_compact.py  什么时候压、怎么压
    memory.py           记什么、怎么挑回来
    tools.py            模型能调哪些工具、怎么调
"""

from .config import MODEL, client
from .context_compact import COMPACTOR, MAX_REACTIVE_RETRIES
from .hooks import trigger_hooks
from .memory import after_turn, augment_system
from .skills import SYSTEM as BASE_SYSTEM
from .tools import TOOL_HANDLERS, TOOLS

# 任务系统的使用说明。放在 system prompt 里，而不是工具的 description 里，
# 是因为它约束的是"整体工作方式"：先建完所有节点，再加依赖。
# 模型经常不先 create 就直接 update，这句提示就是为了纠正这个顺序。
TASK_GUIDE = (
    "Use task tools to track dependencies and progress. "
    "Create all task nodes first. After create_task returns runtime-generated "
    "IDs, use update_task with those exact IDs to add dependencies."
)

SYSTEM = f"{BASE_SYSTEM}\n\n{TASK_GUIDE}"


def execute_tool(block) -> str:
    """执行单个 tool_use 块，并跑完它前后的 Hook。

    注意：这个函数不认识 compact —— 那是"伪工具"，由 agent_loop 特殊处理。
    """
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
    """带上下文管理 + 记忆 + 任务的 agent 循环。

    参数：
        messages       整段对话历史（cli.py 里持有着同一个 list 对象）
        active_request 本轮用户的原始输入。历史被摘要压缩后，
                       模型还能靠它记住"现在要干什么"。
    """
    # reactive_retries：本轮因"API 报超长"重试过几次，用完就放弃，避免死循环。
    reactive_retries = 0

    # ② 记忆召回只做一次，放在循环外面：
    #    - 一个用户请求 = 一轮对话，记忆在轮内不会变；
    #    - 召回本身是一次模型调用，每圈都做会把成本翻好几倍。
    # augment_system 会把"记忆目录 + 召回正文 + 优先级规则"接到系统提示词后面。
    system = augment_system(SYSTEM, messages)

    while True:
        # ① 请求前的"事前压缩"：按代价从低到高逐级尝试，
        #   截断最新工具结果 -> 归档中间消息 -> 替换旧结果 -> 再截断 -> 整段摘要。
        #   具体每一级见 context_compact.py 的 prepare()。
        #   用 messages[:] = ... 原地替换，保证 cli.py 持有的 history 还是同一个列表对象。
        messages[:] = COMPACTOR.prepare(messages, active_request)

        # ③ 请求模型。system 是本轮固定的，messages 是滚动增长的。
        try:
            response = client.messages.create(
                model=MODEL, system=system, messages=messages,
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

        # ④ 出口判断：模型不再请求工具，说明这一轮结束了。
        if not tool_calls:
            # Stop Hook 返回内容时可强制继续（比如"别忘了跑测试"）。
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            # 收尾：从本轮对话里抽取值得长期记住的东西。
            # 抽取失败只会打印一行提示，不影响已经拿到的答案。
            after_turn(messages)
            return

        # ⑤ 执行工具：一条 assistant 消息里可能同时请求多个工具，全部执行完再回。
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
