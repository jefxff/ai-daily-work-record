"""模型链路金样集：**必须交给模型**的那批问法。

为什么单独一批：303 条矩阵证明的是"能确定性回答的都答对了"，它刻意不碰
模型。可用户真正会问的长尾——跨模块关联、条件叠加、模糊指代——全在模型
那一侧，此前完全没有基线，所以"有时聪明有时离谱"只能靠感觉。

判定只断言可机械验证的部分（工具、模块、禁词、非空），**不断言措辞**：
把"答案好不好"写成正则只会制造假绿或假红，主观质量靠人看报告。

每条可选 ``prelude``：先问一句建立上下文，再问真正的追问——这类用例是给
槽位记忆（B2-2）用的，单轮跑必然失败。
"""

from dataclasses import dataclass

# 这些词出现即说明模型在推脱：不是"没有数据"，而是"没干活"。
DEFAULT_FORBID = ('无法回答', '不知道该怎么', '我无法处理', '抱歉，我不能')


@dataclass(frozen=True)
class ModelCase:
    question: str
    tools: tuple = ()
    keywords: tuple = ()
    forbid: tuple = DEFAULT_FORBID
    scope: str = ''
    prelude: str = ''
    note: str = ''


MODEL_CASES = (
    # —— 客户客诉 ——
    ModelCase('华建建材今年的客诉都处理完了吗',
              ('query_records', 'aggregate_records'), ('客诉',), scope='customer_complaint'),
    ModelCase('客诉里赔偿金额最高和最低相差多少',
              ('aggregate_records', 'query_records'), ('赔偿',), scope='customer_complaint'),
    ModelCase('哪些客户的客诉同时涉及法律程序',
              ('query_records',), ('客诉', '法律'), scope='customer_complaint'),
    ModelCase('客诉处理平均要几天',
              ('aggregate_records', 'query_records'), ('客诉',), scope='customer_complaint'),
    # —— 诉讼案件 ——
    ModelCase('诉讼案件里立案还没结案的有几件',
              ('aggregate_records', 'query_records'), ('诉讼',), scope='complaint'),
    ModelCase('涉案金额超过一百万的案子有哪些',
              ('query_records',), ('诉讼', '涉案'), scope='complaint'),
    ModelCase('被告和我们的客户名单对得上吗',
              ('query_records', 'global_search'), ('被告',), scope='complaint'),
    ModelCase('今年的诉讼比去年多还是少',
              ('aggregate_records', 'query_records'), ('诉讼',), scope='complaint'),
    # —— 保险 ——
    ModelCase('今年到期的保单里哪些已经续保了',
              ('query_records',), ('保',), scope='insurance'),
    ModelCase('各家保险公司的保费占比是多少',
              ('aggregate_records',), ('保费', '保险'), scope='insurance'),
    ModelCase('帮我对比一下今年和去年的保费',
              ('aggregate_records', 'query_records'), ('保费',), scope='insurance'),
    ModelCase('这几张保单里哪张保额最低',
              ('query_records', 'aggregate_records'), ('保额', '保单'), scope='insurance'),
    # —— 客户台账 / 账期 ——
    ModelCase('账期 60 天以上的客户有哪些',
              ('query_records',), ('客户', '账期'), scope='customer_ledger'),
    ModelCase('注册资本最高的三家客户是谁',
              ('query_records',), ('注册资本', '客户'), scope='customer_ledger'),
    ModelCase('有没有重复录入的客户',
              ('query_records', 'global_search'), ('客户',), scope='customer_ledger'),
    ModelCase('哪些客户申请了账期但还没批',
              ('query_records',), ('账期',), scope='customer_credit'),
    ModelCase('账期申请平均几天批下来',
              ('aggregate_records', 'query_records'), ('账期',), scope='customer_credit'),
    # —— 装修 ——
    ModelCase('装修项目里还没完工的有几个',
              ('aggregate_records', 'query_records'), ('装修',), scope='decoration'),
    ModelCase('装修项目和客户台账能对上吗',
              ('query_records', 'global_search'), ('装修',), scope='decoration'),
    # —— 日报 / 制度 ——
    ModelCase('这周谁还没写日报',
              ('query_records',), ('日报',), scope='daily_record'),
    ModelCase('上个月日报里提到最多的问题是什么',
              ('search_knowledge', 'query_records'), ('日报',), scope='daily_record'),
    ModelCase('制度里关于考勤是怎么规定的',
              ('search_knowledge',), ('考勤',), scope='regulation'),
    ModelCase('处罚条款一共有几条',
              ('query_records', 'aggregate_records'), ('处罚',), scope='regulation'),
    ModelCase('哪些制度最近改过',
              ('query_records', 'search_knowledge'), ('制度',), scope='regulation'),
    # —— 核价 / 金属价 ——
    ModelCase('核价记录里供应商报价最低的是哪家',
              ('query_records',), ('报价', '供应商'), scope='price_evaluation'),
    ModelCase('铜价这个月和上个月比涨了多少',
              ('aggregate_records', 'query_records'), ('铜',), scope='metal_price'),
    ModelCase('最近一个月哪种金属涨得最多',
              ('query_records', 'aggregate_records'), ('金属',), scope='metal_price'),
    # —— 试样 / 试产 / 审厂 / 合作方 ——
    ModelCase('试样里还在测试中的有几个',
              ('aggregate_records', 'query_records'), ('试样',), scope='sample'),
    ModelCase('测试不合格的试样接下来怎么处理',
              ('query_records', 'search_knowledge'), ('试样', '测试'), scope='sample_test'),
    ModelCase('试产批次和试样能对应上吗',
              ('query_records',), ('试产', '试样'), scope='sample_batch'),
    ModelCase('审厂没通过的供应商有哪些',
              ('query_records',), ('审厂',), scope='sample_audit'),
    ModelCase('合作方里做表面处理的有几家',
              ('aggregate_records', 'query_records'), ('合作方',), scope='sample_partner'),
    # —— 档案 / 证照 / 借阅 ——
    ModelCase('档案里借出去还没还的有哪些',
              ('query_records',), ('档案',), scope='archive_borrow'),
    ModelCase('营业执照快到期的是哪几家',
              ('query_records',), ('执照', '证照'), scope='license'),
    ModelCase('年审快到的证照有哪些',
              ('query_records',), ('年审', '证照'), scope='license_review'),
    ModelCase('借阅超期的档案是谁借的',
              ('query_records',), ('借阅',), scope='license_borrow'),
    ModelCase('档案和证照是同一份文件吗',
              ('query_records', 'global_search'), ('档案', '证照'), scope='archive'),
    ModelCase('逾期未还的档案和超期的证照一共多少',
              ('aggregate_records', 'query_records'), ('档案', '证照'), scope='archive'),
    # —— 知识库 ——
    ModelCase('知识库里讲报销流程的文档',
              ('search_knowledge',), ('报销',), scope='knowledge'),
    ModelCase('最近发布的制度类文档有哪些',
              ('search_knowledge', 'query_records'), ('制度', '文档'), scope='knowledge'),
    ModelCase('知识库里哪篇文档被收藏得最多',
              ('search_knowledge', 'query_records'), ('知识',), scope='knowledge'),
    # —— 跨模块关联（本地链路一定接管不了，最能验证模型） ——
    ModelCase('客诉最多的客户在我们台账里是什么等级',
              ('query_records', 'aggregate_records'), ('客户',), scope='customer_ledger'),
    ModelCase('诉讼和客诉是不是同一批客户',
              ('query_records', 'global_search'), ('客户',), scope='complaint'),
    ModelCase('保险到期的客户里有多少也有客诉',
              ('query_records', 'aggregate_records'), ('保险', '客诉'), scope='insurance'),
    ModelCase('哪些客户既有诉讼又有账期申请',
              ('query_records', 'global_search'), ('客户',), scope='complaint'),
    ModelCase('本季度新增客户里有多少提了客诉',
              ('query_records', 'aggregate_records'), ('客户', '客诉'),
              scope='customer_complaint'),
    ModelCase('日报里提到的问题和客诉记录对得上吗',
              ('query_records', 'search_knowledge'), ('日报', '客诉'), scope='daily_record'),
    ModelCase('哪个月份的客诉最多，集中在哪些客户',
              ('aggregate_records', 'query_records'), ('客诉',), scope='customer_complaint'),
    ModelCase('我们的供应商和客户有重叠吗',
              ('query_records', 'global_search'), ('供应商',), scope='customer_ledger'),
    ModelCase('装修项目和试样有对应关系吗',
              ('query_records',), ('装修', '试样'), scope='decoration'),
    # —— 模糊指代：必须靠上一轮上下文 ——
    ModelCase('那这个客户的联系方式呢', ('query_records',), ('客户',),
              scope='customer_ledger', prelude='华建建材的客诉有哪些',
              note='指代"这个客户"应继承华建建材'),
    ModelCase('上个月的记录呢', ('query_records', 'aggregate_records'), ('记录',),
              scope='customer_complaint', prelude='华建建材的客诉有哪些'),
    ModelCase('那已完成的呢', ('query_records',), ('完成',),
              scope='customer_complaint', prelude='华建建材的客诉有哪些'),
    ModelCase('按客户分呢', ('aggregate_records',), ('客户',),
              scope='customer_complaint', prelude='客诉一共有多少'),
    ModelCase('他还有别的投诉吗', ('query_records', 'global_search'), ('投诉', '客诉'),
              scope='customer_complaint', prelude='华建建材的客诉有哪些'),
    ModelCase('这个金额是怎么算的', ('query_records', 'aggregate_records'), ('金额',),
              scope='insurance', prelude='保费一共多少钱'),
    ModelCase('有没有更早的', ('query_records',), ('记录', '条'),
              scope='complaint', prelude='诉讼案件里立案还没结案的有几件'),
    ModelCase('还有别的供应商吗', ('query_records',), ('供应商',),
              scope='sample_partner', prelude='合作方里做表面处理的有几家'),
    # —— 综合 / 需要多步 ——
    ModelCase('今年客诉最多的三个客户，各自的诉讼情况怎么样',
              ('query_records', 'aggregate_records'), ('客诉', '诉讼'),
              scope='customer_complaint'),
    ModelCase('把今年到期的保单按保险公司列出来',
              ('query_records', 'aggregate_records'), ('保险',), scope='insurance'),
    ModelCase('哪些客户的账期快到期了而且要续签合同',
              ('query_records',), ('账期',), scope='customer_credit'),
)


def cases():
    """返回内置金样集（供命令 seed 进 ``CopilotEvalCase``）。"""
    return MODEL_CASES
