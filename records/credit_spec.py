"""客户评审评分规则 —— 分值标准与财务指标口径的唯一事实源。

对应《客户经销商准入、信用评价及定期评估管理制度》三张附件表单：

- 客户信用评价表（100 分，5 个维度 25 个评分项）
- 客户定期考评表（100 分，4 个维度 14 个评分项）
- 客户现场审核表（不计分，7 个审核项，判定「符合 / 待整改 / 不符合 / 不适用」）

为什么规则放在 Python 而不是模板或前端：

1) 视图保存时要按同一套规则重算总分，不能信任前端提交的合计值；
2) 打印页、详情页、导出都要能复现「每一项为什么是这个分」；
3) 分档阈值调整时只改这一处，模板只负责渲染。

分档口径遵循三条硬约束（原表未写明，此处在不改变原分值的前提下补齐）：
- 档位穷尽、首尾相接：每个评分项的档位分值加总等于原表标准分值，区间不重叠、不留空档；
- 就低认定：同一事实同时满足两档时取低档；无有效凭证的按最低档计；
- 一票否决单列：命中制裁名单、失信被执行等否决项直接 0 分，不被其他高分抵消。
"""
from datetime import date, timedelta

# ---------------------------------------------------------------------------
# 财务基础数据：录入项
# ---------------------------------------------------------------------------
# 单位统一为万元。字段名同时用于模型字段、表单字段与比率计算。
FIN_FIELDS = [
    {"key": "total_assets", "label": "资产总额", "unit": "万元", "group": "资产负债", "required": True,
     "help": "最近一期资产负债表「资产总计」"},
    {"key": "total_liabilities", "label": "负债总额", "unit": "万元", "group": "资产负债", "required": True,
     "help": "最近一期资产负债表「负债合计」"},
    {"key": "current_assets", "label": "流动资产", "unit": "万元", "group": "资产负债", "required": True,
     "help": "货币资金 + 应收 + 存货 + 其他流动资产"},
    {"key": "current_liabilities", "label": "流动负债", "unit": "万元", "group": "资产负债", "required": True,
     "help": "短期借款 + 应付账款 + 一年内到期非流动负债等"},
    {"key": "inventory", "label": "存货", "unit": "万元", "group": "资产负债", "required": False,
     "help": "计算速动比率用，缺省按 0 计"},
    {"key": "accounts_receivable", "label": "应收账款", "unit": "万元", "group": "资产负债", "required": False,
     "help": "计算应收账款周转率用"},
    {"key": "cash", "label": "货币资金", "unit": "万元", "group": "资产负债", "required": False,
     "help": "库存现金 + 银行存款 + 其他货币资金"},
    {"key": "interest_debt", "label": "有息负债", "unit": "万元", "group": "资产负债", "required": False,
     "help": "短期借款 + 长期借款 + 应付债券 + 融资租赁"},
    {"key": "revenue", "label": "营业收入（本期）", "unit": "万元", "group": "损益", "required": True,
     "help": "最近一个完整会计年度利润表"},
    {"key": "prev_revenue", "label": "营业收入（上期）", "unit": "万元", "group": "损益", "required": False,
     "help": "用于计算营业收入增长率"},
    {"key": "net_profit", "label": "净利润", "unit": "万元", "group": "损益", "required": False,
     "help": "利润表「净利润」，亏损填负数"},
    {"key": "operating_cash_flow", "label": "经营活动现金流量净额", "unit": "万元", "group": "现金流", "required": False,
     "help": "现金流量表「经营活动产生的现金流量净额」"},
]

FIN_GROUP_ORDER = ["资产负债", "损益", "现金流"]

# ---------------------------------------------------------------------------
# 计算指标
# ---------------------------------------------------------------------------
# kind:
#   ratio  → 倍数，保留 2 位
#   pct    → 百分比，保留 1 位
#   amount → 金额，保留 2 位
RATIOS = [
    {"key": "net_assets", "label": "净资产", "kind": "amount", "unit": "万元",
     "formula": "资产总额 − 负债总额"},
    {"key": "debt_ratio", "label": "资产负债率", "kind": "pct", "unit": "%",
     "formula": "负债总额 ÷ 资产总额"},
    {"key": "current_ratio", "label": "流动比率", "kind": "ratio", "unit": "",
     "formula": "流动资产 ÷ 流动负债"},
    {"key": "quick_ratio", "label": "速动比率", "kind": "ratio", "unit": "",
     "formula": "（流动资产 − 存货）÷ 流动负债"},
    {"key": "cash_ratio", "label": "现金比率", "kind": "ratio", "unit": "",
     "formula": "货币资金 ÷ 流动负债"},
    {"key": "ar_turnover", "label": "应收账款周转率", "kind": "ratio", "unit": "次",
     "formula": "营业收入 ÷ 应收账款"},
    {"key": "revenue_growth", "label": "营业收入增长率", "kind": "pct", "unit": "%",
     "formula": "（本期 − 上期）÷ 上期"},
    {"key": "net_margin", "label": "净利润率", "kind": "pct", "unit": "%",
     "formula": "净利润 ÷ 营业收入"},
    {"key": "cash_debt_ratio", "label": "经营现金流 / 有息负债", "kind": "pct", "unit": "%",
     "formula": "经营活动现金流量净额 ÷ 有息负债"},
]


def _num(value):
    """把表单里的字符串/Decimal/None 归一成 float；无法解析返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("，", "")
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def compute_ratios(base):
    """由基础财务数据计算比率。

    base 为 {字段名: 数值} 的字典。除数为 0 或缺失时返回 None，
    模板侧显示「—」，不显示 0，避免把「算不出来」误读成「等于零」。
    返回 {比率 key: float|None}。
    """
    def get(key):
        return _num(base.get(key))

    ta, tl = get("total_assets"), get("total_liabilities")
    ca, cl = get("current_assets"), get("current_liabilities")
    inv, ar, cash = get("inventory"), get("accounts_receivable"), get("cash")
    rev, prev = get("revenue"), get("prev_revenue")
    profit, ocf = get("net_profit"), get("operating_cash_flow")
    idebt = get("interest_debt")

    def div(a, b):
        if a is None or b in (None, 0):
            return None
        return a / b

    return {
        "net_assets": (ta - tl) if (ta is not None and tl is not None) else None,
        "debt_ratio": (lambda v: v * 100 if v is not None else None)(div(tl, ta)),
        "current_ratio": div(ca, cl),
        "quick_ratio": (lambda v: v if v is not None else None)(
            div((ca - (inv or 0)), cl) if ca is not None else None
        ),
        "cash_ratio": div(cash, cl),
        "ar_turnover": div(rev, ar),
        "revenue_growth": (lambda v: v * 100 if v is not None else None)(
            div((rev - prev), prev) if (rev is not None and prev is not None) else None
        ),
        "net_margin": (lambda v: v * 100 if v is not None else None)(div(profit, rev)),
        "cash_debt_ratio": (lambda v: v * 100 if v is not None else None)(div(ocf, idebt)),
    }


def format_ratio(key, value):
    """按 kind 格式化比率，用于详情页/打印页。"""
    if value is None:
        return "—"
    for r in RATIOS:
        if r["key"] == key:
            if r["kind"] == "pct":
                return f"{value:.1f}%"
            if r["kind"] == "ratio":
                return f"{value:.2f}"
            return f"{value:,.2f}"
    return str(value)


# ---------------------------------------------------------------------------
# 等级阈值
# ---------------------------------------------------------------------------
CREDIT_GRADES = [
    {"min": 90, "label": "A 级", "range": "90–100 分", "tone": "a",
     "advice": "可按申请额度授信，账期可放宽至 90 天"},
    {"min": 80, "label": "B 级", "range": "80–89 分", "tone": "b",
     "advice": "正常授信，账期控制在 60 天内"},
    {"min": 70, "label": "C 级", "range": "70–79 分", "tone": "c",
     "advice": "限制授信，缩短账期至 30 天并要求担保"},
    {"min": 0, "label": "D 级", "range": "70 分以下", "tone": "d",
     "advice": "不予授信，仅接受预付或现款现货"},
]

REVIEW_GRADES = [
    {"min": 90, "label": "A 优秀", "range": "90–100 分", "tone": "a", "advice": "维持或提升额度"},
    {"min": 80, "label": "B 良好", "range": "80–89 分", "tone": "b", "advice": "维持现有额度"},
    {"min": 70, "label": "C 合格", "range": "70–79 分", "tone": "c", "advice": "维持额度、加强跟踪"},
    {"min": 0, "label": "D 不合格", "range": "70 分以下", "tone": "d", "advice": "下调额度或终止合作"},
]

AUDIT_VERDICTS = ["符合", "待整改", "不符合", "不适用"]

AUDIT_CONCLUSIONS = [
    ("通过", "全部核心项符合，非核心项无「不符合」"),
    ("有条件通过", "无「不符合」，但存在需限期整改项"),
    ("不通过", "任一核心项判「不符合」，或 OFAC 筛查命中"),
]


def grade_of(total, grades):
    """按分数取等级；total 为 None 时返回最高档占位（模板侧另行提示）。"""
    if total is None:
        return grades[-1]
    for g in grades:
        if total >= g["min"]:
            return g
    return grades[-1]


# ---------------------------------------------------------------------------
# 客户信用评价表 · 5 个维度 25 个评分项（合计 100 分）
# ---------------------------------------------------------------------------
CREDIT_GROUPS = [
    {
        "name": "一、基本情况", "max": 20, "rule": "全客户通用",
        "items": [
            {"name": "注册资本规模", "max": 5, "veto": False,
             "basis": "营业执照、公司章程、验资报告或审计报告（实缴口径）",
             "tiers": [
                 (5, "注册资本（实缴）≥ 5,000 万元"),
                 (4, "2,000 万元 ≤ 注册资本 < 5,000 万元"),
                 (3, "1,000 万元 ≤ 注册资本 < 2,000 万元"),
                 (2, "500 万元 ≤ 注册资本 < 1,000 万元"),
                 (1, "100 万元 ≤ 注册资本 < 500 万元"),
                 (0, "注册资本 < 100 万元，或认缴未实缴且无实际经营投入"),
             ]},
            {"name": "企业成立年限", "max": 4, "veto": False,
             "basis": "营业执照成立日期，至评价日计算",
             "tiers": [
                 (4, "≥ 10 年"),
                 (3, "5 年（含）–10 年"),
                 (2, "3 年（含）–5 年"),
                 (1, "1 年（含）–3 年"),
                 (0, "< 1 年"),
             ]},
            {"name": "股权结构稳定性", "max": 4, "veto": False,
             "basis": "工商股权穿透、章程、近 3 年变更记录",
             "tiers": [
                 (4, "近 3 年控股股东、实际控制人未变更，股权无质押、无冻结"),
                 (3, "近 3 年仅发生增资，或 1 次非控股股东之间的股权转让"),
                 (2, "近 3 年发生 2 次及以上股权变更，或存在已披露的股权质押"),
                 (1, "近 3 年实际控制人发生变更"),
                 (0, "股权代持未披露、股权被冻结，或存在控制权争议及诉讼"),
             ]},
            {"name": "经营规模与行业资质", "max": 4, "veto": False,
             "basis": "审计报告 / 纳税申报表、资质证书清单",
             "tiers": [
                 (4, "上年度营业收入 ≥ 同行业中位数，且经营所需资质证照齐全有效"),
                 (3, "营业收入达同行业中位数 70% 以上，主要资质齐全、个别资质在办"),
                 (2, "营业收入低于同行业中位数 70%，但资质齐全有效"),
                 (1, "缺少 1 项关键经营资质，或营业收入显著低于其经营规模"),
                 (0, "无与主营业务匹配的经营资质，或存在超范围经营"),
             ]},
            {"name": "企业主体合规性", "max": 3, "veto": False,
             "basis": "国家企业信用信息公示系统（近 30 日内查询截图）",
             "tiers": [
                 (3, "存续状态正常，无经营异常、无严重违法失信记录，年报连续报送"),
                 (2, "历史上有经营异常记录但已移出，或有 1 次年报补报记录"),
                 (1, "当前存在地址失联或年报漏报，已启动整改"),
                 (0, "列入经营异常名录满 3 年未移出，或严重违法失信名单，或已吊销/注销"),
             ]},
        ],
    },
    {
        "name": "二、财务状况", "max": 30, "rule": "全客户通用 · 取最近一期审计报告",
        "items": [
            {"name": "资产负债率", "max": 8, "veto": False, "fin": "debt_ratio",
             "basis": "最近一期审计报告或财务报表；资产负债率＝负债总额÷资产总额",
             "tiers": [
                 (8, "≤ 40%"),
                 (7, "40% < X ≤ 50%"),
                 (6, "50% < X ≤ 60%"),
                 (4, "60% < X ≤ 70%"),
                 (2, "70% < X ≤ 80%"),
                 (0, "> 80%"),
             ]},
            {"name": "流动比率 / 速动比率", "max": 7, "veto": False, "fin": "liquidity",
             "basis": "最近一期资产负债表；先按流动比率定档，速动比率低于同档要求时下调 1 分（不低于 0 分）",
             "tiers": [
                 (7, "流动比率 ≥ 2.0 且 速动比率 ≥ 1.2"),
                 (6, "流动比率 1.5–2.0 且 速动比率 ≥ 1.0"),
                 (5, "流动比率 1.2–1.5 且 速动比率 ≥ 0.8"),
                 (3, "流动比率 1.0–1.2，或速动比率 0.6–0.8"),
                 (1, "流动比率 0.8–1.0"),
                 (0, "流动比率 < 0.8，或速动比率 < 0.6"),
             ]},
            {"name": "营业收入增长率", "max": 5, "veto": False, "fin": "revenue_growth",
             "basis": "近两年利润表，本期营业收入较上期同比增幅",
             "tiers": [
                 (5, "≥ 20%"),
                 (4, "10% – 20%"),
                 (3, "0% – 10%"),
                 (2, "-10% – 0%"),
                 (1, "-30% – -10%"),
                 (0, "< -30%"),
             ]},
            {"name": "净利润率", "max": 5, "veto": False, "fin": "net_margin",
             "basis": "近一年利润表；净利润率＝净利润÷营业收入",
             "tiers": [
                 (5, "≥ 10%"),
                 (4, "5% – 10%"),
                 (3, "2% – 5%"),
                 (2, "0% – 2%"),
                 (1, "0 > 净利润率 ≥ -5%（微亏）"),
                 (0, "净利润率 < -5%"),
             ]},
            {"name": "现金流稳定性", "max": 5, "veto": False, "fin": "operating_cash_flow",
             "basis": "近三年现金流量表，经营活动产生的现金流量净额",
             "tiers": [
                 (5, "近 3 年经营活动现金流净额均为正，且逐年增长"),
                 (4, "近 3 年均为正，年度间波动 ≤ 20%"),
                 (3, "近 3 年中 2 年为正"),
                 (2, "近 3 年中 1 年为正"),
                 (1, "近 3 年均为负，但有大额授信或股东资金支持"),
                 (0, "近 3 年均为负且持续恶化"),
             ]},
        ],
    },
    {
        "name": "三、交易记录", "max": 25, "rule": "全客户通用 · 取内部 ERP 数据",
        "items": [
            {"name": "历史交易金额规模", "max": 6, "veto": False,
             "basis": "内部 ERP 销售流水，近 12 个月含税采购额；新客户按准入申请预估额度折算 50% 后定档",
             "tiers": [
                 (6, "近 12 个月采购额 ≥ 500 万元"),
                 (5, "300 – 500 万元"),
                 (4, "150 – 300 万元"),
                 (3, "80 – 150 万元"),
                 (2, "30 – 80 万元"),
                 (1, "< 30 万元"),
                 (0, "近 12 个月无实际交易"),
             ]},
            {"name": "合作年限与稳定性", "max": 5, "veto": False,
             "basis": "首单日期至今，取整年",
             "tiers": [
                 (5, "≥ 5 年"),
                 (4, "3 – 5 年"),
                 (3, "2 – 3 年"),
                 (2, "1 – 2 年"),
                 (1, "< 1 年（新客户）"),
             ]},
            {"name": "付款及时率", "max": 6, "veto": False,
             "basis": "应收台账与对账单；及时率＝按期付款笔数÷到期应付款笔数",
             "tiers": [
                 (6, "≥ 98%"),
                 (5, "95% – 98%"),
                 (4, "90% – 95%"),
                 (3, "80% – 90%"),
                 (2, "70% – 80%"),
                 (0, "< 70%"),
             ]},
            {"name": "退货率与投诉率", "max": 4, "veto": False,
             "basis": "售后工单系统，近 12 个月退货金额占总销售金额比例",
             "tiers": [
                 (4, "退货率 ≤ 1% 且近 12 个月无质量投诉"),
                 (3, "退货率 1%–3%，或投诉 1 次且已闭环"),
                 (2, "退货率 3%–5%，或投诉 2 次"),
                 (1, "退货率 5%–8%，或投诉 3 次及以上"),
                 (0, "退货率 > 8%，或发生重大质量、安全投诉"),
             ]},
            {"name": "合作履约配合度", "max": 4, "veto": False,
             "basis": "业务部门评价记录、往来函件",
             "tiers": [
                 (4, "合同条款全面履约，促销、陈列、数据报送等配合主动及时"),
                 (3, "履约良好，偶有延迟但均提前报备"),
                 (2, "需多次催办方能配合，未造成实际损失"),
                 (1, "存在违约行为但已协商解决，无未结欠款"),
                 (0, "存在未解决的违约、欠款，或拒不配合"),
             ]},
        ],
    },
    {
        "name": "四、行业声誉", "max": 15, "rule": "全客户通用 · 以公开信息为准",
        "items": [
            {"name": "行业地位与品牌影响力", "max": 4, "veto": False,
             "basis": "行业协会资料、公开报道、厂商授权书",
             "tiers": [
                 (4, "全国性或省级行业龙头 / 知名品牌授权商"),
                 (3, "区域市场前列（地市级前三）"),
                 (2, "一般区域经销商，市场份额稳定"),
                 (1, "新进入者，尚无稳定市场份额"),
                 (0, "无实际经营实体、壳公司或空壳贸易商"),
             ]},
            {"name": "市场口碑与商业信誉", "max": 4, "veto": False,
             "basis": "公开搜索、行业媒体、司法公开信息",
             "tiers": [
                 (4, "公开渠道无负面信息，同行评价正面"),
                 (3, "有零星负面信息，且已澄清"),
                 (2, "存在负面舆情，未涉及违法违规"),
                 (1, "存在商业信誉类纠纷（拖欠、欺诈指控）"),
                 (0, "有失信、欺诈类生效判决或负面记录"),
             ]},
            {"name": "上下游合作伙伴评价", "max": 3, "veto": False,
             "basis": "访谈记录、供应商与客户名录（建议访谈 2 家以上）",
             "tiers": [
                 (3, "主要上下游客户 / 供应商评价正面，可提供 2 家以上访谈"),
                 (2, "评价中性，无重大分歧"),
                 (1, "个别合作伙伴反映付款或质量问题"),
                 (0, "多家合作伙伴负面评价，或被供应商列为风险客户"),
             ]},
            {"name": "行业内不良记录情况", "max": 4, "veto": False,
             "basis": "行业协会公示、厂商渠道通报",
             "tiers": [
                 (4, "无行业不良记录"),
                 (3, "有已妥善处理的历史不良记录（发生时间超过 3 年）"),
                 (2, "近 3 年有 1 条行业不良记录"),
                 (1, "近 3 年有 2 条及以上行业不良记录"),
                 (0, "当前处于行业协会黑名单，或被主要厂商终止合作"),
             ]},
        ],
    },
    {
        "name": "五、合规情况", "max": 10, "rule": "全客户通用 · 查询日期需在评价日前 30 日内",
        "items": [
            {"name": "行政处罚记录情况", "max": 3, "veto": False,
             "basis": "信用中国、地方监管部门公示（近 3 年）",
             "tiers": [
                 (3, "近 3 年无行政处罚"),
                 (2, "近 3 年 1 条轻微处罚（罚款 < 5 万元）且已整改"),
                 (1, "近 3 年 2 条及以上轻微处罚，或 1 条一般处罚"),
                 (0, "近 1 年发生安全、环保、税务、质量类重大处罚"),
             ]},
            {"name": "司法涉诉与失信记录", "max": 3, "veto": False,
             "basis": "中国执行信息公开网、裁判文书网",
             "tiers": [
                 (3, "无涉诉、无被执行、无失信记录"),
                 (2, "仅作为原告的民事案件，或已结案且标的 < 净资产 5%"),
                 (1, "作为被告且标的 < 净资产 10%，无失信记录"),
                 (0, "失信被执行人、被执行金额 ≥ 净资产 10%，或存在未结重大诉讼"),
             ]},
            {"name": "制裁名单筛查结果", "max": 2, "veto": True,
             "basis": "OFAC SDN/EU/UN 及国内出口管制清单逐条比对记录（含筛查日期、名单版本）",
             "tiers": [
                 (2, "主体、法定代表人、实际控制人、董监高、受益所有人均未命中制裁清单"),
                 (1, "主体未命中，但存在需说明的关联方，已排除并留档说明"),
                 (0, "任一命中制裁清单，或拒绝提供筛查所需信息（一票否决）"),
             ]},
            {"name": "其他合规风险情况", "max": 2, "veto": False,
             "basis": "合规问卷、合规承诺函",
             "tiers": [
                 (2, "未发现反商业贿赂、数据合规、贸易合规等风险"),
                 (1, "存在一般风险且已制订管控措施"),
                 (0, "存在重大合规风险（商业贿赂、走私、非法转口、数据违规）"),
             ]},
        ],
    },
]

# ---------------------------------------------------------------------------
# 客户定期考评表 · 4 个维度 14 个评分项（合计 100 分）
# ---------------------------------------------------------------------------
REVIEW_GROUPS = [
    {
        "name": "一、销售业绩完成情况", "max": 40, "rule": "经销商适用 · 终端客户本项按满分计",
        "autofull": True,
        "items": [
            {"name": "年度 / 季度销售目标完成率", "max": 20, "veto": False,
             "basis": "ERP 出货数据（按考评周期）；完成率＝实际销售额÷目标销售额",
             "tiers": [
                 (20, "≥ 120%"), (18, "100% – 120%"), (15, "90% – 100%"),
                 (12, "80% – 90%"), (8, "70% – 80%"), (4, "60% – 70%"), (0, "< 60%"),
             ]},
            {"name": "销售增长率", "max": 10, "veto": False,
             "basis": "本考评周期与上一周期同比",
             "tiers": [
                 (10, "≥ 30%"), (8, "15% – 30%"), (6, "5% – 15%"),
                 (4, "0% – 5%"), (2, "-10% – 0%"), (0, "< -10%"),
             ]},
            {"name": "核心产品销售占比", "max": 10, "veto": False,
             "basis": "ERP 品类结构，核心产品销售额占总销售额比例",
             "tiers": [
                 (10, "≥ 60%"), (8, "45% – 60%"), (6, "30% – 45%"),
                 (4, "20% – 30%"), (2, "10% – 20%"), (0, "< 10%"),
             ]},
        ],
    },
    {
        "name": "二、合作履约情况", "max": 30, "rule": "全客户通用",
        "items": [
            {"name": "付款及时率与回款情况", "max": 12, "veto": False,
             "basis": "应收台账、账龄分析；及时率＝按期回款笔数÷到期应回款笔数",
             "tiers": [
                 (12, "及时率 ≥ 98%，无逾期"),
                 (10, "95% – 98%，逾期均 ≤ 15 天"),
                 (8, "90% – 95%，或最长逾期 16–30 天"),
                 (5, "80% – 90%，或最长逾期 31–60 天"),
                 (3, "70% – 80%，或最长逾期 61–90 天"),
                 (0, "< 70%，或最长逾期 > 90 天 / 已形成坏账"),
             ]},
            {"name": "合同条款执行合规性", "max": 8, "veto": False,
             "basis": "合同文本、往来函件、价格审批记录",
             "tiers": [
                 (8, "完全执行合同条款，无偏差"),
                 (6, "轻微偏差（价格、账期、区域），已书面确认"),
                 (4, "一般偏差，经提示后整改到位"),
                 (2, "多次偏差，需反复催办"),
                 (0, "重大违约（擅自降价、跨区销售、私贴牌）"),
             ]},
            {"name": "退换货与售后配合度", "max": 5, "veto": False,
             "basis": "售后工单记录与闭环时效",
             "tiers": [
                 (5, "主动配合，48 小时内响应并闭环"),
                 (4, "配合，72 小时内响应"),
                 (3, "需催办后配合，一周内闭环"),
                 (1, "多次拖延，已影响终端客户"),
                 (0, "拒不配合退换货或售后"),
             ]},
            {"name": "市场秩序维护情况", "max": 5, "veto": False,
             "basis": "渠道巡查记录、序列号追溯、举报核查结果",
             "rule": "经销商适用",
             "tiers": [
                 (5, "本考评周期无窜货、乱价记录"),
                 (4, "有 1 次轻微越界且已整改"),
                 (2, "经核实 1 次窜货或乱价"),
                 (1, "经核实 2 次及以上窜货或乱价"),
                 (0, "恶意窜货、跨区倾销或低价倾销"),
             ]},
        ],
    },
    {
        "name": "三、经营与服务能力", "max": 20, "rule": "全客户通用",
        "items": [
            {"name": "销售渠道覆盖与拓展能力", "max": 6, "veto": False,
             "basis": "终端网点清单、拜访记录", "rule": "经销商适用",
             "tiers": [
                 (6, "覆盖区域目标终端 ≥ 80%，本周期新增有效终端 ≥ 10 家"),
                 (5, "覆盖 60%–80%，或新增 5–9 家"),
                 (4, "覆盖 40%–60%，或新增 2–4 家"),
                 (2, "覆盖 20%–40%，新增 < 2 家"),
                 (0, "覆盖 < 20%，或无拓展动作、终端持续流失"),
             ]},
            {"name": "仓储物流与库存管理能力", "max": 4, "veto": False,
             "basis": "现场勘查、库存盘点表", "rule": "经销商适用",
             "tiers": [
                 (4, "仓储面积与温控符合要求，年周转 ≥ 6 次，账实相符率 ≥ 99%"),
                 (3, "基本符合，周转 4–6 次，账实相符率 95%–99%"),
                 (2, "存在账实差异或临期库存，已整改"),
                 (1, "仓储条件不达标，或周转 < 2 次"),
                 (0, "无固定仓储，或存在重大库存毁损、安全隐患"),
             ]},
            {"name": "售后技术服务能力", "max": 5, "veto": False,
             "basis": "人员名册、厂商认证证书、服务记录",
             "tiers": [
                 (5, "专职售后团队 + 厂商认证技术人员，24 小时内到场"),
                 (4, "有专职人员，48 小时内到场"),
                 (3, "兼职人员，72 小时内到场"),
                 (1, "依赖第三方，响应超过 72 小时"),
                 (0, "无售后能力，投诉集中"),
             ]},
            {"name": "团队稳定性与专业能力", "max": 5, "veto": False,
             "basis": "社保人数或花名册周期对比、关键岗位证书",
             "tiers": [
                 (5, "核心团队任职 ≥ 3 年，关键岗位持证齐全"),
                 (4, "稳定性良好，个别岗位空缺但已补位"),
                 (3, "本周期人员流失率 20%–30%"),
                 (1, "本周期人员流失率 > 30%"),
                 (0, "核心人员大量流失，业务停摆"),
             ]},
        ],
    },
    {
        "name": "四、合规与风险管控", "max": 10, "rule": "全客户通用",
        "items": [
            {"name": "经营资质合规性", "max": 3, "veto": False,
             "basis": "证照扫描件、有效期台账",
             "tiers": [
                 (3, "证照齐全有效，经营范围与主营业务匹配"),
                 (2, "个别证照临期，已在换证"),
                 (1, "关键资质过期未换，或超范围经营已提示未整改"),
                 (0, "无证经营、超范围经营已被查处"),
             ]},
            {"name": "无违法违规、涉诉失信记录", "max": 4, "veto": True,
             "basis": "信用中国、执行信息公开网（考评期内查询）",
             "tiers": [
                 (4, "本周期无新增违法违规、涉诉、失信记录"),
                 (3, "有轻微行政处罚且已整改"),
                 (2, "有未结民事纠纷（非失信）"),
                 (0, "新增失信被执行人、被执行记录，或重大行政处罚"),
             ]},
            {"name": "公司合规要求执行情况", "max": 3, "veto": False,
             "basis": "合规检查记录、合规承诺函",
             "tiers": [
                 (3, "价格、宣传、数据报送、廉洁承诺全面执行"),
                 (2, "基本执行，个别事项经提示后整改"),
                 (1, "多次提示仍未整改"),
                 (0, "违反合规红线（商业贿赂、虚假宣传、泄露客户数据）"),
             ]},
        ],
    },
]

# ---------------------------------------------------------------------------
# 客户现场审核表 · 7 个审核项（不计分，四档判定）
# ---------------------------------------------------------------------------
# core 列出「核心子项」：核心子项不满足时不得降格判为「待整改」，
# 必须判「不符合」——否则现场审核会沦为走过场。
AUDIT_ROWS = [
    {
        "name": "一、经营资质与主体合规性", "tag": "",
        "points": [
            "营业执照、资质证书在有效期内，与申请信息一致",
            "实际经营地址与注册地址匹配，无异常经营情况",
            "无经营异常、失信被执行人等不良记录",
        ],
        "core": "子项 1",
        "pass": "3 个子项全部满足，且能提供证照原件、系统查询截图或现场影像等有效证明。",
        "fix": "证照临期（剩余有效期不足 3 个月）但已启动换证；或实际经营地址与注册地址不一致，但已办理分支机构登记并有实际使用证明。",
        "fail": "营业执照无效、超范围经营；或当前被列入经营异常名录、严重违法失信名单、失信被执行人名单。",
    },
    {
        "name": "二、经营场地与硬件设施", "tag": "",
        "points": [
            "有固定、合规的经营办公场地，面积与经营规模匹配",
            "办公设施齐全，具备正常开展业务的硬件条件",
            "有独立的产品展示、接待区域，符合品牌形象要求",
        ],
        "core": "子项 1",
        "pass": "3 个子项全部满足，现场勘查与申报面积、地址一致。",
        "fix": "场地权属或租赁凭证待补充；展示区尚未按品牌形象完成布置；办公设施部分缺失但不影响业务开展。可限期整改。",
        "fail": "无固定经营场所；或以住宅、虚拟注册地址作为实际经营场所且无法提供使用证明；或场地面积不足申报规模 50%。",
    },
    {
        "name": "三、仓储物流能力", "tag": "经销商必填",
        "points": [
            "有固定的仓储场地，具备产品存储、保管的基础条件",
            "仓储环境符合产品存储要求，无安全隐患",
            "有稳定的物流合作渠道，具备区域配送能力",
        ],
        "core": "子项 1、2",
        "pass": "3 个子项全部满足，仓储现场符合产品存储与消防要求。",
        "fix": "仓储面积与经营规模基本匹配但温湿度记录不完整；或物流以临时外包为主、无稳定合作协议；或消防器材超期未年检。",
        "fail": "无固定仓储场地；或仓储存在消防重大隐患、危化品混存、产品露天堆放等不可整改情形。",
    },
    {
        "name": "四、团队配置与专业能力", "tag": "",
        "points": [
            "有固定的销售、售后、运营团队，人员数量与业务规模匹配",
            "核心人员具备相关行业经验，熟悉公司产品及业务模式",
            "有完善的人员管理制度，团队稳定性良好",
        ],
        "core": "子项 1",
        "pass": "3 个子项全部满足，社保人数与申报团队规模一致。",
        "fix": "个别岗位空缺或核心人员行业经验不足 2 年，但已提交招聘/培训计划；或人员管理制度未成文。",
        "fail": "无常设销售及售后团队（仅 1 人兼营）；或团队规模明显不足以支撑申请区域业务；或核心人员对产品与业务模式完全不了解。",
    },
    {
        "name": "五、销售渠道与市场覆盖", "tag": "经销商必填",
        "points": [
            "具备申请区域内的销售渠道资源，渠道覆盖符合合作要求",
            "有成熟的渠道开发、维护能力，过往销售业绩良好",
            "无跨区域窜货、违规销售等不良行业记录",
        ],
        "core": "子项 1",
        "pass": "3 个子项全部满足，可提供终端网点清单与近 12 个月销售流水。",
        "fix": "渠道覆盖低于申请区域目标终端的 60%；或过往销售业绩未达约定目标 70%，但已提交渠道改善计划。",
        "fail": "无实际渠道资源（仅口头承诺）；或存在跨区域窜货、违规销售且未整改；或提供虚假销售流水。",
    },
    {
        "name": "六、合规与风险管控", "tag": "",
        "points": [
            "有完善的内部管理制度，合规经营意识良好",
            "无重大涉诉、行政处罚等违法违规记录",
            "具备合作风险管控能力，可配合公司合规管理要求",
        ],
        "core": "子项 2",
        "pass": "3 个子项全部满足，近 3 年无重大涉诉与行政处罚。",
        "fix": "内部管理制度不健全（缺价格、返利、售后书面制度）；或存在非重大行政处罚且已整改；或合规承诺函尚未签署。",
        "fail": "存在重大涉诉、行政处罚且未披露；或拒不配合合规审查、拒绝签署合规承诺、抗拒提供涉诉与处罚查询授权。",
    },
    {
        "name": "七、OFAC 国际制裁名单筛查合规", "tag": "全员必填 · 一票否决",
        "points": [
            "客户主体全称、曾用名、别名已完成 OFAC 全名单筛查，未命中 SDN 及各类制裁清单",
            "客户法定代表人、实际控制人、董监高、受益所有人（持股 25% 及以上人员）已完成制裁筛查，无制裁关联人员",
            "无被制裁主体控股（含 50% 及以上受控关联）、挂靠、代持等隐性关联关系",
            "客户经营、交易、物流、资金结算不涉及 OFAC 制裁国家 / 地区、制裁机构及受限交易链路",
            "客户可配合我方定期制裁复筛、信息更新，无规避制裁筛查的行为及记录",
        ],
        "core": "子项 1–5 全部为一票否决项",
        "pass": "5 个子项全部满足，筛查记录完整（含筛查日期、名单版本、比对结果）并留档。",
        "fix": "筛查信息不完整（如受益所有人未穿透至最终自然人、缺少曾用名比对）；或客户暂未提供复筛配合承诺。补齐后需重新复核，不得先行通过。",
        "fail": "任一主体或关联方命中 SDN/制裁清单；或被制裁主体控股、挂靠、代持且无法排除；或交易链路涉及制裁国家/地区。",
    },
]

# ---------------------------------------------------------------------------
# 穿行测试 · 客户 / 供应商付款 8 个控制点
# ---------------------------------------------------------------------------
# 穿行测试的做法：抽取少量样本，把一笔付款从「申请」到「入账」全流程走一遍，
# 确认制度写的控制点是否真的执行。因此每个控制点只判「执行 / 未执行 / 不适用」，
# 并要求留下可复核的证据编号，而不是给分。
WALKTHROUGH_STEPS = [
    {
        "key": "s1", "name": "采购申请与审批", "stage": "事前",
        "control": "采购需求须由需求部门提出书面申请，经部门负责人及授权审批人批准后方可执行。",
        "checks": [
            "有书面采购申请，载明品名、数量、需求日期",
            "审批签字齐全，审批日期早于订单日期",
            "审批人层级符合授权表（金额分档）",
        ],
        "evidence": "采购申请单号 / 审批截图",
    },
    {
        "key": "s2", "name": "供应商准入与主数据", "stage": "事前",
        "control": "付款对象须为合格供应商名录内主体，主数据（名称、税号、银行账户）经审批后维护，变更留痕。",
        "checks": [
            "供应商在合格名录内，且准入审核在合作前完成",
            "收款账户名称与合同主体、发票抬头三者一致",
            "账户信息变更附书面申请及审批",
        ],
        "evidence": "供应商准入表 / 主数据变更单",
    },
    {
        "key": "s3", "name": "合同与订单", "stage": "事前",
        "control": "采购须有合同或订单，价格、账期、质量标准须经核价与法务确认，订单金额不得超合同额度。",
        "checks": [
            "有合同 / 采购订单，条款完整并经双方签章",
            "单价与核价单一致，累计下单未超合同额度",
            "付款条件（账期、预付比例）与合同一致",
        ],
        "evidence": "合同号 / 采购订单号",
    },
    {
        "key": "s4", "name": "到货验收", "stage": "事中",
        "control": "货物到达后由仓储与质检共同验收，出具验收单，数量、质量与订单一致方可入库。",
        "checks": [
            "有验收单，验收人、日期、数量齐全",
            "质检结论合格，不合格品有退换记录",
            "入库单数量与验收单、订单一致",
        ],
        "evidence": "验收单号 / 入库单号",
    },
    {
        "key": "s5", "name": "发票接收与三单匹配", "stage": "事中",
        "control": "发票须经税务查验，并与订单、验收单、入库单匹配（三单匹配）后方可进入付款流程。",
        "checks": [
            "发票真实有效，抬头、税号、金额与合同一致",
            "订单、验收单、发票三者数量金额匹配",
            "发票入账期间与验收期间一致，无跨期",
        ],
        "evidence": "发票号 / 三单匹配表",
    },
    {
        "key": "s6", "name": "付款申请与审批", "stage": "事中",
        "control": "付款申请须附齐合同、验收单、发票，按金额分档经部门负责人、财务负责人、总经理审批。",
        "checks": [
            "付款申请单附件齐全（合同 / 验收 / 发票）",
            "审批层级与金额档位匹配，无越权审批",
            "付款金额不超过发票金额及合同约定比例",
        ],
        "evidence": "付款申请单号 / 审批流截图",
    },
    {
        "key": "s7", "name": "付款执行与账户核对", "stage": "事中",
        "control": "出纳依据审批完毕的付款申请办理付款，收款账户须与准入主数据一致，网银支付须双人复核。",
        "checks": [
            "收款账户与供应商主数据完全一致（防篡改）",
            "网银制单与复核为不同人员，U 盾分管",
            "付款日期、金额与审批一致，银行回单已归档",
        ],
        "evidence": "银行回单 / 网银流水号",
    },
    {
        "key": "s8", "name": "账务处理与月末对账", "stage": "事后",
        "control": "付款后及时编制记账凭证，科目、供应商、金额准确；月末与供应商对账并清理预付、应付余额。",
        "checks": [
            "记账凭证日期与付款日期同期，科目使用正确",
            "应付 / 预付余额与对账单一致，差异有调节表",
            "长期挂账预付款已跟踪清理",
        ],
        "evidence": "记账凭证号 / 对账单",
    },
]

STEP_RESULTS = [
    ("执行", "控制点已执行且有证据支持"),
    ("部分执行", "控制点执行但不完整，或证据缺失"),
    ("未执行", "控制点未执行，或无法提供任何证据"),
    ("不适用", "该笔业务不涉及此控制点"),
]

DEFECT_TYPES = ["无", "设计缺陷", "执行缺陷"]

WALKTHROUGH_CONCLUSIONS = [
    ("有效", "全部控制点执行，无缺陷"),
    ("基本有效", "存在个别执行缺陷，不影响整体控制目标"),
    ("部分有效", "多个控制点未执行或证据缺失，控制目标部分未达成"),
    ("无效", "关键控制点失效，存在舞弊或重大错报风险"),
]

# 判定为「有效」的必要条件：这 4 个控制点任一未执行，整体结论不得高于「部分有效」。
WALKTHROUGH_KEY_STEPS = ["s2", "s5", "s6", "s7"]

SAMPLE_METHODS = [
    "随机抽样",
    "等距抽样",
    "大额重点抽查",
    "异常事项定向抽查",
]

PAYEE_TYPES = ["供应商", "客户"]

# ---------------------------------------------------------------------------
# 账期执行核对：实际付款日 vs 合同约定账期
# ---------------------------------------------------------------------------
# 穿行测试除了「控制点是否执行」，还要回答一个问题：钱是不是按约定期限付的。
# 逾期付款意味着合同/结算条款未被遵循，同时反映资金安排与供应商关系风险，
# 因此单列一层核对，不并入 8 个控制点（否则每笔样本都要重复判定 8 次账期）。
TERMS_BASE_CHOICES = ["验收日", "发票日", "对账单日", "合同约定日", "其他"]

# 到期日之后 N 天内付款仍视为按期：跨行在途、节假日顺延的合理宽限。
TERMS_GRACE_DAYS = 3
# 单笔逾期超过该天数视为重大偏差，直接压低结论。
TERMS_MAJOR_DAYS = 30
# 逾期笔数占已核对样本的比例达到该值，且逾期达 2 笔以上，认定账期控制失效。
# 单笔逾期不按比例判定（1/1 也是 100%），交给「单笔超 30 天」那条规则。
TERMS_BAD_RATIO = 0.5


def parse_date(value):
    """把 'YYYY-MM-DD' / date 解析成 date；失败返回 None（不抛异常）。"""
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def to_int(value, default=None):
    """宽松转整数：'60'、60.0、'60 天' → 60；空值/非法返回 default。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def sample_terms(sample, default_terms=None):
    """单笔样本的账期执行核对结果。

    到期日优先取样本上显式填写的值（合同可能约定了非按天计算的方式），
    没有则按「起算日 + 约定账期天数」推算；两者都没有则判定为「未核对」，
    不参与统计，也不产生缺陷。

    返回：
      terms_days   实际采用的账期天数（样本优先，其次表头默认）
      base_date    起算日
      due_date     到期日
      pay_date     实际付款日
      delay_days   偏差天数：正数为逾期，负数为提前，None 为无法计算
      status       '按期' / '逾期' / '提前' / '未核对'
    """
    terms = to_int(sample.get("terms_days"), None)
    if terms is None:
        terms = to_int(default_terms, None)
    base = parse_date(sample.get("base_date"))
    due = parse_date(sample.get("due_date"))
    pay = parse_date(sample.get("pay_date"))
    if due is None and base and terms is not None:
        due = base + timedelta(days=terms)
    delay = (pay - due).days if (pay and due) else None
    if delay is None:
        status = "未核对"
    elif delay > TERMS_GRACE_DAYS:
        status = "逾期"
    elif delay < 0:
        status = "提前"
    else:
        status = "按期"
    return {
        "terms_days": terms,
        "base_date": base,
        "due_date": due,
        "pay_date": pay,
        "delay_days": delay,
        "status": status,
    }


def terms_stats(samples, default_terms=None):
    """账期执行汇总：按期率、逾期笔数、平均/最长偏差。

    只有「到期日 + 实际付款日」都齐的样本才进入分母，避免样本登记不全时
    被误判为 100% 按期。
    """
    checked = on_time = overdue = early = 0
    delays = []
    major = 0
    for sample in samples:
        info = sample_terms(sample, default_terms)
        if info["delay_days"] is None:
            continue
        checked += 1
        delays.append(info["delay_days"])
        if info["status"] == "逾期":
            overdue += 1
            if info["delay_days"] > TERMS_MAJOR_DAYS:
                major += 1
        elif info["status"] == "提前":
            early += 1
        else:
            on_time += 1
    overdue_list = [d for d in delays if d > TERMS_GRACE_DAYS]
    return {
        "checked": checked,
        "on_time": on_time,
        "overdue": overdue,
        "early": early,
        "major": major,
        "on_time_rate": round((checked - overdue) / checked * 100) if checked else None,
        "avg_delay": round(sum(delays) / len(delays), 1) if delays else None,
        "max_delay": max(delays) if delays else None,
        "avg_overdue": round(sum(overdue_list) / len(overdue_list), 1) if overdue_list else None,
        "bad_ratio": checked > 0 and overdue >= 2 and (overdue / checked) >= TERMS_BAD_RATIO,
    }


# ---------------------------------------------------------------------------
# 计算辅助
# ---------------------------------------------------------------------------
def iter_items(groups):
    """统一的遍历顺序：(组序号, 项序号, 组, 项)。"""
    for gi, group in enumerate(groups):
        for ii, item in enumerate(group["items"]):
            yield gi, ii, group, item


def item_key(gi, ii):
    return f"g{gi}i{ii}"


def parse_scores(raw):
    """把 {'g0i0': '5', ...} 解析成 {item_key: int}，非法值丢弃。"""
    out = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        try:
            out[str(key)] = int(float(value))
        except (TypeError, ValueError):
            continue
    return out


def clamp_scores(scores, groups):
    """按分档可选值收敛得分：只接受该评分项真实存在的档位分值。

    这样即使有人改前端下拉框的值，也无法造出「标准分值之外」的分数。
    """
    clean = {}
    for gi, ii, _group, item in iter_items(groups):
        key = item_key(gi, ii)
        if key not in scores:
            continue
        allowed = {t[0] for t in item["tiers"]}
        value = scores[key]
        if value in allowed:
            clean[key] = value
        else:
            # 非法值按最接近的合法档位处理，避免整份表单作废
            clean[key] = min(allowed, key=lambda a: abs(a - value))
    return clean


def total_score(scores, groups):
    """合计得分。未评分的项按 0 计，与「就低认定」口径一致。"""
    total = 0
    for gi, ii, _group, item in iter_items(groups):
        total += scores.get(item_key(gi, ii), 0)
    return total


def group_scores(scores, groups):
    """各维度小计：[{name, max, got, scored, count}]。"""
    rows = []
    for gi, group in enumerate(groups):
        got = 0
        scored = 0
        for ii, item in enumerate(group["items"]):
            key = item_key(gi, ii)
            if key in scores:
                got += scores[key]
                scored += 1
        rows.append({
            "name": group["name"],
            "max": group["max"],
            "got": got,
            "scored": scored,
            "count": len(group["items"]),
        })
    return rows


def score_items(groups, scores):
    """给模板用的渲染结构：组内每一项带上已选分值、档位列表、是否缺省、维度小计。"""
    out = []
    for gi, group in enumerate(groups):
        items = []
        got = 0
        scored = 0
        for ii, item in enumerate(group["items"]):
            key = item_key(gi, ii)
            value = scores.get(key)
            if key in scores:
                got += scores[key]
                scored += 1
            items.append({
                **item,
                "key": key,
                "value": value,
                "value_text": next((t[1] for t in item["tiers"] if t[0] == value), ""),
                "scored": key in scores,
            })
        out.append({**group, "items": items, "index": gi,
                    "got": got, "scored_count": scored})
    return out


def suggest_fin_scores(ratios):
    """按财务比率自动给出 4 个财务评分项的建议分。

    仅作建议：前端预填后可人工改；后端保存时仍以提交值为准。
    现金流稳定性需要近三年数据，无法由单期数据推出，不给建议。
    """
    out = {}
    debt = ratios.get("debt_ratio")
    if debt is not None:
        if debt <= 40:
            out["g1i0"] = 8
        elif debt <= 50:
            out["g1i0"] = 7
        elif debt <= 60:
            out["g1i0"] = 6
        elif debt <= 70:
            out["g1i0"] = 4
        elif debt <= 80:
            out["g1i0"] = 2
        else:
            out["g1i0"] = 0

    cr, qr = ratios.get("current_ratio"), ratios.get("quick_ratio")
    if cr is not None:
        if cr >= 2.0 and (qr or 0) >= 1.2:
            out["g1i1"] = 7
        elif cr >= 1.5 and (qr or 0) >= 1.0:
            out["g1i1"] = 6
        elif cr >= 1.2 and (qr or 0) >= 0.8:
            out["g1i1"] = 5
        elif cr >= 1.0 or 0.6 <= (qr or 0) < 0.8:
            out["g1i1"] = 3
        elif cr >= 0.8:
            out["g1i1"] = 1
        else:
            out["g1i1"] = 0
        # 原表口径：速动比率低于同档要求下调 1 分
        if qr is not None and qr < 0.6:
            out["g1i1"] = 0

    growth = ratios.get("revenue_growth")
    if growth is not None:
        if growth >= 20:
            out["g1i2"] = 5
        elif growth >= 10:
            out["g1i2"] = 4
        elif growth >= 0:
            out["g1i2"] = 3
        elif growth >= -10:
            out["g1i2"] = 2
        elif growth >= -30:
            out["g1i2"] = 1
        else:
            out["g1i2"] = 0

    margin = ratios.get("net_margin")
    if margin is not None:
        if margin >= 10:
            out["g1i3"] = 5
        elif margin >= 5:
            out["g1i3"] = 4
        elif margin >= 2:
            out["g1i3"] = 3
        elif margin >= 0:
            out["g1i3"] = 2
        elif margin >= -5:
            out["g1i3"] = 1
        else:
            out["g1i3"] = 0

    return out


def audit_conclusion(verdicts):
    """现场审核结论建议。

    verdicts: {'r0': '符合', 'r6': '不符合', ...}
    规则与制度一致：任一核心项不符合 → 不通过；OFAC（第七项）命中 → 不通过；
    无不符合但有「待整改」→ 有条件通过；全部符合（含不适用）→ 通过。
    """
    values = list(verdicts.values())
    if "不符合" in values:
        return "不通过", "存在审核项判定为「不符合」"
    if any(v == "待整改" for v in values):
        return "有条件通过", "存在需限期整改项"
    if not values:
        return "", "尚未开始判定"
    if all(v in ("符合", "不适用", "") for v in values):
        return "通过", "已判定项全部符合"
    return "有条件通过", "存在未完成判定项"


def walkthrough_stats(samples, default_terms=None):
    """穿行测试统计：各控制点执行率、缺陷数、样本数、账期执行、建议结论。

    samples: [{seq, voucher_no, pay_date, terms_days, base_date, due_date,
               steps: {s1: {result, defect, ...}}}]
    default_terms: 表头填写的约定账期天数，样本未填时继承
    """
    step_map = {s["key"]: s["name"] for s in WALKTHROUGH_STEPS}
    stats = []
    total_defects = 0
    key_fail = []
    effective = 0

    for step in WALKTHROUGH_STEPS:
        key = step["key"]
        executed = partial = not_executed = na = 0
        defects = 0
        for sample in samples:
            cell = (sample.get("steps") or {}).get(key) or {}
            result = cell.get("result") or ""
            defect = cell.get("defect") or "无"
            if result == "执行":
                executed += 1
            elif result == "部分执行":
                partial += 1
            elif result == "未执行":
                not_executed += 1
            elif result == "不适用":
                na += 1
            # 缺陷数：判定为部分执行/未执行，或显式标注了缺陷类型，都记 1 项
            if defect != "无" or result in ("部分执行", "未执行"):
                defects += 1
        applicable = executed + partial + not_executed
        rate = round((executed + partial) / applicable * 100) if applicable else 0
        if executed == applicable and applicable:
            effective += 1
        if key in WALKTHROUGH_KEY_STEPS and not_executed:
            key_fail.append(step["name"])
        total_defects += defects
        stats.append({
            "key": key, "name": step_map[key], "stage": step["stage"],
            "executed": executed, "partial": partial,
            "not_executed": not_executed, "na": na,
            "applicable": applicable, "rate": rate, "defects": defects,
        })

    terms = terms_stats(samples, default_terms)

    if not samples:
        conclusion, reason = "", "尚无样本"
    elif key_fail:
        conclusion, reason = "部分有效", "关键控制点未执行：" + "、".join(key_fail)
    elif (terms["major"] or terms["bad_ratio"]):
        # 账期执行失效与关键控制点失效同级：合同约定的付款期限被系统性突破，
        # 说明「付款审批→付款执行」这条链上的期限约束没有真正起作用。
        detail = f"{terms['overdue']} 笔逾期（最长 {terms['max_delay']} 天）"
        conclusion, reason = "部分有效", "账期执行偏差：" + detail
    elif total_defects:
        conclusion, reason = "基本有效", f"存在 {total_defects} 项执行缺陷"
    elif terms["overdue"]:
        conclusion, reason = "基本有效", (
            f"存在 {terms['overdue']} 笔逾期付款（最长 {terms['max_delay']} 天），"
            "控制点本身执行到位"
        )
    else:
        conclusion, reason = "有效", "控制点全部执行且有证据支持"

    return {
        "steps": stats,
        "total_defects": total_defects,
        "sample_count": len(samples),
        "effective_steps": effective,
        "conclusion": conclusion,
        "reason": reason,
        "terms": terms,
    }
