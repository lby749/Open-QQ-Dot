"""Progress dialog for the on-demand NapCat runtime download."""
from __future__ import annotations

from PySide6.QtWidgets import QDialog, QLabel, QProgressBar, QPushButton, QVBoxLayout

from .common import LONG_POOL, run_async


class RuntimeDownloadDialog(QDialog):
    def __init__(self, home, parent=None):
        super().__init__(parent)
        self.home = home
        self.setWindowTitle("下载 QQ 直连运行时")
        self.setMinimumWidth(520)
        self.setModal(True)
        self.title = QLabel("正在准备从 NapCat 官方下载…")
        self.title.setWordWrap(True)
        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet("color:#64748b;font-size:12px;")
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.close_btn = QPushButton("取消")
        self.close_btn.clicked.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(10)
        layout.addWidget(self.title)
        layout.addWidget(self.bar)
        layout.addWidget(self.detail)
        layout.addWidget(self.close_btn)
        self.start()

    def start(self):
        from dotlink import download_official_runtime
        home = self.home

        def job(report):
            def progress(message, percent=None):
                report((message, percent))
            return download_official_runtime(home, progress=progress)

        def done(result):
            self.bar.setValue(100)
            self.detail.setText("NapCat 已配置到 qq-runtime；许可证和来源已保存。")
            self.accept()

        def failed(error):
            self.detail.setText(f"下载失败：{error}")
            self.close_btn.setText("关闭")
            try:
                self.close_btn.clicked.disconnect()
            except Exception:
                pass
            self.close_btn.clicked.connect(self.reject)

        def progress(value):
            if isinstance(value, tuple):
                message, percent = value
                self.detail.setText(str(message))
                if percent is not None:
                    self.bar.setValue(int(percent))
            else:
                self.detail.setText(str(value))

        run_async(job, done=done, error=failed, progress=progress, pool=LONG_POOL)
