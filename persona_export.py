"""Export a verified persona analysis and publish complete immutable versions."""
from pathlib import Path

from persona_distiller import _json
from persona_evidence import ValidatedAnalysis
from dataset_registry import DatasetRegistry
from persona_types import DataScope, InvalidSource, PrivacyView, PrivacyViolation
from persona_versions import PersonaVersionStore, claim_documents, render_derived
from style_examples import build_examples


class PersonaExporter:
    def __init__(self, scope, pair_store):
        self.versions = PersonaVersionStore(scope, pair_store)
        self.scope = self.versions.scope
        self.pairs = pair_store

    def build(self, validated, build_dir, *, version='build',
              privacy_view=PrivacyView.LOCAL_FULL):
        if not isinstance(validated, ValidatedAnalysis):
            raise InvalidSource('export requires ValidatedAnalysis')
        validated = self.versions._verified(validated)
        try:
            privacy_view = PrivacyView(privacy_view)
        except (TypeError, ValueError):
            raise InvalidSource('unknown privacy view') from None
        if validated.result.friend_id != self.scope.friend_id:
            raise InvalidSource('analysis belongs to another friend scope')
        if not isinstance(version, str) or not version.strip():
            raise InvalidSource('version label must be nonempty')
        refs = validated.validated_source_refs
        if privacy_view == PrivacyView.CLOUD_SAFE and any(
                ref.data_scope == DataScope.PRIVATE_FRIEND for ref in refs):
            raise PrivacyViolation('private analysis cannot be exported in CLOUD_SAFE view')
        if privacy_view == PrivacyView.CLOUD_SAFE:
            registry = DatasetRegistry(self.scope, readonly=True)
            try:
                for dataset_id in {ref.dataset_id for ref in refs}:
                    record = registry.get(dataset_id)
                    if (record is None or not record.active or record.data_scope != 'PUBLIC_DATASET' or
                            record.raw_corpus_redistribution_allowed is not True or not record.license_data):
                        raise PrivacyViolation('public source lacks recorded raw redistribution permission')
            finally:
                registry.close()
        directory = Path(build_dir).resolve()
        if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
            raise InvalidSource('build directory must be empty or absent')
        directory.mkdir(parents=True, exist_ok=True)
        try:
            profile = dict(schema_version='persona-profile-v1', friend_id=self.scope.friend_id,
                version=version, run_id=validated.result.run_id,
                source_content_hash=validated.source_content_hash,
                claims=claim_documents(validated, version), sample_status=validated.result.sample_status,
                quality='validated_drafts', manual_revision=0, manual_overlay={}, tombstones=[])
            style = dict(schema_version='style-profile-v1', friend_id=self.scope.friend_id,
                version=version, observed=validated.result.style_profile, manual_overrides={})
            (directory / 'persona_profile.json').write_text(_json(profile), encoding='utf-8')
            (directory / 'style_profile.json').write_text(_json(style), encoding='utf-8')
            skill, report = render_derived(profile, style)
            (directory / 'persona_skill.md').write_text(skill, encoding='utf-8')
            (directory / 'persona_report.md').write_text(report, encoding='utf-8')
            build_examples(validated, directory / 'examples.db', version, privacy_view)
            return directory
        except Exception:
            for name in ('persona_profile.json', 'style_profile.json', 'persona_skill.md',
                         'persona_report.md', 'examples.db'):
                (directory / name).unlink(missing_ok=True)
            raise

    def publish(self, validated, build_dir, *, expected_revision,
                expected_active_version, expected_manual_revision,
                privacy_view=PrivacyView.LOCAL_FULL):
        directory = self.build(validated, build_dir, privacy_view=privacy_view)
        return self.versions.publish_version(validated, directory,
            expected_revision=expected_revision, expected_active_version=expected_active_version,
            expected_manual_revision=expected_manual_revision)
