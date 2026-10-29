"""全表假数据生成器：默认把每张业务表补齐到 50 条，供本地演示 / 联调使用。

用法：
    python manage.py seed_fake_data                # 每表补到 50 条（幂等，可重复执行）
    python manage.py seed_fake_data --count 100    # 补到 100 条
    python manage.py seed_fake_data --seed 7       # 换一组随机种子

设计约定：
- 迁移自带的真实种子数据（RBAC 权限 / 通知模板 / 定时任务）只补差额，不重复造；
- auto_now_add 时间字段先入库再 bulk_update 回散布点，列表页时间分布自然；
- 非空 FileField 写库内相对路径，并在 media/ 下落占位文件（PDF/PNG/TXT），下载链接可用；
- 唯一约束（编号 / 车牌 / 指纹 / 幂等键等）全部按表内序号生成，重复执行不会冲突。
"""

import base64
import hashlib
import os
import pickle
import random
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Group, User
from django.contrib.contenttypes.models import ContentType
from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand
from django.db import models, transaction
from django.utils import timezone

from accounts.models import ApiToken, LoginLog, Permission as RbacPermission, Role, UserProfile
from ai_api.models import AiProviderConfig
from attachments.models import ATTACHMENT_CATEGORIES, Attachment
from archive.models import (
    AnnualReview,
    Archive,
    ArchiveFile,
    BorrowRecord,
    Cabinet,
    CabinetLayer,
    Category,
    Ledger,
    License,
    LicenseBorrowRecord,
)
from knowledge_base.models import (
    KnowledgeCategory,
    KnowledgeDocument,
    KnowledgeFolder,
    KnowledgeNode,
    KnowledgeView,
)
from notifications.models import (
    Notification,
    NotificationDelivery,
    NotificationPreference,
    NotificationTemplate,
    RequestMetric,
    ScheduledTask,
    SystemLog,
    TaskRunLog,
)
from records.models import (
    Complaint,
    CustomerComplaint,
    CustomerCredit,
    CustomerLedger,
    CustomerLedgerChangeLog,
    DailyRecord,
    Decoration,
    Insurance,
    InsurancePolicyYear,
    InsuranceSubject,
    MetalPrice,
    OperationLog,
    PriceEvaluation,
    Regulation,
)
from samples.models import AuditRecord, BatchRecord, Partner, ProofAttachment, Sample, TestRecord
from sysbackup.models import BackupOperationLog, BackupTask

# ---------------------------------------------------------------- 词库 / 工具

SURNAMES = list("张王李赵刘陈杨黄周吴徐孙马朱胡郭何林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董潘袁蔡蒋余于杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤")
GIVEN = ["伟", "芳", "娜", "敏", "静", "磊", "军", "洋", "勇", "艳", "杰", "娟", "涛", "明", "超", "秀", "霞", "平", "刚", "欣怡", "浩然", "梓萱", "宇轩", "诗涵", "子墨", "雨桐", "晨曦", "志强", "淑华", "建国", "丽娟", "文博", "嘉懿", "煜城", "鹤轩", "伟泽", "晟睿", "胤祥", "璟雯", "天翊"]
DEPARTMENTS = ["生产部", "销售部", "采购部", "品质部", "研发部", "仓储部", "财务部", "人事部", "设备部", "工艺部"]
COMPANY_PREFIX = ["华丰", "恒基", "瑞森", "中天", "联合", "万向", "精工", "达利", "宏泰", "永固", "金桥", "南方", "北方", "东方", "西部", "新兴", "奥盛", "博远", "创元", "德昌"]
COMPANY_SUFFIX = ["科技有限公司", "实业有限公司", "五金制品有限公司", "电子科技有限公司", "精密机械有限公司", "新材料有限公司", "模具制品有限公司", "塑胶制品有限公司"]
MATERIALS = ["不锈钢板", "铝合金型材", "电解铜棒", "锌合金锭", "PVC粒子", "ABS原料", "钢丝绳", "轴承钢", "冷轧板", "镀锌板", "铜箔", "铝箔", "尼龙66", "POM板", "环氧树脂"]
MODELS = ["SUS304-2B", "6061-T6", "H62-Y2", "Zn99.995", "SG-5", "PA66-G30", "6*19W", "GCr15", "DC01", "DX51D"]
METALS = ["铜", "铝", "锌", "铅", "镍", "锡"]
INSURERS = ["中国人民保险", "平安财险", "太平洋财险", "中华联合", "大地财险", "阳光财险", "国寿财险"]
INSURANCE_TYPES = ["财产一切险", "公众责任险", "雇主责任险", "货物运输险", "机器损坏险", "产品责任险"]
PLATE_PREFIX = ["粤B", "粤S", "粤A", "粤L"]
LICENSE_NAMES = ["营业执照", "安全生产许可证", "质量管理体系认证证书", "环境管理体系认证证书", "高新技术企业证书", "职业健康安全管理体系认证证书", "商标注册证", "专利证书"]
ARCHIVE_NAMES = ["采购合同", "销售合同", "质量协议", "保密协议", "框架协议", "验收报告", "对账单", "发票存根", "检测报告", "资质文件", "会议纪要", "工程图纸"]
SECURITY_LEVELS = ["机密", "秘密", "内部", "一般"]
KB_TITLES = ["Django 5.2 升级注意事项", "SQLite WAL 模式调优笔记", "RBAC 权限模型设计说明", "备份中心加密方案", "试样跟进流程 SOP", "客户账期审核要点", "档案柜定位规则", "通知中心聚合降噪实践", "DRF 分页与响应外壳约定", "HTTPS 内网部署指南"]
KB_CONTENT = "# 概述\n\n这是用于演示的知识文档内容，涵盖背景、方案与落地细节。\n\n## 背景\n\n业务侧提出需要沉淀可检索的过程文档。\n\n## 方案\n\n- 大纲节点化存储\n- 按天分组展示\n- 权限按可见性隔离\n\n## 落地\n\n已完成开发与联调，进入试运行阶段。"

_rng = random.Random()


def cn_name():
    return _rng.choice(SURNAMES) + "".join(_rng.choices(GIVEN, k=_rng.choice([1, 1, 2])))


def cn_company():
    return _rng.choice(COMPANY_PREFIX) + _rng.choice(COMPANY_SUFFIX)


def phone():
    return f"1{_rng.choice('35789')}{_rng.randint(100000000, 999999999)}"


def email_of(username):
    return f"{username}@example.com"


def rand_date(start_year=2023, end_year=2026):
    y = _rng.randint(start_year, end_year)
    m = _rng.randint(1, 12)
    d = _rng.randint(1, 28)
    return date(y, m, d)


def rand_future_date(days_min=1, days_max=800):
    return timezone.localdate() + timedelta(days=_rng.randint(days_min, days_max))


def rand_past_date(days_min=1, days_max=800):
    return timezone.localdate() - timedelta(days=_rng.randint(days_min, days_max))


def rand_dt(days_min=0, days_max=400):
    base = timezone.now() - timedelta(days=_rng.randint(days_min, days_max))
    return base.replace(hour=_rng.randint(7, 21), minute=_rng.randint(0, 59), second=_rng.randint(0, 59), microsecond=0)


def rand_decimal(max_val=100000, places=2):
    return Decimal(str(round(_rng.uniform(10, max_val), places)))


def rand_text(sentences=3):
    parts = []
    for _ in range(sentences):
        parts.append(_rng.choice([
            "已完成现场核对并整理归档。",
            "与供应商确认了交期与质量标准。",
            "走完内部审批流，等待上级复核。",
            "客户反馈问题已定位，正在制定纠正措施。",
            "数据已录入系统，附件同步上传。",
            "按周计划推进，进度正常。",
            "会议结论已同步相关方。",
            "检测报告出具，结论为合格。",
        ]))
    return "".join(parts)


def seq(prefix, i):
    return f"{prefix}-{i:04d}"


# ---------------------------------------------------------------- 占位媒体文件

_PNG_BYTES = None


def _placeholder_bytes(ext):
    """按扩展名返回占位文件字节（尽量给真实可解析的格式）。"""
    global _PNG_BYTES
    ext = ext.lower()
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"):
        if _PNG_BYTES is None:
            import io

            from PIL import Image
            buf = io.BytesIO()
            Image.new("RGB", (8, 8), (220, 225, 230)).save(buf, format="PNG")
            _PNG_BYTES = buf.getvalue()
        return _PNG_BYTES
    if ext == ".pdf":
        return (
            b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
            b"trailer<</Root 1 0 R>>\n%%EOF"
        )
    return "这是用于演示的占位文件。".encode("utf-8")


def write_media(rel_path):
    """在 MEDIA_ROOT 下落一个占位文件（已存在则跳过），返回相对路径。"""
    from django.conf import settings

    abs_path = os.path.join(settings.MEDIA_ROOT, rel_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    if not os.path.exists(abs_path):
        ext = os.path.splitext(rel_path)[1]
        with open(abs_path, "wb") as fh:
            fh.write(_placeholder_bytes(ext))
    return rel_path


def fake_session_data():
    return base64.b64encode(pickle.dumps({"_auth_user_id": "1", "_auth_user_backend": "django.contrib.auth.backends.ModelBackend"})).decode()


# ---------------------------------------------------------------- 主命令


class Command(BaseCommand):
    help = "为每张业务表生成假数据（默认补齐到 50 条）"

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=50, help="每表目标行数（默认 50）")
        parser.add_argument("--seed", type=int, default=20261029, help="随机种子，便于复现")

    # ---------- 通用小工具 ----------

    @staticmethod
    def _need(model, target):
        existing = model.objects.count()
        return max(0, target - existing), existing

    def _top_up_note(self, label, created, existing, target):
        if created == 0 and existing >= target:
            self.stdout.write(f"  - {label}: 已有 {existing} 条，跳过")
        else:
            self.stdout.write(f"  - {label}: 新增 {created} 条（原有 {existing}，合计 {existing + created}）")

    def handle(self, *args, **options):
        target = options["count"]
        _rng.seed(options["seed"])
        self.stdout.write(self.style.WARNING(f"开始生成假数据：每表目标 {target} 条（seed={options['seed']}）"))

        with transaction.atomic():
            self._users(target)
            self._auth_groups(target)
            self._accounts(target)
            self._ai_api(target)
            self._archive(target)
            self._knowledge(target)
            self._notifications(target)
            self._records(target)
            self._attachments(target)
            self._samples(target)
            self._sysbackup(target)
            self._django_builtin(target)

        self.stdout.write(self.style.SUCCESS("造数完成，各表行数："))
        self._print_summary()

    # ---------- 用户与 RBAC ----------

    def _users(self, target):
        need, existing = self._need(User, target)
        created = 0
        users = []
        for i in range(need):
            n = existing + i + 1
            username = f"demo_user_{n:03d}"
            user = User(username=username, email=email_of(username), first_name=cn_name()[0], last_name=cn_name()[1:], is_staff=(n % 10 == 0), is_active=True)
            user.set_password("Demo@12345")
            users.append(user)
        if users:
            User.objects.bulk_create(users)
            created = len(users)
        self.users = list(User.objects.order_by("id"))
        self._top_up_note("auth.User", created, existing, target)

    def _auth_groups(self, target):
        need, existing = self._need(Group, target)
        groups = [Group(name=f"演示分组{existing + i + 1:02d}") for i in range(need)]
        if groups:
            Group.objects.bulk_create(groups)
        self._top_up_note("auth.Group", len(groups), existing, target)

    def _accounts(self, target):
        users = self.users

        need, existing = self._need(Role, target)
        roles = []
        for i in range(need):
            n = existing + i + 1
            roles.append(Role(code=f"demo_role_{n:02d}", name=f"演示角色{n:02d}", description="造数生成的演示角色", is_builtin=False))
        if roles:
            Role.objects.bulk_create(roles)
            perms = list(RbacPermission.objects.all()[:12])
            for role in roles:
                if perms:
                    role.permissions.set(_rng.sample(perms, k=min(6, len(perms))))
                    role.users.set(_rng.sample(users, k=min(5, len(users))))
        self._top_up_note("accounts.Role", len(roles), existing, target)

        need, existing = self._need(UserProfile, target)
        have = {p.user_id for p in UserProfile.objects.all()}
        profiles = [
            UserProfile(user=u, real_name=cn_name(), department=_rng.choice(DEPARTMENTS), position=_rng.choice(["专员", "主管", "经理", "工程师", "文员"]))
            for u in users if u.id not in have
        ][:need]
        if profiles:
            UserProfile.objects.bulk_create(profiles)
        self._top_up_note("accounts.UserProfile", len(profiles), existing, target)

        need, existing = self._need(LoginLog, target)
        logs = [
            LoginLog(
                user=_rng.choice(users),
                username=_rng.choice(users).username,
                ip=f"192.168.{_rng.randint(1, 254)}.{_rng.randint(2, 254)}",
                user_agent=_rng.choice(["Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)", "Mozilla/5.0 (X11; Linux x86_64)"]),
                login_type=_rng.choice(["login", "login", "login", "logout"]),
                success=_rng.random() > 0.15,
                reason="" if _rng.random() > 0.15 else "密码错误",
            )
            for _ in range(need)
        ]
        if logs:
            LoginLog.objects.bulk_create(logs)
            for obj, dt in zip(logs, [rand_dt(0, 60) for _ in logs]):
                obj.created_at = dt
            LoginLog.objects.bulk_update(logs, ["created_at"])
        self._top_up_note("accounts.LoginLog", len(logs), existing, target)

        need, existing = self._need(ApiToken, target)
        tokens = []
        for u in _rng.sample(users, k=min(need, len(users))):
            tokens.append(ApiToken.issue(u, ip=f"192.168.1.{_rng.randint(2, 254)}"))
        self._top_up_note("accounts.ApiToken", len(tokens), existing, target)

    # ---------- AI 配置 ----------

    def _ai_api(self, target):
        need, existing = self._need(AiProviderConfig, target)
        created = 0
        for i in range(need):
            n = existing + i + 1
            cfg = AiProviderConfig(
                provider=_rng.choice(["minimax", "ollama", "openai_compatible"]),
                base_url=_rng.choice(["https://api.minimax.chat", "http://127.0.0.1:11434", "http://10.0.0.9:8000/v1"]),
                model=_rng.choice(["abab6.5s-chat", "qwen2.5:7b", "gpt-4o-mini", "deepseek-chat"]),
                temperature=round(_rng.uniform(0.1, 0.8), 2),
                max_tokens=_rng.choice([512, 800, 1024, 2048]),
                timeout=_rng.choice([15, 30, 60]),
                is_enabled=False,
                updated_by=f"demo_user_{n:03d}",
            )
            cfg.set_api_key(f"sk-demo-{uuid.uuid4().hex[:24]}")
            cfg.save()
            created += 1
        self._top_up_note("ai_api.AiProviderConfig", created, existing, target)

    # ---------- 档案管理 ----------

    def _archive(self, target):
        users = self.users

        need, existing = self._need(Cabinet, target)
        cabinets = []
        for i in range(need):
            n = existing + i + 1
            cabinets.append(Cabinet(code=f"CAB-{n:03d}", location=f"{_rng.choice(['一号', '二号', '三号'])}库房-{chr(65 + n % 5)}区", layers=2))
        if cabinets:
            Cabinet.objects.bulk_create(cabinets)
            layers = [CabinetLayer(cabinet=c, layer_no=no, cells=30) for c in cabinets for no in (1, 2)]
            CabinetLayer.objects.bulk_create(layers)
        self.cabinets = list(Cabinet.objects.order_by("id"))
        self._top_up_note("archive.Cabinet", len(cabinets), existing, target)
        self._top_up_note("archive.CabinetLayer", 2 * len(cabinets), CabinetLayer.objects.count() - 2 * len(cabinets), target)

        need, existing = self._need(Category, target)
        roots = [Category(name=f"{_rng.choice(['客户', '供应商', '项目', '财务', '人事', '工程'])}类目{existing + i + 1:02d}", sort_order=i) for i in range(need // 2 or 1)]
        if roots:
            Category.objects.bulk_create(roots)
            children = [Category(name=f"{r.name}-子类{j}", parent=r, sort_order=j) for r in roots for j in range(1, 3)]
            Category.objects.bulk_create(children[: max(0, need - len(roots))])
        self.archive_categories = list(Category.objects.order_by("id"))
        self._top_up_note("archive.Category", max(0, need), existing, target)

        need, existing = self._need(Ledger, target)
        ledgers = [Ledger(name=f"{rand_date(2022, 2025).year}年度{_rng.choice(ARCHIVE_NAMES)}台账{existing + i + 1:02d}-{uuid.uuid4().hex[:6]}", description="造数生成的演示台账", sort_order=i) for i in range(need)]
        if ledgers:
            Ledger.objects.bulk_create(ledgers)
        self.ledgers = list(Ledger.objects.order_by("id"))
        self._top_up_note("archive.Ledger", len(ledgers), existing, target)

        need, existing = self._need(Archive, target)
        # 50 柜 × 2 层 × 30 格 = 3000 个唯一位置；按行号线性铺开，保证 (柜,层,格) 不重复
        archives = []
        for i in range(need):
            n = existing + i + 1
            cab = self.cabinets[((n - 1) // 60) % len(self.cabinets)]
            layer = ((n - 1) // 30) % 2 + 1
            cell = (n - 1) % 30 + 1
            archives.append(
                Archive(
                    code=f"DA-{n:04d}",
                    name=f"{_rng.choice(ARCHIVE_NAMES)}（{cn_company()[:6]}）",
                    cabinet=cab,
                    layer=layer,
                    cell=cell,
                    location_code=f"{cab.code.replace('CAB', '09')}-{layer}-{cell}",
                    ledger=_rng.choice(self.ledgers),
                    category=_rng.choice(self.archive_categories),
                    tags=",".join(_rng.sample(["合同", "扫描件", "待归档", "重要", "2025"], k=2)),
                    description=rand_text(2),
                    counterpart=cn_company(),
                    contract_no=f"HT-{rand_date(2022, 2025):%Y%m%d}-{n:04d}",
                    amount=str(_rng.randint(1000, 999999)),
                    sign_date=rand_past_date(30, 1200),
                    expire_date=rand_future_date(10, 900),
                    department=_rng.choice(DEPARTMENTS),
                    renewal_state=_rng.choice(["pending", "pending", "renewed", "ended"]),
                    security_level=_rng.choice(SECURITY_LEVELS),
                    archive_label=f"{_rng.choice(ARCHIVE_NAMES)}({_rng.choice('一二三四五六')})",
                    handler=cn_name(),
                    copies=_rng.randint(1, 5),
                    attachment_desc=_rng.choice(["原件扫描", "复印件", "电子版"]),
                    creator=_rng.choice(users),
                    source_sheet=f"Sheet{_rng.randint(1, 3)}",
                )
            )
        if archives:
            Archive.objects.bulk_create(archives)
        self.archives = list(Archive.objects.order_by("id"))
        self._top_up_note("archive.Archive", len(archives), existing, target)

        need, existing = self._need(ArchiveFile, target)
        files = []
        for i in range(need):
            arc = self.archives[(existing + i) % len(self.archives)]
            ext = _rng.choice([".pdf", ".pdf", ".png", ".jpg", ".txt"])
            rel = write_media(f"archives/{arc.code}/{uuid.uuid4().hex[:8]}_scan{ext}")
            files.append(ArchiveFile(archive=arc, file=rel))
        if files:
            ArchiveFile.objects.bulk_create(files)
        self._top_up_note("archive.ArchiveFile", len(files), existing, target)

        need, existing = self._need(BorrowRecord, target)
        # 已有未归还借阅的档案不能再挂未归还记录（同一档案同时仅允许一条未归还）
        open_archive_ids = set(BorrowRecord.objects.filter(return_time__isnull=True).values_list("archive_id", flat=True))
        borrows = []
        for i in range(need):
            arc = self.archives[(existing + i) % len(self.archives)]
            open_borrow = (i % 7 == 0) and arc.id not in open_archive_ids
            if open_borrow:
                open_archive_ids.add(arc.id)
            borrow_time = rand_dt(1, 120)
            borrows.append(
                BorrowRecord(
                    archive=arc,
                    borrower=cn_name(),
                    borrow_time=borrow_time,
                    due_time=(borrow_time + timedelta(days=_rng.randint(7, 60))).date(),
                    return_time=None if open_borrow else borrow_time + timedelta(days=_rng.randint(1, 20)),
                    status="borrowed" if open_borrow else "returned",
                    remark=_rng.choice(["", "项目急用", "年审借阅", "复印后归还"]),
                )
            )
        if borrows:
            BorrowRecord.objects.bulk_create(borrows)
            for obj, dt in zip(borrows, [rand_dt(1, 120) for _ in borrows]):
                obj.borrow_time = dt
            BorrowRecord.objects.bulk_update(borrows, ["borrow_time"])
        self._top_up_note("archive.BorrowRecord", len(borrows), existing, target)

        need, existing = self._need(License, target)
        licenses = []
        for i in range(need):
            n = existing + i + 1
            issue = rand_past_date(60, 2000)
            licenses.append(
                License(
                    name=f"{_rng.choice(LICENSE_NAMES)}（{cn_company()[:8]}）",
                    code=f"LIC-{n:04d}",
                    holder=cn_company(),
                    issuer=_rng.choice(["深圳市市场监督管理局", "广东省应急管理厅", "国家认监委", "国家知识产权局"]),
                    issue_date=issue,
                    expire_date=rand_future_date(30, 1500),
                    review_cycle=_rng.choice([0, 12, 12, 24]),
                    remark=rand_text(1),
                    creator=_rng.choice(users),
                )
            )
        for lic in licenses:
            lic.save()  # save() 里按发证日期 + 年审周期自动算 next_review_date
        self.licenses = list(License.objects.order_by("id"))
        self._top_up_note("archive.License", len(licenses), existing, target)

        need, existing = self._need(AnnualReview, target)
        reviews = [
            AnnualReview(
                license=_rng.choice(self.licenses),
                review_date=rand_past_date(1, 900),
                result=_rng.choice(["passed", "passed", "pending", "failed"]),
                reviewer=cn_name(),
                new_expire_date=rand_future_date(60, 900) if _rng.random() > 0.4 else None,
                remark=_rng.choice(["", "材料齐全", "需补检测报告"]),
            )
            for _ in range(need)
        ]
        if reviews:
            AnnualReview.objects.bulk_create(reviews)
        self._top_up_note("archive.AnnualReview", len(reviews), existing, target)

        need, existing = self._need(LicenseBorrowRecord, target)
        open_license_ids = set(LicenseBorrowRecord.objects.filter(return_time__isnull=True).values_list("license_id", flat=True))
        borrows = []
        for i in range(need):
            lic = self.licenses[(existing + i) % len(self.licenses)]
            open_borrow = i % 7 == 0 and lic.id not in open_license_ids
            if open_borrow:
                open_license_ids.add(lic.id)
            bt = rand_dt(1, 120)
            borrows.append(
                LicenseBorrowRecord(
                    license=lic,
                    borrower=cn_name(),
                    borrow_time=bt,
                    due_time=(bt + timedelta(days=_rng.randint(7, 45))).date(),
                    return_time=None if open_borrow else bt + timedelta(days=_rng.randint(1, 15)),
                    status="borrowed" if open_borrow else "returned",
                    remark=_rng.choice(["", "投标借阅", "年审借阅"]),
                )
            )
        if borrows:
            LicenseBorrowRecord.objects.bulk_create(borrows)
            for obj, dt in zip(borrows, [rand_dt(1, 120) for _ in borrows]):
                obj.borrow_time = dt
            LicenseBorrowRecord.objects.bulk_update(borrows, ["borrow_time"])
        self._top_up_note("archive.LicenseBorrowRecord", len(borrows), existing, target)

    # ---------- 知识库 ----------

    def _knowledge(self, target):
        users = self.users

        need, existing = self._need(KnowledgeCategory, target)
        roots = [KnowledgeCategory(name=f"知识一级类{existing + i + 1:02d}", sort_order=i, description="造数生成的演示分类") for i in range(need // 2 or 1)]
        if roots:
            KnowledgeCategory.objects.bulk_create(roots)
            children = [KnowledgeCategory(name=f"{r.name}-二级", parent=r, sort_order=j) for r in roots for j in range(1, 3)]
            KnowledgeCategory.objects.bulk_create(children[: max(0, need - len(roots))])
        self.kb_categories = list(KnowledgeCategory.objects.order_by("id"))
        self._top_up_note("knowledge_base.KnowledgeCategory", max(0, need), existing, target)

        need, existing = self._need(KnowledgeFolder, target)
        folders = [KnowledgeFolder(name=f"我的文件夹{existing + i + 1:02d}", owner=_rng.choice(users), sort=i) for i in range(need)]
        if folders:
            KnowledgeFolder.objects.bulk_create(folders)
        self.kb_folders = list(KnowledgeFolder.objects.order_by("id"))
        self._top_up_note("knowledge_base.KnowledgeFolder", len(folders), existing, target)

        need, existing = self._need(KnowledgeDocument, target)
        docs = []
        for i in range(need):
            n = existing + i + 1
            docs.append(
                KnowledgeDocument(
                    title=f"{_rng.choice(KB_TITLES)} #{n:02d}",
                    category=_rng.choice(self.kb_categories),
                    content=KB_CONTENT + f"\n\n> 演示文档 #{n}",
                    summary=_rng.choice(["过程沉淀", "技术方案", "操作手册", "复盘总结"]),
                    tags=",".join(_rng.sample(["django", "sqlite", "安全", "性能", "流程"], k=2)),
                    status=_rng.choice(["published", "published", "draft"]),
                    view_count=_rng.randint(0, 320),
                    owner=_rng.choice(users),
                    visibility=_rng.choice(["private", "public"]),
                    source="",
                    editor_mode=_rng.choice(["markdown", "outline"]),
                    folder=_rng.choice(self.kb_folders) if _rng.random() > 0.3 else None,
                )
            )
        if docs:
            KnowledgeDocument.objects.bulk_create(docs)
        self.kb_docs = list(KnowledgeDocument.objects.order_by("id"))
        self._top_up_note("knowledge_base.KnowledgeDocument", len(docs), existing, target)

        need, existing = self._need(KnowledgeNode, target)
        nodes = []
        for i in range(need):
            doc = self.kb_docs[(existing + i) % len(self.kb_docs)]
            node_type = _rng.choice(["text", "todo", "code", "quote", "ref"])
            nodes.append(
                KnowledgeNode(
                    document=doc,
                    uid=uuid.uuid4().hex[:12],
                    sort=i,
                    text=_rng.choice(["整理测试用例", "输出评审结论", "更新接口文档", "**重点**：上线前回归", "代码：`git push origin main`", "参见《部署指南》"]),
                    note=_rng.choice(["", "待确认", "已同步"]),
                    node_type=node_type,
                    is_done=_rng.random() > 0.5 if node_type == "todo" else False,
                    due_date=rand_future_date(1, 90) if node_type == "todo" and _rng.random() > 0.5 else None,
                    heading_level=_rng.choice([None, 1, 2, 3]),
                    ref_module=_rng.choice(["", "samples", "archive"]),
                    ref_object_id=_rng.randint(1, 50) if _rng.random() > 0.7 else None,
                    ref_label=_rng.choice(["", "样品主档", "档案"]),
                )
            )
        if nodes:
            KnowledgeNode.objects.bulk_create(nodes)
        self._top_up_note("knowledge_base.KnowledgeNode", len(nodes), existing, target)

        need, existing = self._need(KnowledgeView, target)
        views = [
            KnowledgeView(
                document=_rng.choice(self.kb_docs),
                user=_rng.choice(users),
                viewer=_rng.choice(users).username,
                ip=f"10.0.{_rng.randint(0, 255)}.{_rng.randint(2, 254)}",
            )
            for _ in range(need)
        ]
        if views:
            KnowledgeView.objects.bulk_create(views)
            for obj, dt in zip(views, [rand_dt(0, 90) for _ in views]):
                obj.viewed_at = dt
            KnowledgeView.objects.bulk_update(views, ["viewed_at"])
        self._top_up_note("knowledge_base.KnowledgeView", len(views), existing, target)

    # ---------- 通知 ----------

    def _notifications(self, target):
        users = self.users

        need, existing = self._need(NotificationTemplate, target)
        templates = []
        for i in range(need):
            n = existing + i + 1
            templates.append(
                NotificationTemplate(
                    code=f"demo_template_{n:02d}",
                    name=f"演示通知模板{n:02d}",
                    description="造数生成的演示模板",
                    title_template="【演示】{{ title }}",
                    content_template="这是一条演示通知：{{ detail }}",
                    email_subject_template="演示通知：{{ title }}" if n % 2 else None,
                    email_body_template="<p>{{ detail }}</p>" if n % 2 else None,
                    category=_rng.choice(["business", "task", "announcement"]),
                )
            )
        if templates:
            NotificationTemplate.objects.bulk_create(templates)
        self.templates = list(NotificationTemplate.objects.order_by("id"))
        self._top_up_note("notifications.NotificationTemplate", len(templates), existing, target)

        need, existing = self._need(Notification, target)
        notifications = []
        for i in range(need):
            status = _rng.choice(["pending", "sent", "sent", "sent", "failed", "revoked"])
            is_read = status == "sent" and _rng.random() > 0.5
            notifications.append(
                Notification(
                    recipient=_rng.choice(users),
                    template=_rng.choice(self.templates),
                    created_by=_rng.choice(users) if _rng.random() > 0.5 else None,
                    title=f"演示通知{i + existing + 1:03d}",
                    content=rand_text(2),
                    category=_rng.choice(["business", "task", "announcement"]),
                    status=status,
                    is_read=is_read,
                    read_at=rand_dt(0, 30) if is_read else None,
                    priority=_rng.choice(["normal", "normal", "high"]),
                    dedup_key=uuid.uuid4().hex,
                    agg_key=uuid.uuid4().hex[:16],
                    agg_count=_rng.randint(1, 5),
                    batch_id=uuid.uuid4().hex if _rng.random() > 0.7 else "",
                    link=_rng.choice(["", "/records/daily/", "/samples/", "/archive/"]),
                    context_json={"title": "演示", "detail": rand_text(1)},
                    scheduled_at=rand_dt(0, 30),
                    expires_at=rand_future_date(1, 60) if _rng.random() > 0.5 else None,
                    retry_count=_rng.randint(0, 3),
                    sent_at=rand_dt(0, 30) if status in ("sent", "failed") else None,
                    last_error="" if status != "failed" else "SMTP 连接超时",
                )
            )
        if notifications:
            Notification.objects.bulk_create(notifications)
        self.notifications = list(Notification.objects.order_by("id"))
        self._top_up_note("notifications.Notification", len(notifications), existing, target)

        need, existing = self._need(NotificationDelivery, target)
        # 唯一约束防护：(notification, channel) 唯一
        used_pairs = set(NotificationDelivery.objects.values_list("notification_id", "channel"))
        deliveries = []
        for i in range(need):
            notif = self.notifications[(existing + i) % len(self.notifications)]
            channel = "inapp" if i % 2 else "email"
            if (notif.id, channel) in used_pairs:
                channel = "email" if channel == "inapp" else "inapp"
                if (notif.id, channel) in used_pairs:
                    continue
            used_pairs.add((notif.id, channel))
            deliveries.append(
                NotificationDelivery(
                    notification=notif,
                    channel=channel,
                    status=_rng.choice(["success", "success", "failed", "skipped"]),
                    detail=_rng.choice(["", "已投递", "收件人地址无效"]),
                    attempts=_rng.randint(1, 3),
                )
            )
        if deliveries:
            NotificationDelivery.objects.bulk_create(deliveries)
        self._top_up_note("notifications.NotificationDelivery", len(deliveries), existing, target)

        have = {p.user_id for p in NotificationPreference.objects.all()}
        prefs = [
            NotificationPreference(
                user=u,
                channel_prefs=_rng.choice([{}, {"task": {"email": False}}, {"business": {"email": False}}]),
                agg_window_minutes=_rng.choice([0, 5, 10, 30]),
                quiet_start=time(22, 0) if _rng.random() > 0.5 else None,
                quiet_end=time(7, 30) if _rng.random() > 0.5 else None,
            )
            for u in users if u.id not in have
        ]
        if prefs:
            NotificationPreference.objects.bulk_create(prefs)
        self._top_up_note("notifications.NotificationPreference", len(prefs), NotificationPreference.objects.count() - len(prefs), target)

        need, existing = self._need(ScheduledTask, target)
        tasks = []
        for i in range(need):
            n = existing + i + 1
            tasks.append(
                ScheduledTask(
                    code=f"demo_task_{n:02d}",
                    name=f"演示定时任务{n:02d}",
                    description="造数生成的演示任务（默认不启用）",
                    enabled=False,
                    trigger_type=_rng.choice(["daily", "interval"]),
                    run_time=time(_rng.randint(1, 23), _rng.choice([0, 30])),
                    interval_minutes=_rng.choice([15, 30, 60, 120]),
                    params={"demo": True},
                )
            )
        if tasks:
            ScheduledTask.objects.bulk_create(tasks)
        self.tasks = list(ScheduledTask.objects.order_by("id"))
        self._top_up_note("notifications.ScheduledTask", len(tasks), existing, target)

        need, existing = self._need(TaskRunLog, target)
        run_logs = []
        for i in range(need):
            started = rand_dt(0, 60)
            run_logs.append(
                TaskRunLog(
                    task=_rng.choice(self.tasks),
                    code=_rng.choice(self.tasks).code,
                    source=_rng.choice(["schedule", "manual"]),
                    status=_rng.choice(["success", "success", "fail"]),
                    duration_ms=_rng.randint(120, 9800),
                    message=_rng.choice(["执行成功，无异常", "执行成功：处理 12 条", "失败：命令返回非零"]),
                    started_at=started,
                    finished_at=started + timedelta(milliseconds=_rng.randint(120, 9800)),
                )
            )
        if run_logs:
            TaskRunLog.objects.bulk_create(run_logs)
        self._top_up_note("notifications.TaskRunLog", len(run_logs), existing, target)

        need, existing = self._need(RequestMetric, target)
        paths = ["/", "/records/daily/", "/samples/", "/archive/", "/knowledge/", "/notifications/", "/sysbackup/", "/api/records/daily-records/", "/accounts/sessions/"]
        metrics = [
            RequestMetric(
                method=_rng.choice(["GET", "GET", "GET", "POST"]),
                path=_rng.choice(paths),
                status_code=_rng.choice([200, 200, 200, 200, 302, 403, 500]),
                duration_ms=_rng.randint(8, 2400),
                db_count=_rng.randint(1, 40),
                db_time_ms=_rng.randint(2, 900),
                slow_query=_rng.choice(["", "SELECT ... FROM records_dailyrecord ..."]),
                user=_rng.choice(users).username,
            )
            for _ in range(need)
        ]
        if metrics:
            RequestMetric.objects.bulk_create(metrics)
            for obj, dt in zip(metrics, [rand_dt(0, 30) for _ in metrics]):
                obj.created_at = dt
            RequestMetric.objects.bulk_update(metrics, ["created_at"])
        self._top_up_note("notifications.RequestMetric", len(metrics), existing, target)

        need, existing = self._need(SystemLog, target)
        logs = []
        for i in range(need):
            level = _rng.choice(["INFO", "INFO", "INFO", "WARNING", "ERROR", "DEBUG"])
            msg = _rng.choice([
                "请求处理完成",
                "定时任务执行成功",
                "检测到慢查询（>1s）",
                "用户登录成功",
                "备份任务完成",
                "证书即将到期提醒",
            ])
            logs.append(
                SystemLog(
                    logged_at=rand_dt(0, 30),
                    level=level,
                    logger=_rng.choice(["django.request", "work_record.scheduler", "notifications.worker", "sysbackup.services"]),
                    source=_rng.choice(["scheduler.out", "runserver.log", "backup.log"]),
                    message=f"[演示] {msg} #{i + existing + 1}",
                    fingerprint=hashlib.sha1(f"{level}-{msg}-{i}-{uuid.uuid4().hex}".encode()).hexdigest(),
                )
            )
        if logs:
            SystemLog.objects.bulk_create(logs)
        self._top_up_note("notifications.SystemLog", len(logs), existing, target)

    # ---------- 业务记录 ----------

    def _records(self, target):
        users = self.users

        need, existing = self._need(Decoration, target)
        rows = []
        for i in range(need):
            original = rand_decimal(50000, 900000)
            factor = Decimal(str(round(_rng.uniform(0.6, 0.98), 4)))
            rows.append(
                Decoration(
                    project_name=f"{cn_company()[:4]}车间改造项目{i + existing + 1:02d}期",
                    constructor=cn_company(),
                    project_address=f"{_rng.choice(['深圳', '东莞', '惠州'])}市{_rng.choice(['南山', '宝安', '龙岗', '长安'])}区{_rng.choice(['工业', '科技', '大道'])}路{_rng.randint(1, 999)}号",
                    start_date=rand_past_date(30, 1500),
                    original_price=original,
                    final_price=original * factor,
                )
            )
        if rows:
            Decoration.objects.bulk_create(rows)
        self._top_up_note("records.Decoration", len(rows), existing, target)

        need, existing = self._need(CustomerCredit, target)
        rows = []
        for i in range(need):
            has_assess = _rng.random() > 0.5
            rows.append(
                CustomerCredit(
                    proposal_date=rand_past_date(1, 900),
                    site_date=rand_past_date(1, 900) if _rng.random() > 0.4 else None,
                    customer_name=cn_company(),
                    customer_type=_rng.choice(["普通客户", "代加工客户"]),
                    salesperson=cn_name(),
                    work_content=_rng.choice(["客户催款或复审", "客户账期审核", "提升账期或额度"]),
                    specific_content=_rng.choice(["老客户重审", "提升账期或额度", "新客户账期"]),
                    pre_survey=_rng.choice(["有", "无"]),
                    work_method=_rng.choice(["刘总特批", "现场审核", "内部流程"]),
                    result=_rng.choice(["通过", "有条件通过", "驳回", "暂缓"]),
                    remarks=rand_text(1),
                    has_assessment_period=has_assess,
                    assessment_start_date=rand_past_date(1, 300) if has_assess else None,
                    assessment_end_date=rand_future_date(1, 300) if has_assess else None,
                    assessment_content="回款及时率、订单增长率" if has_assess else "",
                )
            )
        if rows:
            CustomerCredit.objects.bulk_create(rows)
        self._top_up_note("records.CustomerCredit", len(rows), existing, target)

        need, existing = self._need(CustomerComplaint, target)
        rows = []
        for i in range(need):
            closed = _rng.random() > 0.6
            complaint_date = rand_past_date(1, 700)
            rows.append(
                CustomerComplaint(
                    customer_name=cn_company(),
                    responsible_salesperson=cn_name(),
                    complaint_date=complaint_date,
                    terminal_customer=cn_company(),
                    product_model=_rng.choice(MODELS),
                    our_shipment=_rng.randint(100, 9000),
                    customer_shipment=_rng.randint(100, 9000),
                    complaint_description=rand_text(3),
                    compensation_amount=rand_decimal(500, 200000),
                    is_insured=_rng.random() > 0.5,
                    engineering_report=rand_text(2),
                    engineering_analysis=rand_text(2),
                    missing_materials=_rng.choice(["", "出货单", "检测报告", "聊天记录"]),
                    complaint_status="已完成" if closed else _rng.choice(["未完成", "处理中"]),
                    follow_up_status=rand_text(2),
                    legal_stage=_rng.choice(["", "内部处理", "保险理赔", "协商中", "准备诉讼", "诉讼中", "已判决"]),
                    remarks=rand_text(1),
                    closed_at=rand_dt(1, 60) if closed else None,
                    closed_by=_rng.choice(users) if closed else None,
                    closure_result=_rng.choice(["accepted", "partial", "rejected"]) if closed else "",
                    closure_note="已与客户达成一致" if closed else "",
                    sla_hours=_rng.choice([0, 24, 48, 72]),
                )
            )
        if rows:
            CustomerComplaint.objects.bulk_create(rows)
        self._top_up_note("records.CustomerComplaint", len(rows), existing, target)

        need, existing = self._need(Regulation, target)
        rows = [
            Regulation(
                issue_date=rand_past_date(30, 1200),
                title=f"关于{_rng.choice(['考勤管理', '出差报销', '安全生产', '保密管理', '样品确认', '供应商准入'])}的规定{existing + i + 1:02d}号",
                content=rand_text(4),
                doc_no=f"XX司〔{rand_date(2022, 2025).year}〕{existing + i + 1:03d}号",
            )
            for i in range(need)
        ]
        if rows:
            Regulation.objects.bulk_create(rows)
        self._top_up_note("records.Regulation", len(rows), existing, target)

        need, existing = self._need(PriceEvaluation, target)
        rows = []
        for i in range(need):
            sup = [rand_decimal(100, 50000) if _rng.random() > 0.2 else None for _ in range(3)]
            valid = [p for p in sup if p is not None]
            base_price = min(valid) if valid else rand_decimal(100, 50000)
            rows.append(
                PriceEvaluation(
                    project_category=_rng.choice(["原材料", "设备", "工程", "服务", "耗材"]),
                    proposing_department=_rng.choice(DEPARTMENTS),
                    proposer=cn_name(),
                    evaluation_content=rand_text(2),
                    supplier1_name=cn_company() if sup[0] is not None else "",
                    supplier1_price=sup[0],
                    supplier2_name=cn_company() if sup[1] is not None else "",
                    supplier2_price=sup[1],
                    supplier3_name=cn_company() if sup[2] is not None else "",
                    supplier3_price=sup[2],
                    internal_control_supplier=cn_company(),
                    internal_control_price=base_price * Decimal(str(round(_rng.uniform(0.85, 1.0), 4))),
                )
            )
        if rows:
            PriceEvaluation.objects.bulk_create(rows)
        self._top_up_note("records.PriceEvaluation", len(rows), existing, target)

        need, existing = self._need(DailyRecord, target)
        rows = []
        for i in range(need):
            done = _rng.random() > 0.4
            rows.append(
                DailyRecord(
                    user=_rng.choice(users),
                    department=f"{_rng.choice(DEPARTMENTS)}/{cn_name()}",
                    work_content=rand_text(3),
                    due_date=rand_future_date(-30, 60),
                    status="完成" if done else "未完成",
                    progress=100 if done else _rng.randint(0, 95),
                    kb_enabled=_rng.random() > 0.7,
                    kb_visibility=_rng.choice(["private", "public"]),
                )
            )
        if rows:
            DailyRecord.objects.bulk_create(rows)
            for obj, dt in zip(rows, [rand_dt(0, 120) for _ in rows]):
                obj.record_date = dt
            DailyRecord.objects.bulk_update(rows, ["record_date"])
        self._top_up_note("records.DailyRecord", len(rows), existing, target)

        need, existing = self._need(MetalPrice, target)
        rows = []
        for i in range(need):
            price = rand_decimal(50, 90000)
            rows.append(
                MetalPrice(
                    metal_name=_rng.choice(METALS),
                    price=price,
                    unit=_rng.choice(["元/吨", "元/千克", "元/吨", "美元/吨"]),
                    price_change=rand_decimal(-2000, 2000),
                )
            )
        if rows:
            MetalPrice.objects.bulk_create(rows)
            for obj, dt in zip(rows, [rand_dt(0, 30) for _ in rows]):
                obj.crawl_time = dt
                obj.crawl_date = dt.date()
            MetalPrice.objects.bulk_update(rows, ["crawl_time", "crawl_date"])
        self._top_up_note("records.MetalPrice", len(rows), existing, target)

        need, existing = self._need(InsuranceSubject, target)
        # 唯一约束防护：车牌非空唯一；(非车险 kind, insurance_type) 唯一
        used_plates = set(InsuranceSubject.objects.exclude(plate="").values_list("plate", flat=True))
        used_noncar = set(InsuranceSubject.objects.filter(kind="non_car").values_list("insurance_type", flat=True))
        subjects = []
        for i in range(need):
            n = existing + i + 1
            if n % 2 == 1:
                while True:
                    plate = f"{PLATE_PREFIX[_rng.randint(0, len(PLATE_PREFIX) - 1)]}{_rng.choice('ABCDEFGHJK')}{_rng.randint(10000, 99999)}"
                    if plate not in used_plates:
                        used_plates.add(plate)
                        break
                subjects.append(
                    InsuranceSubject(
                        kind="car",
                        label=f"{plate} 公司用车",
                        plate=plate,
                        insurance_type="车险",
                        department=_rng.choice(DEPARTMENTS),
                        remarks=_rng.choice(["", "新车投保", "续保"]),
                        is_one_off=False,
                    )
                )
            else:
                k = len(used_noncar)
                base = INSURANCE_TYPES[k % len(INSURANCE_TYPES)]
                insurance_type = base if k < len(INSURANCE_TYPES) else f"{base}({k // len(INSURANCE_TYPES) + 1})"
                used_noncar.add(insurance_type)
                subjects.append(
                    InsuranceSubject(
                        kind="non_car",
                        label=insurance_type,
                        plate="",
                        insurance_type=insurance_type,
                        department=_rng.choice(DEPARTMENTS),
                        remarks=_rng.choice(["", "年度续保"]),
                        is_one_off=_rng.random() > 0.8,
                    )
                )
        InsuranceSubject.objects.bulk_create(subjects)
        self.subjects = list(InsuranceSubject.objects.order_by("id"))
        self._top_up_note("records.InsuranceSubject", len(subjects), existing, target)

        need, existing = self._need(InsurancePolicyYear, target)
        # 唯一约束防护：(subject, year) 唯一——优先挑还没有保单年度的标的
        covered = set(InsurancePolicyYear.objects.values_list("subject_id", "year"))
        candidates = [s for s in self.subjects if not any((s.id, y) in covered for y in (2024, 2025, 2026))] or self.subjects
        years = []
        for i in range(need):
            subj = candidates[(existing + i) % len(candidates)]
            year = 2024 + (existing + i) % 2
            while (subj.id, year) in covered:
                year += 1
            covered.add((subj.id, year))
            start = rand_past_date(30, 900)
            years.append(
                InsurancePolicyYear(
                    subject=subj,
                    year=year,
                    start_date=start,
                    end_date=start + timedelta(days=365),
                    premium=rand_decimal(1000, 200000),
                    insurer=_rng.choice(INSURERS),
                )
            )
        if years:
            InsurancePolicyYear.objects.bulk_create(years)
        self.policy_years = list(InsurancePolicyYear.objects.order_by("id"))
        self._top_up_note("records.InsurancePolicyYear", len(years), existing, target)

        need, existing = self._need(Insurance, target)
        rows = []
        for i in range(need):
            idx = (existing + i) % len(self.policy_years)
            py = self.policy_years[idx]
            rows.append(
                Insurance(
                    company_name=py.insurer,
                    canonical_name=py.insurer,
                    title=f"{py.subject.label} {py.year}年度保单",
                    order_number=f"BX-{rand_date(2023, 2025):%Y%m%d}-{existing + i + 1:04d}",
                    premium=py.premium,
                    issue_date=py.start_date,
                    end_date=py.end_date,
                    department=py.subject.department or _rng.choice(DEPARTMENTS),
                    remarks=_rng.choice(["", "自动续保", "新单"]),
                    subject=py.subject,
                    policy_year=py,
                )
            )
        if rows:
            Insurance.objects.bulk_create(rows)
        self._top_up_note("records.Insurance", len(rows), existing, target)

        need, existing = self._need(Complaint, target)
        rows = []
        for i in range(need):
            progress = _rng.choice(["立案", "审理中", "判决", "执行", "结案"])
            filed = rand_past_date(60, 1200)
            rows.append(
                Complaint(
                    dispute_type=_rng.choice(["买卖合同纠纷", "产品质量纠纷", "加工承揽纠纷", "服务合同纠纷"]),
                    defendant=cn_company(),
                    case_number=f"（{rand_date(2022, 2025).year}）{_rng.choice(['粤03', '粤19', '粤06'])}民初{_rng.randint(10000, 99999)}号",
                    amount=rand_decimal(10000, 5000000),
                    court=f"{_rng.choice(['深圳市', '东莞市', '广州市'])}{_rng.choice(['南山', '宝安', '中级'])}人民法院",
                    lawyer=cn_name(),
                    progress=progress,
                    detailed_progress=rand_text(2),
                    remarks=rand_text(1),
                    filed_at=filed,
                    hearing_at=filed + timedelta(days=_rng.randint(30, 120)) if progress in ("审理中", "判决", "执行", "结案") else None,
                    judgment_at=filed + timedelta(days=_rng.randint(120, 300)) if progress in ("判决", "执行", "结案") else None,
                    execution_at=filed + timedelta(days=_rng.randint(300, 500)) if progress in ("执行", "结案") else None,
                    closed_at=filed + timedelta(days=_rng.randint(400, 800)) if progress == "结案" else None,
                )
            )
        if rows:
            Complaint.objects.bulk_create(rows)
        self._top_up_note("records.Complaint", len(rows), existing, target)

        need, existing = self._need(OperationLog, target)
        rows = []
        for i in range(need):
            op = _rng.choice(["新增", "编辑", "删除", "导出", "登录"])
            rows.append(
                OperationLog(
                    operator=cn_name(),
                    operator_username=f"demo_user_{_rng.randint(1, len(users)):03d}",
                    module_type=_rng.choice(["每日记录", "客户台账", "样品管理", "档案管理", "保险管理", "系统管理"]),
                    operation_type=op,
                    object_identifier=f"OBJ-{existing + i + 1:04d}",
                    change_summary=f"{op}了一条演示数据",
                    before_json={"status": "未完成"} if op == "编辑" else None,
                    after_json={"status": "完成"} if op in ("编辑", "新增") else None,
                )
            )
        if rows:
            OperationLog.objects.bulk_create(rows)
            for obj, dt in zip(rows, [rand_dt(0, 90) for _ in rows]):
                obj.operation_time = dt
            OperationLog.objects.bulk_update(rows, ["operation_time"])
        self._top_up_note("records.OperationLog", len(rows), existing, target)

        need, existing = self._need(CustomerLedger, target)
        ledgers = []
        for i in range(need):
            n = existing + i + 1
            ledgers.append(
                CustomerLedger(
                    responsible_salesperson=cn_name(),
                    customer_name=cn_company(),
                    customer_category=_rng.choice(["生产型", "贸易型", "上市", "国企"]),
                    legal_representative=cn_name(),
                    address=f"{_rng.choice(['深圳', '东莞', '苏州'])}市{_rng.choice(['南山', '宝安', '昆山'])}区{_rng.choice(['工业', '科技'])}园{_rng.randint(1, 50)}栋",
                    credit_code=f"91{_rng.randint(10000000000000000, 99999999999999999)}",
                    registered_capital=rand_decimal(50, 20000),
                    registered_capital_text="",
                    paid_in_capital=rand_decimal(50, 20000) if _rng.random() > 0.3 else None,
                    company_status=_rng.choice(["存续", "在业", "注销"]),
                    business_license=_rng.choice(["有", "无"]),
                    payment_terms=_rng.choice(["月结30天", "月结60天", "货到付款", "月结90天"]),
                    credit_limit=rand_decimal(10, 2000),
                    credit_limit_text="",
                    change_date=rand_past_date(1, 500),
                    basic_contract=_rng.choice(["已签", "未签", "续签中"]),
                    contract_sign_date=rand_past_date(30, 900),
                    contract_issues=_rng.choice(["", "付款条款待明确"]),
                    asset_guarantee=_rng.choice(["房产抵押", "设备抵押", "无"]),
                    legal_id_card=f"44030{_rng.randint(19000101, 20261231)}{_rng.randint(100, 999)}",
                    guarantor_id_card=f"44030{_rng.randint(19000101, 20261231)}{_rng.randint(100, 999)}",
                    asset_guarantor=cn_name(),
                    guaranteed_asset_detail=_rng.choice(["", "商品房一套", "生产线一条"]),
                    asset_mortgaged=_rng.choice(["是", "否"]),
                    asset_guarantee_issues=_rng.choice(["", "抵押顺位需核实"]),
                    contract_count=str(_rng.randint(1, 12)),
                    handling_suggestion=_rng.choice(["维持现有额度", "适度提升", "收缩额度"]),
                    system_info=_rng.choice(["金蝶K3", "SAP", "用友U8"]),
                    location=f"{_rng.choice(['A', 'B', 'C'])}区-{_rng.randint(1, 20)}柜",
                    archive_no=f"TZ-{n:04d}",
                    archive_date=rand_past_date(1, 300),
                    remarks=rand_text(1),
                )
            )
        # save() 会按账期/额度自动生成变更历史，逐条创建
        for ledger in ledgers:
            ledger.save()
        self.ledger_objs = list(CustomerLedger.objects.order_by("id"))
        self._top_up_note("records.CustomerLedger", len(ledgers), existing, target)

        need, existing = self._need(CustomerLedgerChangeLog, target)
        if need > 0:
            # 改一版账期/额度，让「编辑」来源的变更历史也凑足差额
            extra = 0
            for ledger in self.ledger_objs:
                if extra >= need:
                    break
                ledger._credit_change_source = "编辑"
                ledger.payment_terms = _rng.choice(["月结45天", "月结75天", "货到付款"])
                ledger.credit_limit_text = f"{_rng.randint(100, 3000)} 万元"
                ledger.save()
                extra += 1
            need, existing = self._need(CustomerLedgerChangeLog, target)
        self._top_up_note("records.CustomerLedgerChangeLog", max(0, target - existing), existing - max(0, target - existing), target)

    # ---------- 试样管理 ----------

    def _samples(self, target):
        need, existing = self._need(Partner, target)
        partners = []
        for i in range(need):
            n = existing + i + 1
            partners.append(
                Partner(
                    partner_code=f"SUP-{n:04d}",
                    name=f"{cn_company()[:10]}{n:03d}",
                    contact_person=cn_name(),
                    contact_info=phone(),
                    status=_rng.choice(["active", "active", "inactive"]),
                )
            )
        if partners:
            Partner.objects.bulk_create(partners)
        self.partners = list(Partner.objects.order_by("id"))
        self._top_up_note("samples.Partner", len(partners), existing, target)

        need, existing = self._need(Sample, target)
        samples = []
        for i in range(need):
            n = existing + i + 1
            samples.append(
                Sample(
                    sample_no=f"SP-{rand_date(2024, 2026):%Y}-{n:04d}",
                    material_name=_rng.choice(MATERIALS),
                    model=_rng.choice(MODELS),
                    sample_date=rand_past_date(1, 700),
                    status=_rng.choice(["pending", "testing", "small_batch", "imported", "terminated"]),
                    registrar=cn_name(),
                    registered_at=rand_dt(1, 700),
                    spec=f"规格：{_rng.choice(['0.5mm', '1.2mm', 'Φ20mm', '2.0m/支'])}",
                    incoming_qty=f"{_rng.randint(10, 500)}{_rng.choice(['kg', 'pcs', '卷'])}",
                    supplier=_rng.choice(self.partners),
                    supplier_type=_rng.choice(["new", "existing"]),
                )
            )
        if samples:
            Sample.objects.bulk_create(samples)
            for obj, dt in zip(samples, [rand_dt(1, 700) for _ in samples]):
                obj.registered_at = dt
            Sample.objects.bulk_update(samples, ["registered_at"])
        self.samples = list(Sample.objects.order_by("id"))
        self._top_up_note("samples.Sample", len(samples), existing, target)

        # 试样 / 小批量记录：轮次分布在 10 条主档上，避免 (sample, round_no) 唯一约束冲突
        sample_span = min(10, len(self.samples)) or 1
        test_rounds = {sid: mx for sid, mx in TestRecord.objects.values_list("sample").annotate(models.Max("round_no")).values_list("sample", "round_no__max")}
        batch_rounds = {sid: mx for sid, mx in BatchRecord.objects.values_list("sample").annotate(models.Max("round_no")).values_list("sample", "round_no__max")}

        need, existing = self._need(TestRecord, target)
        rows = []
        for i in range(need):
            n = existing + i + 1
            sample = self.samples[(existing + i) % sample_span]
            round_no = test_rounds.get(sample.id, 0) + (existing + i) // sample_span + 1
            rows.append(
                TestRecord(
                    test_no=f"TR-{n:04d}",
                    sample=sample,
                    round_no=round_no,
                    test_date=rand_past_date(1, 600),
                    test_result=rand_text(2),
                    review_conclusion=_rng.choice(["pass", "fail", "improve"]),
                    report_code=f"RPT-{rand_date(2024, 2026):%Y%m%d}-{n:04d}",
                    notified=_rng.random() > 0.4,
                    operator=cn_name(),
                    package_type=_rng.choice(["", "卷装", "盒装", "托盘"]),
                    incoming_date=rand_past_date(1, 600) if _rng.random() > 0.5 else None,
                    remark=_rng.choice(["", "复测一轮"]),
                )
            )
        if rows:
            TestRecord.objects.bulk_create(rows)
            for obj, dt in zip(rows, [rand_dt(1, 600) for _ in rows]):
                obj.operated_at = dt
            TestRecord.objects.bulk_update(rows, ["operated_at"])
        self._top_up_note("samples.TestRecord", len(rows), existing, target)

        need, existing = self._need(BatchRecord, target)
        rows = []
        for i in range(need):
            n = existing + i + 1
            sample = self.samples[(existing + i) % sample_span]
            rows.append(
                BatchRecord(
                    batch_no=f"BR-{n:04d}",
                    sample=sample,
                    round_no=batch_rounds.get(sample.id, 0) + (existing + i) // sample_span + 1,
                    ordered=_rng.random() > 0.3,
                    batch_result=_rng.choice(["pass", "fail", "ongoing"]),
                    kingdee_entered=_rng.random() > 0.5,
                    operator=cn_name(),
                    unit_price=rand_decimal(1, 500),
                    alternative_model=_rng.choice(["", _rng.choice(MODELS)]),
                    remark=_rng.choice(["", "分批验证"]),
                )
            )
        if rows:
            BatchRecord.objects.bulk_create(rows)
            for obj, dt in zip(rows, [rand_dt(1, 600) for _ in rows]):
                obj.operated_at = dt
            BatchRecord.objects.bulk_update(rows, ["operated_at"])
        self._top_up_note("samples.BatchRecord", len(rows), existing, target)

        partner_span = min(10, len(self.partners)) or 1
        audit_rounds = {pid: mx for pid, mx in AuditRecord.objects.values_list("partner").annotate(models.Max("round_no")).values_list("partner", "round_no__max")}

        need, existing = self._need(AuditRecord, target)
        rows = []
        for i in range(need):
            n = existing + i + 1
            partner = self.partners[(existing + i) % partner_span]
            rel = write_media(f"audits/{partner.partner_code}/{uuid.uuid4().hex[:8]}.pdf")
            rows.append(
                AuditRecord(
                    audit_no=f"AR-{n:04d}",
                    partner=partner,
                    round_no=audit_rounds.get(partner.id, 0) + (existing + i) // partner_span + 1,
                    audit_date=rand_past_date(1, 700),
                    audit_result=_rng.choice(["pass", "fail", "conditional"]),
                    audit_remark=rand_text(2),
                    auditor=cn_name(),
                    file=rel,
                )
            )
        if rows:
            AuditRecord.objects.bulk_create(rows)
            for obj, dt in zip(rows, [rand_dt(1, 700) for _ in rows]):
                obj.operated_at = dt
            AuditRecord.objects.bulk_update(rows, ["operated_at"])
        self._top_up_note("samples.AuditRecord", len(rows), existing, target)

        need, existing = self._need(ProofAttachment, target)
        rows = []
        stages = ["test_report", "price_list_update", "small_batch_notification", "small_batch_order", "small_batch_result", "mass_production_order"]
        for i in range(need):
            sample = self.samples[(existing + i) % len(self.samples)]
            stage = stages[(existing + i) % len(stages)]
            ext = _rng.choice([".pdf", ".png"])
            rel = write_media(f"proofs/{sample.sample_no}/{stage}/{uuid.uuid4().hex[:8]}{ext}")
            rows.append(
                ProofAttachment(
                    sample=sample,
                    stage=stage,
                    file=rel,
                    original_name=f"{stage}_凭证_{existing + i + 1:03d}{ext}",
                    uploaded_by=cn_name(),
                    remark=_rng.choice(["", "系统截图"]),
                )
            )
        if rows:
            ProofAttachment.objects.bulk_create(rows)
        self._top_up_note("samples.ProofAttachment", len(rows), existing, target)

    # ---------- 通用附件（客户客诉/诉讼/保险/客户台账） ----------

    # 各模块演示附件的文件名池（不含扩展名）
    _ATTACHMENT_DEMO_NAMES = {
        "customer_complaint": ["客诉现象照片", "不良品实物照片", "物流签收单", "工程报告", "8D整改报告", "赔偿协议"],
        "complaint": ["起诉状", "证据材料清单", "庭审笔录", "民事判决书", "执行申请书"],
        "insurance": ["电子保单", "批单", "缴费凭证", "理赔申请材料"],
        "customer_ledger": ["营业执照扫描件", "基本合同", "对账单", "法人身份证复印件"],
    }

    def _attachments(self, target):
        """给四个模块的业务记录补演示附件（幂等：已有附件的记录跳过）。

        附件不属于「每表 50 条」契约（一条业务记录可挂 0~N 个附件），这里按
        记录补 1~3 个，media/ 下放占位文件，下载/预览链接可用。
        """
        plans = [
            (CustomerComplaint, "customer_complaint"),
            (Complaint, "complaint"),
            (Insurance, "insurance"),
            (CustomerLedger, "customer_ledger"),
        ]
        for model, module_key in plans:
            ct = ContentType.objects.get_for_model(model)
            cats = ATTACHMENT_CATEGORIES.get(module_key, [])
            names = self._ATTACHMENT_DEMO_NAMES.get(module_key, ["附件"])
            has_any = set(
                Attachment.objects.filter(content_type=ct)
                .values_list("object_id", flat=True)
                .distinct()
            )
            rows = []
            for obj in model.objects.all().iterator():
                if obj.pk in has_any:
                    continue
                for k in range(_rng.randint(1, 3)):
                    ext = _rng.choice([".pdf", ".png", ".jpg", ".xlsx", ".docx"])
                    rel = write_media(
                        f"attachments/{module_key}/{ct.model}/{obj.pk}/"
                        f"{uuid.uuid4().hex[:8]}_{k + 1}{ext}"
                    )
                    rows.append(
                        Attachment(
                            content_type=ct,
                            object_id=obj.pk,
                            file=rel,
                            original_name=_rng.choice(names) + ext,
                            size=_rng.randint(20 * 1024, 5 * 1024 * 1024),
                            category=_rng.choice(cats)[0] if cats else "",
                            uploaded_by=_rng.choice(self.users),
                        )
                    )
            if rows:
                Attachment.objects.bulk_create(rows)
                for obj, dt in zip(rows, [rand_dt(0, 90) for _ in rows]):
                    obj.created_at = dt
                Attachment.objects.bulk_update(rows, ["created_at"])
            self.stdout.write(f"  - attachments.{module_key}: 新增 {len(rows)} 个附件")

    # ---------- 系统备份 ----------

    def _sysbackup(self, target):
        need, existing = self._need(BackupOperationLog, target)
        rows = [
            BackupOperationLog(
                op_type=_rng.choice(["backup", "restore"]),
                operator=f"demo_user_{_rng.randint(1, len(self.users)):03d}",
                file_name=f"backup_{rand_date(2024, 2026):%Y%m%d}_{_rng.randint(100000, 999999)}.zip" if _rng.random() > 0.3 else "",
                file_path="backups/" if _rng.random() > 0.5 else "",
                success=_rng.random() > 0.2,
                detail=_rng.choice(["操作成功", "校验通过", "失败：磁盘空间不足"]),
            )
            for _ in range(need)
        ]
        if rows:
            BackupOperationLog.objects.bulk_create(rows)
            for obj, dt in zip(rows, [rand_dt(0, 90) for _ in rows]):
                obj.created_at = dt
            BackupOperationLog.objects.bulk_update(rows, ["created_at"])
        self._top_up_note("sysbackup.BackupOperationLog", len(rows), existing, target)

        need, existing = self._need(BackupTask, target)
        rows = []
        for i in range(need):
            started = rand_dt(0, 60)
            status = _rng.choice(["success", "success", "failed"])
            rows.append(
                BackupTask(
                    id=uuid.uuid4().hex,
                    kind=_rng.choice(["backup", "restore"]),
                    status=status,
                    stage=_rng.choice(["打包数据库", "压缩附件", "写入存储", "校验完整性"]),
                    percent=100 if status == "success" else _rng.randint(10, 90),
                    message=_rng.choice(["任务完成", "任务失败：磁盘空间不足"]),
                    error="" if status == "success" else "OSError: [Errno 28] No space left on device",
                    log=[{"t": started.isoformat(), "msg": "任务启动"}],
                    result={"size": _rng.randint(1024, 104857600)} if status == "success" else {},
                )
            )
        if rows:
            BackupTask.objects.bulk_create(rows)
            for obj, dt in zip(rows, [rand_dt(0, 60) for _ in rows]):
                obj.started_at = dt
                obj.heartbeat_at = dt
                obj.finished_at = dt + timedelta(seconds=_rng.randint(5, 300))
            BackupTask.objects.bulk_update(rows, ["started_at", "heartbeat_at", "finished_at"])
        self._top_up_note("sysbackup.BackupTask", len(rows), existing, target)

    # ---------- Django 内置表 ----------

    def _django_builtin(self, target):
        # Admin 操作日志
        need, existing = self._need(LogEntry, target)
        cts = list(ContentType.objects.exclude(app_label__in=["contenttypes", "sessions"]))
        rows = []
        for i in range(need):
            rows.append(
                LogEntry(
                    user=_rng.choice(self.users),
                    content_type=_rng.choice(cts) if cts else ContentType.objects.first(),
                    object_id=str(_rng.randint(1, 60)),
                    object_repr=f"演示对象 #{i + existing + 1}",
                    action_flag=_rng.choice([1, 1, 2]),
                    change_message=_rng.choice(["已新增", "已修改 name 字段", "已删除"]),
                )
            )
        if rows:
            LogEntry.objects.bulk_create(rows)
            for obj, dt in zip(rows, [rand_dt(0, 90) for _ in rows]):
                obj.action_time = dt
            LogEntry.objects.bulk_update(rows, ["action_time"])
        self._top_up_note("admin.LogEntry", len(rows), existing, target)

        # 会话表
        need, existing = self._need(Session, target)
        rows = [
            Session(
                session_key=uuid.uuid4().hex,
                session_data=fake_session_data(),
                expire_date=timezone.now() + timedelta(days=_rng.randint(1, 30)),
            )
            for _ in range(need)
        ]
        if rows:
            Session.objects.bulk_create(rows)
        self._top_up_note("sessions.Session", len(rows), existing, target)

    # ---------- 汇总 ----------

    def _print_summary(self):
        from django.apps import apps

        rows = []
        for model in apps.get_models():
            if model._meta.proxy or model._meta.abstract:
                continue
            count = model.objects.count()
            if count:
                rows.append((f"{model._meta.app_label}.{model._meta.object_name}", count))
        for label, count in sorted(rows, key=lambda x: -x[1]):
            flag = "" if count >= 50 else "  <-- 不足 50"
            self.stdout.write(f"  {label:<55} {count:>5}{flag}")
        total = sum(c for _, c in rows)
        self.stdout.write(self.style.SUCCESS(f"合计 {len(rows)} 张表 / {total} 行"))
