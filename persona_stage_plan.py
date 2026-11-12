"""The small set of Persona jobs that can safely run outside the Pack preset."""
from dataclasses import dataclass


PRESET_KIND = 'pack'
PRESET_PIPELINE = (
    'RuleFilter', 'LocalWeakLabeler', 'PersonaJudge', 'DiversitySampler',
    'PersonaDistiller', 'EvidenceValidator', 'publish',
)


@dataclass(frozen=True)
class StageSpec:
    kind: str
    label: str
    prerequisites: tuple[str, ...]
    output: str
    note: str


ADVANCED_STAGES = {
    'distill': StageSpec(
        kind='distill',
        label='本地蒸馏草稿',
        prerequisites=('selected_sessions', 'verified_source_refs'),
        output='DRAFT 检查点；不会发布 PersonaVersion',
        note='沿用现有本地 Job，不调用云端 Judge。',
    ),
    'pack_media': StageSpec(
        kind='pack_media',
        label='公共 Pack 媒体元数据补版',
        prerequisites=('active_five_file_pack', 'verified_public_grants'),
        output='新的不可变公共 Pack 版本',
        note='确定性本地处理；只接受当前活动的五文件公共 Pack。',
    ),
}

PREREQUISITE_LABELS = {
    'selected_sessions': '选择至少一个有效会话',
    'verified_source_refs': '来源通过同一好友范围的校验',
    'active_five_file_pack': '选择当前活动的五文件公共 Pack',
    'verified_public_grants': '公共来源许可和导入哈希仍然有效',
}


@dataclass(frozen=True)
class StagePlan:
    kind: str
    ready: bool
    missing: tuple[str, ...]


def plan_advanced_stage(kind, available):
    """Check declared prerequisites; the existing Job API revalidates them at enqueue."""
    spec = ADVANCED_STAGES.get(kind)
    if spec is None:
        return StagePlan(kind, False, ('此阶段没有独立 Job 接口',))
    missing = tuple(PREREQUISITE_LABELS[name] for name in spec.prerequisites
                    if available.get(name) is not True)
    return StagePlan(kind, not missing, missing)


def existing_job_action(job):
    """Reuse exact completed jobs and keep uncertain external attempts fail-closed."""
    if job is None:
        return 'create'
    state = job.get('state')
    attempt = job.get('attempt') or {}
    if job.get('tokens_unknown') or attempt.get('status') in ('STARTED', 'UNCERTAIN'):
        return 'blocked_unknown_usage'
    if state == 'completed':
        return 'reuse'
    if state == 'queued':
        return 'start'
    if state == 'running':
        return 'monitor'
    if state in ('paused', 'failed'):
        return 'resume'
    return 'blocked'


def job_progress_label(job):
    checkpoints = job.get('checkpoint') or {}
    if not checkpoints:
        return '尚无 checkpoint'
    stages = set()
    for value in checkpoints.values():
        if isinstance(value, dict) and 'hash' in value:
            value = value.get('result', {})
        stage = value.get('stage') if isinstance(value, dict) else None
        if isinstance(stage, str) and stage:
            stages.add(stage)
    suffix = '; confirmstage ' + ' / '.join(sorted(stages)) if stages else ''
    return f"{len(checkpoints)} 个 checkpoint" + suffix


def job_usage_label(job):
    """Show only persisted, confirmed usage; never turn missing data into zero."""
    if job.get('tokens_unknown') or (job.get('attempt') or {}).get('status') in ('STARTED', 'UNCERTAIN'):
        return '用量未知（外部调用结果未确认；不可自动重试）'

    input_tokens = output_tokens = 0
    found = False
    unknown = False
    seen_runs = {}
    for checkpoint in (job.get('checkpoint') or {}).values():
        result = checkpoint.get('result', checkpoint) if isinstance(checkpoint, dict) else {}
        if isinstance(result, dict) and result.get('stage') == 'judge':
            run_id = result.get('run_id')
            if not isinstance(run_id, str) or not run_id:
                unknown = True
                continue
            result = result.get('result', {})
        else:
            run_id = None
        usage = result.get('usage') if isinstance(result, dict) else None
        if usage is None:
            if run_id is not None:
                unknown = True
            continue
        if (not isinstance(usage, dict) or usage.get('status') not in (None, 'known') or
                type(usage.get('input_tokens')) is not int or
                type(usage.get('output_tokens')) is not int or
                usage['input_tokens'] < 0 or usage['output_tokens'] < 0 or
                (run_id is not None and (usage.get('status') != 'known' or
                    type(usage.get('total_tokens')) is not int or
                    usage['total_tokens'] != usage['input_tokens'] + usage['output_tokens']))):
            unknown = True
            continue
        if run_id is not None:
            if run_id in seen_runs:
                if seen_runs[run_id] != usage:
                    unknown = True
                continue
            seen_runs[run_id] = usage
        input_tokens += usage['input_tokens']
        output_tokens += usage['output_tokens']
        found = True
    if unknown:
        return '用量未知（存在未确认记录）'
    if found:
        total = input_tokens + output_tokens
        return f'Confirmed {total:,} tokensInput {input_tokens:,} / Output {output_tokens:,}Cost not recorded'
    if job.get('kind') in ('distill', 'pack_media'):
        return '本地/确定性作业，无云端 Token；金额不适用'
    return 'Token 用量未知（尚无确认记录）；金额未记录'
