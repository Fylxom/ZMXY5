"""任务框架的入口：任务文件、界面和命令行都从这里 import。

taskdef.py   结果类型、异常、@task 注册表
activity.py  运行中的任务登记、三种停止
session.py   Session（受控发包）、TaskContext、Account
runner.py    run_account / run_batch
"""

from .activity import active_tasks, engaged, force_stop, stop_after
from .runner import (hosting, inject, pending, read_auto, read_clock, read_info, run_account, run_batch, task_order)
from .session import Account, Session, TaskContext, TeamLink, login_lock, wanted
from .taskdef import (BAG_FULL_HINT, BLOCKED, DISPLAY_ORDER, ERROR, FORCE_STOP_HINT, PAUSED, PROGRESS, REGISTRY, SKIPPED,
                      SUBMITTED, TODO, UNCERTAIN, Choices, ForceStop, GuardViolation, Pick, TaskResult, TaskSpec,
                      UncertainOutcome, display_rank, is_bag_full, is_kicked, load_tasks, run_order, task)
