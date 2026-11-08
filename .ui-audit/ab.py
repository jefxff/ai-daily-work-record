"""
A/B 对比渲染：在同一张真实页面上，分别以「现状」与「注入优化后」渲染并截图。
不修改任何项目文件——优化样式以 <style> 注入，仅用于验证建议是否真的有效。

输出：.ui-audit/ab/*.png
"""
import os

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:9000"
ROOT = os.path.dirname(os.path.abspath(__file__))
AB = os.path.join(ROOT, "ab")
os.makedirs(AB, exist_ok=True)

# ---------------------------------------------------------------- 优化样式 A
# 修复 1（台账不撑破页面轨道）：去掉页面级硬 min-width，表格回到轨道宽度
FIX_TABLE_FIT = """
@media (min-width: 961px) {
  .table-responsive table,
  .table-responsive table.table-ledger,
  .table-responsive table.dr-table,
  .table-responsive table.table--cards {
    min-width: 0 !important;
    width: 100% !important;
  }
  .table-responsive table :is(th, td) { min-width: 0; }
  /* 短值列不截断：分类 / 金额 / 状态 */
  .table-responsive table.table-ledger :is(th, td)[data-label="纠纷类型"],
  .table-responsive table.table-ledger :is(th, td)[data-label="涉案金额（元）"],
  .table-responsive table.table-ledger :is(th, td)[data-label="进度"] {
    white-space: nowrap;
  }
}
"""

# 修复 2（列预算）：轨道变窄时按业务优先级隐藏次要列，而不是压扁
# 注意：th 与 td 必须同时隐藏（data-label 只在 td 上），否则表头会残留空列
FIX_COLUMN_BUDGET = """
@media (min-width: 961px) {
  /* 关键列 px 保底（诉讼台账列序：1序号 2纠纷类型 3原告 4被告 5案号
     6金额 7法院/律师 8进度 9详细进度 10备注 11附件 12操作） */
  .table-responsive table.table-ledger > thead > tr > th:nth-child(2)  { width: 106px; }
  .table-responsive table.table-ledger > thead > tr > th:nth-child(3)  { width: 70px; }
  .table-responsive table.table-ledger > thead > tr > th:nth-child(4)  { width: 124px; }
  .table-responsive table.table-ledger > thead > tr > th:nth-child(5)  { width: 140px; }
  .table-responsive table.table-ledger > thead > tr > th:nth-child(6)  { width: 106px; }
  .table-responsive table.table-ledger > thead > tr > th:nth-child(7)  { width: 118px; }
  .table-responsive table.table-ledger > thead > tr > th:nth-child(8)  { width: 80px; }
  .table-responsive table.table-ledger > thead > tr > th:nth-child(12) { width: 116px; }
}
@media (min-width: 961px) and (max-width: 1340px) {
  .table-responsive table.table-ledger > thead > tr > th:nth-child(11),
  .table-responsive table.table-ledger > tbody > tr > td:nth-child(11) { display: none; }
}
@media (min-width: 961px) and (max-width: 1200px) {
  .table-responsive table.table-ledger > thead > tr > th:nth-child(9),
  .table-responsive table.table-ledger > tbody > tr > td:nth-child(9) { display: none; }
}
@media (min-width: 961px) and (max-width: 1040px) {
  .table-responsive table.table-ledger > thead > tr > th:nth-child(3),
  .table-responsive table.table-ledger > tbody > tr > td:nth-child(3) { display: none; }
}
"""

# 修复 3（小屏卡片模式重做）
FIX_CARD_MODE = """
@media (max-width: 640px) {
  /* 3.1 序号行不再占一整行，退化为右上角角标 */
  .table--cards td:first-child {
    position: absolute; top: 10px; right: 12px;
    background: none; border-radius: 0; padding: 0 !important;
    font-size: 12px; color: var(--c-text-secondary);
  }
  .table--cards tr { position: relative; padding-top: 14px; }
  .table--cards td:first-child::before { display: none; }

  /* 3.2 长文本字段：标签独占一行，值整行展开，杜绝徽标压字 */
  .table--cards td[data-label="工作内容"],
  .table--cards td[data-label="详细进度"],
  .table--cards td[data-label="受理法院 / 律师"],
  .table--cards td[data-label="案情说明"] {
    display: block;
  }
  .table--cards td[data-label="工作内容"]::before,
  .table--cards td[data-label="详细进度"]::before,
  .table--cards td[data-label="受理法院 / 律师"]::before,
  .table--cards td[data-label="案情说明"]::before {
    display: block; width: 100%; text-align: left;
    margin-bottom: 2px; font-size: 12px; font-weight: 600;
  }
  .table--cards td[data-label="工作内容"] > *,
  .table--cards td[data-label="详细进度"] > *,
  .table--cards td[data-label="受理法院 / 律师"] > * { display: block; width: 100%; }

  /* 3.3 徽标与标签字号下限 12px（可读性） */
  .table--cards :is(.badge, .status-badge, .kb-chip, .cell-stack__main, .cell-stack__sub) {
    font-size: 12px !important;
  }

  /* 3.4 操作行：图标按钮 → 带文字标签、44px 触控区 */
  .table--cards td[data-label="操作"] {
    display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-start;
    padding-top: 12px !important;
  }
  .table--cards td[data-label="操作"]::before { display: none; }
  .table--cards td[data-label="操作"] :is(a, button, .btn) {
    min-height: 44px; min-width: 44px;
    display: inline-flex; align-items: center; gap: 6px;
    padding: 0 14px; font-size: 13px;
    border-radius: var(--r-sm);
  }
  .table--cards td[data-label="操作"] :is(a, button, .btn)::after {
    content: attr(data-act-label);
    font-size: 13px;
  }
}
"""

# 修复 4（抽屉遮罩）
FIX_SCRIM = """
body.sidebar-open .sidebar-scrim { display: block !important; }
"""

# 修复 5（悬浮按钮不遮挡内容）
FIX_FAB = """
.copilot-launcher { bottom: 16px !important; }
@media (max-width: 640px) {
  .copilot-launcher { bottom: 12px !important; right: 12px !important; }
}
"""


def login(pg):
    pg.goto(f"{BASE}/accounts/login/", wait_until="domcontentloaded")
    pg.fill("input[name=username]", "admin")
    pg.fill("input[name=password]", os.environ["UI_AUDIT_PASSWORD"])
    pg.click("button[type=submit], input[type=submit]")
    pg.wait_for_load_state("networkidle")


def shot(pg, name):
    pg.screenshot(path=os.path.join(AB, name), full_page=False)
    print("   saved", name)


def hscroll(pg):
    return pg.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")


def run():
    with sync_playwright() as p:
        b = p.chromium.launch()

        # ---------- 对比 1：1180px 笔记本 —— 整页横向溢出 ----------
        ctx = b.new_context(viewport={"width": 1180, "height": 900})
        pg = ctx.new_page()
        login(pg)
        pg.goto(f"{BASE}/complaint/", wait_until="networkidle")
        pg.wait_for_timeout(600)
        print("P1 before 横向溢出 =", hscroll(pg), "px")
        shot(pg, "P1_1180_before.png")
        pg.add_style_tag(content=FIX_TABLE_FIT + FIX_COLUMN_BUDGET)
        pg.wait_for_timeout(500)
        print("P1 after  横向溢出 =", hscroll(pg), "px")
        shot(pg, "P1_1180_after.png")
        ctx.close()

        # ---------- 对比 2：1440px —— 列被压扁 / 数据不可读 ----------
        ctx = b.new_context(viewport={"width": 1440, "height": 900})
        pg = ctx.new_page()
        login(pg)
        pg.goto(f"{BASE}/complaint/", wait_until="networkidle")
        pg.wait_for_timeout(600)
        print("P2 before 横向溢出 =", hscroll(pg), "px")
        shot(pg, "P2_1440_before.png")
        pg.add_style_tag(content=FIX_TABLE_FIT + FIX_COLUMN_BUDGET)
        pg.wait_for_timeout(500)
        print("P2 after  横向溢出 =", hscroll(pg), "px")
        shot(pg, "P2_1440_after.png")
        ctx.close()

        # ---------- 对比 3：390px 卡片模式 ----------
        ctx = b.new_context(viewport={"width": 390, "height": 844})
        pg = ctx.new_page()
        login(pg)
        pg.goto(f"{BASE}/daily_record/", wait_until="networkidle")
        pg.wait_for_timeout(600)
        shot(pg, "P3_390_before.png")
        # 给操作按钮补可读标签（JS，仅渲染层）
        pg.evaluate("""() => {
          const map = { 'detail': '查看', 'edit': '编辑', 'delete': '删除' };
          document.querySelectorAll('.table--cards td[data-label="操作"] a, .table--cards td[data-label="操作"] button').forEach(el => {
            const href = el.getAttribute('href') || '';
            const key = Object.keys(map).find(k => href.includes(k));
            if (key) el.setAttribute('data-act-label', map[key]);
          });
        }""")
        pg.add_style_tag(content=FIX_CARD_MODE)
        pg.wait_for_timeout(500)
        shot(pg, "P3_390_after.png")

        # ---------- 对比 4：390px 抽屉遮罩 ----------
        pg.goto(f"{BASE}/daily_record/", wait_until="networkidle")
        pg.wait_for_timeout(400)
        pg.click("#mobileMenuToggle")
        pg.wait_for_timeout(700)
        shot(pg, "P4_390_before_drawer.png")
        pg.add_style_tag(content=FIX_SCRIM)
        pg.wait_for_timeout(400)
        shot(pg, "P4_390_after_drawer.png")
        ctx.close()

        b.close()


if __name__ == "__main__":
    run()
