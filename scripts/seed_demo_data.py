"""为各业务模块生成演示数据（每个模块 20 条主记录）。

用途：本地/演示环境需要可见的数据来检查列表、卡片化表格、状态徽标、分页、
      统计口径与到期提醒，而不必手工录入。

安全约定：
  * 本脚本生成的所有记录都带 `【演示】` 前缀或 `DEMO` 编号段；
  * 重复运行会先删除自己上一次生成的数据（只删带标记的记录），不影响真实数据；
  * 全部写入在一个事务内完成，中途失败不会留下半套数据；
  * 走 ORM 正常 save()，因此会触发 Category 的防环校验、编号自动生成等业务逻辑。

用法：
    python scripts/seed_demo_data.py                # 先清理旧演示数据，再生成
    python scripts/seed_demo_data.py --keep         # 保留旧演示数据，只补缺失的
    python scripts/seed_demo_data.py --purge-only   # 只清理演示数据，不生成
"""

import os
import random
import sys
from datetime import date, datetime, time, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "work_record.settings")

try:  # Windows 控制台默认 GBK，避免中文输出报 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import django  # noqa: E402

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from archive.models import (  # noqa: E402
    AnnualReview,
    Archive,
    BorrowRecord,
    Cabinet,
    CabinetLayer,
    Category,
    Ledger,
    License,
    LicenseBorrowRecord,
)
from knowledge_base.models import KnowledgeCategory, KnowledgeDocument  # noqa: E402
from notifications.models import Notification, NotificationPreference, NotificationTemplate  # noqa: E402
from records.models import (  # noqa: E402
    Complaint,
    CustomerComplaint,
    CustomerCredit,
    DailyRecord,
    Decoration,
    Insurance,
    MetalPrice,
    OperationLog,
    PriceEvaluation,
    Regulation,
)
from samples import serialno  # noqa: E402
from samples.models import AuditRecord, BatchRecord, Partner, Sample, TestRecord  # noqa: E402

User = get_user_model()

TAG = "【演示】"
COUNT = 20
TODAY = timezone.localdate()
NOW = timezone.now()
rnd = random.Random(20261019)  # 固定种子，保证每次生成的数据一致

# ---------------------------------------------------------------------------
# 清理：按依赖顺序（Cabinet / Sample.supplier 外键是 PROTECT，必须先删引用方）
# ---------------------------------------------------------------------------


def purge():
    deleted = {}

    def _del(label, qs):
        n, _ = qs.delete()
        if n:
            deleted[label] = n

    # 档案：借阅/年审 -> 档案 -> 证照 -> 柜/台账/分类
    _del("档案借阅", BorrowRecord.objects.filter(archive__code__startswith="DEMO"))
    _del("证照借阅", LicenseBorrowRecord.objects.filter(license__code__startswith="DEMO"))
    _del("证照年审", AnnualReview.objects.filter(license__code__startswith="DEMO"))
    _del("档案", Archive.objects.filter(code__startswith="DEMO"))
    _del("证照", License.objects.filter(code__startswith="DEMO"))
    _del("档案柜层配置", CabinetLayer.objects.filter(cabinet__code__startswith="DEMO"))
    _del("档案柜", Cabinet.objects.filter(code__startswith="DEMO"))
    _del("档案台账", Ledger.objects.filter(name__startswith=TAG))
    _del("档案分类", Category.objects.filter(name__startswith=TAG))

    # 试样：检测/批次/审厂 -> 样品 -> 供应商
    _del("审厂记录", AuditRecord.objects.filter(audit_no__startswith="DEMO"))
    _del("试样检测", TestRecord.objects.filter(sample__material_name__startswith=TAG))
    _del("小批量", BatchRecord.objects.filter(sample__material_name__startswith=TAG))
    _del("样品", Sample.objects.filter(material_name__startswith=TAG))
    _del("供应商", Partner.objects.filter(partner_code__startswith="DEMO"))

    # 记录类
    _del("每日记录", DailyRecord.objects.filter(work_content__startswith=TAG))
    _del("装修", Decoration.objects.filter(project_name__startswith=TAG))
    _del("客户账期", CustomerCredit.objects.filter(customer_name__startswith=TAG))
    _del("客户客诉", CustomerComplaint.objects.filter(customer_name__startswith=TAG))
    _del("诉讼", Complaint.objects.filter(defendant__startswith=TAG))
    _del("制度", Regulation.objects.filter(doc_no__startswith="DEMO-"))
    _del("核价", PriceEvaluation.objects.filter(project_category__startswith=TAG))
    _del("保险", Insurance.objects.filter(title__startswith=TAG))
    _del("金属价格", MetalPrice.objects.filter(metal_name__startswith=TAG))
    _del("操作日志", OperationLog.objects.filter(change_summary__startswith=TAG))

    # 知识库
    _del("知识文档", KnowledgeDocument.objects.filter(title__startswith=TAG))
    _del("知识分类", KnowledgeCategory.objects.filter(name__startswith=TAG))

    # 通知：迁移种子（notifications/0010）生成的通知标题不带 TAG，按接收者清
    _del("通知(演示接收)", Notification.objects.filter(recipient__username__startswith="demo"))
    _del("通知", Notification.objects.filter(title__startswith=TAG))

    # 通知偏好与演示用户一一对应，先删偏好再删用户（OneToOne 同为 PROTECT）
    demo_users = User.objects.filter(username__startswith="demo")
    _del("通知偏好", NotificationPreference.objects.filter(user__username__startswith="demo"))
    _del("演示用户", demo_users)

    if deleted:
        print("已清理旧演示数据：" + "，".join(f"{k} {v}" for k, v in deleted.items()))
    else:
        print("无可清理的旧演示数据")


# ---------------------------------------------------------------------------
# 各模块 20 条
# ---------------------------------------------------------------------------

DEPARTMENTS = ["生产部", "品质部", "销售部", "技术部", "采购部", "仓储部"]
SALES = ["张伟", "李娜", "王强", "刘敏", "陈静", "赵磊"]
CUSTOMERS = [
    "华远机械制造有限公司",
    "新科电子配件厂",
    "恒信汽车零部件公司",
    "长通精密五金厂",
    "东方电气成套厂",
    "瑞泰家电事业部",
    "中远重工集团",
    "金鹏仪表有限公司",
    "德昌铜业加工厂",
    "嘉信通讯设备公司",
]


def seed_daily_record(admin):
    """每日记录 20 条：16 完成 / 4 未完成，跨最近 20 天。"""
    works = [
        "跟进新品试样进度，整理第 2 轮检测报告并同步客户",
        "核对本月客户账期复审资料，补齐近 12 个月回款记录",
        "现场跟进装修施工，确认吊顶材料进场时间",
        "整理档案室 3 号柜台账，补录 2025 年合同附件",
        "处理客户投诉：表面划伤问题，安排返工与补偿沟通",
        "更新金属价格日报，采集铜铝铅现货价",
        "编制月度品质异常汇总，输出 TOP3 问题对策",
        "核对保险保单到期情况，提交续保申请",
        "修订《供应商考核管理办法》，组织评审",
        "跟进诉讼案件开庭材料，与律师确认证据清单",
        "整理知识库技术文档，补充工艺参数说明",
        "协助完成核价资料收集，比价 3 家供应商",
        "盘点样品库，清理超过 6 个月的失效样品",
        "整理审厂问题整改报告，跟踪关闭情况",
        "统计本周逾期未完成事项，输出提醒清单",
        "对接金蝶系统导入，核对小批量订单单价",
        "编制设备点检表，落实日常巡检责任",
        "复核合同扫描件，补齐签署页缺失文件",
        "整理客户走访记录，输出需求变更点",
        "更新部门工作看板，梳理下周重点事项",
    ]
    created, skipped = 0, 0
    for i, work in enumerate(works):
        day = TODAY - timedelta(days=i)
        done = i % 10 != 4 and i % 10 != 9  # 每 10 条留 2 条未完成
        obj, is_new = DailyRecord.objects.get_or_create(
            user=admin,
            record_date=timezone.make_aware(datetime.combine(day, time(18, 0))),
            defaults={
                "department": DEPARTMENTS[i % len(DEPARTMENTS)],
                "work_content": f"{TAG}{work}",
                "due_date": day + timedelta(days=3),
                "status": "完成" if done else "未完成",
                "progress": 100 if done else rnd.choice([30, 50, 70, 80]),
            },
        )
        created += is_new
        skipped += not is_new
    return created


def seed_decoration():
    projects = [
        ("办公楼三层办公区改造", "宏基建筑工程有限公司", "城南工业园 A 栋 3 层", 486000, 452000),
        ("生产车间地面环氧施工", "恒达地坪工程公司", "城南工业园 A 栋 1 层", 128000, 121500),
        ("样品展示厅装修", "尚品装饰设计公司", "城南工业园 B 栋 1 层", 265000, 248000),
        ("仓库货架区隔断工程", "中立钢结构有限公司", "城北仓储中心 2 号库", 96000, 92000),
        ("质检实验室通风改造", "科净环境工程公司", "城南工业园 A 栋 4 层", 178000, 175000),
        ("员工食堂翻新", "家和装饰工程队", "生活区 1 号楼", 152000, 139000),
        ("会议室声学改造", "声美影音工程公司", "办公楼 2 层", 88000, 86500),
        ("厂区大门及围墙维修", "宏基建筑工程有限公司", "厂区主入口", 64000, 62000),
        ("档案室防潮层施工", "恒达地坪工程公司", "办公楼 1 层档案室", 42000, 40500),
        ("车间照明系统更换", "光明机电安装公司", "生产车间 1-3 层", 215000, 198000),
        ("办公区网络布线改造", "联通智能工程公司", "办公楼全楼层", 76000, 74500),
        ("污水处理池扩容", "清源环保工程公司", "厂区东北角", 385000, 372000),
        ("成品仓地面加固", "恒达地坪工程公司", "城北仓储中心 1 号库", 168000, 159000),
        ("客户接待区景观改造", "绿野园林工程公司", "厂区前广场", 112000, 108000),
        ("宿舍楼卫生间翻新", "家和装饰工程队", "生活区 2 号楼", 196000, 188000),
        ("生产线隔音房搭建", "声美影音工程公司", "生产车间 2 层", 134000, 130000),
        ("消防管道改造", "安泰消防工程公司", "全厂区", 245000, 238000),
        ("办公楼屋面防水", "宏基建筑工程有限公司", "办公楼顶楼", 87000, 83000),
        ("车间更衣室扩建", "中立钢结构有限公司", "生产车间西侧", 145000, 141000),
        ("停车场划线及道闸", "畅行交通设施公司", "厂区停车场", 58000, 56000),
    ]
    for i, (name, ctor, addr, orig, final) in enumerate(projects):
        Decoration.objects.create(
            project_name=f"{TAG}{name}",
            constructor=ctor,
            project_address=addr,
            start_date=TODAY - timedelta(days=30 + i * 9),
            original_price=orig,
            final_price=final,
            reduced_price=orig - final,
        )
    return len(projects)


def seed_customer_credit():
    types = ["普通客户", "代加工客户"]
    works = ["客户催款或复审", "客户账期审核", "提升账期或额度"]
    specifics = ["老客户重审", "提升账期或额度", "新客户账期"]
    surveys = ["有", "无"]
    methods = ["刘总特批", "现场审核", "内部流程"]
    results = ["同意", "不同意", "待定"]
    created = 0
    for i in range(COUNT):
        has_period = i % 3 == 0
        start = TODAY - timedelta(days=120 + i * 3)
        CustomerCredit.objects.create(
            proposal_date=TODAY - timedelta(days=10 + i * 2),
            site_date=TODAY - timedelta(days=8 + i * 2),
            customer_name=f"{TAG}{CUSTOMERS[i % len(CUSTOMERS)]}",
            customer_type=types[i % 2],
            salesperson=SALES[i % len(SALES)],
            work_content=works[i % 3],
            specific_content=specifics[i % 3],
            pre_survey=surveys[i % 2],
            work_method=methods[i % 3],
            result=results[i % 3],
            remarks=f"账期复审第 {i + 1} 单，资料已归档。",
            has_assessment_period=has_period,
            assessment_start_date=start if has_period else None,
            assessment_end_date=(start + timedelta(days=90)) if has_period else None,
            assessment_content="回款及时率、合作年限、订单稳定性" if has_period else "",
        )
        created += 1
    return created


def seed_customer_complaint():
    models = ["MX-220", "MX-350", "HT-110", "HT-180", "KT-500"]
    statuses = ["未完成", "处理中", "已完成"]
    created = 0
    for i in range(COUNT):
        st = statuses[i % 3]
        CustomerComplaint.objects.create(
            customer_name=f"{TAG}{CUSTOMERS[(i + 3) % len(CUSTOMERS)]}",
            responsible_salesperson=SALES[(i + 1) % len(SALES)],
            complaint_date=TODAY - timedelta(days=5 + i * 2),
            terminal_customer=f"{TAG}终端客户 {i + 1:02d}",
            product_model=models[i % len(models)],
            our_shipment=rnd.choice([500, 1000, 2000, 3000]),
            customer_shipment=rnd.choice([0, 20, 50, 100]),
            complaint_description=f"客户反馈第 {i + 1} 批产品存在外观/尺寸偏差，要求给出原因分析与处理方案。",
            compensation_amount=rnd.choice([0, 2000, 5000, 8000, 12000]),
            is_insured=i % 4 == 0,
            engineering_report="已出具 8D 报告初稿。" if st != "未完成" else "",
            engineering_analysis="判定为模具磨损导致尺寸漂移，已安排修模。" if st == "已完成" else "",
            missing_materials="客户原样照片" if i % 5 == 0 else "",
            complaint_status=st,
            follow_up_status="已回访，客户认可处理结果" if st == "已完成" else "待客户确认",
            remarks="",
        )
        created += 1
    return created


def seed_complaint():
    types = ["买卖合同纠纷", "货款追偿", "质量责任纠纷", "劳动争议", "知识产权纠纷"]
    courts = ["市中级人民法院", "区人民法院", "仲裁委员会"]
    lawyers = ["金诚律师事务所 王律师", "正衡律师事务所 李律师", "德恒律师事务所 赵律师"]
    progresses = ["立案", "审理中", "判决", "执行", "结案"]
    created = 0
    # 五态间隔（天）：立案→审理中→判决→执行→结案
    gaps = [40, 55, 38, 25]
    for i in range(COUNT):
        progress = progresses[i % len(progresses)]
        # 五态关键日期：按进度回填「已到达状态」的日期（与推进 action 同口径）。
        # 必须从今天往回推——当前状态 8~33 天前进入、更早状态依次再往前，
        # 否则后续状态的日期会落到未来，详情页「已持续 N 天」会出现负数。
        idx = i % len(progresses)
        state_dates = {}
        cur = TODAY - timedelta(days=8 + (i % 6) * 5)
        state_dates[idx] = cur
        for j in range(idx - 1, -1, -1):
            cur -= timedelta(days=gaps[j] + (i % 4) * 3)
            state_dates[j] = cur
        dates = {}
        if 0 in state_dates:
            dates["filed_at"] = state_dates[0]
        if 1 in state_dates:
            dates["hearing_at"] = state_dates[1]
        if 2 in state_dates:
            dates["judgment_at"] = state_dates[2]
        if 3 in state_dates:
            dates["execution_at"] = state_dates[3]
        if 4 in state_dates:
            dates["closed_at"] = state_dates[4]
        Complaint.objects.create(
            dispute_type=types[i % len(types)],
            defendant=f"{TAG}被告方 {i + 1:02d}",
            case_number=f"(2026)示民初{1000 + i}号",
            amount=rnd.choice([50000, 120000, 280000, 460000, 830000]),
            court=courts[i % len(courts)],
            lawyer=lawyers[i % len(lawyers)],
            progress=progress,
            detailed_progress=f"第 {i + 1} 案已进入{progress}阶段，材料齐全。",
            remarks="",
            **dates,
        )
        created += 1
    return created


def seed_regulation():
    items = [
        ("供应商准入与考核管理办法", "供应商准入条件、年度考核评分与退出机制。"),
        ("客户账期审批流程规定", "明确账期申请、复审、额度调整的审批层级与时限。"),
        ("产品质量异常处理规范", "从异常发现到 8D 闭环的处理流程与责任分工。"),
        ("安全生产责任制实施细则", "各岗位安全职责、检查频次与考核办法。"),
        ("档案借阅与保密管理规定", "档案借阅审批、归还时限与密级管理要求。"),
        ("样品管理规定", "样品登记、检测、留样与报废的全流程要求。"),
        ("设备点检与维护制度", "设备日常点检项目、周期与异常上报流程。"),
        ("考勤与请假管理办法", "打卡规则、请假审批权限与异常处理。"),
        ("差旅费用报销标准", "各职级出差住宿、交通与补贴标准。"),
        ("合同评审管理办法", "合同签订前的法务、财务、技术三方评审要求。"),
        ("危化品仓储管理规范", "危化品分区存放、台账登记与应急处置。"),
        ("消防设施巡检制度", "消防通道、器材、报警系统的巡检周期。"),
        ("员工培训管理办法", "新员工入职培训、在岗技能培训与考核。"),
        ("绩效考核实施细则", "月度/季度考核指标、评分与结果应用。"),
        ("信息安全与数据备份规定", "账号权限、数据分级与备份恢复要求。"),
        ("采购比价管理规定", "三家比价原则、内控审核价与例外审批。"),
        ("成品出入库管理规范", "出入库单据、盘点与账实核对要求。"),
        ("客户投诉响应时效规定", "24 小时响应、72 小时初步方案的时效要求。"),
        ("知识产权管理办法", "专利、商标申报与保密要求。"),
        ("环境与职业健康管理规范", "三废排放、职业健康体检与防护用品管理。"),
    ]
    for i, (title, content) in enumerate(items):
        Regulation.objects.create(
            issue_date=TODAY - timedelta(days=400 - i * 15),
            title=f"{TAG}{title}",
            content=content + f"（示范文本第 {i + 1} 版，用于演示制度列表与详情页。）",
            doc_no=f"DEMO-QMS-{i + 1:03d}",
        )
    return len(items)


def seed_price_evaluation():
    categories = ["原材料采购", "外协加工", "设备采购", "物流运输", "包装材料"]
    depts = ["采购部", "技术部", "生产部", "品质部"]
    created = 0
    for i in range(COUNT):
        base = rnd.choice([12000, 28000, 45000, 68000, 96000])
        PriceEvaluation.objects.create(
            project_category=f"{TAG}{categories[i % len(categories)]}",
            proposing_department=depts[i % len(depts)],
            proposer=SALES[i % len(SALES)],
            evaluation_content=f"针对第 {i + 1} 批采购需求，完成三家供应商比价与内控价核定。",
            supplier1_name=f"{TAG}供应商甲{i + 1:02d}",
            supplier1_price=base,
            supplier2_name=f"{TAG}供应商乙{i + 1:02d}",
            supplier2_price=round(base * 1.06, 2),
            supplier3_name=f"{TAG}供应商丙{i + 1:02d}",
            supplier3_price=round(base * 0.98, 2),
            internal_control_supplier=f"{TAG}内控供应商{i + 1:02d}",
            internal_control_price=round(base * 0.96, 2),
        )
        created += 1
    return created


def seed_insurance():
    companies = ["中国人保财险", "平安产险", "太平洋产险", "大地保险"]
    kinds = ["财产综合险", "机器损坏险", "雇主责任险", "产品责任险", "货物运输险"]
    created = 0
    for i in range(COUNT):
        # 5 条已过期、5 条 30 天内到期、10 条正常 —— 覆盖列表的三态展示
        if i < 5:
            end = TODAY - timedelta(days=10 + i * 12)
        elif i < 10:
            end = TODAY + timedelta(days=2 + (i - 5) * 6)  # 2/8/14/20/26 天，均 < 30
        else:
            end = TODAY + timedelta(days=120 + (i - 10) * 25)
        issue = end - timedelta(days=365)
        Insurance.objects.create(
            company_name=companies[i % len(companies)],
            title=f"{TAG}{kinds[i % len(kinds)]}（第 {i + 1} 期）",
            order_number=f"DEMO-POL-{2026}{i + 1:04d}",
            premium=rnd.choice([8600, 12000, 26500, 48000, 88000]),
            issue_date=issue,
            end_date=end,
            department=DEPARTMENTS[i % len(DEPARTMENTS)],
            remarks="演示数据，用于验证到期提醒与状态徽标。",
        )
        created += 1
    return created


def seed_metal_price():
    """5 种金属 × 最近 4 天 = 20 条，供价格走势使用。

    注意：MetalPrice.crawl_date / crawl_time 都是 auto_now_add，直接赋值会被
    忽略（全部落成创建时刻）。必须 create 之后用 update() 回写，update 不触发
    auto_now_add，才能造出历史日期。
    """
    metals = [
        ("电解铜", "元/吨", 78500),
        ("铝锭", "元/吨", 20800),
        ("铅锭", "元/吨", 17200),
        ("锌锭", "元/吨", 24100),
        ("螺纹钢", "元/吨", 3980),
    ]
    created = 0
    for d in range(4):
        day = TODAY - timedelta(days=d)
        for name, unit, base in metals:
            label = f"{TAG}{name}"
            # --keep 模式下也能幂等：先删同一天同一金属的演示记录
            MetalPrice.objects.filter(metal_name=label, crawl_date=day).delete()
            obj = MetalPrice.objects.create(
                metal_name=label,
                price=base + rnd.randint(-300, 300),
                unit=unit,
                price_change=rnd.randint(-150, 150),
            )
            MetalPrice.objects.filter(pk=obj.pk).update(
                crawl_date=day,
                crawl_time=timezone.make_aware(datetime.combine(day, time(9, 30))),
            )
            created += 1
    return created


def seed_knowledge():
    cats = [
        ("工艺技术", "材料工艺、参数与作业指导"),
        ("质量体系", "体系文件、审核与整改"),
        ("销售支持", "报价、合同与客户资料"),
        ("设备维护", "设备手册与维保记录"),
        ("安全环保", "安全规程与环保要求"),
    ]
    roots = []
    for i, (name, desc) in enumerate(cats):
        obj = KnowledgeCategory.objects.create(
            name=f"{TAG}{name}", description=desc, sort_order=i
        )
        roots.append(obj)

    # 「Django 学习」系列：父子分类演示文件夹系统（同一主题的多篇教程归拢在一棵树下）
    django_root = KnowledgeCategory.objects.create(
        name=f"{TAG}Django 学习", description="从入门到部署的系列教程", sort_order=90
    )
    django_children = []
    for j, (cname, cdesc) in enumerate([
        ("Django 基础", "项目结构、路由与视图"),
        ("Django 数据模型", "ORM、查询与迁移"),
        ("Django 部署运维", "生产配置、静态文件与监控"),
    ]):
        django_children.append(KnowledgeCategory.objects.create(
            name=f"{TAG}{cname}", description=cdesc, sort_order=j, parent=django_root
        ))

    titles = [
        ("铜合金挤压工艺参数速查表", 0, "汇总常用牌号的挤压温度、速度与模具间隙。"),
        ("表面处理常见缺陷图谱", 0, "划伤、氧化、色差等缺陷的成因与对策。"),
        ("模具更换标准作业流程", 0, "换模前准备、换模步骤与首件确认。"),
        ("尺寸公差判定指引", 0, "关键尺寸的公差带与测量方法。"),
        ("包装作业指导书", 0, "内包装、外箱与标签规范。"),
        ("ISO9001 内审检查表", 1, "按条款拆解的内审提问清单。"),
        ("8D 报告编写模板", 1, "问题描述、根因、纠正措施的填写示例。"),
        ("不合格品处置流程", 1, "标识、隔离、评审与处置方式。"),
        ("客户投诉响应话术", 1, "首次响应、过程沟通与结案回访用语。"),
        ("供应商年度考核表", 1, "质量、交期、价格、服务的评分细则。"),
        ("报价审批权限说明", 2, "不同金额区间的审批层级。"),
        ("合同评审要点清单", 2, "法务、财务、技术三方评审关注点。"),
        ("客户信用等级评定办法", 2, "评级维度与对应账期额度。"),
        ("样品送样与留样规范", 2, "送样数量、留样期限与登记要求。"),
        ("关键设备点检表", 3, "日常点检项目与判定标准。"),
        ("设备故障应急处置", 3, "常见故障的排查顺序与临时措施。"),
        ("备件库存管理规则", 3, "安全库存、领用与补货触发条件。"),
        ("危险源识别清单", 4, "各岗位危险源与防护措施。"),
        ("危废转移申报流程", 4, "申报、联单与台账要求。"),
        ("消防演练组织方案", 4, "演练计划、分工与评估要点。"),
    ]
    for i, (title, cat_idx, summary) in enumerate(titles):
        KnowledgeDocument.objects.create(
            title=f"{TAG}{title}",
            category=roots[cat_idx],
            content=(
                f"一、适用范围\n{summary}\n\n"
                f"二、操作要点\n1. 按作业指导书执行，首件必检；\n"
                f"2. 异常立即停线并上报；\n3. 记录留档不少于 3 年。\n\n"
                f"三、相关记录\n《作业记录表》《异常处理单》"
            ),
            summary=summary,
            tags="演示,规范,流程",
            status="published" if i % 4 != 3 else "draft",
            visibility="public",  # 演示数据等同存量文档口径：公开可见
            view_count=rnd.randint(3, 260),
        )

    # Django 系列教程：内容用标准 Markdown 写（标题/任务列表/引用/代码/表格/标签），
    # 用于演示详情页的阅读排版、大纲与思维导图三种视图
    django_docs = [
        ("Django 教程 01：项目起手式", 0,
         "创建虚拟环境、安装 Django、跑通第一个项目。",
         """# 环境准备

- [x] 安装 Python 3.10+ 并确认 `python --version`
- [x] 创建虚拟环境 `.venv` 并激活
- [ ] 安装 Django：`pip install django`
- [ ] 执行 `django-admin startproject config .`

## 目录结构

> `manage.py` 是命令行入口，`config/` 是项目配置包，应用级的业务代码另建 app。

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `runserver` | 启动开发服务器 |
| `makemigrations` | 生成迁移文件 |
| `migrate` | 应用迁移到数据库 |

#重点 #入门 #环境"""),
        ("Django 教程 02：URL 与视图", 0,
         "URLconf 路由分发、函数视图与类视图的写法。",
         """# 路由基础

1. `urls.py` 用 `path()` 把 URL 模式映射到视图；
2. 模式中的 `<int:pk>` 是路径转换器，捕获后作为视图参数；
3. 用 `name=` 给路由命名，模板与代码里一律反解路由名，避免硬编码路径。

## 视图两种写法

- 函数视图（FBV）：简单直接，适合逻辑短的页面；
- 类视图（CBV）：继承 `ListView` / `DetailView`，适合增删改查成套的场景。

> 约定：详情/编辑/删除走同一组 CBV，列表与创建用 FBV，全项目保持一种味道。

#重点 #路由 #视图"""),
        ("Django 教程 03：模型与迁移", 1,
         "模型字段、关系与迁移的生命周期管理。",
         """# 模型即 schema

- 每个模型类对应一张表，字段即列；
- `ForeignKey` 表达多对一，`ManyToManyField` 表达多对多；
- 迁移是模型的版本快照：`makemigrations` 生成、`migrate` 应用。

## 变更字段的安全顺序

1. 新字段先给默认值或 `null=True`；
2. 部署迁移，再部署使用该字段的代码；
3. 确认无旧代码引用后，再去掉默认值。

> 生产库改字段前先 `sqlmigrate` 看生成的 SQL，大表加索引走 `CONCURRENTLY`。

#重点 #模型 #迁移"""),
        ("Django 教程 04：查询优化实务", 1,
         "N+1 问题、select_related 与 only/defer 的取舍。",
         """# 先测量再优化

- [x] 用 `django-debug-toolbar` 或 `CaptureQueriesContext` 数 SQL 条数
- [ ] 对列表页外键循环访问上 `select_related`
- [ ] 对多对多循环访问上 `prefetch_related`
- [ ] 详情页大字段用 `only()` 收敛 SELECT 列

## 常见误区

> `select_related` 不是越多越好： join 太深比多发几条 SQL 更慢。

#重点 #性能 #ORM"""),
        ("Django 教程 05：生产部署清单", 2,
         "DEBUG 关闭、静态文件、日志与备份的落地配置。",
         """# 上线前检查单

- [x] `DEBUG=False` 且 `ALLOWED_HOSTS` 显式列主机
- [x] `SECRET_KEY` 从环境变量注入，绝不进仓库
- [ ] 静态文件用 `collectstatic` + 反向代理提供
- [ ] 日志按天切割，ERROR 级接告警
- [ ] 数据库定时冷备并验证可恢复

## 监控三件事

1. 进程存活（systemd restart 策略）；
2. 磁盘与备份成功率；
3. 慢查询与 5xx 比例。

#重点 #部署 #运维"""),
        ("Django 教程 06：常见坑速查", 2,
         "时区、迁移冲突与模板继承的高频问题。",
         """# 时区

> `USE_TZ=True` 下入库是 UTC，展示层才转本地时间；直接 `datetime.now()` 会引入 naive 时间，埋下跨时区坑。

# 迁移冲突

两人各自生成了同号迁移时，用 `--merge` 合并，不要手动删对方的迁移文件。

# 模板继承

子模板覆盖父模板的 `block` 是整块替换——样式块被覆盖时，父级对应规则会整体消失。

#重点 #排错 #经验"""),
    ]
    for i, (title, child_idx, summary, content) in enumerate(django_docs):
        KnowledgeDocument.objects.create(
            title=f"{TAG}{title}",
            category=django_children[child_idx],
            content=content,
            summary=summary,
            tags="Django,教程",
            status="published",
            visibility="public",
            view_count=rnd.randint(30, 400),
        )
    return len(titles) + len(django_docs)


def seed_samples():
    """供应商 8 家、样品主档 20 条，并配套检测/小批量/审厂记录。"""
    partners = []
    names = [
        "鑫源金属材料有限公司",
        "广达铜业有限公司",
        "精诚模具制造厂",
        "华美表面处理厂",
        "东盛包装材料公司",
        "联创电子元件厂",
        "宏发五金配件厂",
        "远洋物流服务公司",
    ]
    for i, name in enumerate(names):
        partners.append(
            Partner.objects.create(
                partner_code=f"DEMO-P{i + 1:03d}",
                name=f"{TAG}{name}",
                contact_person=SALES[i % len(SALES)],
                contact_info=f"1380000{i:04d}",
                status="active" if i % 4 != 3 else "inactive",
            )
        )

    materials = [
        ("H62 黄铜带", "0.8×200mm"),
        ("T2 紫铜板", "2.0×600mm"),
        ("6061 铝型材", "40×40mm"),
        ("H65 黄铜线", "φ3.0mm"),
        ("C3604 易切削铜棒", "φ12mm"),
        ("304 不锈钢带", "0.5×300mm"),
        ("PA66 工程塑料", "本色颗粒"),
        ("硅胶密封圈", "φ25×3mm"),
    ]
    statuses = ["pending", "testing", "small_batch", "imported", "terminated"]
    samples = []
    for i in range(COUNT):
        st = statuses[i % len(statuses)]
        sdate = TODAY - timedelta(days=60 - i * 2)
        mat, spec = materials[i % len(materials)]
        sample = Sample.objects.create(
            sample_no=serialno.next_daily_serial(Sample, "sample_no", "S", sdate),
            material_name=f"{TAG}{mat}",
            model=f"MX-{200 + i:03d}",
            sample_date=sdate,
            status=st,
            registrar=SALES[i % len(SALES)],
            spec=spec,
            incoming_qty=f"{rnd.choice([2, 5, 10, 20])} 件",
            supplier=partners[i % len(partners)],
            supplier_type="new" if i % 3 == 0 else "existing",
            terminate_reason="客户项目取消，样品退回。" if st == "terminated" else "",
            terminated_by=SALES[i % len(SALES)] if st == "terminated" else "",
            terminated_at=timezone.make_aware(datetime.combine(sdate + timedelta(days=20), time(15, 0)))
            if st == "terminated"
            else None,
            imported_by="王主管" if st == "imported" else "",
            imported_at=timezone.make_aware(datetime.combine(sdate + timedelta(days=25), time(10, 0)))
            if st == "imported"
            else None,
            is_deleted=False,
        )
        samples.append(sample)

        # 检测记录：前 12 条样品各 1 轮
        if i < 12:
            conclusion = rnd.choice(["pass", "pass", "improve", "fail"])
            rd = sdate + timedelta(days=5)
            TestRecord.objects.create(
                test_no=serialno.next_daily_serial(TestRecord, "test_no", "T", rd),
                sample=sample,
                round_no=1,
                test_date=rd,
                test_result="抗拉强度、延伸率、硬度均在标准范围内。" if conclusion == "pass" else "延伸率偏低，建议调整退火工艺。",
                review_conclusion=conclusion,
                report_code=f"DEMO-RPT-{i + 1:03d}",
                notified=conclusion == "pass",
                operator=SALES[i % len(SALES)],
                operated_at=timezone.make_aware(datetime.combine(rd, time(16, 0))),
                package_type=rnd.choice(["纸箱", "木箱", "托盘"]),
                incoming_date=sdate,
                remark="",
            )

        # 小批量：进入 small_batch 及之后状态的样品
        if st in ("small_batch", "imported"):
            bd = sdate + timedelta(days=18)
            BatchRecord.objects.create(
                batch_no=serialno.next_daily_serial(BatchRecord, "batch_no", "B", bd),
                sample=sample,
                round_no=1,
                ordered=True,
                batch_result="pass" if st == "imported" else "ongoing",
                kingdee_entered=st == "imported",
                operator=SALES[i % len(SALES)],
                operated_at=timezone.make_aware(datetime.combine(bd, time(14, 0))),
                unit_price=rnd.choice([12.5, 18.0, 26.8, 45.0]),
                alternative_model="",
                remark="",
            )

    # 审厂记录：前 6 家供应商各 1 轮
    for i, partner in enumerate(partners[:6]):
        ad = TODAY - timedelta(days=40 - i * 3)
        AuditRecord.objects.create(
            audit_no=f"DEMO-AUD-{i + 1:03d}",
            partner=partner,
            round_no=1,
            audit_date=ad,
            audit_result=rnd.choice(["pass", "pass", "conditional", "fail"]),
            audit_remark="现场审核完成，问题点已列入整改计划。" if i % 3 else "部分工序文件缺失，限期整改。",
            auditor=SALES[(i + 2) % len(SALES)],
            operated_at=timezone.make_aware(datetime.combine(ad, time(17, 0))),
            file="audits/demo_placeholder.pdf",
        )
    return len(samples)


def seed_archive(admin):
    """档案：分类树 + 台账 + 档案柜 + 20 条档案 + 20 条证照 + 借阅/年审。"""
    # 分类树（两级）
    tree = {
        "客户档案": ["合同协议", "技术协议", "往来函件"],
        "供应商档案": ["准入资料", "考核记录"],
        "质量记录": ["检测报告", "不合格处置"],
        "行政文书": ["制度文件", "会议纪要"],
        "财务凭证": ["发票", "对账单"],
    }
    cat_leaves = []
    for i, (root_name, children) in enumerate(tree.items()):
        root = Category.objects.create(name=f"{TAG}{root_name}", sort_order=i)
        for j, child in enumerate(children):
            cat_leaves.append(
                Category.objects.create(name=f"{TAG}{child}", parent=root, sort_order=j)
            )

    # 台账
    ledgers = []
    for i, name in enumerate(["2024 年度台账", "2025 年度台账", "2026 年度台账"]):
        ledgers.append(
            Ledger.objects.create(name=f"{TAG}{name}", description="演示用台账", sort_order=i)
        )

    # 档案柜：2 柜 × 5 层 × 10 格
    cabinets = []
    for i in range(2):
        cab = Cabinet.objects.create(
            code=f"DEMO-C{i + 1:02d}",
            location=f"档案室 {'AB'[i]} 区",
            layers=5,
        )
        for layer_no in range(1, 6):
            CabinetLayer.objects.create(cabinet=cab, layer_no=layer_no, cells=10)
        cabinets.append(cab)

    # 20 条档案
    names = [
        "华远机械年度框架合同", "新科电子采购协议", "恒信汽车技术协议",
        "长通精密供货合同", "东方电气成套合同", "瑞泰家电年度协议",
        "中远重工采购合同", "金鹏仪表技术协议", "德昌铜业供货合同",
        "嘉信通讯框架合同", "鑫源金属准入资料", "广达铜业考核记录",
        "精诚模具准入资料", "华美表面处理考核记录", "东盛包装准入资料",
        "2026 年 3 月检测报告", "2026 年 4 月检测报告", "不合格品处置单 2026-05",
        "总经理办公会纪要 2026-06", "2026 年 6 月对账单",
    ]
    for i, name in enumerate(names):
        # 格位分配必须满足 (cabinet, layer, cell) 唯一约束：
        # 前 10 条进 1 号柜、后 10 条进 2 号柜，层按 1-5 循环、格按每 5 条进一格
        cab = cabinets[i // 10]
        layer = i % 5 + 1
        cell = i // 5 + 1
        # 合同类档案设一个到期日，用于"档案合同临近到期"提醒
        expire = TODAY + timedelta(days=rnd.choice([-30, 15, 25, 200, 400]))
        Archive.objects.create(
            code=f"DEMO-{2026}{i + 1:03d}",
            name=f"{TAG}{name}",
            cabinet=cab,
            layer=layer,
            cell=cell,
            location_code=f"{cab.code}-{layer:02d}-{cell:02d}",
            ledger=ledgers[i % len(ledgers)],
            source_sheet=f"2026 年移交清单第 {i + 1} 项",
            category=cat_leaves[i % len(cat_leaves)],
            tags="合同,年度" if i < 10 else "记录,质量",
            description=f"演示档案，位于 {cab.location}，可用于检查格位定位与检索。",
            counterpart=CUSTOMERS[i % len(CUSTOMERS)],
            contract_no=f"DEMO-HT-{2026}{i + 1:03d}",
            amount=str(rnd.choice([120000, 350000, 680000, 1250000])),
            sign_date=TODAY - timedelta(days=200 + i * 5),
            expire_date=expire,
            department=DEPARTMENTS[i % len(DEPARTMENTS)],
            security_level=rnd.choice(["公开", "内部", "秘密"]),
            archive_label=rnd.choice(["合同类", "技术类", "财务类"]),
            handler=SALES[i % len(SALES)],
            copies=rnd.choice([1, 2, 3]),
            attachment_desc="正本 1 份、附件 3 页",
            creator=admin,
        )

    # 20 条证照：5 过期 / 5 临期 / 10 正常
    licenses_data = [
        ("营业执照", "统一社会信用代码证"), ("ISO9001 质量体系认证", "体系认证证书"),
        ("ISO14001 环境体系认证", "体系认证证书"), ("安全生产许可证", "行政许可"),
        ("排污许可证", "行政许可"), ("特种设备使用登记证", "设备类"),
        ("计量器具检定证书", "检定类"), ("高新技术企业证书", "资质类"),
        ("进出口经营权备案", "备案类"), ("道路运输经营许可证", "行政许可"),
    ]
    licenses = []
    for i in range(COUNT):
        lname, issuer_kind = licenses_data[i % len(licenses_data)]
        if i < 5:
            expire = TODAY - timedelta(days=15 + i * 20)
        elif i < 10:
            expire = TODAY + timedelta(days=3 + i * 3)  # 18/21/24/27/30 天，均 <= 30
        else:
            expire = TODAY + timedelta(days=180 + (i - 10) * 30)
        cycle = rnd.choice([12, 12, 24, 36])
        # 下次年审日不能无脑取 expire-30：License.status 把"年审日已过"也算过期，
        # 那样临期组会被判成过期。这里让年审日与目标状态一致：
        #   过期组 -> 年审日已过；临期组 -> 年审日与到期日同日；正常组 -> 远期。
        if i % 3 == 2:
            next_review = None  # 部分证照无年审要求
        elif i < 10:
            next_review = expire
        else:
            next_review = expire - timedelta(days=30)
        lic = License.objects.create(
            name=f"{TAG}{lname}（{i + 1:02d}）",
            code=f"DEMO-LIC-{i + 1:03d}",
            holder="本公司",
            issuer=rnd.choice(["市场监督管理局", "认证中心", "应急管理局", "生态环境局"]),
            issue_date=expire - timedelta(days=cycle * 30),
            expire_date=expire,
            review_cycle=cycle,
            next_review_date=next_review,
            remark=f"{issuer_kind}，演示数据。",
            creator=admin,
        )
        licenses.append(lic)

    # 年审记录：前 8 张证照各 1 条（含通过/不通过/待审）
    for i, lic in enumerate(licenses[:8]):
        result = ["passed", "passed", "failed", "pending"][i % 4]
        AnnualReview.objects.create(
            license=lic,
            review_date=TODAY - timedelta(days=60 - i * 5),
            result=result,
            reviewer=SALES[i % len(SALES)],
            new_expire_date=(lic.expire_date + timedelta(days=365)) if result == "passed" and i % 2 == 0 else None,
            remark="年审材料齐全，予以通过。" if result == "passed" else "待补充材料。",
        )

    # 证照借阅：6 条（3 借出中、3 已归还）
    for i in range(6):
        lic = licenses[i]
        borrow = timezone.make_aware(
            datetime.combine(TODAY - timedelta(days=10 + i * 3), time(10, 0))
        )
        returned = i >= 3
        LicenseBorrowRecord.objects.create(
            license=lic,
            borrower=SALES[i % len(SALES)],
            borrow_time=borrow,
            due_time=(TODAY - timedelta(days=10 + i * 3) + timedelta(days=7)),
            return_time=(borrow + timedelta(days=3)) if returned else None,
            status="returned" if returned else "borrowed",
            remark="投标使用" if i % 2 == 0 else "资质审查",
        )

    # 档案借阅：6 条（借出中 / 已归还 / 已逾期）
    arcs = list(Archive.objects.filter(code__startswith="DEMO"))
    for i, arc in enumerate(arcs[:6]):
        borrow_day = TODAY - timedelta(days=7 + i * 5)
        borrow = timezone.make_aware(datetime.combine(borrow_day, time(9, 30)))
        if i < 2:
            status, ret = "borrowed", None
        elif i < 4:
            status, ret = "returned", borrow + timedelta(days=4)
        else:
            status, ret = "overdue", None
        BorrowRecord.objects.create(
            archive=arc,
            borrower=SALES[i % len(SALES)],
            borrow_time=borrow,
            due_time=borrow_day + timedelta(days=5),
            return_time=ret,
            status=status,
            remark="复印后归还" if status != "borrowed" else "",
        )
    return 20


def seed_notifications(admin):
    templates = list(NotificationTemplate.objects.all())
    tpl_by_code = {t.code: t for t in templates}
    picks = [
        ("license_expiry", "有 1 张证照将在 15 天内到期", "business"),
        ("borrow_due", "借阅档案《华远机械年度框架合同》即将到期", "business"),
        ("insurance_expiry", "财产综合险将于 10 天后到期", "business"),
        ("archive_expiry", "档案合同《新科电子采购协议》临近到期", "business"),
        ("daily_record_incomplete", "你有 3 条每日记录逾期未完成", "task"),
        ("daily_record_missing", "今日尚未填写每日记录", "task"),
        ("sample_audit_result", "供应商审厂结果为「有条件通过」", "business"),
        ("system_announcement", "关于 10 月消防演练的通知", "announcement"),
    ]
    created = 0
    for i in range(COUNT):
        code, title, category = picks[i % len(picks)]
        tpl = tpl_by_code.get(code) or templates[0]
        is_read = i % 3 != 0
        Notification.objects.create(
            recipient=admin,
            template=tpl,
            created_by=admin if category == "announcement" else None,
            title=f"{TAG}{title}",
            content=f"这是第 {i + 1} 条演示通知正文，用于检查通知中心的未读标记、分类筛选与优先级展示。",
            category=category,
            status="revoked" if i in (7, 15) else "sent",
            is_read=is_read,
            read_at=NOW - timedelta(hours=i) if is_read else None,
            priority="high" if i % 5 == 0 else "normal",
            dedup_key=f"demo-notify-{i + 1:03d}",
            link="",
            sent_at=NOW - timedelta(hours=i * 3),
        )
        created += 1

    NotificationPreference.objects.get_or_create(user=admin)
    return created


def seed_accounts():
    """20 个演示账号（staff 角色），用于检查账号列表、分页与角色分配。"""
    from accounts.models import Role, UserProfile

    staff = Role.objects.filter(code="staff").first()
    names = [
        "张伟", "李娜", "王强", "刘敏", "陈静", "赵磊", "孙倩", "周涛",
        "吴丹", "郑凯", "冯雪", "徐亮", "马丽", "何军", "高云", "林峰",
        "罗静", "梁辉", "宋佳", "唐勇",
    ]
    depts = ["生产部", "品质部", "销售部", "技术部", "采购部", "仓储部", "财务部", "行政部"]
    created = 0
    for i, real_name in enumerate(names):
        username = f"demo{i + 1:02d}"
        user, is_new = User.objects.get_or_create(
            username=username,
            defaults={"is_active": True, "email": f"{username}@example.com"},
        )
        if is_new:
            user.set_password("Demo@2026")
            user.save()
            created += 1
        UserProfile.objects.get_or_create(
            user=user,
            defaults={
                "real_name": f"{TAG}{real_name}",
                "department": depts[i % len(depts)],
                "position": rnd.choice(["专员", "主管", "工程师", "业务员"]),
            },
        )
        if staff:
            staff.users.add(user)
    return created or 20


def seed_operation_logs(admin):
    """操作日志 20 条 —— 同时支撑「数据展示/下载」与「系统管理」两个模块。"""
    modules = ["每日记录", "装修", "客户账期", "客户客诉", "档案", "样品", "证照"]
    actions = ["新增", "编辑", "删除", "导出"]
    created = 0
    for i in range(COUNT):
        OperationLog.objects.create(
            operation_time=NOW - timedelta(hours=i * 6),
            operator=admin.username,
            module_type=modules[i % len(modules)],
            operation_type=actions[i % len(actions)],
            object_identifier=f"DEMO-{i + 1:03d}",
            change_summary=f"{TAG}演示操作日志第 {i + 1} 条。",
        )
        created += 1
    return created


def main():
    keep = "--keep" in sys.argv
    admin = User.objects.filter(is_superuser=True).order_by("id").first()
    if not admin:
        print("未找到超级用户，请先创建账号。")
        return 1

    # 只清理不生成：便于验收结束后还原成干净环境
    if "--purge-only" in sys.argv:
        with transaction.atomic():
            purge()
        return 0

    print(f"操作人：{admin.username}    日期基准：{TODAY}\n")

    with transaction.atomic():
        if not keep:
            purge()

        stats = [
            ("每日记录", seed_daily_record(admin)),
            ("装修记录", seed_decoration()),
            ("客户账期", seed_customer_credit()),
            ("客户客诉", seed_customer_complaint()),
            ("诉讼记录", seed_complaint()),
            ("制度及处罚", seed_regulation()),
            ("核价", seed_price_evaluation()),
            ("保险", seed_insurance()),
            ("金属价格", seed_metal_price()),
            ("知识库文档", seed_knowledge()),
            ("试样（样品主档）", seed_samples()),
            ("档案", seed_archive(admin)),
            ("消息通知", seed_notifications(admin)),
            ("演示账号", seed_accounts()),
            ("操作日志", seed_operation_logs(admin)),
        ]

    print("生成结果：")
    for name, n in stats:
        print(f"  {name:<16} {n:>3} 条")

    print("\n各表当前总量：")
    checks = [
        ("每日记录", DailyRecord), ("装修", Decoration), ("客户账期", CustomerCredit),
        ("客户客诉", CustomerComplaint), ("诉讼", Complaint), ("制度", Regulation),
        ("核价", PriceEvaluation), ("保险", Insurance), ("金属价格", MetalPrice),
        ("操作日志", OperationLog), ("知识文档", KnowledgeDocument),
        ("供应商", Partner), ("样品", Sample), ("检测记录", TestRecord),
        ("小批量", BatchRecord), ("审厂记录", AuditRecord),
        ("档案分类", Category), ("档案", Archive), ("证照", License),
        ("档案借阅", BorrowRecord), ("证照借阅", LicenseBorrowRecord),
        ("通知", Notification), ("用户", User),
    ]
    for label, model in checks:
        print(f"  {label:<10} {model.objects.count():>4}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
