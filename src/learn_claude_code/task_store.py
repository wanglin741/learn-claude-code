"""任务系统：把"要做什么、做到哪了、谁挡住了谁"存成磁盘上的 JSON 文件。

    .tasks/
      task_a1b2c3d4.json  {status: completed, blockedBy: []}
      task_e5f6a7b8.json  {status: pending,   blockedBy: [task_a1b2c3d4]}

一条任务 = 一个文件，文件名就是任务 ID。这样做的好处：

    - 增删改是文件级操作，不需要读改写一整块数据；
    - 人和工具都能直接打开看，出问题时能手修；
    - 一条任务文件损坏，不影响其他任务。

任务状态机很简单，只有三步：

    pending --claim_task--> in_progress --complete_task--> completed
       ^                                                      |
       +-----------------（不能倒退）-------------------------+

依赖是"有向无环图"：A blockedBy B 表示"B 完成之后 A 才能开工"。
这里没有引入图算法库，只有两个小函数：
    _depends_on()        判断传递依赖，用来挡住环；
    incomplete_dependencies() 判断"还差谁没做完"，决定能不能 claim。

模块分两层：
    TaskStore      —— 纯存储层，只管读写文件、校验 ID、维护索引；
    create_task 等 —— 业务层，把状态机的每一步写清楚，顺便打印给终端看。
工具层（tools.py）只负责调这些函数并把结果转成字符串给模型。
"""

import json
import re
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import WORKDIR

# 任务都存在工作目录下的 .tasks/，跟着项目走，换机器也还在。
TASKS_DIR = WORKDIR / ".tasks"

# 任务 ID 只接受 task_ + 8 位十六进制。
# 用正则而不是直接拼路径，是因为 ID 最终会变成文件名：
# 一旦允许 "../" 之类的字符，就等于允许模型写文件到项目外面。
TASK_ID_PATTERN = re.compile(r"^task_[0-9a-f]{8}$")


@dataclass
class Task:
    """一条任务的全部持久化字段。

    字段名故意保留 camelCase（blockedBy），因为工具 Schema 里也叫这个名字，
    模型传参时可以直接对上，不用做名字转换。
    """

    id: str
    subject: str          # 一句话标题，显示在待办列表里
    description: str      # 详细说明，可以留空
    status: str           # pending / in_progress / completed
    owner: str | None     # 认领者，None 表示还没人做
    blockedBy: list[str]  # 依赖的任务 ID 列表


class TaskStore:
    """任务文件的读写层。所有路径都必须落在工作目录内。"""

    def __init__(self, directory: Path):
        self.directory = directory

    def _root(self, create: bool = False) -> Path:
        """返回任务目录；create=True 时顺便建目录。"""
        if create:
            self.directory.mkdir(parents=True, exist_ok=True)
        root = self.directory.resolve()
        # 目录本身也不能跑出工作目录（比如被软链接指到外面）。
        if not root.is_relative_to(WORKDIR.resolve()):
            raise ValueError("Task store escapes the workspace")
        return root

    def _path(self, task_id: str, create_root: bool = False) -> Path:
        """把任务 ID 变成文件路径，同时做格式和越界两道校验。"""
        if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
            raise ValueError(f"Invalid task ID: {task_id!r}")
        root = self._root(create=create_root)
        path = (root / f"{task_id}.json").resolve()
        # 格式校验已经挡住了 "../"，这里再确认一次解析结果没跑出去。
        if not path.is_relative_to(root):
            raise ValueError(f"Invalid task ID: {task_id!r}")
        return path

    def exists(self, task_id: str) -> bool:
        return self._path(task_id).is_file()

    def create(self, subject: str, description: str = "") -> Task:
        """新建任务：ID 随机生成，用 "x" 模式写入，天然避免覆盖已有文件。"""
        subject = subject.strip()
        if not subject:
            raise ValueError("Task subject cannot be empty")

        self._root(create=True)
        # 随机 ID 理论上可能撞，重试 100 次还撞就算异常（概率可以忽略）。
        for _ in range(100):
            task = Task(
                id=f"task_{secrets.token_hex(4)}",
                subject=subject,
                description=description,
                status="pending",
                owner=None,
                blockedBy=[],
            )
            try:
                # "x" = 独占创建，文件已存在会抛 FileExistsError 而不是覆盖。
                with self._path(task.id, create_root=True).open(
                    "x", encoding="utf-8"
                ) as handle:
                    json.dump(asdict(task), handle, indent=2)
                return task
            except FileExistsError:
                continue
        raise RuntimeError("Could not allocate a unique task ID")

    def _depends_on(self, task_id: str, target_id: str) -> bool:
        """task_id 是否（直接或间接）依赖 target_id。

        就是一次 DFS：沿着 blockedBy 一直往上走，能走到 target 就返回 True。
        添加依赖前先问这个问题，就能提前发现"环"：
        如果 B 已经依赖 A，那再让 A 依赖 B 就死锁了。
        """
        pending = [task_id]
        visited = set()
        while pending:
            current = pending.pop()
            if current == target_id:
                return True
            if current in visited:   # 图里已经有环时也要能停下来
                continue
            visited.add(current)
            pending.extend(self.load(current).blockedBy)
        return False

    def update_dependencies(self, task_id: str,
                            add_blocked_by: list[str]) -> Task:
        """给任务追加依赖。只允许在"还没开工"时改，避免进行中的任务被改乱。"""
        if not isinstance(add_blocked_by, list):
            raise ValueError("addBlockedBy must be a list of task IDs")

        task = self.load(task_id)
        if task.status != "pending" or task.owner is not None:
            raise ValueError(
                f"Task {task_id} dependencies can only be updated while "
                "pending and unowned"
            )

        # dict.fromkeys 去重同时保持顺序（模型可能重复传同一个 ID）。
        dependencies = list(dict.fromkeys(add_blocked_by))
        for dependency in dependencies:
            if dependency == task_id:
                raise ValueError("Task cannot depend on itself")
            if not self.exists(dependency):
                raise ValueError(f"Dependency not found: {dependency}")
            # 防环：如果新依赖已经反过来依赖我，加进去就成环了。
            if dependency not in task.blockedBy and self._depends_on(
                dependency, task_id
            ):
                raise ValueError(
                    f"Dependency cycle detected: {task_id} -> {dependency}"
                )

        task.blockedBy.extend(
            dependency for dependency in dependencies
            if dependency not in task.blockedBy
        )
        self.save(task)
        return task

    def save(self, task: Task) -> None:
        self._path(task.id, create_root=True).write_text(
            json.dumps(asdict(task), indent=2),
            encoding="utf-8",
        )

    def load(self, task_id: str) -> Task:
        """读文件 -> Task。顺便校验内容和文件名一致，防止手改出错。"""
        data = json.loads(self._path(task_id).read_text(encoding="utf-8"))
        task = Task(**data)
        if task.id != task_id:
            raise ValueError(f"Task file ID does not match {task_id}")
        if task.status not in ("pending", "in_progress", "completed"):
            raise ValueError(f"Invalid task status: {task.status}")
        return task

    def list(self) -> list[Task]:
        if not self.directory.exists():
            return []
        root = self._root()
        # 文件名排序 = ID 排序，输出稳定，方便对比前后差异。
        return [self.load(path.stem)
                for path in sorted(root.glob("task_*.json"))]


# 全局单例：整个进程共用一个存储对象（目录是固定的）。
TASKS = TaskStore(TASKS_DIR)


# ---- 业务层：状态机的每一步 ----


def create_task(subject: str, description: str = "") -> Task:
    return TASKS.create(subject, description)


def update_task(task_id: str, addBlockedBy: list[str]) -> Task:
    return TASKS.update_dependencies(task_id, addBlockedBy)


def load_task(task_id: str) -> Task:
    return TASKS.load(task_id)


def list_tasks() -> list[Task]:
    return TASKS.list()


def incomplete_dependencies(task: Task) -> list[str]:
    """返回还没完成（或已经消失）的依赖 ID。

    依赖文件不存在时也算"没完成"：宁可挡住，也不要放一个可能出问题的任务过去。
    """
    incomplete = []
    for dependency in task.blockedBy:
        try:
            if load_task(dependency).status != "completed":
                incomplete.append(dependency)
        except (FileNotFoundError, ValueError):
            incomplete.append(dependency)
    return incomplete


def can_start(task_id: str) -> bool:
    """所有依赖都完成了吗？—— 可以开工的唯一条件。"""
    return not incomplete_dependencies(load_task(task_id))


def claim_task(task_id: str, owner: str = "agent") -> str:
    """认领任务：pending -> in_progress。依赖没做完会被拒绝。"""
    task = load_task(task_id)
    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"
    dependencies = incomplete_dependencies(task)
    if dependencies:
        return f"Blocked by: {dependencies}"
    task.owner = owner
    task.status = "in_progress"
    TASKS.save(task)
    print(f"  [claim] {task.subject} -> in_progress (owner: {owner})")
    return f"Claimed {task.id} ({task.subject})"


def complete_task(task_id: str, owner: str = "agent") -> str:
    """完成任务：in_progress -> completed，并告诉调用方"谁因此解锁了"。

    "解锁"要在状态变更前后各算一次：
      完成前就 ready 的任务集合  -> ready_before
      完成后才 ready 的任务     -> 就是本次新解锁的
    这样模型能立刻知道下一步该干什么。
    """
    task = load_task(task_id)
    if task.status != "in_progress":
        return f"Task {task_id} is {task.status}, cannot complete"
    if task.owner != owner:
        return f"Task {task_id} is owned by {task.owner}, not {owner}"

    # 快照：完成之前就已经可以开工的任务。
    ready_before = {
        candidate.id
        for candidate in list_tasks()
        if candidate.status == "pending"
        and candidate.blockedBy
        and can_start(candidate.id)
    }
    task.status = "completed"
    TASKS.save(task)

    # 差值：完成之后新出现的可开工任务。
    unblocked = [candidate.subject for candidate in list_tasks()
                 if candidate.status == "pending"
                 and candidate.blockedBy
                 and candidate.id not in ready_before
                 and can_start(candidate.id)]
    print(f"  [complete] {task.subject}")
    message = f"Completed {task.id} ({task.subject})"
    if unblocked:
        message += f"\nUnblocked: {', '.join(unblocked)}"
        print(f"  [unblocked] {', '.join(unblocked)}")
    return message


if __name__ == "__main__":
    # 自检：依赖图的核心规则（防环、防自依赖、文件落盘）坏没坏。
    import tempfile

    with tempfile.TemporaryDirectory(dir=WORKDIR) as tmp:
        store = TaskStore(Path(tmp) / ".tasks")
        a = store.create("schema", "建表")
        b = store.create("api", "写接口")
        store.update_dependencies(b.id, [a.id])
        assert store.load(b.id).blockedBy == [a.id]
        try:
            store.update_dependencies(a.id, [b.id])   # b 已依赖 a，反向再加就成环
        except ValueError as error:
            assert "cycle" in str(error)
        else:
            raise AssertionError("cycle not detected")
        try:
            store.update_dependencies(a.id, [a.id])
        except ValueError:
            pass
        else:
            raise AssertionError("self dependency not rejected")
        try:
            store._path("../../etc/passwd")
        except ValueError:
            pass
        else:
            raise AssertionError("path escape not rejected")
    print("task_store self-check OK")
