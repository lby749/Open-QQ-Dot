"""Per-friend boundary: may a chat/agent turn change the house configuration?

Default is OFF. Owner-facing desktop/phone settings pages do not use this
guard; it applies to agent tools/prompts, where a friend could otherwise ask
the model to raise its own permissions or switch models.
"""
CONFIG_TERMS = (
    'friend.json', 'friends.json', 'house_web.local.json',
    'model_api', 'computer_tools', 'chat_config_edits',
    '/api/friend/', 'post_model', 'post_basic', 'post_style',
    '127.0.0.1:8790', 'localhost:8790',
)


def chat_config_allowed(profile) -> bool:
    return bool((profile or {}).get('chat_config_edits', False))


def command_touches_config(command: str) -> bool:
    lower = str(command or '').casefold()
    return any(term.casefold() in lower for term in CONFIG_TERMS)
