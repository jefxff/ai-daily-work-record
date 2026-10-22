"""把存量保险流水按归一口径回填到标的/保单年度（阶段 2 批 1）。

口径与 records.insurance_grouping.subject_of 完全一致（纯函数，不依赖
模型当前状态），回填后：
- 每条 Insurance 都挂上 subject 与 policy_year；
- 每个 InsurancePolicyYear 的冗余汇总字段由明细统一重算。

只新增挂接关系与汇总行，不修改 Insurance 原有业务字段，可安全重复执行
（幂等：已挂接记录会按同一口径得到同一结果）。
"""

from django.db import migrations

from records.insurance_grouping import normalize_insurer, subject_of


def backfill(apps, schema_editor):
    Insurance = apps.get_model('records', 'Insurance')
    InsuranceSubject = apps.get_model('records', 'InsuranceSubject')
    InsurancePolicyYear = apps.get_model('records', 'InsurancePolicyYear')

    subject_map = {}   # key -> subject_id
    year_map = {}      # (key, year) -> policy_year_id

    for rec in Insurance.objects.all().order_by('id'):
        meta = subject_of(rec)
        key = meta['key']
        subject_id = subject_map.get(key)
        if subject_id is None:
            if meta['kind'] == 'car':
                subject, _ = InsuranceSubject.objects.get_or_create(
                    plate=meta['plate'],
                    defaults={
                        'kind': meta['kind'], 'label': meta['label'],
                        'plate': meta['plate'],
                        'insurance_type': meta['insurance_type'],
                    },
                )
            else:
                subject, _ = InsuranceSubject.objects.get_or_create(
                    kind='non_car', insurance_type=meta['insurance_type'],
                    defaults={
                        'kind': meta['kind'], 'label': meta['label'],
                        'plate': '', 'insurance_type': meta['insurance_type'],
                    },
                )
            subject_id = subject.id
            subject_map[key] = subject_id

        ykey = (key, rec.end_date.year)
        year_id = year_map.get(ykey)
        if year_id is None:
            policy_year, _ = InsurancePolicyYear.objects.get_or_create(
                subject_id=subject_id, year=rec.end_date.year,
                defaults={'start_date': rec.issue_date, 'end_date': rec.end_date},
            )
            year_id = policy_year.id
            year_map[ykey] = year_id

        if rec.subject_id != subject_id or rec.policy_year_id != year_id:
            rec.subject_id = subject_id
            rec.policy_year_id = year_id
            rec.save(update_fields=['subject_id', 'policy_year_id'])

    # 统一从明细重算年度冗余字段（含保险记录被部分预先挂接的场景）
    for policy_year in InsurancePolicyYear.objects.all():
        records = list(policy_year.records.all())
        if not records:
            policy_year.delete()
            continue
        policy_year.start_date = min(r.issue_date for r in records)
        policy_year.end_date = max(r.end_date for r in records)
        premium = sum((r.premium for r in records), 0)
        policy_year.premium = premium
        policy_year.insurer = '、'.join(
            sorted({normalize_insurer(r.company_name) for r in records})
        )
        policy_year.save(update_fields=[
            'start_date', 'end_date', 'premium', 'insurer', 'updated_at',
        ])


def noop(apps, schema_editor):
    """回滚不拆挂接：外键随 0015 的反向迁移一并删除。"""


class Migration(migrations.Migration):

    dependencies = [
        ('records', '0015_insurance_subject_year'),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
