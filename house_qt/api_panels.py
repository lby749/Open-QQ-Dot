"""Retrieval and function API cards, using the existing house controls."""
from ui_locale import tr

import json

from PySide6.QtWidgets import QFileDialog, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CheckBox, ComboBox, LineEdit, PasswordLineEdit, PlainTextEdit, PushButton

from api_backend import key_path, save_key
from house_status import load_profile, write_profile
from .common import Card, hint, row


class APIPanels:
    def __init__(self, house):
        self.house = house
        self.retrieval = Card(tr('Retrieval API'))
        self.endpoints = {}
        self.local_options = {}
        from small_models import PRESETS
        for kind, title in (('embedding', tr('Embedding Model')), ('rerank', tr('Rerank Model'))):

            enabled, url, model, key = CheckBox(tr('Enable ') + title), LineEdit(), LineEdit(), PasswordLineEdit()
            url.setPlaceholderText(tr('Base URL (including /v1) or full endpoint; local services allowed'))
            model.setPlaceholderText(tr('Provider model ID or local model name'))
            key.setPlaceholderText(tr('Dedicated API Key; leave empty to keep existing'))
            dimension = LineEdit()
            dimension.setPlaceholderText(tr('Must match model output dimension'))
            self.endpoints[kind] = (enabled, url, model, dimension, key)
            mode, preset, pooling, device = ComboBox(), ComboBox(), ComboBox(), ComboBox()
            mode.addItems(['接口服务（远程或本机）', '本地模型目录'])
            preset.addItems([tr('Manual Configuration')] + list(PRESETS[kind]))
            for label, code in (('均值聚合（mean）', 'mean'), ('首位向量（cls）', 'cls')):
                pooling.addItem(label, userData=code)
            for label, code in (('自动选择', 'auto'), ('CUDA 显卡', 'cuda'), ('苹果图形处理器（MPS）', 'mps'), ('处理器（CPU）', 'cpu')):
                device.addItem(label, userData=code)
            path, query_prefix, document_prefix = LineEdit(), LineEdit(), LineEdit()
            path.setPlaceholderText(tr('Downloaded Transformers model directory; does not auto-download'))
            pick = PushButton(tr('Browse Directory'))
            pick.clicked.connect(lambda _, edit=path: edit.setText(
                QFileDialog.getExistingDirectory(self.house, '选择已有的模型目录') or edit.text()))
            api_fields = QWidget()
            api_layout = QVBoxLayout(api_fields)
            api_layout.setContentsMargins(0, 0, 0, 0)
            api_layout.addLayout(row(BodyLabel(tr('API URL')), url))
            api_layout.addLayout(row(BodyLabel(tr('API Key')), key))
            local_fields = Card(tr('Local Model Settings')).add(row(BodyLabel(tr('Preset')), preset),
                row(BodyLabel(tr('Local Path')), path, pick), row(BodyLabel(tr('Device')), device, None))
            if kind == 'embedding':
                local_fields.add(row(BodyLabel(tr('Pooling')), pooling, None),
                    row(BodyLabel(tr('Query Prefix')), query_prefix), row(BodyLabel(tr('Doc Prefix')), document_prefix))
            self.local_options[kind] = {'mode': mode, 'preset': preset, 'path': path, 'pooling': pooling,
                'device': device, 'query_prefix': query_prefix, 'document_prefix': document_prefix,
                'api_fields': api_fields, 'local_fields': local_fields}
            mode.currentIndexChanged.connect(lambda index, a=api_fields, l=local_fields:
                                             (a.setVisible(index == 0), l.setVisible(index == 1)))
            preset.currentTextChanged.connect(lambda text, k=kind: self.apply_preset(k, text))
            local_fields.hide()
            self.retrieval.add(enabled, row(BodyLabel(tr('Source')), mode, None), api_fields,
                               row(BodyLabel(tr('Model')), model))
            if kind == 'embedding':
                self.retrieval.add(row(BodyLabel(tr('Dimension')), dimension))
            self.retrieval.add(local_fields)
        self.retrieval.add(hint('API sources send queries and candidate texts to the configured service; local sources read local models only. Falls back to keyword/BM25 if unconfigured or failed. '
                                'Vector cache is rebuilt when switching embedding models; rerank scores relevance without faking program scores as model scores.'))
        self.functions = []
        self.active = -1
        self.pending_keys = {}
        self.selector = ComboBox()
        add = PushButton(tr('Add Function API'))
        add.clicked.connect(self.add_function)
        self.selector.currentIndexChanged.connect(self.select_function)
        self.enabled = CheckBox(tr('Allow model to invoke this Function API for this friend'))
        self.name, self.description, self.url = LineEdit(), LineEdit(), LineEdit()
        self.name.setPlaceholderText(tr('Tool name, e.g. api_weather'))
        self.description.setPlaceholderText(tr('Describe when the model should call it and what it returns'))
        self.url.setPlaceholderText(tr('Static HTTP(S) endpoint URL'))
        self.method, self.header = ComboBox(), ComboBox()
        self.method.addItems(['GET', 'POST'])
        self.header.addItems(['Authorization', 'X-API-Key'])
        self.key = PasswordLineEdit()
        self.key.setPlaceholderText(tr('API key for this function; leave empty to keep existing'))
        self.schema, self.fixed = PlainTextEdit(), PlainTextEdit()
        self.schema.setMinimumHeight(120)
        self.fixed.setMaximumHeight(100)
        self.function = Card(tr('Function API')).add(row(BodyLabel(tr('Configured APIs')), self.selector, add), self.enabled,
            row(BodyLabel(tr('Tool Name')), self.name), row(BodyLabel(tr('Description')), self.description),
            row(BodyLabel('URL'), self.url), row(BodyLabel(tr('Method')), self.method, BodyLabel(tr('Auth Header')), self.header, None),
            row(BodyLabel(tr('API Key')), self.key), BodyLabel(tr('Parameters Schema (JSON Schema)')), self.schema,
            BodyLabel(tr('Fixed Parameters (JSON object, optional)')), self.fixed,
            hint('Each API saves keys independently. Enabled APIs become tools available to CLI and chat models. '
                 'GET parameters are query params, POST parameters form JSON body; return payload is treated as data.'))

    def apply_preset(self, kind, name):
        from small_models import PRESETS
        value = PRESETS[kind].get(name)
        if value is None:
            return
        controls = self.local_options[kind]
        self.endpoints[kind][2].setText(name)
        if kind == 'embedding':
            self.endpoints[kind][3].setText(str(value['dimension']))
            controls['pooling'].setCurrentIndex(max(0, controls['pooling'].findData(value['pooling'])))
            controls['query_prefix'].setText(value.get('query_prefix', ''))
            controls['document_prefix'].setText(value.get('document_prefix', ''))
        from model_downloader import MODEL_CATALOG, project_root
        for cat in MODEL_CATALOG.get(kind, {}).values():
            if cat['name'] == name or cat['repo_id'] == name:
                p = project_root() / cat['target_dir']
                if p.is_dir() and (p / 'config.json').is_file():
                    controls['path'].setText(str(p.resolve()))
                break

    def load(self, profile):
        for kind, (enabled, url, model, dimension, key) in self.endpoints.items():
            value = (profile.get('retrieval_api') or {}).get(kind) or {}
            enabled.setChecked(value.get('enabled', False))
            url.setText(value.get('base_url', ''))
            model.setText(value.get('model', ''))
            dimension.setText(str(value.get('dimension') or 1024))
            local = self.local_options[kind]
            local['mode'].setCurrentIndex(1 if value.get('mode') == 'local' else 0)
            local['path'].setText(value.get('model_path', ''))
            local['pooling'].setCurrentIndex(max(0, local['pooling'].findData(value.get('pooling', 'mean'))))
            local['device'].setCurrentIndex(max(0, local['device'].findData(value.get('device', 'auto'))))
            local['query_prefix'].setText(value.get('query_prefix', ''))
            local['document_prefix'].setText(value.get('document_prefix', ''))
            local['preset'].blockSignals(True)
            local['preset'].setCurrentText(value.get('model') if value.get('model') in __import__('small_models').PRESETS[kind] else tr('Manual Configuration'))
            local['preset'].blockSignals(False)
            key.clear()
            key.setPlaceholderText('Saved; leave empty to keep' if key_path(self.house.friend_dir, kind).exists() else 'No API Key saved yet')
        self.functions = list(profile.get('function_apis') or [])
        self.active = -1
        self.pending_keys = {}
        self.selector.blockSignals(True)
        self.selector.clear()
        self.selector.addItems([item['name'] for item in self.functions])
        self.selector.blockSignals(False)
        self.select_function(0 if self.functions else -1)

    def read_function(self):
        return {'enabled': self.enabled.isChecked(), 'name': self.name.text().strip(),
                'description': self.description.text().strip(), 'url': self.url.text().strip(),
                'method': self.method.currentText(), 'auth_header': self.header.currentText(),
                'inputSchema': json.loads(self.schema.toPlainText()),
                'fixed_body': json.loads(self.fixed.toPlainText() or '{}')}

    def keep_draft(self):
        if self.active >= 0:
            self.functions[self.active] = self.read_function()
            if self.key.text().strip():
                self.pending_keys[self.functions[self.active]['name']] = self.key.text().strip()

    def select_function(self, index):
        try:
            self.keep_draft()
        except (ValueError, TypeError):
            from .common import toast
            self.selector.blockSignals(True)
            self.selector.setCurrentIndex(self.active)
            self.selector.blockSignals(False)
            toast(self.house, 'error', '请检查当前功能 API 的 JSON Schema 定义。')
            return
        self.active = index
        value = self.functions[index] if 0 <= index < len(self.functions) else {}
        self.enabled.setChecked(value.get('enabled', False))
        self.name.setText(value.get('name', 'api_function'))
        self.description.setText(value.get('description', ''))
        self.url.setText(value.get('url', ''))
        self.method.setCurrentText(value.get('method', 'GET'))
        self.header.setCurrentText(value.get('auth_header', 'Authorization'))
        self.schema.setPlainText(json.dumps(value.get('inputSchema', {'type': 'object', 'properties': {},
                                                                     'additionalProperties': False}), ensure_ascii=False, indent=2))
        self.fixed.setPlainText(json.dumps(value.get('fixed_body', {}), ensure_ascii=False, indent=2))
        self.key.clear()

    def add_function(self):
        self.keep_draft()
        self.functions.append({'name': f'api_function_{len(self.functions) + 1}'})
        self.active = -1
        self.selector.blockSignals(True)
        self.selector.addItem(self.functions[-1]['name'])
        self.selector.setCurrentIndex(len(self.functions) - 1)
        self.selector.blockSignals(False)
        self.select_function(len(self.functions) - 1)

    def save(self, section):
        from retrieval_api import validate as validate_retrieval
        from function_apis import validate as validate_function
        profile = load_profile(self.house.profile_path)
        keys = []
        if section == 'retrieval':
            values = {}
            for kind, (enabled, url, model, dimension, key) in self.endpoints.items():
                value = {'enabled': enabled.isChecked(), 'base_url': url.text().strip(), 'model': model.text().strip()}
                local = self.local_options[kind]
                value.update(mode='local' if local['mode'].currentIndex() == 1 else 'api',
                    model_path=local['path'].text().strip(), pooling=local['pooling'].currentData(),
                    device=local['device'].currentData(), query_prefix=local['query_prefix'].text(),
                    document_prefix=local['document_prefix'].text())
                if kind == 'embedding':
                    value['dimension'] = int(dimension.text() or '1024')
                validate_retrieval(value, kind)
                values[kind] = value
                if key.text().strip():
                    keys.append((kind, key.text().strip()))
            profile['retrieval_api'] = values
        else:
            if self.active < 0:
                self.functions = [self.read_function()]
                self.active = 0
            self.keep_draft()
            names = [item['name'] for item in self.functions]
            if len(names) != len(set(names)):
                raise ValueError('功能 API 的工具名称不能重复')
            for item in self.functions:
                validate_function(item)
            profile['function_apis'] = self.functions
            keys = [('function-' + name, key) for name, key in self.pending_keys.items()]
        for purpose, value in keys:
            save_key(self.house.friend_dir, value, purpose)
        write_profile(self.house.profile_path, profile)
        if section == 'retrieval':
            for _, _, _, _, key in self.endpoints.values():
                key.clear()
                key.setPlaceholderText(tr('Leave empty to keep saved key'))
        else:
            self.pending_keys.clear()
            self.key.clear()
            self.selector.blockSignals(True)
            self.selector.clear()
            self.selector.addItems([item['name'] for item in self.functions])
            self.selector.setCurrentIndex(self.active)
            self.selector.blockSignals(False)
