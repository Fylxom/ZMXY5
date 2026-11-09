"""同一个设置目录只运行一个界面 / 后台执行实例，避免重复登录和覆盖设置。"""

from __future__ import annotations

from pathlib import Path
import time
from typing import Callable

from PySide6.QtCore import QLockFile, QObject
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from .scheduling import key


class Instance(QObject):
    def __init__(self, path: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.name = f"ZMXY5-{key(path)}"
        self.lock = QLockFile(str(path.with_name("instance.lock")))
        self.lock.setStaleLockTime(0)  # 只按进程是否还活着判断，不把长时间运行当成过期
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.UserAccessOption)

    def acquire(self) -> bool:
        return self.lock.tryLock(0)

    def listen(self, callback: Callable[[str], None]) -> None:
        # 必须先持有进程锁；Windows 允许两个 QLocalServer 同名监听，不能靠 listen 做互斥。
        if not self.lock.isLocked():
            raise RuntimeError("未取得实例锁")
        QLocalServer.removeServer(self.name)
        if not self.server.listen(self.name):
            raise OSError(f"无法建立程序本机通信：{self.server.errorString()}")

        def accept() -> None:
            while self.server.hasPendingConnections():
                socket = self.server.nextPendingConnection()

                def read(socket=socket) -> None:
                    if socket.canReadLine():
                        command = bytes(socket.readLine(32)).decode("ascii", errors="ignore").strip()
                        if command in ("show", "tick"):
                            callback(command)
                        socket.disconnectFromServer()

                socket.readyRead.connect(read)
                socket.disconnected.connect(socket.deleteLater)
                read()

        self.server.newConnection.connect(accept)

    def send(self, command: str) -> bool:
        """启动期间服务器可能尚未就绪，有限等待；等待对端处理后再销毁临时 QApplication。"""
        for _ in range(10):
            socket = QLocalSocket(self)  # 由实例持有连接，直到确认命令交付
            socket.connectToServer(self.name)
            if socket.waitForConnected(500):
                data = (command + "\n").encode("ascii")
                if socket.write(data) != len(data):
                    socket.abort()
                    return False
                socket.flush()
                deadline = time.monotonic() + 1.0
                while socket.bytesToWrite() > 0:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not socket.waitForBytesWritten(max(1, int(remaining * 1000))):
                        socket.abort()
                        return False
                # 服务端在解析完整命令、执行 callback 后会断开；等断开才说明
                # 事件循环实际收到了命令，避免当前这个临时 QApplication 先退出。
                if socket.state() != QLocalSocket.UnconnectedState:
                    socket.waitForDisconnected(1000)
                delivered = socket.state() == QLocalSocket.UnconnectedState
                socket.deleteLater()
                return delivered
            socket.deleteLater()
            time.sleep(0.1)
        return False

    def close(self) -> None:
        self.server.close()
        self.lock.unlock()
