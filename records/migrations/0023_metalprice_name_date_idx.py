"""给 MetalPrice 加 (品名, 爬取日期) 复合索引。

为什么要这条索引：**AI 查价格走势的默认形态就是"某个金属在某个日期区间"**，
只按 crawl_date 的单列索引仍要回表逐行比对 metal_name；加上复合索引后，
区间过滤和品名过滤可以一次走完（SELECT … WHERE metal_name=? AND crawl_date BETWEEN ? AND ?）。

为什么**不**动原有的 crawl_date 单列索引：首页/看板是按"当日或最近一批"
取数（只按日期，不带品名），那条索引仍然用得上，拆了反而退步。
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('records', '0022_customerledgerchangelog'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='metalprice',
            index=models.Index(
                fields=['metal_name', 'crawl_date'],
                name='idx_metalprice_name_date',
            ),
        ),
    ]
