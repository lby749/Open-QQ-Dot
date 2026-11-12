"""vNext account, device, health and experience overview in the Fluent shell."""
from pathlib import Path

from qfluentwidgets import BodyLabel, LineEdit, PushButton
from PySide6.QtWidgets import QMessageBox

from house_status import ROOT
from .common import Card, Page, hint, row


class VNextPage(Page):
    def __init__(self, house):
        super().__init__(house, 'vNextPage')
        self.refresh_button = PushButton('刷新 vNext 数据')
        self.refresh_button.clicked.connect(self.refresh)
        self.test_qq = LineEdit(); self.test_qq.setPlaceholderText('好友 QQ（必须已绑定）')
        self.test_text = LineEdit(); self.test_text.setPlaceholderText('测试消息内容')
        self.test_button = PushButton('发送真实测试消息')
        self.test_button.clicked.connect(self.send_test)
        self.test_status = BodyLabel('')
        self.invite_status = BodyLabel('')
        self.invite_button = PushButton('邀请当前好友通话')
        self.invite_button.clicked.connect(self.invite_call)
        self.status = BodyLabel('读取中…')
        self.status.setWordWrap(True)
        self.body = BodyLabel('读取中…')
        self.body.setWordWrap(True)
        self.add(Card('vNext 功能聚合', actions=(self.refresh_button,)).add(
            hint('账号目录、Provider 路由、已配对设备、健康事件与经验库；只读取本机真实数据。'),
            row(self.invite_button, self.invite_status, None),
            self.status,
            self.body,
            hint('真实 QQ 发送需要你明确填写好友 QQ 和内容；不会自动发给任何人。'),
            row(self.test_qq, self.test_text, self.test_button, None),
            self.test_status))

    def load(self):
        self.refresh()

    def shown(self):
        self.refresh()

    def send_test(self):
        qq = self.test_qq.text().strip()
        text = self.test_text.text().strip()
        if not qq or not text:
            self.test_status.setText('请填写好友 QQ 和测试内容')
            return
        if QMessageBox.question(self, '确认真实发送', f'将向 QQ {qq} 真实发送：\n\n{text}\n\n继续吗？') != QMessageBox.Yes:
            self.test_status.setText('已取消发送')
            return
        try:
            import agent_service
            receipt = agent_service.send_friend_message(qq, text)
            self.test_status.setText(f"已发送，message_id={receipt.get('message_id')}；本地保存={receipt.get('local_saved')}")
            toast(self.house, 'success', f'QQ {qq} 已返回发送回执', '真实发送')
        except Exception as exc:
            self.test_status.setText(f'发送失败：{exc}')

    def invite_call(self):
        qq = getattr(self.house, 'current_qq', None)
        if not qq:
            self.invite_status.setText('请先选择好友')
            return
        try:
            import voice_invites
            row = voice_invites.create(ROOT, str(qq))
            self.invite_status.setText(f"已邀请 {qq}，邀请 {row['id']} 在 2 分钟内有效；手机打开通话页会收到接听提示。")
            toast(self.house, 'success', '通话邀请已发送到手机', '语音邀请')
        except Exception as exc:
            self.invite_status.setText(f'邀请失败：{exc}')

    def refresh(self):
        status = []
        apk = ROOT / 'mobile' / 'app' / 'build' / 'outputs' / 'apk' / 'release' / 'app-release.apk'
        status.append(f"Android APK：{'已构建 %.2f MiB' % (apk.stat().st_size / 1048576) if apk.is_file() else '未构建'}")
        status.append('AccountService / ProviderRouter：已接入本页账号数据')
        status.append('DeviceGateway：已接入本页设备列表')
        status.append('HealthBridge：已接入本页健康来源')
        status.append('ExperienceEngine：已接入本页经验计数')
        try:
            import task_session_service
            sessions = task_session_service.list_sessions(ROOT)
            status.append(f'TaskSession 冻结：{len(sessions)} 个')
            for item in sessions[:5]:
                lines.append(f"  · {item.get('task_id', '')[:32]}  {item.get('provider', '')} / {item.get('model', '')}")
        except Exception:
            status.append('TaskSession 冻结：读取失败')
        status.append('语音 WSS：代码已存在；真机双向通话仍需验收')
        self.status.setText('  ·  '.join(status))
        lines = []
        try:
            import account_service
            accounts = account_service.list_accounts(ROOT)
            bindings = account_service.bindings(ROOT)
            lines.append(f'账号目录：{len(accounts)} 个；已绑定好友：{len(bindings)} 个')
            for item in accounts[:8]:
                lines.append(f"  · {item.get('label') or item.get('id')}  "
                             f"{item.get('provider', '')} / {item.get('model', '')}")
        except Exception as exc:
            lines.append(f'账号目录读取失败：{type(exc).__name__}: {exc}')
        try:
            from device_gateway import DeviceStore
            devices = DeviceStore(ROOT / 'data' / 'vnext_devices.json').list_devices()
            lines.append(f'已配对设备：{len(devices)} 台')
            for device in devices[:6]:
                lines.append(f"  · {device.get('name')}  {device.get('id')}  "
                             f"{'、'.join(device.get('scopes') or [])}")
        except Exception as exc:
            lines.append(f'设备读取失败：{type(exc).__name__}: {exc}')
        try:
            from health_bridge import HealthBridge
            status = HealthBridge(ROOT / 'data' / 'health_bridge.json').status()
            supported = [x for x in status.get('adapters', []) if x.get('supported')]
            lines.append(f"健康来源：{len(supported)} 个可用；事件：{status.get('event_count', 0)}"
                         f"{'（有真实事件）' if status.get('has_real_events') else '（暂无真实事件）'}")
        except Exception as exc:
            lines.append(f'健康读取失败：{type(exc).__name__}: {exc}')
        try:
            import experience_service
            snap = experience_service.snapshot(ROOT, owner=self.house.current_qq)
            lines.append(f"经验库：{snap.get('total', 0)} 条；已验证 {snap.get('verified', 0)}；"
                         f"候选 {int((snap.get('by_state') or {}).get('CANDIDATE', 0))}")
        except Exception as exc:
            lines.append(f'经验读取失败：{type(exc).__name__}: {exc}')
        self.body.setText('\n'.join(lines))
