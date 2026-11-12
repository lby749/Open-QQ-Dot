"""One-time read-only copy of the old friend's local state into this project."""
import json
import sqlite3
from pathlib import Path
from store import FriendStore, dump_json
from source_state import source_lock

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parent / "project" / "qq-codex-bridge"


def main():
    registry_path = ROOT / "friends.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    for qq_id, record in registry.get("friends", {}).items():
        target_dir = ROOT / "friends" / qq_id
        target_dir.mkdir(parents=True, exist_ok=True)
        source_db = SOURCE / "friends" / qq_id / "history.sqlite3"
        target_db = target_dir / "history.sqlite3"
        with source_lock(target_dir):
            if target_db.exists():
                continue
            if source_db.exists():
                source = sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True)
                target = sqlite3.connect(target_db)
                try:
                    source.backup(target)
                finally:
                    target.close()
                    source.close()
                store = FriendStore(target_dir)
                try:
                    with store.db:
                        store.db.execute("UPDATE source_state SET identity=lower(hex(randomblob(16))),revision=revision+1")
                        store.db.execute("UPDATE messages SET handled=1")
                        store.db.execute("DELETE FROM metadata WHERE key IN ('thread_id','last_seen_id','baseline_set','proactive_test') OR key LIKE 'plan:%' OR key LIKE 'slot:%'")
                finally:
                    store.close()
        profile_path = target_dir / "friend.json"
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        profile["bridge_enabled"] = False
        dump_json(profile_path, profile)
        record["directory"] = str(target_dir)
        record.pop("conversation_id", None)
        record.pop("self_id", None)
    registry_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
