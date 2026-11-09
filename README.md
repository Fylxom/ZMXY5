# ZMXY5
ZMXY5 程序发布

## 下载

程序在右边的 [Releases](https://github.com/Fylxom/ZMXY5/releases) 里。打开程序后会自动检查新版本。

## 部分源码

这个程序是和 AI 一起写的（Python + PySide6）。这里放的是界面、程序骨架和各个任务，登录和通讯没有放，所以这些代码不能单独运行，只供参考。

| 位置 | 内容 |
| --- | --- |
| `zmxy/widgets.py` | 自绘控件：按钮、开关、勾选框、下拉框、弹窗等，不用系统自带外观 |
| `zmxy/mainwin/` | 主窗口按职责拆成多个文件：账号栏、各页面、定时页、设置等 |
| `zmxy/logtable.py`、`zmxy/dialogs.py` | 日志表格、账号编辑框 |
| `zmxy/taskdef.py`、`zmxy/runner.py` | 任务怎么定义、怎么一个接一个执行 |
| `zmxy/store.py` | 本机设置，密码用 Windows DPAPI 加密保存 |
| `zmxy/timeplan.py`、`zmxy/scheduling.py` | 定时规则，以及登记到 Windows 任务计划程序 |
| `zmxy/instance.py` | 只允许开一个程序 |
| `zmxy/appupdate.py` | 从本仓库的 Releases 自动更新 |
| `zmxy/tasks/` | 一个任务一个文件，`@task` 注册 |
| `docs/TASKS.md` | 给 AI 的交接文档：架构、规则、各模块进度 |
