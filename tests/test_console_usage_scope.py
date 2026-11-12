from types import SimpleNamespace
from unittest.mock import patch

import pytest
pytest.importorskip('PySide6')
from house_qt.agent_console_widget import AgentConsolePage


def test_usage_cache_is_bound_to_friend_directory(tmp_path):
    first, second = tmp_path / '12345', tmp_path / '67890'
    first.mkdir()
    second.mkdir()
    current = [first]
    panel = SimpleNamespace(_metric_usage=None, _metric_usage_key=None, _metric_usage_at=0,
                            _metric_friend_dir=lambda: current[0])
    def totals(directory):
        return {'tokens': {'input_tokens': 100 if directory == first else 900,
                           'output_tokens': 20, 'cached_input_tokens': 50}}
    with patch('usage_accounting.summary', side_effect=totals) as read:
        assert AgentConsolePage._metric_usage_fields(panel)['input'] == 100
        assert AgentConsolePage._metric_usage_fields(panel)['input'] == 100
        current[0] = second
        assert AgentConsolePage._metric_usage_fields(panel)['input'] == 900
        assert read.call_count == 2
