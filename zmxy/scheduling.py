"""Windows 定时入口：每日与每周计划共用，任务计划程序每分钟唤起一次，没到点时立即退出。

同一 Windows 用户的交互会话运行，沿用用户自己的 DPAPI；不保存 Windows 密码。
这里只登记 / 移除本程序自己的任务，不登录游戏、不读取账号明文。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

from .store import Store, settings_path

NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
FLAG = "--scheduled-run"


def key(path: Path | None = None) -> str:
    return hashlib.sha256(os.path.normcase(str((path or settings_path()).resolve())).encode()).hexdigest()[:20]


def task_name(path: Path | None = None) -> str:
    return f"ZMXY5-Daily-{key(path)}"


def enabled(store: Store) -> bool:
    """只要有一个开着、能执行的定时项就保留系统入口，不受任务勾选影响。"""
    from . import globalcfg
    return globalcfg.enabled(store)


def launch_command() -> tuple[list[str], Path]:
    """Nuitka onefile 用外层 exe；源码用同一解释器的 pythonw，且固定工作目录。"""
    if "__compiled__" in globals() or getattr(sys, "frozen", False):
        exe = Path(sys.argv[0]).resolve()
        if not exe.is_file():
            raise OSError("找不到程序文件，请从保存好的 exe 重新打开程序")
        return [str(exe), FLAG], exe.parent
    python = Path(sys.executable).resolve()
    if os.name == "nt":
        python = python.with_name("pythonw.exe")
    if not python.is_file():
        raise OSError("找不到后台运行用的 Python，请重新安装 Python 或使用打包程序")
    return [str(python), "-m", "zmxy.entry", FLAG], Path(__file__).resolve().parents[1]


def task_xml(command: list[str], directory: Path, now: datetime | None = None) -> str:
    """执行路径 / 参数用 XML 转义和 Windows 参数引用，不经 cmd.exe。"""
    ET.register_namespace("", NS)
    root = ET.Element(f"{{{NS}}}Task", version="1.2")

    def add(parent, name, text=None, **attrs):
        element = ET.SubElement(parent, name, attrs)
        element.text = text
        return element

    registration = add(root, "RegistrationInfo")
    add(registration, "Description", "造梦西游5 定时：关闭窗口后仍按已保存的每日/每周设置执行；全部关闭定时后自动移除。")
    triggers = add(root, "Triggers")
    clock = add(triggers, "TimeTrigger")
    repeat = add(clock, "Repetition")
    add(repeat, "Interval", "PT1M")
    add(repeat, "StopAtDurationEnd", "false")
    start = ((now or datetime.now()) + timedelta(minutes=1)).replace(second=0, microsecond=0)
    add(clock, "StartBoundary", start.isoformat())
    add(clock, "Enabled", "true")
    logon = add(triggers, "LogonTrigger")
    add(logon, "Enabled", "true")
    add(logon, "UserId", "__CURRENT_USER_SID__")
    principals = add(root, "Principals")
    principal = add(principals, "Principal", id="User")
    add(principal, "UserId", "__CURRENT_USER_SID__")
    add(principal, "LogonType", "InteractiveToken")
    add(principal, "RunLevel", "LeastPrivilege")
    settings = add(root, "Settings")
    for name, value in (("MultipleInstancesPolicy", "IgnoreNew"), ("DisallowStartIfOnBatteries", "false"),
                        ("StopIfGoingOnBatteries", "false"), ("AllowHardTerminate", "false"),
                        ("StartWhenAvailable", "true"), ("RunOnlyIfNetworkAvailable", "false"),
                        ("AllowStartOnDemand", "true"), ("Enabled", "true"), ("Hidden", "true"),
                        ("WakeToRun", "false"), ("ExecutionTimeLimit", "PT0S")):
        add(settings, name, value)
    action = add(add(root, "Actions", Context="User"), "Exec")
    add(action, "Command", command[0])
    add(action, "Arguments", subprocess.list2cmdline(command[1:]))
    add(action, "WorkingDirectory", str(directory))
    return ET.tostring(root, encoding="unicode")


def sync(store: Store) -> bool:
    """同步当前程序路径和定时开关；返回是否启用。失败交给界面明确显示。"""
    active = enabled(store)
    command, directory = launch_command() if active else ([], Path.cwd())
    register(task_name(store.path), task_xml(command, directory) if active else None)
    return active


def register(name: str, xml: str | None, *, validate: bool = False) -> None:
    """注册 / 删除唯一的任务名。validate 只调用 Windows 的 XML 校验，不安装或执行。"""
    if os.name != "nt":
        raise OSError("后台定时需要 Windows 任务计划程序")
    payload = base64.b64encode(json.dumps({"name": name, "xml": xml, "validate": validate}).encode()).decode()
    script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
try {
$data = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PAYLOAD__')) | ConvertFrom-Json
$service = New-Object -ComObject 'Schedule.Service'
$service.Connect()
$folder = $service.GetFolder('\')
if ($null -eq $data.xml) {
    foreach ($item in $folder.GetTasks(1)) {
        if ($item.Name -eq $data.name) { $folder.DeleteTask($data.name, 0) }
    }
} else {
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $xml = $data.xml.Replace('__CURRENT_USER_SID__', $sid)
    $flags = if ($data.validate) { 1 } else { 6 }
    $null = $folder.RegisterTask($data.name, $xml, $flags, $sid, $null, 3, $null)
}
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
""".replace("__PAYLOAD__", payload)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                            capture_output=True, timeout=25, creationflags=0x08000000)
    if result.returncode:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
        raise OSError(f"Windows 任务计划程序登记失败：{detail[:500] or result.returncode}")
