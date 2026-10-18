"""证照列表的状态查询下推。

背景（审计 #10）：status 与 is_borrowed 原本都是 Python @property，列表页用
三次列表推导做统计：

    sum(1 for l in License.objects.all() if l.status == "expiring")

每行一次 EXISTS，100 行 = 300+ 条 SQL。这里把两个属性下推为数据库注解，
并让 License.status / is_borrowed 优先读取注解值（models.py 已配合改造），
模板可保持 {{ l.status }} 不变。
"""

from datetime import timedelta

from django.db.models import BooleanField, Case, CharField, Count, Exists, OuterRef, Q, Value, When
from django.utils import timezone

from ..models import License, LicenseBorrowRecord


def annotate_status(qs):
    """给 License 查询集补 _status / _borrowed 两个注解。

    判定顺序必须与 License.status 原实现严格一致：
    先判 expired（含年审日已过），再判 expiring（30 天内）。
    写反会让"临期"被算成"已过期"。
    """
    today = timezone.localdate()
    soon = today + timedelta(days=30)
    return qs.annotate(
        _status=Case(
            When(
                Q(expire_date__lt=today) | Q(next_review_date__lt=today),
                then=Value("expired"),
            ),
            When(
                Q(expire_date__lte=soon) | Q(next_review_date__lte=soon),
                then=Value("expiring"),
            ),
            default=Value("normal"),
            output_field=CharField(),
        ),
        _borrowed=Exists(
            LicenseBorrowRecord.objects.filter(
                license=OuterRef("pk"), return_time__isnull=True
            ),
            output_field=BooleanField(),
        ),
    )


def status_aggregate(qs):
    """一条 SQL 出四个统计值；qs 必须已经 annotate_status。"""
    return qs.aggregate(
        total=Count("pk"),
        expiring_count=Count("pk", filter=Q(_status="expiring")),
        expired_count=Count("pk", filter=Q(_status="expired")),
        borrowed_count=Count("pk", filter=Q(_borrowed=True)),
    )
