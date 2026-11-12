"""Native configuration cards for the existing trusted agent adapters."""
import json
import os
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QHBoxLayout, QSpinBox, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CheckBox, ComboBox, LineEdit, PushButton

from agent_tool_adapters import BUILTINS, LocalOpenCode, probe_adapter, validate_command
from .common import Card, LONG_POOL, hint, row, run_async, toast


class AdapterSettings:
    def __init__(self, page):
        self.page = page
        self.fields = {}
        self.cards = {}
        self.saved = {}
        self.models = []  # the last OpenCode model list fetched from the local service
        for backend, spec in BUILTINS.items():
            title = {'acp': '自定义智能体（ACP）', 'mcp': '工具服务（MCP）'}.get(backend, spec.name)
            status = hint('尚未检测；安装状态与登录状态分别验证。')
            test = PushButton('检测接入')
            test.clicked.connect(lambda _, key=backend: self.probe(key))
            prepare = PushButton('一键下载并启动' if backend == 'opencode'
                                 else '配置检查' if backend in ('acp', 'mcp') else '一键安装/打开')
            prepare.clicked.connect(lambda _, key=backend: self.prepare(key))
            card = Card(title, actions=(prepare, test))
            self.fields[backend] = fields = {'status': status, 'test': test,
                                             'prepare': prepare, 'install': prepare}
            if backend == 'opencode':
                # 2026-10-26 user: read the models from the service and pick one, with a thinking-effort
                # dropdown — the same shape as the Codex / Claude / Antigravity cards, not a free-text box.
                fields['url'] = LineEdit()
                fields['url'].setMinimumWidth(300)
                fields['url'].setPlaceholderText('留空自动发现本机服务；也可填 http://127.0.0.1:<端口>')
                fields['model'] = ComboBox()
                fields['model'].setMinimumWidth(280)
                fields['variant'] = ComboBox()
                fields['variant'].setMinimumWidth(120)
                fields['model'].currentIndexChanged.connect(lambda *_: self.refresh_variants())
                fetch = PushButton('提取可用模型')
                fetch.clicked.connect(lambda _=False: self.fetch_models())
                card.add(row(BodyLabel('本机服务地址'), fields['url'], None),
                         row(BodyLabel('模型'), fields['model'], fetch,
                             BodyLabel('思考档位'), fields['variant'], None),
                         hint('地址留空时自动发现 OpenCode 桌面版的后台服务（端口和登录口令都会自动读取）。'
                              '点「提取可用模型」读取该账号实际可用的模型；思考档位来自 OpenCode 模型自带的档位。'
                              '额度与隐私限制仍由 OpenCode 账号决定。'
                              '没安装时点「一键下载并启动」：从官方 GitHub Release 下载桌面版、校验 SHA-256、'
                              '安装并打开；登录由你在 OpenCode 里完成。'))
            elif backend == 'mcp':
                # 2026-10-26 user: 工具服务不止接一个——可以同时接多个，每个单独检测、单独启用。
                fields['rows'] = []
                fields['rows_box'] = QWidget()
                fields['rows_layout'] = QVBoxLayout(fields['rows_box'])
                fields['rows_layout'].setContentsMargins(0, 0, 0, 0)
                fields['rows_layout'].setSpacing(8)
                add = PushButton('＋ 添加工具服务')
                add.clicked.connect(lambda _=False: self.add_mcp_row())
                fields['add'] = add
                card.add(fields['rows_box'], row(add, None),
                         hint('可以同时接多个工具服务，每个单独检测、单独启用。'
                              '打开本页会自动检测一次，检测通过后卡片自动收起。'))
            else:
                fields['program'], fields['args'] = LineEdit(), LineEdit()
                fields['program'].setPlaceholderText(spec.executable or '可信本机程序的完整路径')
                fields['args'].setPlaceholderText('启动参数，例如 ["acp"]；留空表示无附加参数')
                card.add(row(BodyLabel('程序路径'), fields['program']), row(BodyLabel('启动参数'), fields['args']))
                if backend != 'mcp':
                    fields['protocol'] = QSpinBox()
                    fields['protocol'].setRange(1, 2)
                    if backend in ('acp', 'kimi_code'):
                        card.add(row(BodyLabel('协议版本'), fields['protocol'], None))
            if backend == 'mcp':
                fields['enabled'] = CheckBox('启用工具服务，供 Codex 与 Claude Code 调用')
                card.add(fields['enabled'], hint('工具服务不作为独立思考模型；沿用下方命令执行与电脑访问授权。'))
            elif backend != 'opencode':  # OpenCode 的模型与思考档位就在上面选，不再沿用工具自身默认
                card.add(hint('与上方 CLI 共用主智能体、交接顺序和控制台；模型沿用工具自身配置。'))
            card.add(status)
            self.cards[backend] = page.add(card)

    def load(self, profile):
        self.saved = profile.get('agent_adapters') or {}
        for backend, fields in self.fields.items():
            config = self.saved.get(backend) or {}
            if backend == 'mcp':
                self._load_mcp(fields, config)
                continue
            if 'url' in fields:
                fields['url'].setText(config.get('url', ''))
                model, variant = config.get('model', ''), config.get('variant', '')
                fields['model'].blockSignals(True)
                fields['model'].clear()
                if model:
                    fields['model'].addItem(model, userData=model)
                fields['model'].blockSignals(False)
                fields['variant'].clear()
                fields['variant'].addItem('默认', userData='')
                if variant:
                    fields['variant'].addItem(variant, userData=variant)
                    fields['variant'].setCurrentIndex(1)
            else:
                command = config.get('command') or []
                fields['program'].setText(command[0] if command else '')
                args = command[1:] if command else list(BUILTINS[backend].args)
                fields['args'].setText(json.dumps(args, ensure_ascii=False))
                if 'protocol' in fields:
                    fields['protocol'].setValue(int(config.get('protocol_version', 1)))
            if 'enabled' in fields:
                fields['enabled'].setChecked(bool(config.get('enabled')))
            fields['status'].setText('尚未检测；沿用工具本机登录配置。')

    def _clear_mcp_rows(self, fields):
        while fields['rows_layout'].count():
            item = fields['rows_layout'].takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        fields['rows'] = []

    def _load_mcp(self, fields, config):
        self._clear_mcp_rows(fields)
        entries = list(config.get('servers') or [])
        if not entries and config.get('command'):
            entries = [{'name': 'external_tools', 'command': config.get('command'),
                        'enabled': config.get('enabled', True)}]
        for entry in entries:
            if isinstance(entry, dict):
                self.add_mcp_row(entry)
        fields['enabled'].setChecked(bool(config.get('enabled', True)))
        fields['status'].setText('可以同时接多个工具服务；每个单独检测、单独启用。')

    def add_mcp_row(self, entry=None):
        fields = self.fields.get('mcp')
        if not fields:
            return
        entry = entry or {}
        holder = QWidget()
        line = QHBoxLayout(holder)
        line.setContentsMargins(0, 0, 0, 0)
        name = LineEdit()
        name.setPlaceholderText('名称，如 github')
        name.setText(str(entry.get('name') or f'tool_{len(fields["rows"]) + 1}'))
        command = LineEdit()
        command.setPlaceholderText('命令 JSON 数组，如 ["python", "server.py"]')
        raw_command = entry.get('command')
        command.setText(json.dumps(raw_command, ensure_ascii=False) if isinstance(raw_command, list) else str(raw_command or ''))
        enabled = CheckBox('启用')
        enabled.setChecked(bool(entry.get('enabled', True)))
        remove = PushButton('移除')
        line.addWidget(name, 1)
        line.addWidget(command, 3)
        line.addWidget(enabled)
        line.addWidget(remove)
        row = {'widget': holder, 'name': name, 'command': command, 'enabled': enabled}
        remove.clicked.connect(lambda _=False: self._remove_mcp_row(row))
        fields['rows'].append(row)
        fields['rows_layout'].addWidget(holder)

    def _remove_mcp_row(self, row):
        fields = self.fields.get('mcp')
        if not fields or row not in fields['rows']:
            return
        fields['rows'].remove(row)
        row['widget'].setParent(None)
        row['widget'].deleteLater()

    def _value_mcp(self, fields):
        import shlex
        servers = []
        for row in fields['rows']:
            name = row['name'].text().strip()
            text = row['command'].text().strip()
            if not text:
                continue
            try:
                command = json.loads(text)
            except ValueError:
                command = shlex.split(text, posix=False)
            if not isinstance(command, list) or not command:
                raise ValueError('工具服务命令须为 JSON 数组或可执行命令')
            command = [str(item).strip('"') for item in command]
            validate_command(command)
            servers.append({'name': name or f'tool_{len(servers) + 1}',
                            'command': command, 'enabled': row['enabled'].isChecked()})
        enabled = fields['enabled'].isChecked()
        if enabled and servers and not any(item['enabled'] for item in servers):
            raise ValueError('启用工具服务前，请至少勾选一个工具服务')
        if not servers:
            enabled = False
        result = dict(self.saved.get('mcp') or {})
        result.update({'enabled': enabled, 'servers': servers})
        if servers:
            result['command'] = next((item['command'] for item in servers if item['enabled']), servers[0]['command'])
        else:
            result.pop('command', None)
        return result

    def value(self, backend):
        fields = self.fields[backend]
        value = dict(self.saved.get(backend) or {})
        if backend == 'mcp':
            return self._value_mcp(fields)
        if 'url' in fields:
            # Empty means "discover the local service"; an explicit address still has to be loopback 127.0.0.1.
            url = fields['url'].text().strip()
            model = fields['model'].currentData() or ''
            variant = fields['variant'].currentData() or ''
            LocalOpenCode(url, model=model, variant=variant)  # validates, without connecting
            if self.models:
                value['model_options'] = {f"{item['provider']}/{item['id']}": list(item.get('variants') or [])
                                          for item in self.models if item.get('enabled')}
            for key, item in (('url', url), ('model', model), ('variant', variant)):
                if item:
                    value[key] = item
                else:
                    value.pop(key, None)
        else:
            program = fields['program'].text().strip()
            args = json.loads(fields['args'].text().strip() or '[]')
            if not isinstance(args, list):
                raise ValueError('启动参数须为 JSON 字符串数组')
            if program:
                value['command'] = [program, *args]
                validate_command(value['command'])
            else:
                value.pop('command', None)
                if args != list(BUILTINS[backend].args):
                    raise ValueError('填写启动参数时，也请设置程序路径')
            if 'protocol' in fields:
                value['protocol_version'] = fields['protocol'].value()
        if 'enabled' in fields:
            value['enabled'] = fields['enabled'].isChecked()
            if value['enabled'] and not value.get('command'):
                raise ValueError('启用工具服务前，请设置程序路径')
        return value

    def values(self):
        values = dict(self.saved)
        values.update({key: self.value(key) for key in self.fields})
        for backend in self.page.get_checked_providers():
            if backend == 'acp' and not values[backend].get('command'):
                raise ValueError('自定义智能体需要设置可信程序路径')
        return values

    def prepare(self, backend):
        if backend == 'opencode':
            self.install_opencode()
        else:
            self.page.prepare_agent(backend)

    def install_opencode(self):
        """Download the official desktop app, install it, and open it for manual sign-in."""
        fields = self.fields['opencode']
        button = fields.get('install')
        if button is not None:
            button.setEnabled(False)
        fields['status'].setText('正在准备 OpenCode 桌面版…')

        def progress(value):
            if isinstance(value, tuple):
                percent, message = value
                fields['status'].setText(f'{message}（{int(percent)}%）')
            else:
                fields['status'].setText(str(value))

        def done(message):
            if button is not None:
                button.setEnabled(True)
            fields['status'].setText(message)
            toast(self.page, 'success', message, 'OpenCode')

        def failed(exc):
            if button is not None:
                button.setEnabled(True)
            fields['status'].setText('OpenCode 安装未完成：' + str(exc))
            toast(self.page, 'error', str(exc), 'OpenCode')

        def job(report):
            import opencode_install
            return opencode_install.install_and_launch(report)

        run_async(job, done, failed, progress=progress, pool=LONG_POOL)

    def fetch_models(self):
        """「提取可用模型」: ask the local OpenCode service which models and effort tiers it offers."""
        fields = self.fields['opencode']
        try:
            config = self.value('opencode')
        except (ValueError, TypeError) as exc:
            fields['status'].setText('配置有误：' + str(exc))
            return
        fields['status'].setText('正在读取模型清单…')
        client = LocalOpenCode(config.get('url') or '',
                               password=os.environ.get('OPENCODE_SERVER_PASSWORD', ''))

        def done(models):
            self.models = models
            combo = fields['model']
            current = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            for item in models:
                if not item['enabled']:
                    continue
                combo.addItem(f"{item['name']} · {item['provider']}/{item['id']}",
                              userData=f"{item['provider']}/{item['id']}")
            index = combo.findData(current) if current else -1
            if index >= 0:
                combo.setCurrentIndex(index)
            combo.blockSignals(False)
            self.refresh_variants()
            fields['status'].setText(
                f'已读取 {combo.count()} 个可用模型；选中模型后可切换思考档位。' if combo.count()
                else '该账号当前没有可用模型；请先在 OpenCode 里登录或订阅。')

        run_async(lambda _: client.list_models(), done,
                  lambda exc: fields['status'].setText('读取模型失败：' + str(exc)), pool=LONG_POOL)

    def refresh_variants(self):
        """The selected model's own thinking-effort tiers; '默认' leaves OpenCode's choice alone."""
        fields = self.fields.get('opencode')
        if not fields or 'variant' not in fields:
            return
        key = fields['model'].currentData() or ''
        item = next((m for m in self.models if f"{m['provider']}/{m['id']}" == key), None)
        combo = fields['variant']
        current = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem('默认', userData='')
        for variant in (item or {}).get('variants') or []:
            combo.addItem(variant, userData=variant)
        index = combo.findData(current) if current else -1
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)

    def probe(self, backend):
        fields = self.fields[backend]
        try:
            config = self.value(backend)
        except (ValueError, TypeError) as exc:
            fields['status'].setText('配置有误：' + str(exc))
            return
        owner = self.page.house.current_qq
        directory = Path(self.page.house.friend_dir) if owner else Path.cwd()
        fields['test'].setEnabled(False)
        fields['status'].setText('正在检测…')
        def done(message):
            fields['test'].setEnabled(True)
            if self.page.house.current_qq == owner:
                fields['status'].setText(message)
        run_async(lambda _: probe_adapter(backend, config, directory), done,
                  lambda exc: done('接入检测失败：' + str(exc)), pool=LONG_POOL)
