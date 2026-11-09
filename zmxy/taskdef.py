"""任务的定义：结果类型、异常、@task 注册表和显示 / 执行顺序。

每个任务是一个独立函数，用 ``@task`` 注册，并**声明**自己会用到的路由：
``reads``（只读）与 ``writes``（会改变游戏状态）。发包时按声明检查，见 session.py。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Mapping

from . import protocol as p

if TYPE_CHECKING:
    from .session import TaskContext

# ---------------------------------------------------------------- 结果类型

SUBMITTED, SKIPPED, BLOCKED, ERROR, UNCERTAIN = (
    "submitted", "skipped", "blocked", "error", "uncertain",
)
PAUSED = "paused"  # 需要用户处理后继续（如背包满）：停掉本次所有任务，断点保存，点“继续”从这里接着跑
FORCE_STOP_HINT = "已强制停止（用户确认）：正在进行的操作结果未知，请登录游戏核对"
BAG_FULL_HINT = "背包空间不足，已停止所有任务，请清理背包后点「继续」"
BAG_FULL_WORDS = ("背包空间不足", "背包不足", "背包已满", "背包满", "背包容量不足", "物品栏已满", "道具栏已满")
KICK_WORDS = ("服务器踢出了当前会话", "Pomelo 连接尚未完成握手")  # 被顶号后：先收到踢出包，之后再发请求就是“尚未完成握手”
PROGRESS = "progress"  # 仅用于 emit 的实时进度，不是任务的最终结果，不会出现在 run_account 的返回值里
TODO = "todo"  # 待处理：要用户自己去做的事（如换采集材料）。只是日志里的提醒行，不是任务结果，不暂停、不影响后面的任务（见 TaskResult.todo）


def is_bag_full(text: object) -> bool:
    return any(w in str(text) for w in BAG_FULL_WORDS)


def is_kicked(text: object) -> bool:
    return any(w in str(text) for w in KICK_WORDS)


@dataclass(frozen=True)
class TaskResult:
    task: str
    status: str
    detail: str = ""
    profile: Mapping[str, Any] | None = None  # 要写回账号设置的信息（如幻兽列表，供界面选择），不影响状态判断
    progress: Mapping[str, int] | None = None  # 副本任务各关已结算次数 {关卡 id: 次数}（含断点前的）；暂停时存进断点，继续时扣掉
    pending: bool = False  # 任务还有后续：运行中显示“进行中”；中断收束后仍保留标记供断点判断
    todo: str = ""  # 要用户自己处理的事：界面在这次执行的“结束”前另记一行“待处理”，不影响状态判断
    account_profile: Mapping[str, Any] | None = None  # 要写回登录账号设置的信息（与存档 profile 分开）
    popup: Mapping[str, Any] | None = None  # 只在前台界面显示的结果弹窗数据，不写入设置或日志文件
    log: bool = True  # 事件层：仅补充已显示结果的资料时为 False，仍处理保存但不再新增日志
    retry_in: float | None = None  # 过这么多秒再重跑本任务（逃跑禁赛等）：执行器先做后面的任务，到时间再回来；配合 pending=True


class UncertainOutcome(RuntimeError):
    """写请求可能已到达服务器但结果未知；调用方必须停止并让用户核对。

    故意不继承 AllianceError：任务里的 ``except AllianceError`` 不能把它当成普通失败吞掉。
    """


class GuardViolation(p.AllianceError):
    """任务试图发送未声明的路由，或违反消费保护。"""


class ForceStop(BaseException):
    """用户确认的“强制停止”：像结束脚本一样立刻打断正在运行的任务。

    继承 BaseException：任务里的 ``except Exception`` / ``except AllianceError`` 吞不掉它；
    由 ``force_stop`` 在运行任务的线程里异步抛出。"""

# ---------------------------------------------------------------- 任务注册

@dataclass(frozen=True)
class Pick:
    """对话框里的一行附加选择。

    items 按账号（设置里的 dict）生成 [(值, 显示名)]；single=True 是下拉单选，
    否则是方块多选；top=True 放在材料网格上方；in_grid=True 时并入主网格。
    text=True 时使用自由文本输入框；integer=True 只对 number 输入框生效，
    用于要求非负整数的协议参数。
    """

    key: str                                                  # 结果存进 options[任务 id][key]
    label: str
    items: Callable[[Mapping[str, Any]], list[tuple[Any, str]]]
    single: bool = True
    top: bool = False
    default: Any = None                                       # 单选没存过时选中的值（None = “无”）
    blank: bool = True                                        # 单选下拉里是否有“无”这一项
    number: tuple[float, float] | None = None                 # 不为 None：这一行是数字输入框（下限, 上限），留空 = 不设；items 不用
    in_grid: bool = False                                     # 多选并入主网格；仍按 key 单独保存，适合把同一配置块的选项合在一起
    text: bool = False                                        # 自由文本输入框；items 仅为保持 Pick 的统一接口
    integer: bool = False                                     # number 输入框只接受整数


@dataclass(frozen=True)
class Choices:
    """任务可选的项目（如关卡）。GUI 据此自动生成"选择"对话框，选择结果存进账号的 options[任务 id]。"""

    items: Callable[[], list[tuple[int, str]]] | None  # [(值, 显示名)]，按显示顺序；None = 没有网格，只有 picks
    noun: str = "关"
    times: bool = False                          # 是否有"每项次数"输入框
    times_max: int = 99                          # 次数上限
    times_default: int = 1                       # 输入框的初始值；留空 = 不执行
    option: str = "levels"                       # 选择结果在 options 里的键
    toggles: tuple[tuple[str, str], ...] = ()    # 对话框里的开关 [(options 里的键, 文字)]
    toggle_defaults: tuple[tuple[str, bool], ...] = ()  # 未保存过时的开关默认值 [(options 里的键, 默认值)]
    picks: tuple[Pick, ...] = ()                 # 网格下面的附加选择行
    load: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None  # 对话框“读取”按钮：登录后由角色信息算出要存进账号 profile 的内容
    cols: int = 5                                # 网格每行几个方块
    toggles_left: bool = False                   # 开关靠左紧贴网格下方（默认靠右）
    caption: str = ""                            # 网格上方的小标题
    limit: int = 0                               # 最多选几个；1 = 单选（点另一个会替换），0 = 不限
    note: str = ""                               # 附加选择行下面的一行说明（如推荐值）
    pick_total: int = 0                          # 数字型单选行的总和上限（0 = 不限）
    mode_inside: bool = False                    # 任务的 modes 不放在行上，放进对话框里选（世界Boss 的低 / 中 / 高）
    extra: tuple[str, str] = ()                  # 对话框里多一个按钮：(文字, 主窗口上的方法名)，点了调 方法(对话框)
    actions: tuple[tuple[str, str], ...] = ()     # 选择框里的附加操作按钮（可多个）
    available: Callable[[int, Mapping[str, Any]], bool] | None = None  # (值, 存档 profile) → 网格里是否显示该项；profile 里没有所需信息时由函数自己决定（通常放行）
    button: str = "选择"                         # 任务行上打开选择框的按钮文字
    one_shot_toggles: tuple[str, ...] = ()          # 本次执行后自动关闭的开关键（必须属于 toggles）


@dataclass(frozen=True)
class TaskSpec:
    id: str
    title: str
    run: Callable[["TaskContext"], TaskResult]
    reads: frozenset[str] = frozenset()
    writes: frozenset[str] = frozenset()
    spends: frozenset[str] = frozenset()  # 已审核的购买类路由（仅耗灵魂/物品，见 guard.py）
    group: str = "日常"
    default: bool = False
    choices: Choices | None = None
    first: bool = False  # 排在本次所有普通任务之前执行（如福禄寿）
    last: bool = False  # 排在本次所有普通任务之后执行（成长之路、限时任务：要等其他任务把进度做完）
    terminal: bool = False  # 任务排序里的最后一项之后执行；用于需等完整在线时长的收尾任务
    prelude: Callable[["TaskContext"], "TaskResult | None"] | None = None  # 前置步骤：在本次所有任务（含 first 任务）之前先执行；返回 TaskResult = 中止该任务（记录该结果，不再执行 run）
    account_wide: bool = False  # 一个登录账号只需做一次（与存档无关）：同账号的多个存档只在其中一个执行
    modes: tuple[tuple[str, str], ...] = ()  # 行上点按切换的模式 [(值, 文字)]，按显示顺序；存进 options[任务 id]["mode"]
    mode_default: str = ""  # 没存过时的模式；留空 = 第一项
    mode_note: str = ""  # 鼠标停在行上档位切换上时显示的说明
    finale: Callable[["TaskContext"], "TaskResult | None"] | None = None  # 收尾步骤（领取奖励）：本次所有普通任务做完后执行，早于 last 任务；返回的结果并入该任务已有的那一行
    times_max: int = 0  # >0：周常页该任务行上有“次数”输入框（存进 options[任务 id]["times"]，留空 = 不执行），值为上限
    times_default: int = 1  # 次数输入框没存过时的初始值；任务自己读 options 时也用同一个默认
    prelude_late: bool = False  # 前置步骤排在其他任务的前置步骤之后（竞技场/斗宠：要等接取日常/限时任务做完）
    window: Callable[[int], bool] | None = None  # 活动页的“识别”：服务器时间（秒）→ 活动是否还在档期内；None = 没有档期（一直显示在活动区）
    overnight: bool = False  # 不吃每日次数（刷益灵花、只读查询）：执行跨过 0 点也照常开始，见 daycut
    long_running: bool = False  # 一场要打几分钟的长线任务：运行中点“停止”要弹窗确认，确认后强制停止（见 force_stop）
    force_warning: str = ""  # 强制停止确认框里补充的后果说明
    hosts: bool = False  # 执行中会长时间等待（护送经书等船）：等的时候同一存档的其他任务可以插进来执行（见 inject）
    reserve: int = 0  # >0：这个任务的次数要留给第几个养成周（state.GROWTH_WEEKS 的序号 1–4）；该周开始前 4 天内界面上任务名变红，自动配置少填当天要留的次数（state.growth_reserve）
    reserve_gain: int = 1  # 留次数的任务每天涨几次（上限 = 5 天的量）：上古蛮荒每天 2 次、最多 10 次，要留的次数跟着翻倍
    auto: Callable[["TaskContext"], int | tuple | None] | None = None  # 周常页“自动配置”：登录后只读地算出这个存档现在拥有的最大次数；None = 读不到，不改输入框；返回元组 = 状态说明，原样交给界面（不是次数）
    defer_until: tuple[int, int] | None = None  # 服务器时间（servertime.clock）早于该时刻时，运行器先执行其他任务，到点后再执行
    after_saves: bool = False  # 有同账号多个存档时，等它们各自的其他任务都结束后再执行（长在线等待）
    needs: tuple[tuple[str, ...], ...] | None = None  # 必须选了才做的选项：每组里至少一个键不为空；None = 有网格就是网格那一项（见 unpicked）

    def __post_init__(self) -> None:
        for name in ("reads", "writes", "spends"):
            object.__setattr__(self, name, frozenset(getattr(self, name)))
        object.__setattr__(self, "modes", tuple(self.modes))
        when = self.defer_until
        if when is not None and (not isinstance(when, tuple) or len(when) != 2 or any(type(v) is not int for v in when)
                                 or not 0 <= when[0] <= 23 or not 0 <= when[1] <= 59):
            raise ValueError(f"任务 {self.id} 的 defer_until 必须是有效的 (时, 分)")

    @property
    def default_mode(self) -> str:
        return self.mode_default or (self.modes[0][0] if self.modes else "")


def unpicked(spec: TaskSpec, opt: Mapping[str, Any] | None) -> bool:
    """该选的没选（关卡、祖巫、船只、徒弟……）：定时和“自动配置并执行”不做这个任务（任务自己也会因为没选跳过）。"""
    needs = spec.needs
    if needs is None:
        needs = ((spec.choices.option,),) if spec.choices is not None and spec.choices.items is not None else ()
    opt = opt or {}
    return any(all(opt.get(key) in (None, "", (), []) for key in group) for group in needs)


REGISTRY: dict[str, TaskSpec] = {}


def task(id: str, title: str, **options: Any):
    """注册任务；options 是 TaskSpec 的字段。"""
    def deco(fn: Callable[["TaskContext"], TaskResult]):
        if id in REGISTRY:
            raise ValueError(f"重复的任务 id: {id}")
        REGISTRY[id] = TaskSpec(id=id, title=title, run=fn, **options)
        return fn

    return deco


# 界面显示顺序（先到后，两列按行读）：没选项的在上，有选项的在下；执行以此为基准，再应用 first / account_wide / last / terminal 优先级
DISPLAY_ORDER = (
    "pet_elf", "monster_card",              # 没有任何选项的在上面：3 字
    "wish_pool", "treasure_cave",
    "free_maze", "pet_collection",          # 4 字
    "pet_trial",
    "mystery_shop", "fairy_trial",
    "black_market",
    "growth_road", "quick_develop",         # 有选项的在下面：先是行上切换
    "arena", "pet_arena",
    "fulushou", "forest",
    "tree_king",                            # 选择 + 次数
    "wanxiang_tree",
    "nine_tower",
    "daily_quest", "ten_yama",              # 选择旁边没有摘要文字的放最后
    "fairy_feed", "union_warehouse",
    "refine",
    "dungeon_main", "dungeon_hidden",
    "dungeon_elite", "dungeon_nightmare",
    "teacher_trial", "teacher_trial_student",  # 周常页（两列按行读）：师徒成对
    "ten_jue", "demon_tower",
    "star_maze", "ancient_wilderness",
    "last_rewards", "tree_king_nightmare",
    "pet_brawl", "pvp_match", "pet_catch", "pet_cultivation", "garden",
    "union_boss", "world_boss", "uid_lookup",
    "time_boat", "limited_time_shop", "fairy_flag_shop", "tongtian_consult",
    "tongtian_hire", "tongtian_escort",
    "shanfa_like", "alliance",              # 其他
    "teacher_teach", "teacher_learn",
    "month_card", "vip_gift",
    "group_signin",
    "double_drop", "online_welfare", "festival", "hallowmas", "double11_dog", "limited_quest", "login_gift",  # 活动页
    "cloud_ladder", "game_high_jump", "mid_autumn_bobing",
    "golden_autumn", "cyclic_trial", "dragon_treasure",
    "chapter_role",
    "bull_demon", "magic_dragon", "gm_spar",
)


_RANK = {task_id: i for i, task_id in enumerate(DISPLAY_ORDER)}


def display_rank(task_id: str) -> int:
    return _RANK.get(task_id, len(DISPLAY_ORDER))


def run_order(spec: TaskSpec) -> tuple[bool, bool, bool, bool, int]:
    """执行顺序：first 最前，其次账号级任务；last 靠后，terminal 在所选任务的最后。"""
    return not spec.first, spec.terminal, not spec.account_wide, spec.last, display_rank(spec.id)


def load_tasks() -> dict[str, TaskSpec]:
    """导入 tasks 包，触发各任务文件里的 @task 注册。"""
    from . import tasks  # noqa: F401

    return REGISTRY

