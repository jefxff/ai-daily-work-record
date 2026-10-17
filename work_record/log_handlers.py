"""日志 Handler：Windows 下安全的按大小轮转。

标准库 RotatingFileHandler 在轮转时对 debug.log 做 rename，若文件句柄被其他
进程（如另一个 runserver 进程）占用会抛 PermissionError，导致日志通道中断。
这里在 rename 失败时降级为截断当前文件，保证日志可继续写入（文件句柄以追加
模式打开，截断后下一次写入自动落在新 EOF，不会出现 NUL 填充）。
"""

import os
from logging.handlers import RotatingFileHandler


class SafeRotatingFileHandler(RotatingFileHandler):
    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        try:
            if self.backupCount > 0:
                for i in range(self.backupCount - 1, 0, -1):
                    sfn = self.rotation_filename("%s.%d" % (self.baseFilename, i))
                    dfn = self.rotation_filename("%s.%d" % (self.baseFilename, i + 1))
                    if os.path.exists(sfn):
                        if os.path.exists(dfn):
                            os.remove(dfn)
                        os.replace(sfn, dfn)
                dfn = self.rotation_filename(self.baseFilename + ".1")
                if os.path.exists(dfn):
                    os.remove(dfn)
                os.replace(self.baseFilename, dfn)
        except OSError:
            # Windows：目标文件被运行中的服务进程占用，rename 失败 → 截断当前文件
            try:
                with open(self.baseFilename, "r+b") as f:
                    f.truncate(0)
            except OSError:
                pass
        if not self.delay:
            self.stream = self._open()
