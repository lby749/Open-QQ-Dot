"""Friend-owned background model and literal QQ quota reply settings."""
from copy import deepcopy
from pathlib import Path

from PySide6.QtWidgets import QFormLayout, QSizePolicy, QWidget
from qfluentwidgets import ComboBox, PlainTextEdit, PushButton

import agent_model_selection as selection
from house_status import load_profile, write_profile
from .common import Card, body, hint, row


EFFORT_LABELS = {'off': '关闭', 'none': '关闭', 'low': '低', 'medium': '中', 'high': '高',
                 'xhigh': '超高', 'max': '最高', 'ultra': '极高', '': '由工具管理'}


class BackgroundModelSettings(Card):
    def __init__(self, house, provider_titles):
        self.save_button = PushButton('保存')
        super().__init__('后台整理模型', actions=(self.save_button,), collapsible=True, collapsed=True)
        self.house, self.provider_titles = house, provider_titles
        self.owner, self.path, self.profile, self.catalog = None, None, {}, {}
        self.provider, self.model, self.effort = ComboBox(), ComboBox(), ComboBox()
        for field in (self.provider, self.model, self.effort):
            field.setMinimumWidth(0)
            field.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        form_box = QWidget()
        form = QFormLayout(form_box)
        form.setContentsMargins(0, 0, 0, 0)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.addRow('模型来源', self.provider)
        form.addRow('模型', self.model)
        form.addRow('思考强度', self.effort)
        self.refresh_button = PushButton('刷新可选模型')
        self.quota_reply = PlainTextEdit()
        self.quota_reply.setPlaceholderText('例如：我这边的模型额度暂时用完了，恢复后再接着聊。')
        self.quota_reply.setMinimumHeight(80)
        self.quota_reply.setMaximumHeight(130)
        self.status = hint('选择好友后读取配置。')
        self.add(hint('后台整理可单独选择模型，用于记忆整理和任务失败反思。反思只使用可关闭工具的纯文本通道；候选来自当前好友配置和本机缓存。'),
                 form_box, row(self.refresh_button, None),
                 hint('先保存普通模型或工具配置，再刷新这里。小模型和智能体工具内模型按同样方式选择。'),
                 body('模型额度用完时的 QQ 回复'), self.quota_reply,
                 hint('最多 1200 字，按这段原文回复当前好友；留空则不额外回复。'), self.status)
        self.provider.currentIndexChanged.connect(self.change_provider)
        self.model.currentIndexChanged.connect(self.change_model)
        self.save_button.clicked.connect(self.save)
        self.refresh_button.clicked.connect(self.refresh_choices)

    def _scope_matches(self):
        return (self.owner is not None and str(self.house.current_qq or '') == self.owner
                and Path(self.house.profile_path).resolve() == self.path)

    def _choices(self, profile):
        catalog = {}
        for provider in self.provider_titles:
            try:
                catalog.update(selection.options(profile, [provider]))
            except (OSError, ValueError, KeyError, TypeError):
                continue  # A malformed backend stays unavailable; other settings remain readable.
        return catalog

    def _discard(self):
        self.owner, self.path, self.profile, self.catalog = None, None, {}, {}
        for field in (self.provider, self.model, self.effort):
            field.blockSignals(True)
            field.clear()
            field.blockSignals(False)
        self.quota_reply.clear()
        self.status.setText('好友已切换，旧草稿已清空；请重新打开当前好友的模型设置。')

    def load(self, profile=None):
        self.owner = str(self.house.current_qq or '') or None
        self.path = Path(self.house.profile_path).resolve() if self.owner else None
        if not self.owner:
            self._discard()
            return
        profile = profile if profile is not None else load_profile(self.path)
        self.profile = deepcopy(profile)
        route = profile.get('background_model') or {}
        route = route if isinstance(route, dict) else {}
        self.catalog = self._choices(profile)
        self.provider.blockSignals(True)
        self.provider.clear()
        self.provider.addItem('沿用当前聊天配置', userData='')
        for key, title in self.provider_titles.items():
            self.provider.addItem(title[0] if isinstance(title, tuple) else title, userData=key)
        chosen = selection.provider_name(str(route.get('provider') or ''))
        if chosen and self.provider.findData(chosen) < 0:
            self.provider.addItem('已保存的后端（需重新选择）', userData=chosen)
        self.provider.setCurrentIndex(max(0, self.provider.findData(chosen)))
        self.provider.blockSignals(False)
        raw = (profile.get('model_api') or {}).get('quota_reply', '')
        self.quota_reply.setPlainText(raw if isinstance(raw, str) else '')
        self.change_provider(preferred=route)

    def change_provider(self, *_args, preferred=None):
        provider = self.provider.currentData() or ''
        self.model.blockSignals(True)
        self.model.clear()
        entry = self.catalog.get(provider)
        if not provider:
            self.model.addItem('沿用当前聊天模型', userData='')
        elif entry:
            for name in entry['models']:
                self.model.addItem(name or '沿用工具配置', userData=name)
            chosen = (preferred or {}).get('model') or entry['default_model']
            if chosen not in entry['models']:
                self.model.addItem('已保存模型（需重新选择）', userData=chosen)
            self.model.setCurrentIndex(max(0, self.model.findData(chosen)))
        self.model.setEnabled(bool(entry))
        self.model.blockSignals(False)
        effort = (preferred or {}).get('effort') if preferred else None
        self.change_model(preferred=effort)

    def change_model(self, *_args, preferred=None):
        provider = self.provider.currentData() or ''
        entry = self.catalog.get(provider) or {}
        levels = (entry.get('models') or {}).get(self.model.currentData() or '', [])
        self.effort.clear()
        for level in levels or ['']:
            self.effort.addItem(EFFORT_LABELS.get(level, level), userData=level)
        chosen = preferred if preferred is not None else entry.get('default_effort', '')
        if chosen and chosen not in levels:
            chosen = 'low' if 'low' in levels else (levels[0] if levels else '')
        self.effort.setCurrentIndex(max(0, self.effort.findData(chosen)))
        self.effort.setEnabled(bool(levels))
        self.status.setText('沿用当前聊天模型及思考强度。' if not provider else (
            '候选来自当前好友配置与缓存。不能枚举模型的工具沿用其已保存配置。' if entry
            else '已保存的模型来源当前不可用，请重新选择。'))

    def refresh_choices(self):
        if not self._scope_matches():
            self._discard()
            return
        route = {'model': self.model.currentData() or '', 'effort': self.effort.currentData() or ''}
        self.profile = load_profile(self.path)
        self.catalog = self._choices(self.profile)
        self.change_provider(preferred=route)

    def save(self):
        if not self._scope_matches():
            self._discard()
            return False
        text = self.quota_reply.toPlainText()
        if text.strip() and len(text) > 1200:
            self.status.setText('额度回复最多 1200 字，请缩短后保存；原设置未改动。')
            return False
        try:
            from source_state import source_lock
            with source_lock(self.path.parent):
                profile = load_profile(self.path)
                provider = self.provider.currentData() or ''
                route = {}
                if provider:
                    catalog = selection.options(profile, [provider])
                    if provider not in self.provider_titles:
                        raise ValueError('请选择当前可用的模型来源')
                    model, effort = selection.validate(catalog, provider, self.model.currentData() or '', self.effort.currentData() or '')
                    route = {'provider': provider, 'model': model, 'effort': effort}
                profile['background_model'] = route
                profile.setdefault('model_api', {})['quota_reply'] = text if text.strip() else ''
                write_profile(self.path, profile)
            self.load(profile)
            self.status.setText('后台整理模型与额度回复已保存，仅对当前好友生效。')
            return True
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            self.status.setText('保存未完成：请检查当前模型配置，并刷新可选模型后重试。')
            return False
