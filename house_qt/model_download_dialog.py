"""Progress dialog for the first-run local model download."""
from __future__ import annotations

import threading

from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout
from qfluentwidgets import PrimaryPushButton

from .common import LONG_POOL, notify, run_async, toast


class ModelDownloadDialog(QDialog):
    def __init__(self, items, parent=None, start=True):
        super().__init__(parent)
        self.backgrounding = False
        self.setWindowTitle("下载本地模型")
        self.setMinimumWidth(480)
        self.setModal(True)
        self.items = [(kind, tier) for kind, tier in items] if isinstance(items, (list, tuple)) else [('llm', items)]
        self.cancelled = threading.Event()
        self.title = QLabel("正在准备下载本地辅助模型…")
        self.title.setWordWrap(True)
        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet("color:#64748b;font-size:12px;")
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.background = PrimaryPushButton("后台下载")
        self.background.clicked.connect(self._background)
        self.cancel = QPushButton("取消下载")
        self.cancel.clicked.connect(self._cancel)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(10)
        layout.addWidget(self.title)
        layout.addWidget(self.bar)
        layout.addWidget(self.detail)
        buttons = QHBoxLayout()
        buttons.addWidget(self.background)
        buttons.addWidget(self.cancel)
        layout.addLayout(buttons)
        if start:
            self.start()

    def start(self):
        from model_downloader import MODEL_CATALOG, download_model
        import local_runtimes

        self.runtimes_installed = False

        def job(report):
            # A model family can need a Python runtime the installer deliberately left
            # out (torch for BGE embeddings). Install it here, before the weights, so the
            # download ends in a ready environment.
            runtimes = [runtime for runtime in local_runtimes.runtimes_for(self.items)
                        if not local_runtimes.imports_ready(runtime)]
            phases = max(1, len(runtimes) + len(self.items))
            results = []
            for position, runtime in enumerate(runtimes):
                if self.cancelled.is_set():
                    raise RuntimeError("下载已取消")

                def runtime_report(percent, message, position=position):
                    progress = max(0, min(100, percent or 0))
                    report((int((position + progress / 100) / phases * 100), message))

                try:
                    outcome = local_runtimes.install(runtime, emit=runtime_report,
                                                     cancel_event=self.cancelled)
                except Exception as exc:  # no venv / no uv: still download the weights
                    report((0, f'运行环境未安装（{exc}），继续下载模型……'))
                    outcome = {'installed': False}
                if outcome.get('installed'):
                    self.runtimes_installed = True
                elif outcome.get('message'):
                    report((0, f"{outcome['message']}；仅下载模型"))
            offset = len(runtimes)
            for index, (kind, tier) in enumerate(self.items):
                if self.cancelled.is_set():
                    raise RuntimeError("下载已取消")
                def callback(percent, message, index=index):
                    overall = int((offset + index + max(0, min(100, percent)) / 100) / phases * 100)
                    report((overall, message))
                results.append(download_model(kind, tier,
                                              progress_callback=callback,
                                              cancel_event=self.cancelled,
                                              mirror="https://hf-mirror.com"))
            return results

        def done(_result):
            extra = "；运行环境已一并安装，重启程序后生效" if getattr(self, "runtimes_installed", False) else ""
            if self.backgrounding:
                notify(None, "本地模型下载完成", f"本地辅助模型已下载并装配完成{extra}。")
                self.deleteLater()
                return
            self.bar.setValue(100)
            self.detail.setText(f"下载并装配完成{extra}。")
            self.cancel.setText("关闭")
            self.cancel.clicked.disconnect()
            self.cancel.clicked.connect(self.accept)
            self.background.setEnabled(False)
            self.title.setText("本地辅助模型已就绪")

        def failed(error):
            if self.backgrounding:
                notify(None, "本地模型下载失败", f"后台下载失败：{error}")
                self.deleteLater()
                return
            self.detail.setText(f"下载失败：{error}")
            self.cancel.setText("关闭")
            self.cancel.clicked.disconnect()
            self.cancel.clicked.connect(self.reject)
            self.background.setEnabled(False)

        def progress(value):
            if isinstance(value, tuple):
                percent, message = value
                self.bar.setValue(int(percent))
                self.detail.setText(str(message))
            else:
                self.detail.setText(str(value))

        run_async(job, done=done, error=failed, progress=progress, pool=LONG_POOL)

    def _background(self):
        if self.backgrounding:
            return
        self.backgrounding = True
        self.setParent(None)
        self.hide()
        self.done(0)
        toast(None, 'info', '已转入后台下载；完成后会弹出提醒，不会打断当前操作。', '本地模型')

    def _set_title(self, text):
        self.title.setText(text)

    def _cancel(self):
        self.cancelled.set()
        self.detail.setText("正在中止…")
