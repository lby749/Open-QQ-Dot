"""CC Switch-style provider presets for the account page.

Only official/standard endpoints are listed.  A preset is metadata: the user
still supplies the API key, and the key is stored through the existing DPAPI path.
"""
from __future__ import annotations

PRESETS: dict[str, tuple[str, str, str]] = {
    'OpenCode 免费': ('opencode', 'opencode-go/mimo-v2.6-flash', ''),
    'OpenCode 免费备用': ('opencode', 'opencode-go/iceman-contributor-free', ''),
    'Claude Code 官方': ('claude', 'claude-sonnet-5-5', ''),
    'Codex 官方': ('codex', 'gpt-6-luna', ''),
    'DeepSeek 官方': ('deepseek', 'deepseek-chat', 'https://api.deepseek.com'),
    'DeepSeek 推理': ('deepseek', 'deepseek-reasoner', 'https://api.deepseek.com'),
    'DeepSeek Harness': ('dsh', 'DeepSeek-V41-Flash', ''),
    'OpenAI 兼容': ('custom', 'gpt-5.6-luna', 'https://api.openai.com/v1'),
    'OpenRouter': ('custom', 'openai/gpt-5.6-luna', 'https://openrouter.ai/api/v1'),
    'SiliconFlow 硅基流动': ('custom', 'deepseek-ai/DeepSeek-V3', 'https://api.siliconflow.cn/v1'),
    'Moonshot Kimi': ('custom', 'kimi-k3', 'https://api.moonshot.cn/v1'),
    'Zhipu GLM': ('custom', 'glm-5.3', 'https://open.bigmodel.cn/api/paas/v4'),
    'Qwen DashScope': ('custom', 'qwen3.8-max', 'https://dashscope.aliyuncs.com/compatible-mode/v1'),
    'MiniMax': ('custom', 'minimax-m3', 'https://api.minimax.chat/v1'),
    'Groq': ('custom', 'llama-4.1-70b', 'https://api.groq.com/openai/v1'),
    'Together AI': ('custom', 'deepseek-ai/DeepSeek-V3', 'https://api.together.xyz/v1'),
    'Fireworks': ('custom', 'accounts/fireworks/models/deepseek-v3', 'https://api.fireworks.ai/inference/v1'),
    'Mistral': ('custom', 'mistral-large-latest', 'https://api.mistral.ai/v1'),
    'xAI Grok': ('custom', 'grok-4.7', 'https://api.x.ai/v1'),
    'Perplexity': ('custom', 'sonar-pro', 'https://api.perplexity.ai'),
    'Volcengine 豆包': ('custom', 'doubao-seed-2.0', 'https://ark.cn-beijing.volces.com/api/v3'),
    'Baichuan': ('custom', 'baichuan4', 'https://api.baichuan-ai.com/v1'),
    'StepFun 阶跃': ('custom', 'step-5-preview', 'https://api.stepfun.com/v1'),
    '01.AI 零一万物': ('custom', 'yi-large', 'https://api.lingyiwanwu.com/v1'),
    'Ollama 本地': ('ollama', 'qwen3.8:8b', 'http://127.0.0.1:11434/v1'),
    'LM Studio 本地': ('custom', 'local-model', 'http://127.0.0.1:1234/v1'),
    'vLLM 本地': ('custom', 'local-model', 'http://127.0.0.1:8000/v1'),
    '自定义 OpenAI 兼容': ('custom', '', ''),
}
