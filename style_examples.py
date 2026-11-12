"""Build a source-faithful, per-version Example database."""
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
import sqlite3

from persona_distiller import _json
from persona_types import DataScope, InvalidSource, PrivacyView, PrivacyViolation
from persona_versions import EXAMPLE_COLUMNS


def build_examples(validated, path, version, privacy_view):
    if privacy_view == PrivacyView.CLOUD_SAFE and any(
            ref.data_scope == DataScope.PRIVATE_FRIEND for ref in validated.validated_source_refs):
        raise PrivacyViolation('private examples cannot be exported in CLOUD_SAFE view')
    scenes = {row.episode_id: row.scene for row in validated.result.scene_analysis.scenes}
    pairs = validated.result.scene_analysis.analysis_input.role_pairs
    with closing(sqlite3.connect(Path(path))) as db:
        db.execute('CREATE TABLE examples (' + ','.join(
            ('id TEXT PRIMARY KEY', 'user TEXT NOT NULL', 'context TEXT NOT NULL',
             'target_bubbles TEXT NOT NULL', 'scene TEXT NOT NULL', 'emotion TEXT',
             'topic TEXT', 'reaction_type TEXT', 'source_origin TEXT NOT NULL',
             'friend_id TEXT', 'relationship_tags TEXT', 'source_refs TEXT NOT NULL',
             'version TEXT NOT NULL')) + ')')
        rows = []
        for pair in pairs:
            origins = {b.source.origin.value for b in pair.target_bubbles}
            owners = {b.source.friend_id for b in pair.target_bubbles}
            if len(origins) != 1 or len(owners) != 1:
                raise InvalidSource('target bubbles have mixed source provenance')
            scene = scenes.get(pair.id)
            if scene is None:
                raise InvalidSource('verified scene is missing for Target pair')
            owner = next(iter(owners))
            if owner is not None and owner != validated.result.friend_id:
                raise InvalidSource('private Target owner differs from validated friend')
            rows.append((pair.id, _json([asdict(v) for v in pair.user_bubbles]),
                _json([asdict(v) for v in pair.context_turns]),
                _json([asdict(v) for v in pair.target_bubbles]), scene, None, None, None,
                next(iter(origins)), owner, None,
                _json([asdict(v) for v in pair.source_refs]), version))
        db.executemany('INSERT INTO examples VALUES(' + ','.join('?' for _ in EXAMPLE_COLUMNS) + ')', rows)
        db.commit()
    return Path(path)
