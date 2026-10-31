"""把调度里的 copilot_prune 从"每晚干跑"升级为"每晚真删"。

为什么改：0003 把任务种成 ``params={}``，调度器按白名单传参后命令走默认
dry-run——每晚 03:30 只在任务历史里写一份统计报告，**一条数据也没删过**。
保留策略"会话保留 90 天"的承诺依旧只落在配置界面上，和没有这个任务一样。

安全边界：
1. 只在 ``params`` 仍为空（即管理员没改过）时升级，不覆盖管理员自定义参数；
2. 删除口径不变：活跃会话超 ``retention_days``、归档超 30 天反悔期、外发日志
   超 ``egress_retention_days``（默认 180 天，独立口径）；
3. 手工执行仍默认 dry-run、必须显式 ``--confirm``，双闸只在命令行路径生效。
"""
from django.db import migrations


def upgrade(apps, schema_editor):
    ScheduledTask = apps.get_model("notifications", "ScheduledTask")
    # JSONField 的 NULL 与空字典都要兜住；已带自定义参数的行跳过
    for task in ScheduledTask.objects.filter(code="copilot_prune"):
        if not task.params:
            task.params = {"confirm": True}
            task.save(update_fields=["params"])


def downgrade(apps, schema_editor):
    ScheduledTask = apps.get_model("notifications", "ScheduledTask")
    ScheduledTask.objects.filter(code="copilot_prune").update(params={})


class Migration(migrations.Migration):

    dependencies = [
        ("copilot", "0005_seed_copilot_push_tasks"),
    ]

    operations = [
        migrations.RunPython(upgrade, downgrade),
    ]
