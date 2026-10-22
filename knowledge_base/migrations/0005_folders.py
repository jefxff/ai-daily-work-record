"""文件夹树（方案 04，批次 6）：KnowledgeFolder + 文档 folder 外键。

三个刻意取舍（方案 04 第二节）：
- document.folder 用 SET_NULL：文件夹删除时文档永不跟着删，回到「未归档」；
- folder.owner 用 CASCADE：文件夹只是空壳，用户注销随之清理；
- 存量文档不做回填：全部落「未归档」，上线当天零感知。
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('knowledge_base', '0004_knowledgedocument_editor_mode_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='KnowledgeFolder',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField('文件夹名', max_length=50)),
                ('parent', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='children', to='knowledge_base.knowledgefolder', verbose_name='父文件夹')),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='knowledge_folders', to=settings.AUTH_USER_MODEL, verbose_name='所有者')),
                ('sort', models.IntegerField(default=0, verbose_name='排序')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
            ],
            options={
                'verbose_name': '知识库文件夹',
                'verbose_name_plural': '知识库文件夹',
                'ordering': ['sort', 'id'],
            },
        ),
        migrations.AddField(
            model_name='knowledgedocument',
            name='folder',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='documents', to='knowledge_base.knowledgefolder', verbose_name='所属文件夹'),
        ),
        migrations.AddIndex(
            model_name='knowledgefolder',
            index=models.Index(fields=['owner'], name='knowledge_folder_owner_idx'),
        ),
    ]
