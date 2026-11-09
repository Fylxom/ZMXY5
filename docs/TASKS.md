# 造梦西游5 日常助手（重写版）— TASKS

> 交接入口。文档都在 `docs/`：本文件（现状）、`MODULES.md`（各任务规则）、`宠物培养.md`（刷益灵花）、`CHANGELOG.md`（变更流水）、`LESSONS.md`（真机踩坑）、`优化方案.md`。
> **每完成/变更一个子模块，必须更新本文件**（见第 9 节"维护规则"）。
> 最后更新：2026-11-08（GPT 改过的约 100 个文件逐个重写、大文件按职责拆开，行为不变：见 `CHANGELOG.md` 第一条）。

## 1. 目标与范围

重写旧项目 `C:\Users\Fylxom\OneDrive\Desktop\codex\造梦西游5\daily\`（旧 `daily_auto.py` 17144 行单文件，结构混乱、界面难用、功能过于复杂）。
本次只重写 **daily（日常任务）**；`teacherguess`、`threehit`、`shanfa_hall` 等其他旧模块暂不动。
工作方式：**一次只做一个子模块**，做完测试、更新本文档，再做下一个。用户不赶进度。

## 2. 基础文件（只读，所有功能必须从这里取数据）

| 路径 | 内容 |
| --- | --- |
| `basic/cg1_<日期>_<序号>_extracted/` | CG1 数据表，一版一个目录（约 397 个文件），程序读最新的那个。启动时自动同步到线上当前版本，只留最新两版（见 `MODULES.md`“数据自动更新”）。旧版"0921 基底 + 0928 叠加"逻辑**已废弃** |
| `tests/tables/cg1_20260928_1_extracted/` | **测试专用**的数据表快照（80 张，不进 git）：测试只读它，不读 `basic/`，所以游戏每周更新不影响测试结果。`python tools/test_tables.py` 生成；新表合不合用由 `tests/test_new_tables.py` 单独查。见 `LESSONS.md`“数据表一换，11 个测试失败” |
| `basic/v<版本号>.swf` | 主客户端（需要对照客户端行为时使用）。源码运行时启动会自动更新到线上最新版（见下“数据自动更新”），只留最新两版 |
| `basic/as_scripts_v<版本号>/` | 对应 `v<版本号>.swf` 反编译出的 ActionScript（v4740 是 4210 个 `.as`，按包名分目录，如 `modules/collect/model/CollectProxy.as`）。**目录名带版本**，只留最新两版（现在是 `as_scripts_v4740` 和上一版 `as_scripts_v4730`）；查协议字段/路由/客户端流程时 grep 最新那个（`scriptsync.scripts_dir()`），不用再反编译；只读。**主客户端换版后要手动导出**：`python -m zmxy.scriptsync`（约十分钟；启动日志“当前版本”那一行会提醒脚本落后了），查协议前先看最新的 `as_scripts_v*` 是不是当前主客户端的版本 |

规则：
- 代码里**只通过 `zmxy/data.py`** 读表：`data.table("Dungeon")`、`data.row("Prop", 101045121)`；按表名查，不写死 `0082_` 编号。
- 返回对象是共享缓存，只读；运行时状态/账号数据不得写入表。
- 不得把表复制到别处（唯一的例外是上面的测试快照）；不得修改 `basic/` 下任何文件。

## 3. 架构（`zmxy/`）

```
zmxy/
  protocol.py   协议层的入口（来自旧 alliance_daily.py）：Pomelo 连接 PomeloConnection、response_code、choose_record
  config.py     错误类型、默认地址与路由常量、连接配置 ClientConfig
  codec.py      Pomelo 编解码：protobuf、消息、包
  guard.py      消费保护：点券/元宝/宝券/未审核购买路由的集中拒绝闸门
  core.py       任务框架的入口：只转发下面四个文件的名字，任务、界面、命令行都从这里 import
  taskdef.py    结果类型、异常、@task 注册表、显示/执行顺序
  activity.py   运行中的任务登记（哪个线程在跑什么、是否正打着一场）、三种停止
  session.py    Session（受控发包、次数记账、宠物实体记录）、TaskContext、Account
  runner.py     run_account（前置→本体→收尾、插入任务）、run_batch（各登录账号并行）
  pinyin.py     选关搜索用：通过 Windows ICU(ctypes) 转拼音，多音字(重/长)保留两种读法；无 ICU 时只搜名称/编号
  quota.py      次数保护（副本部分）：主线不限 / 精英每关每日 / 噩梦共享宝箱池；读不到返回 None
  dungeon.py    主线/精英/噩梦共用流程（创建→拾取→结算→翻牌），三个任务的入口 run_kind；关卡数据在 dungeon_levels.py，掉落统计在 dungeon_gains.py，翻牌在 dungeon_cards.py（旧名字都还能从 dungeon 导入）
  pickup.py     不拾取：把掉落归到 灵魂/经验/宠物经验/萝卜蓝瓶 四类（旧版叫“奖励排除”）
  online.py     在线副本共用：等待房间/战斗服务器的路由常量（R_ROOM…R_EXIT_BATTLE，只在这里定义）、解析 onCreateOnlineCopy 广播（战斗服务器地址、座位）
  quest.py      info.quest 共用：accept/commit 请求函数、接取/领取路由（R_ACCEPT/R_COMMIT）、状态值、status(info)
  data.py       只读数据表（basic/）；大表只要一两列时用 `data.slim(表, 字段…)`，不把整表留在内存（Skill 40MB、Monster 10MB）
  store.py      本机设置，DPAPI 加密密码/二级密码/Cookie
  servertime.py 服务器时钟：进角时记下“服务器 − 本机”的秒数（存 server_clock.json），定时到点、顺延、界面上的周/天判断都用它，不用本机时间
  timeplan.py   按开放时段的定时规则：各任务开放时段（读表）、场次 + 开场后几分钟的到点判断、报名期、网络失败重试
  globalcfg.py  定时（账号栏最上面的“定时”）：所有定时项的设置、选账号 / 存档、按时刻的到点、周常按养成周；配置在 store.data["global"]
  results.py    任务结果写回本机设置（界面和命令行共用一份）：读到的信息、上届奖励按周合并、暂停进度、一次性开关、做完的活动关开关
  cli.py        命令行：python -m zmxy.cli [--list-tasks] [--account 名]
  gui.py        界面入口（PySide6）：启动、App 类由 mainwin/ 里的各部分拼成
  mainwin/      主窗口按职责分文件：pages 骨架与任务行、choices 行上选择、accounts 账号、running 执行控制、events 工作线程事件（按 `_on_<事件名>` 分发）、timers 定时、globalpage 全局任务页、limited 活动页、autoconfig 周常自动配置、boat 时光之舟、petcard 宠物捕捉卡片、logpage 日志页、rules 任务分页、parts 小控件
  widgets.py    自绘控件和设计令牌（按钮、开关、分段、下拉、方块、右键菜单、提示框 notice / ask_confirm、对话框基类 Dialog）；都用 QPainter 自己画，不用 Qt 自带外观的按钮、下拉、QMessageBox
  logtable.py   日志表（自己画，只画看得见的行；可见行的重排是惰性的）
  dialogs.py    账号 / 验证码 / 选存档 等小对话框，也转发下面三个的名字
  choice_dialog.py  任务选择对话框（由 core.Choices 生成）
  refine_dialog.py  自动洗练选择（法宝 / 羽毛 / 翅膀）
  read_dialog.py    两个选择框共用的“读取”
  api.py        跨模块共用路由和请求体；接收 ctx.request 等发包函数，每次只发一次
  run_records.py 实战记录归档与 AppData 保留规则
  uid/          UID 资料：fields 字段含义、profile 解析归类、ui 查询弹窗
  tasks/        一个任务一个文件，在 tasks/__init__.py 里 import 即注册
    pet_cultivation/  刷益灵花：task 入口、harvest 流程入口（engine 主流程、ledger 放生授权账本、field 刷图与经验、admission 准入）、decision/policy/metrics 决策、journal 断点
    boss_raid.py      世界/联盟 Boss 注册入口和换房间流程；boss_fight 出手循环、boss_room 房间局面、boss_rules 打法参数
    team_room.py      十绝阵/师徒试炼等共用的组队房间步骤；level_pick.py 选关+每关次数共用
    rewards.py        领奖类任务共用的小步骤
    events/           有档期的节日活动：festival、hallowmas、double11_dog、game_high_jump
tests/          unittest，全部离线
```

### 发包的闸门（`Session.request` / `request_many`，顺序固定）
1. `guard.check`：路由名/请求体含点券、元宝、宝券标记（`basic/` 的 ticket/yuanbao/baoquan/点券/元宝/宝券，兼容点卷写法）→ 拒绝。购买类路由（buy/purchase/lottery/wish/draw/refresh/charge，词表取自 `basic/`）必须被任务 `spends=` 登记，否则拒绝；**登记不能覆盖点券/元宝/宝券检查。**
2. 路由必须在任务声明的 `reads ∪ writes` 内，否则 `GuardViolation`。
3. 写请求在传输层失败（超时、断线、批量只收到一部分响应，包括协议层抛的 `AllianceError`）→ `UncertainOutcome`：**绝不自动重发**，运行器停止该账号后续任务，提示用户登录游戏核对。

### 新增一个任务的步骤
1. 在旧代码里定位该任务（第 6 节有行号），**只读那个函数及其直接依赖**，提取规则，**重写而不是复用**：不照抄旧实现，目标是比旧版精简得多。
2. 新建 `zmxy/tasks/<name>.py`，用 `@task(id, 标题, reads=…, writes=…, spends=…)` 注册，函数签名 `run(ctx) -> TaskResult`。
3. 在 `tasks/__init__.py` 加一行 import。GUI 自动出现复选框，CLI `--list-tasks` 自动列出。
4. 写 `tests/test_<name>.py`（假连接，不联网）。至少覆盖：读不到字段时阻止而不是当成 0、结果未知不重发。**产品中不设“检查/试运行”模式**；调试用的检查代码只能临时加入，不得留在成品里。
5. 本文件第 5 节的表加一行状态，规则细节写进 `MODULES.md`，并在 `CHANGELOG.md` 记一条。

### TaskResult 状态
`submitted`（已执行）/ `skipped`（今日已完成等）/ `blocked`（安全原因未执行）/ `error`（任务自身异常）/ `uncertain`（结果未知，需人工核对）。

## 4. 必须保留的业务规则（来自旧版，已被验证有价值）

- 界面存档 **1–8**，协议 `record_index` **0–7**；只在 `store.to_account` 换算一次。
- 读不到次数/余额字段 ≠ 次数为 0，也不是"可免费尝试"，一律 `blocked`。
- 点券、元宝、宝券受独立拦截；灵魂、物品等仍是真实消耗，"不是点券"不等于"免费"。
- 请求超时可能已到达服务器：不重发领取/购买/结算。
- 中途停止：做了一部分保留明细并标 `blocked`，一点没做标 `skipped`，全部任务都这样。
- 宝券（`baoquan`）绝对不花，和点券/元宝一样受拦截，也不要询问用户是否例外。
- 掉落：`drops/drop` 只是待拾取；`give`、确认的领取响应及推送才算奖励。
- 密码/二级密码/Cookie 分开存；DPAPI 解不开时不得用空值覆盖原密文。
- 次数：共享次数组不能取成员最大值；VIP 增量、跨天、服务器绝对计数按实际字段处理（详见旧 `daily/quota_protocol.md`）。
- `Session.spent` 只记“上一次取角色快照之后”本连接用掉的次数：重新取快照（`refresh`、回连）时必须清空，否则同一次消耗扣两遍。
- 等待一律用 `ctx.sleep`（会回服务器心跳），不要在任务里直接 `time.sleep` 长时间干等。

## 5. 子模块状态

| 子模块 | 状态 | 文件 | 测试 | 备注 |
| --- | --- | --- | --- | --- |
| 定时页（账号栏最上面的“定时”） | 🟡 2026-11-09 重排完成（离线测试、离屏截图），**未真机验证** | `globalcfg.py`、`timeplan.py`、`mainwin/globalpage.py`、`mainwin/timers.py` | `tests/test_global_tasks.py`、`tests/test_store.py`（`TimerTests`）、`tests/test_scheduling.py` | 任务页只管做什么，定时页只管什么时候做；上面每项一行时间，下面“参加的存档”一张表（一行一个存档、一列一个定时项打勾，新账号默认不勾）。上古蛮荒单独一项，周常跳过它勾了的存档。规则见 MODULES.md “定时” |
| 公共游戏接口 | ✅ 源码收敛与离线对照完成 | `api.py`、`quest.py`及22个业务模块 | `tests/test_api.py`15项通过；843项定向无新增失败 | 12条路由在公共请求层维护，任务接取/领奖留在quest；9条跨文件重复定义降为0。全量1487项仍有32项基线可复现的失败/错误，未登录或打包 |
| 世界Boss周常布局 | ✅ 源码与离屏验证 | `gui.py`、`dialogs.py` | `tests/test_scheduling.py`、`tests/test_dialog_paint.py` 等 60 项相关测试通过 | 与上届奖励交换显示位置；存档/档位/祖巫合并摘要；选存档位于弹窗档位下一行；保留每周定时，未重打包 EXE |
| 世界Boss本轮出金跳过 | ✅ 源码与专项离线回归 | `tasks/boss_raid.py` | `tests/test_boss_raid.py`、既有开关复位回归，共 5 项专项通过 | 每轮重新挑战所选祖巫，仅移除本轮成功结算新出传说阵眼的祖巫；不读历史出金记录决定跳过，开关在最终结果后自动关闭。未登录游戏、未重打包 EXE |
| 世界Boss积分与节拍 | 🟡 2026-10-18 源码已改，离线重放完成；短停计数仍偏多 | `tasks/boss_raid.py`、`tasks/boss_beat.py`、`tools/boss_beat.py` | 1,830 次旁观重放；183,499 笔窗口审计；本轮未运行 unittest | 已过去时间积分、短停排除、片段接续、保留实际增量时刻与峰值；详见本轮报告。未真机验证、未重打包 EXE |
| 消费保护 | ✅ 点券/元宝/宝券专项离线通过 | `guard.py`、`core.py` | `tests/test_guard_login.py`、`tests/test_core.py` 共49项通过 | 2026-10-28 新增宝券（baoquan）路由、嵌套参数及已审核购买路由不可绕过保护；未登录游戏、未重打包 EXE |
| 数据表读取 | ✅ 完成 | `data.py` | `tests/test_data.py` | 读 `basic/` |
| 任务框架/运行器 | ✅ 完成（离线） | `core.py` | `tests/test_core.py`、`test_inject.py`、`test_pause_resume.py`、`test_force_stop.py` | “打完这场再停”等待同批次已进入战斗的任务全部结束；后续任务、领奖、归库和注入任务不再启动。结果未知停止同一登录账号全部后续存档、注入队列、收尾与归库，也不生成该账号的继续项；其他登录账号照常 |
| 本机存储 | ✅ 完成（离线） | `store.py` | `tests/test_store.py` | 含真实 DPAPI 往返测试（仅 Windows）；后台登记与到点判断共用有效定时遍历，忽略未知任务、损坏容器和无效存档 |
| 命令行 | ✅ 完成（离线） | `cli.py` | `tests/test_cli.py` | 已核对离线任务清单和账号级任务执行顺序 |
| GUI | 🟡 骨架；启动/日志离屏验证、弹窗原生故障恢复验证 | `gui.py`、`widgets.py`、`dialogs.py`、`logtable.py` | `tests/test_dialog_paint.py`、`tests/test_logtable.py`（需 PySide6） | 2026-10-17 共用弹窗在显示/曝光/恢复后完整提交画面，洗练重建异常不遗留禁刷状态；12 项弹窗回归离屏/原生通过，150%/200% 缩放 152 次原生故障恢复通过；原始偶发触发仍未捕获。修复重复 UID 查询的相同完成结果被并入旧行：没有进行中行时仅严格追加的收尾文字合并，相同结果另起一行；本次未运行测试。2026-10-13 修复启动在终端打印版本消息时阻塞：更新消息只写界面日志；不可写终端回归和真实主窗口离屏启动通过，997 条全套离线测试通过。2026-10-12 去掉日志筛选的空白账号选项，“数据更新”仅在“全部账号”显示；963 条全套离线测试及日志页离屏点击检查通过。同日修复重复执行后残留“进行中”：最终结果优先结束当前行，只有没有进行中行时才合并到旧结果；6 条日志回归通过。截图检查过主窗口与选关对话框布局，**未人工点击全流程**；存档选择仍在账号对话框。任务可声明 `core.Choices`（可选项目）→ 任务行自动出现"选X"按钮 + `ChoiceDialog`，后续任务（星斗、远古荒野等）直接复用 |
| 次数保护 `quota` | 🟡 副本部分完成（离线） | `quota.py` | `tests/test_dungeon.py` | 只实现了主线/精英/噩梦三种；**镇妖塔、噩梦天树王（wing）仍由各自的任务自己读次数，`quota.of` 继续拒绝它们**，遇到共享组成员、噩梦天树王（`type=12`）、镇妖塔（`countdownMode>0`）都返回 None 阻止。旧 `daily_quota.py` + `quota_protocol.md` 仍是这些的依据 |
| 任务：主线/隐藏/精英/噩梦 | ✅ 完成（离线），**未真机验证** | `dungeon.py`、`tasks/dungeons.py` | `tests/test_dungeon.py` | 任务 id `dungeon_main/_hidden/_elite/_nightmare`（界面顺序：主线、隐藏、精英、噩梦），默认不勾选。见 `MODULES.md` "副本说明" |
| 不拾取 | ✅ 完成（离线 + GUI 截图/脚本点击），**未真机验证** | `pickup.py`，接入 `dungeon.py`、`gui.py` | `tests/test_dungeon.py`（`NoPickupTests`） | 旧版“奖励排除”的重写，见 `MODULES.md` “不拾取说明” |
| 任务：师徒授课、师徒学艺 | ✅ 完成（离线），**未真机验证** | `tasks/teacher.py`（`teacher_teach`/`teacher_learn`，界面“日常”组，在联盟仓库之后） | `tests/test_daily_tasks.py`（`TeacherTests`） | 授课：`Teacher[1].teachingDayNum - giveLessonCnt`，需有徒弟(`my.userdata`)，`giveLessons{}`；学艺：`learnNum_free - personFreeLearnNum`，需有师父，档位 = `TeacherLearn` 里 `likingAll ≥ 各徒弟好感度之和` 的第一档，`getBuffer{type:0,id}`。旧版奖励汇报没搬。**师徒试炼仍未做** |
| 账号级任务（`account_wide`） | ✅ 完成（离线 + GUI 脚本），**未真机验证** | `core.py`、`store.py`、`gui.py`、`cli.py` | `tests/test_store.py`（`WideTaskTests`）、`tests/test_daily_tasks.py`（`AccountWideTests`） | 联盟贡献、VIP 礼包、师徒授课、师徒学艺、善法堂、超值月卡（界面在“其他”组）。**按登录账号保存**（`settings.json` 的 `wide[用户名][任务id] = {on, record}`），同账号各存档**共用**（用户明确要求，这是唯一允许共用的选择；其余设置一律按存档各存各的）；行上“选存档”选 1–8（没有“自动”，默认 1）；**联盟贡献可开“多存档”**（`store.WEEKLY_WIDE`；选存档对话框里的开关，打开后周一…周日各选一个存档，存 `multi`/`week`，执行时 `Store.wide_record` 按北京时间的星期几取；行上摘要显示“多存档 · 今天 n”）；批量执行与断点继续时，账号级执行项排在普通存档任务之前（提前执行受时间限制的任务除外）；账号级执行和断点继续均忽略所选存档的同物归库配置；同账号去重保留作兜底 |
| 任务：4399 群组签到 | ✅ 账号1线上签到及正式入口复核通过（2026-10-31） | `tasks/group_signin.py`、`tasks/__init__.py`、`core.py` | `tests/test_group_signin.py`；相关53项离线通过 | 固定群组mtag-83193，账号级；旧Cookie→一次社区同步→同账号账密登录。独立CookieJar不改游戏配置，明确未签到才单次提交；实测code=100、连续6天、独立GET确认已签到，正式入口复跑跳过。未打包EXE。 |
| 任务：竞技场、斗宠大会、藏宝洞、快速养成、灵宠试炼、万能强化石 | ✅ 完成（离线 + GUI 脚本打开选择对话框），**全部未真机验证** | `tasks/arena.py`（`arena`/`pet_arena`）、`tasks/treasure_cave.py`、`tasks/quick_develop.py`、`tasks/pet_trial.py`、`tasks/daily_quest.py`（原万能强化石） | `tests/test_challenge_tasks.py`（PetTrialTests 覆盖稀疏日计数、免费重置前刷新及损坏阻止；ArenaTests 覆盖客户端默认周记录、损坏保护；ArenaInterleaveTests 覆盖携带账号资料时单次完成事件） | 见 `MODULES.md` “日常页第二块”。竞技场/斗宠有有效周状态时按客户端沿用进角记录或默认空记录；完全缺失或损坏时阻止，并记录具体缺项。同次完成汇总仅补资料时不重复显示日志；同时 `quota.py` 支持 DungeonHelper 共享次数组 |
| 任务：宠物培养（潜能筛选） | 🟡 源码已更新，未真机验证 | `tasks/pet_cultivation/task.py`、`tasks/pet_catch.py`、`release_guard.py`、`core.py`、`dungeon.py` | `tests/test_pet_cultivation.py`、`tests/test_release_guard.py` | 地图只显示主线原名，固定只捕捉天马；逐只筛选两只1级、最高初始潜能正好为2级的样本，自动分别装备到1P/2P；宠物可在对应角色不在场上时获得经验，但不能超过该角色等级，开刷前确认1P/2P等级均至少100。主线失败结算不增加 `passNum`，只拾取宠物经验；VIP3+回收3级目标，VIP0–2回收4级目标，分别处理本场新达到目标的潜能并核验其他潜能不变、益灵花增加；刷选定主线至两只都100级或次数耗尽。放生开关默认关闭且持续保存；打开后复用宠物捕捉模块的严格规则，只放生本次新捕且筛选不合格的天马，合格样本留下培养；两只都到100级后先分别卸下并确认不在出战栏，再放生。失败时阻止放生并报告未完成；关闭时落选样本保留，旧宠物不会进入放生流程。**未运行测试、未真机验证、未重打包 EXE** |
| 秋枫盛典 | 🟡 已接入每日挑战、已开放周任务及等级奖励领取；**未真机验证** | `tasks/events/festival.py`、`tasks/daily_quest.py`、`tasks/dungeons.py`、`dungeon.py`（`festival`，活动页；按 `HolidayWelfareHelper.activityId` 识别档期） | `tests/test_festival.py`（专项 17 通过、10 缺表跳过；在线回归 14 项全通过） | 每日挑战保持原有完成流程；领奖时按活动日期处理当前已开放的每日 / 第 1～7 周页面，普通任务只领取当前任务链状态 2 的奖励；每日在线目标达标后按客户端单项流程领取，不代做周任务。等级奖励读取 `holidayWelfare` 的积分、领取记录及 `open` 档位：无档位 2 只领免费奖励，已开通时免费和付费都领，逐项刷新。最高连击 / 击败怪物 / 使用法宝目标分别映射到 `maxCombo` / `killMonsterCnt` / `useMagicWeaponCnt`；在线等待、在副本内使用配置的 `Prop.propType=1009/1010` 关卡消耗品、普通炼丹炉炼制分别走客户端确认的状态 / 路由，不自动使用 1050 清心符。副本承载顺序为日常任务 → 主线 → 精英，只在优先级最高的第一场实际副本注入盛典统计和配置道具；三个任务都未勾选时才兜底跑主线第一关且不拾取物品，多关卡 / 多次执行只注入第一次；兜底第一关用 `win=0` 结算，不增加 `passNum`。盛典旁新增“选择”，可选一/二/三品回元露和全部宝石，以及一阶烈火咒、一阶寒冰咒；另有“使用关卡道具”“做炼丹炉”“做在线任务”三个开关，默认全部开启；界面按任务分三块，打开才展开各自的选项；选择按钮旁单项显示任务名，多项显示数量；只在勾选类别中找材料足够且已解锁的普通配方，未配置不擅自炼制。日常目标先提交/领取服务器已确认的完成项并处理炼丹炉，15 分钟在线等待最后执行；中途停止时此前已领取项目不会留在待办。在线等待由真实连接的 `Session.idle` 自动处理服务器心跳；服务器在线进度已达标，或在同一连接每 300 秒同步服务器时间并确认累计在线秒数达标后，提交一次在线奖励，再刷新确认；每 30 秒报告等待进度，换连接、时钟异常或跨每日重置时不提交。其他任务仍只领取状态 2，材料或状态异常不猜进度，在线领奖被拒、结果未知或未确认时不重试。新增等待中断顺序回归；**未真机验证** |
| 任务：福禄寿、九宫玄塔、十殿阎罗、神兽森林（含扫荡模式）、万象古树、天树王 | ✅ 完成（离线 + GUI 脚本打开选关对话框），**全部未真机验证** | `tasks/fulushou.py`、`tasks/special.py`（九宫/十殿/森林/天树王共用）、`tasks/forest_sweep.py`、`tasks/wanxiang_tree.py`、`tasks/tree_king.py` | `tests/test_challenge_tasks.py`（共 59 个） | 见 `MODULES.md` “日常页独立项” |
| 任务：联盟贡献 | ✅ 服务器时间及结算等待专项离线验证，未真机验证 | `tasks/alliance.py`、`core.py` | `tests/test_core.py`（联盟/保护专项 18 项通过） | 本地 00:15 前单独顺延，其他任务先执行；提交前刷新并等待服务器 00:15:30。明确结算拒绝后每 30 秒重查状态、最多 5 分钟；已完成不再提交，未知结果不重发，日志保留双时钟及秒差；暂停/停止不提前贡献 |
| 任务：VIP 礼包 | ✅ 完成（离线） | `tasks/vip_gift.py` | `tests/test_core.py` | 旧 2685 行 |
| 任务：超值月卡 | ✅ 完成（离线），**未真机验证** | `tasks/month_card.py` | `tests/test_daily_tasks.py`（`MonthCardTests`） | 日常，账号级（`account_wide`，只在一个存档领，同 VIP 礼包），排在 VIP 礼包后。路由 `scene.activityHandler.getMonthCardReward`（空请求体，只领不花费）；读 `info.monthCard`（卡id→expiredTime/history），无有效卡或 history 已有今天（键 `年/月/日` 不补零）则不发请求。服务器时间、卡结构/有效期或显式 history 格式无法确认时不领取；省略 history 按客户端视为尚无记录。因服务器拒绝文本被协议层抹成通用提示，不能靠报错判断已领；history 字段格式未经真机核对 |
| 任务：双倍掉宝 / 在线福利 | ✅ 完成（离线），**未真机验证** | `tasks/double_drop.py`、`tasks/online_welfare.py` | `tests/test_double_drop.py`、`tests/test_online_welfare.py`；65项相关回归通过 | 活动页常驻；双倍掉宝先执行，在线福利最后执行，必要时补领首档再刷新后续奖励链；首档已领不重复、未就绪不越级、刷新期间停止不领奖、结果未知不重试。部分已领但后续未就绪时返回blocked，保留已领收益；再次执行按服务器状态只领剩余档位 |
| 任务：登高采菊 | ✅ 完成（离线），**未真机验证** | `tasks/events/game_high_jump.py` | `tests/test_game_high_jump.py` | 活动页；每个登录账号首次在 100–120 之间随机取一个固定分数并复用，自动执行 `ClimbHigh[1]` 的每日免费剩余次数；按客户端契约逐次发送开始 / 结算请求，不碰付费再次挑战路由 |
| 任务：万圣活动 | ✅ 自动选关与固定水晶球拾取（离线+GUI任务行检查），**未真机验证11个到账** | `tasks/events/hallowmas.py` | `tests/test_hallowmas.py`（25项） | 自动刷最新已解锁且有魔女配置的主线，固定只取水晶球至11；无手选和拾取开关，已到10不能补 |
| 限时：挑战刑天 | ✅ 已接入（离线），**未真机验证** | `tasks/limited.py` | `tests/test_limited_tasks.py` | 普通/噩梦切换，按 `DungeonHelper` 类型 10（500105/500111）共享的每日 2 次免费次数执行；仅在 `Dungeon.activityId` 档期内挑战，不走 200 点券额外挑战路由 |
| 限时：轮回试炼 | 🟡 已接入直接本地战斗协议，**未真机验证** | `tasks/cyclic_trial.py` | `tests/test_cyclic_trial.py` | 地阶三重、五重、十重对应总第 13/15/20 层；每层 `openChallenge → endChallenge(bossHp=0)`，后续层走当前副本传送门，不退出重开；每层核对 dungeonId 与 cur，自动领奖并严格停在所选目标 |
| 任务：成长之路 | ✅ 完成（离线），**未真机验证** | `tasks/growth_road.py` | `tests/test_growth_road.py` | 见 `MODULES.md` “日常页任务” |
| 任务：灵宠采集、幻兽试炼、神秘商店、黑市商店、聚灵池、善法堂、妖王帖、许愿池、幻兽喂养、联盟仓库 | ✅ 完成（离线 + GUI 脚本验证任务列表/选材料对话框），**全部未真机验证** | `tasks/<名>.py`（`pet_collection fairy_trial mystery_shop black_market pet_elf shanfa monster_card wish_pool fairy_feed union_warehouse`） | `tests/test_daily_tasks.py`（MonsterCardTests 覆盖损坏计数阻止招募） | 见 `MODULES.md` “日常页任务”。2026-10-30 模块审查发现并修复共享仓库并发补足后的旧库存误判；仅做语法和 diff 检查，未运行测试。 |
| 任务：自动洗练（法宝 / 羽毛 / 阵法） | 🟡 功能接好，**2026-10-11 起真洗**（用户说关掉保护，`refine.LIVE = True`；法宝同日起也洗：免费洗炼成长值 + 祝福一起重洗，按“成长值锁定 / 祝福锁定”两个筛选条件洗到满足为止），**未真机验证** | `tasks/refine.py`、`dialogs.RefineDialog`、`gui.open_refine` | `tests/test_refine.py` | 2026-10-12 修复切页闪烁：共用堆叠页面预留高度，空/有资料各 30 次离屏点击切换无缩放/移动，保存重开及 977 条全套离线测试通过。阵法页改为“暂未开放”，保留旧配置但不执行；法宝、羽毛照常。68 条洗练测试、972 条全套离线测试和离屏页签点击/保存/重开检查通过。日常页最后一行，点“选择”分三块；规则见 `MODULES.md`“自动洗练”。2026-10-12 法宝下拉区分 1P/2P 与装备/背包，同角色同名多件加序号，仍按实体保存选择；946 条离线测试通过，离屏 GUI 点击/保存/重开通过。旧缓存点“读取”更新归属。进角信息里法宝 / 羽毛 / 精魄的位置、三个响应的字段都是按客户端脚本推的，第一次跑看日志 |
| 周常页副本/活动任务（原 9 项；十绝阵、上古蛮荒已于 2026-10-01 按 123 目录的提取文件重写） | ✅ 完成（离线），**未真机验证** | `tasks/{ancient,demon_tower,tree_king_nightmare,star_maze,maze_free,time_boat,tongtian,ten_jue}.py`、`online.py` | `tests/test_weekly_*.py`、`tests/test_challenge_tasks.py`（`AncientTests`） | 见 `MODULES.md` “周常页” |
| 周常：限时抢购 | ✅ 完成（离线），**未真机验证** | `tasks/limited_shop.py`、`login.py` | `tests/test_limited_shop.py`、`tests/test_login.py` | 从认证账号状态确认是否已领取本周养成福利，未领取时按客户端参数免费领取；随后动态读取最新 `GlobalBuy`，只购买当前上架、无角色条件且以灵魂标价的商品，并核对实时库存、账号限购和余额。见 `MODULES.md` “周常页” |
| 斗神大会、灵宠乱斗（周常页“有开放时间”） | ✅ 2026-10-12 自动目标与次数框移除完成（离线 + GUI 离屏），**本次未真机验证**；斗神真人挂机结算仍待真机确认（见 `LESSONS.md`） | `tasks/{pvp_match,pet_brawl}.py`、`store.py`（清理旧次数）、`protocol.py` `poll_push`、`core.TeamLink.ask` | `tests/test_weekly_pvp.py`、`tests/test_wait_push.py`、`tests/test_store.py` | 不再输入、读取或保存手动次数；读取和保存设置时清理这两项任务在所有账号、存档下遗留的 `times`。灵宠乱斗自动补满今日乱斗币上限（现为 20），斗神自动用完今日保护次数，人机局不扣保护次数。原有开放时间、二转技能、积分、异常结算与停止检查保留；具体规则见 `MODULES.md` “斗神大会 / 灵宠乱斗” |
| 联盟药园（界面隐藏） | ✅ 已实现；定时页的任务行和每周定时设置行已从 GUI 隐藏，**未真机验证** | `tasks/garden.py`（`garden`，只有“执行”，不参与批量） | `tests/test_garden.py` | 采摘 → 铲除 → 浇灌盟友，没有选项；规则见 `MODULES.md` “周常页”里的“联盟药园” |
| 设置 / 程序自动更新 | 🟡 源码、离线测试和离屏截图完成；发布仓库 2026-11-10 建好（公开 `Fylxom/ZMXY5`，放了部分源码），`REPO` 已填；**还没发过 Release、未真机下载替换** | `appupdate.py`、`mainwin/settings.py`、`tools/build.py`、`tools/publish.py` | `tests/test_appupdate.py` 6 项 | 下一步：打包一版（带 REPO 的第一版，之后的版本才能被它检测到）发出去，再发一个更高版本试一次完整更新。gh 装在 `C:\Program Files\GitHub CLI`，已登录 Fylxom。规则见 MODULES.md“设置 / 程序自动更新” |
| UID资料查询（周常页最底下） | ✅ 七类重做（专项离线、Qt 弹窗离屏交互/中文布局及历史字段核对），**未做本次在线查询、未重打包 EXE** | `tasks/uid_lookup.py`、`uid/fields.py`、`uid/profile.py`、`uid/ui.py`、`gui.py` | `tests/test_uid_lookup.py`、`tests/test_uid_profile_gui.py`、`tests/test_scheduling.py` | 独立于世界 Boss；保留 UID-only / 明确 RID+index 查询及实际返回目标解析。固定七类、紧凑条目/展开详情/页内搜索/字段显隐/完整复制；原始路径防止中文标签覆盖，逐叶节点核对去向；修正孔位、显示开关、丹药服用次数与客户端枚举；宠物相册按同类宠物合并一行，保留所有场景快照详情及相册奖励；原始返回只留内存，不生成查询文件。规则见 MODULES.md |

各任务的规则细节（路由、次数算法、停止条件；日常页 / 活动页 / 周常页 / 副本 / 不拾取 / 只捡强化石）在 **`docs/MODULES.md`**，
不用通读：要改哪个任务就 grep 它的名字读那一条。新任务做完：上面的表加一行状态，规则写进 `MODULES.md`。

## 6. 旧代码对照（旧页面已全部迁移完）

旧项目 `codex/造梦西游5/daily/daily_auto.py` 只在查规则时用：`grep -n` 定位后按行号读，不要通读。
全部任务的旧行号表在 `CHANGELOG.md` 末尾的附录；下面只列**还有没做完部分**的：

| 任务 | 旧行号 | 备注 |
| --- | --- | --- |
| 常规副本 dungeons | 7425 | ✅ 主线/精英/噩梦已重写。隐藏关已做（见下）。**未做**：分界廊饰品 BOSS（1015，需 2000 灵魂、次数规则不明）、神兽森林/雷电山谷/蛛之炼狱等 difficult=3 的关卡、`resume` 断点续跑 |
| 上古蛮荒 ancient_wilderness | 14619 / 14734 / 14774 | ✅ 单人关卡已重写（周常页）；入口 490 与组队房间未做 |
| 宠物试炼 pet_trial | 16393 | ✅ 已重写（只做当前周天；组合关卡 special_dungeons 里的其他项未做） |
| 组合关卡 special_dungeons | 16618 | 分派表；全部已拆成独立任务（日常页与周常页），入口 490 / 组队蛮荒房间未做；牛魔已在活动页 |

## 7. 运行与测试

```
# 项目根目录（含 zmxy/ 与 basic/）
python -B -m unittest discover -s tests          # 全部离线测试（数量以实际输出为准）
python -B -m zmxy.cli --list-tasks               # 离线列出任务
python -B -m zmxy.cli                            # 执行已勾选任务
python -m zmxy.gui    或双击 run_gui.bat          # 界面（每日 / 每周定时由 Windows 任务计划程序后台唤起）
```
- 需要 Python 3.10+（用到 `X | None` 类型标注、海象运算符）。界面用 PySide6（`pip install PySide6-Essentials`，只要基础模块）；其余除标准库外无第三方依赖，命令行和测试不需要显示器。
- 设置文件：`%LOCALAPPDATA%\Zmxy5Next\settings.json`（与旧版 `Zmxy5Daily` 不共用）。
- Windows 控制台中文乱码时加 `PYTHONIOENCODING=utf-8`。
- 测试共用的假连接和帮手函数在 `tests/support.py`（`FakeConn` + `run_daily`、`Fake` + `run` / `run_dungeon`），测试文件之间不互相 import。
- 测试一律不联网：`tests/__init__.py` 装了闸门，外网连接会被拦下并记进 `tests.BLOCKED`，`tests/test_zz_offline.py` 最后检查它是空的。
- 日志表状态回写有离屏自动测试（`tests/test_logtable.py`，需 PySide6，未安装时跳过）；其余界面尚无自动测试。改了界面要实际打开点一遍（`run_gui.bat`）。界面 2026-10-06 从 Tk 换成 PySide6（原因和做法见 `LESSONS.md`“界面：从 Tk 换到 PySide6”），Tk 版的 5 个文件留在 `release/tk-backup/`（不入库）。
- `python tools/undefined.py zmxy/*.py`：拆文件/挪函数后查有没有漏 import 的名字。
- 项目已用 git 管理（`basic/`、`release/` 不入库）。每完成一个子模块提交一次。

## 8. 变更日志

挪到了 `CHANGELOG.md`（新的写在最上面）。本文件只留现状，不记流水。

## 9. 维护规则（给每一位 AI）

1. 开始前读本文件，再读目标模块的源码与测试；不要通读旧 `daily_auto.py`，用 `grep -n` 定位再按行号读。
2. 每次只做一个子模块。完成后：跑全部测试 → 更新第 5 节状态（规则细节写进 `MODULES.md`），并在 `CHANGELOG.md` 最上面记一条 → 如有新规则写入第 4 节。
3. 如实标注验证级别：**离线测试** / **GUI 实际运行** / **真机登录** / **线上任务**。没跑过的不能写成已完成。
4. 不得读取、打印或让用户粘贴账号密码/Cookie/token；日志里不得出现它们。
5. 不得修改 `basic/`；不得绕过 `Session.request`；不得为"方便"放宽 `guard.py`——要放行某购买路由，只能在任务里 `spends=` 登记，并在任务内自行校验货币与次数。
6. 已知待办：见第 5 节中 🟡/⬜ 项；`store.py`、`cli.py`、GUI 缺测试。

## 10. 可直接复制给下一位 AI 的开场说明

```text
你在接手"造梦西游5 日常助手（重写版）"。请先完整阅读项目里的 docs/TASKS.md，
然后只读目标子模块的源码与测试。基础数据全部来自 basic/，通过 zmxy/data.py 读取。
旧项目在 C:\Users\Fylxom\OneDrive\Desktop\codex\造梦西游5\daily\，仅作为规则参考，
用 grep -n 定位后按行号读，不要通读 daily_auto.py。

本次任务：[填写要做的子模块]。

要求：一次只做一个子模块；所有发包走 Session.request；不放宽 guard.py；
读不到字段时 blocked 而非假定为 0；写请求超时不重发。
完成后运行全部测试，更新 docs/TASKS.md 的状态表、在 docs/CHANGELOG.md 记一条，并如实说明验证级别
（离线 / GUI / 真机 / 线上）。不要泄露任何账号凭据。
```
