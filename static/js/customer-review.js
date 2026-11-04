/* ============================================================================
   customer-review.js · 客户评审 / 付款穿行测试的交互
   ----------------------------------------------------------------------------
   职责边界：
   - 只做「录入时的实时反馈」：合计分、各维度小计、财务比率、审核结论建议。
   - 不做校验的最终裁决：保存时后端按同一套规则（records/credit_spec.py）
     重算并收敛分值，前端算错不会写进库。
   - 不做 AJAX 保存：沿用站点统一的全页表单提交，避免两套错误提示。
   ========================================================================== */
(function () {
  'use strict';

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function readJSON(id, fallback) {
    var el = document.getElementById(id);
    if (!el) return fallback;
    try { return JSON.parse(el.textContent); } catch (e) { return fallback; }
  }

  function num(value) {
    if (value === '' || value === null || value === undefined) return null;
    var n = parseFloat(String(value).replace(/,/g, ''));
    return isNaN(n) ? null : n;
  }

  function fmt(value, kind) {
    if (value === null || value === undefined || isNaN(value)) return '—';
    if (kind === 'pct') return value.toFixed(1) + '%';
    if (kind === 'ratio') return value.toFixed(2);
    return value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  /* ------------------------------------------------------------------ */
  /* 一、评分表                                                          */
  /* ------------------------------------------------------------------ */
  function initScoreForm() {
    var form = $('#review-form');
    if (!form) return;

    var grades = readJSON('grade-spec', []);
    var selects = $$('select[data-score]', form);
    var totalEl = $('[data-total]');
    var trackEl = $('[data-track]');
    var gradeEl = $('[data-grade]');
    var gradeAdviceEl = $('[data-grade-advice]');
    var missEl = $('[data-miss]');
    var dimEls = $$('[data-dim-got]');

    function gradeOf(total) {
      for (var i = 0; i < grades.length; i++) {
        if (total >= grades[i].min) return grades[i];
      }
      return grades[grades.length - 1] || null;
    }

    function recalc() {
      var total = 0;
      var missing = 0;
      var byGroup = {};

      selects.forEach(function (sel) {
        var group = sel.getAttribute('data-group');
        var max = parseInt(sel.getAttribute('data-max'), 10) || 0;
        var value = sel.value === '' ? null : parseInt(sel.value, 10);
        if (value === null) missing += 1; else total += value;
        if (!byGroup[group]) byGroup[group] = { got: 0, max: 0 };
        byGroup[group].max += max;
        if (value !== null) byGroup[group].got += value;
        sel.classList.toggle('is-set', value !== null);
      });

      if (totalEl) totalEl.textContent = total;
      if (trackEl) {
        var possible = 0;
        selects.forEach(function (s) { possible += parseInt(s.getAttribute('data-max'), 10) || 0; });
        trackEl.style.width = (possible ? Math.min(100, total / possible * 100) : 0) + '%';
      }

      dimEls.forEach(function (el) {
        var key = el.getAttribute('data-dim-got');
        var data = byGroup[key];
        if (!data) return;
        el.innerHTML = '<b>' + data.got + '</b> / ' + data.max;
      });
      $$('[data-dim-bar]').forEach(function (bar) {
        var key = bar.getAttribute('data-dim-bar');
        var data = byGroup[key];
        if (!data || !data.max) return;
        bar.style.width = Math.min(100, data.got / data.max * 100) + '%';
      });

      var grade = gradeOf(total);
      if (gradeEl && grade) {
        gradeEl.className = 'cr-grade cr-grade--' + grade.tone;
        gradeEl.innerHTML = '<b>' + grade.label + '</b><span>' + grade.range + '</span>';
      }
      if (gradeAdviceEl && grade) gradeAdviceEl.textContent = grade.advice || '';

      if (missEl) {
        if (missing > 0) {
          missEl.className = 'cr-miss';
          missEl.textContent = '尚有 ' + missing + ' 项未评分，当前合计仅供试算，不作为评定结论。';
        } else {
          missEl.className = 'cr-miss is-ok';
          missEl.textContent = '全部评分项均已评分，可提交复核。';
        }
      }
    }

    // 下拉框在窄列里只能显示截断的条件，选中后把完整档位说明放到下方，
    // 避免「选了 3 分但不知道 3 分对应什么条件」。
    function syncDesc(sel) {
      var desc = document.querySelector('[data-desc="' + sel.name.replace('score_', '') + '"]');
      if (!desc) return;
      if (!sel.value) { desc.textContent = ''; return; }
      var option = sel.options[sel.selectedIndex];
      var text = option ? option.textContent : '';
      var pos = text.indexOf('·');
      desc.textContent = pos >= 0 ? text.slice(pos + 1).trim() : text;
    }

    selects.forEach(function (sel) {
      syncDesc(sel);
      sel.addEventListener('change', function () {
        // 手工改过该项后，财务区自动建议不再覆盖
        sel.setAttribute('data-manual', '1');
        syncDesc(sel);
        recalc();
      });
    });
    recalc();

    initFinance(recalc);
    initSuggestion(selects, recalc);
  }

  /* ------------------------------------------------------------------ */
  /* 二、财务基础数据 → 比率                                           */
  /* ------------------------------------------------------------------ */
  // 比率与建议分的对应关系：与后端 credit_spec.suggest_fin_scores 保持同口径
  function suggestFor(key, r) {
    if (key === 'debt_ratio' && r.debt_ratio !== null) {
      var d = r.debt_ratio;
      if (d <= 40) return 8;
      if (d <= 50) return 7;
      if (d <= 60) return 6;
      if (d <= 70) return 4;
      if (d <= 80) return 2;
      return 0;
    }
    if (key === 'liquidity' && r.current_ratio !== null) {
      var cr = r.current_ratio, qr = r.quick_ratio;
      if (qr !== null && qr < 0.6) return 0;
      if (cr >= 2.0 && (qr || 0) >= 1.2) return 7;
      if (cr >= 1.5 && (qr || 0) >= 1.0) return 6;
      if (cr >= 1.2 && (qr || 0) >= 0.8) return 5;
      if (cr >= 1.0 || ((qr || 0) >= 0.6 && (qr || 0) < 0.8)) return 3;
      if (cr >= 0.8) return 1;
      return 0;
    }
    if (key === 'revenue_growth' && r.revenue_growth !== null) {
      var g = r.revenue_growth;
      if (g >= 20) return 5;
      if (g >= 10) return 4;
      if (g >= 0) return 3;
      if (g >= -10) return 2;
      if (g >= -30) return 1;
      return 0;
    }
    if (key === 'net_margin' && r.net_margin !== null) {
      var m = r.net_margin;
      if (m >= 10) return 5;
      if (m >= 5) return 4;
      if (m >= 2) return 3;
      if (m >= 0) return 2;
      if (m >= -5) return 1;
      return 0;
    }
    return null;
  }

  function initFinance(afterChange) {
    var box = $('[data-finance]');
    if (!box) return;
    var inputs = $$('input[data-fin]', box);
    if (!inputs.length) return;

    function values() {
      var out = {};
      inputs.forEach(function (input) {
        out[input.getAttribute('data-fin')] = num(input.value);
      });
      return out;
    }

    function compute(v) {
      function div(a, b) { return (a === null || b === null || b === 0) ? null : a / b; }
      var r = {};
      r.net_assets = (v.total_assets !== null && v.total_liabilities !== null)
        ? v.total_assets - v.total_liabilities : null;
      r.debt_ratio = (function (x) { return x === null ? null : x * 100; })(div(v.total_liabilities, v.total_assets));
      r.current_ratio = div(v.current_assets, v.current_liabilities);
      r.quick_ratio = v.current_assets === null ? null
        : div(v.current_assets - (v.inventory || 0), v.current_liabilities);
      r.cash_ratio = div(v.cash, v.current_liabilities);
      r.ar_turnover = div(v.revenue, v.accounts_receivable);
      r.revenue_growth = (function (x) { return x === null ? null : x * 100; })(div(v.revenue - v.prev_revenue, v.prev_revenue));
      r.net_margin = (function (x) { return x === null ? null : x * 100; })(div(v.net_profit, v.revenue));
      r.cash_debt_ratio = (function (x) { return x === null ? null : x * 100; })(div(v.operating_cash_flow, v.interest_debt));
      return r;
    }

    function tone(key, value) {
      if (value === null) return '';
      if (key === 'debt_ratio') {
        if (value <= 40) return 'is-good';
        if (value <= 60) return '';
        if (value <= 80) return 'is-warn';
        return 'is-bad';
      }
      if (key === 'current_ratio' || key === 'quick_ratio') {
        if (value >= 1.5) return 'is-good';
        if (value >= 1.0) return '';
        return 'is-bad';
      }
      if (key === 'net_margin' || key === 'revenue_growth') {
        if (value >= 5) return 'is-good';
        if (value >= 0) return '';
        return 'is-bad';
      }
      if (key === 'cash_debt_ratio') {
        if (value >= 20) return 'is-good';
        if (value >= 0) return '';
        return 'is-bad';
      }
      return '';
    }

    var kinds = {};
    $$('[data-ratio]', box).forEach(function (el) {
      kinds[el.getAttribute('data-ratio')] = el.getAttribute('data-kind') || 'amount';
    });

    var lastRatios = {};

    function refresh() {
      var r = compute(values());
      lastRatios = r;
      Object.keys(r).forEach(function (key) {
        var el = $('[data-ratio="' + key + '"]', box);
        if (!el) return;
        var value = r[key];
        el.textContent = fmt(value, kinds[key]);
        el.className = 'cr-ratio__value' + (value === null ? ' is-empty' : ' ' + tone(key, value));
      });

      var fillBtn = $('[data-fill-fin]', box);
      if (fillBtn) {
        var hasAny = Object.keys(r).some(function (k) { return r[k] !== null; });
        fillBtn.disabled = !hasAny;
      }
      autoApply(r);
    }

    function autoApply(r) {
      var selects = $$('select[data-fin-score]');
      selects.forEach(function (sel) {
        if (sel.getAttribute('data-manual') === '1') return;
        var score = suggestFor(sel.getAttribute('data-fin-score'), r);
        if (score === null) return;
        if (String(score) !== sel.value) {
          sel.value = String(score);
          var desc = document.querySelector('[data-desc="' + sel.name.replace('score_', '') + '"]');
          if (desc) {
            var opt = sel.options[sel.selectedIndex];
            var txt = opt ? opt.textContent : '';
            var pos = txt.indexOf('·');
            desc.textContent = pos >= 0 ? txt.slice(pos + 1).trim() : txt;
          }
          if (afterChange) afterChange();
        }
      });
    }

    inputs.forEach(function (input) {
      input.addEventListener('input', refresh);
      input.addEventListener('change', refresh);
    });
    refresh();
  }

  function initSuggestion(selects, recalc) {
    var btn = $('[data-fill-fin]');
    if (!btn) return;
    btn.addEventListener('click', function () {
      // 一键按建议值填入 4 个财务评分项，并清除「手工改过」标记
      var r = {};
      $$('[data-fin]').forEach(function (i) { r[i.getAttribute('data-fin')] = num(i.value); });
      // 重新计算一次，逻辑与 initFinance 内部保持一致
      function div(a, b) { return (a === null || b === null || b === 0) ? null : a / b; }
      var ratios = {
        debt_ratio: (function (x) { return x === null ? null : x * 100; })(div(r.total_liabilities, r.total_assets)),
        current_ratio: div(r.current_assets, r.current_liabilities),
        quick_ratio: r.current_assets === null ? null : div(r.current_assets - (r.inventory || 0), r.current_liabilities),
        revenue_growth: (function (x) { return x === null ? null : x * 100; })(div(r.revenue - r.prev_revenue, r.prev_revenue)),
        net_margin: (function (x) { return x === null ? null : x * 100; })(div(r.net_profit, r.revenue))
      };
      var applied = 0;
      selects.forEach(function (sel) {
        var key = sel.getAttribute('data-fin-score');
        if (!key) return;
        var score = suggestFor(key, ratios);
        if (score === null) return;
        sel.value = String(score);
        sel.removeAttribute('data-manual');
        applied += 1;
      });
      if (recalc) recalc();
      if (window.showToast) {
        window.showToast(applied ? ('已按财务比率填入 ' + applied + ' 项建议分') : '财务数据不完整，暂无法给出建议分');
      }
    });
  }

  /* ------------------------------------------------------------------ */
  /* 三、现场审核                                                        */
  /* ------------------------------------------------------------------ */
  function initAuditForm() {
    var box = $('#audit-form');
    if (!box) return;

    var rows = $$('.au-row', box);
    var resultEl = $('[data-audit-conclusion]');
    var reasonEl = $('[data-audit-reason]');

    function refresh() {
      var verdicts = {};
      var hasFail = false;
      var hasFix = false;
      var undecided = 0;

      rows.forEach(function (row) {
        var checked = $('input[type="radio"]:checked', row);
        var verdict = checked ? checked.value : '';
        verdicts[verdict] = true;
        row.classList.remove('is-fail', 'is-fix');
        if (verdict === '不符合') { hasFail = true; row.classList.add('is-fail'); }
        else if (verdict === '待整改') { hasFix = true; row.classList.add('is-fix'); }
        if (!verdict) undecided += 1;
      });

      var conclusion = '', reason = '';
      if (hasFail) { conclusion = '不通过'; reason = '存在审核项判定为「不符合」'; }
      else if (hasFix) { conclusion = '有条件通过'; reason = '存在需限期整改项'; }
      else if (undecided === rows.length) { conclusion = ''; reason = '尚未开始判定'; }
      else if (undecided > 0) { conclusion = '有条件通过'; reason = '尚有 ' + undecided + ' 项未判定'; }
      else { conclusion = '通过'; reason = '已判定项全部符合'; }

      if (resultEl) {
        resultEl.textContent = conclusion || '—';
        resultEl.className = 'cr-grade' + (conclusion === '通过' ? ' cr-grade--a'
          : conclusion === '有条件通过' ? ' cr-grade--c'
            : conclusion === '不通过' ? ' cr-grade--d' : '');
      }
      if (reasonEl) reasonEl.textContent = reason;
    }

    $$('input[type="radio"]', box).forEach(function (input) {
      input.addEventListener('change', refresh);
    });
    refresh();
  }

  /* ------------------------------------------------------------------ */
  /* 四、付款穿行测试                                                    */
  /* ------------------------------------------------------------------ */
  function initWalkthrough() {
    var root = $('#walkthrough-form');
    if (!root) return;

    var list = $('[data-samples]', root);
    var tpl = document.getElementById('sample-template');
    var hidden = $('[data-samples-json]', root);
    if (!list || !tpl || !hidden) return;

    var steps = readJSON('step-spec', []);
    var counter = 0;

    // 账期核对的三个口径与后端 credit_spec.py 保持一致：
    // 宽限 3 天、单笔逾期超 30 天或逾期占比过半即认定账期控制失效
    var defaultTerms = num(root.getAttribute('data-default-terms'));
    var grace = num(root.getAttribute('data-grace'));
    if (grace === null) grace = 3;
    var MAJOR_DAYS = 30;
    var BAD_RATIO = 0.5;

    function parseDay(text) {
      var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(text || '').trim());
      if (!m) return null;
      return new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
    }

    function fmtDay(d) {
      if (!d) return '';
      var mm = String(d.getUTCMonth() + 1).padStart(2, '0');
      var dd = String(d.getUTCDate()).padStart(2, '0');
      return d.getUTCFullYear() + '-' + mm + '-' + dd;
    }

    function plusDays(d, n) {
      return new Date(d.getTime() + n * 86400000);
    }

    /* 单笔样本的账期核对：天数→到期日→偏差，与后端 sample_terms() 同口径 */
    function termsOf(node) {
      var daysInput = $('[data-f="terms_days"]', node);
      var days = num(daysInput ? daysInput.value : '');
      if (days === null) days = defaultTerms;
      var base = parseDay(($('[data-f="base_date"]', node) || {}).value);
      var due = parseDay(($('[data-f="due_date"]', node) || {}).value);
      if (!due && base && days !== null) due = plusDays(base, days);
      var pay = parseDay(($('[data-f="pay_date"]', node) || {}).value);
      var delay = (due && pay) ? Math.round((pay - due) / 86400000) : null;
      var status = '';
      if (delay !== null) {
        if (delay > grace) status = '逾期';
        else if (delay < 0) status = '提前';
        else status = '按期';
      }
      return { days: days, base: base, due: due, pay: pay, delay: delay, status: status };
    }

    /* 回写到期日与偏差徽标。
       auto=true 时按「起算日 + 天数」推算到期日，但只在到期日为空或由系统
       自动填过（data-auto）时才覆盖，手工填写的到期日不动。 */
    function syncTerms(node, auto) {
      var dueInput = $('[data-f="due_date"]', node);
      var autoFilled = dueInput && dueInput.getAttribute('data-auto') === '1';
      // 注意：termsOf() 会在内存里按起算日推算出到期日，判断「输入框是否为空」
      // 必须看 DOM 的值，不能看 termsOf 的返回值，否则自动回填永远不触发
      var typedDue = dueInput ? dueInput.value : '';
      var info = termsOf(node);
      if (auto && (!typedDue || autoFilled) && info.base && info.days !== null) {
        var computed = plusDays(info.base, info.days);
        if (dueInput) {
          dueInput.value = fmtDay(computed);
          dueInput.setAttribute('data-auto', '1');
        }
        info.due = computed;
        info.delay = info.pay ? Math.round((info.pay - computed) / 86400000) : null;
        info.status = info.delay === null ? ''
          : (info.delay > grace ? '逾期' : (info.delay < 0 ? '提前' : '按期'));
      }
      var badge = $('[data-terms-delta]', node);
      if (!badge) return info;
      if (info.delay === null) {
        badge.textContent = info.base ? '待填付款日' : '未填账期';
        badge.className = 'wt-terms__delta';
      } else if (info.status === '逾期') {
        badge.textContent = '逾期 ' + info.delay + ' 天';
        badge.className = 'wt-terms__delta is-bad';
      } else if (info.status === '提前') {
        badge.textContent = '提前 ' + Math.abs(info.delay) + ' 天';
        badge.className = 'wt-terms__delta is-early';
      } else {
        badge.textContent = '按期' + (info.delay ? '（+' + info.delay + ' 天）' : '');
        badge.className = 'wt-terms__delta is-ok';
      }
      return info;
    }

    function addSample(data) {
      counter += 1;
      var html = tpl.innerHTML.replace(/__IDX__/g, String(counter));
      var wrap = document.createElement('div');
      wrap.innerHTML = html.trim();
      var node = wrap.firstElementChild;
      list.appendChild(node);

      if (data) {
        var no = $('[data-f="voucher_no"]', node);
        var date = $('[data-f="pay_date"]', node);
        var amount = $('[data-f="amount"]', node);
        var payee = $('[data-f="payee"]', node);
        if (no) no.value = data.voucher_no || '';
        if (date) date.value = data.pay_date || '';
        if (amount) amount.value = data.amount || '';
        if (payee) payee.value = data.payee || '';
        var tDays = $('[data-f="terms_days"]', node);
        var tBase = $('[data-f="base_date"]', node);
        var tDue = $('[data-f="due_date"]', node);
        if (tDays) tDays.value = (data.terms_days === null || data.terms_days === undefined)
          ? '' : data.terms_days;
        if (tBase) tBase.value = data.base_date || '';
        if (tDue) tDue.value = data.due_date || '';
        var cellSteps = data.steps || {};
        Object.keys(cellSteps).forEach(function (key) {
          var cell = $('.wt-step[data-step="' + key + '"]', node);
          if (!cell) return;
          var c = cellSteps[key] || {};
          if (c.result) {
            var radio = $('input[type="radio"][value="' + c.result + '"]', cell);
            if (radio) radio.checked = true;
          }
          ['doc_no', 'doc_date', 'amount', 'note'].forEach(function (f) {
            var input = $('[data-f="' + f + '"]', cell);
            if (input && c[f] !== undefined) input.value = c[f] || '';
          });
          var defect = $('[data-f="defect"]', cell);
          if (defect && c.defect) defect.value = c.defect;
        });
      }
      renumber();
      syncTerms(node, true);
      refreshStats();
      return node;
    }

    function renumber() {
      $$('.wt-sample', list).forEach(function (node, index) {
        var badge = $('.wt-sample__no', node);
        if (badge) badge.textContent = String(index + 1);
      });
    }

    function collect() {
      return $$('.wt-sample', list).map(function (node, index) {
        var sample = {
          seq: index + 1,
          voucher_no: ($('[data-f="voucher_no"]', node) || {}).value || '',
          pay_date: ($('[data-f="pay_date"]', node) || {}).value || '',
          amount: ($('[data-f="amount"]', node) || {}).value || '',
          payee: ($('[data-f="payee"]', node) || {}).value || '',
          // 留空表示继承主表账期，不要在这里把默认值写死成样本自己的值，
          // 否则表头改账期时这些样本不会跟着变
          terms_days: ($('[data-f="terms_days"]', node) || {}).value || '',
          base_date: ($('[data-f="base_date"]', node) || {}).value || '',
          due_date: ($('[data-f="due_date"]', node) || {}).value || '',
          steps: {}
        };
        $$('.wt-step', node).forEach(function (cell) {
          var key = cell.getAttribute('data-step');
          var checked = $('input[type="radio"]:checked', cell);
          sample.steps[key] = {
            result: checked ? checked.value : '',
            defect: ($('[data-f="defect"]', cell) || {}).value || '无',
            doc_no: ($('[data-f="doc_no"]', cell) || {}).value || '',
            doc_date: ($('[data-f="doc_date"]', cell) || {}).value || '',
            amount: ($('[data-f="amount"]', cell) || {}).value || '',
            note: ($('[data-f="note"]', cell) || {}).value || ''
          };
        });
        return sample;
      });
    }

    function refreshStats() {
      var samples = collect();
      var total = samples.length;
      var keySteps = (root.getAttribute('data-key-steps') || '').split(',').filter(Boolean);
      var keyFail = [];
      var defects = 0;

      steps.forEach(function (step) {
        var executed = 0, partial = 0, notExecuted = 0, na = 0;
        samples.forEach(function (sample) {
          var cell = (sample.steps || {})[step.key] || {};
          var result = cell.result || '';
          var defect = cell.defect || '无';
          if (result === '执行') executed += 1;
          else if (result === '部分执行') partial += 1;
          else if (result === '未执行') notExecuted += 1;
          else if (result === '不适用') na += 1;
          if (defect !== '无' || result === '部分执行' || result === '未执行') defects += 1;
        });
        var applicable = executed + partial + notExecuted;
        var rate = applicable ? Math.round((executed + partial) / applicable * 100) : 0;
        var row = $('[data-stat="' + step.key + '"]', root);
        if (row) {
          var valueEl = $('.wt-stat__val', row);
          var barEl = $('.wt-stat__bar i', row);
          if (valueEl) valueEl.textContent = applicable ? rate + '%' : '—';
          if (barEl) barEl.style.width = (applicable ? rate : 0) + '%';
          row.classList.toggle('is-bad', notExecuted > 0);
          row.classList.toggle('is-warn', notExecuted === 0 && partial > 0);
        }
        if (keySteps.indexOf(step.key) >= 0 && notExecuted > 0) keyFail.push(step.name);
      });

      var conclusion = '', reason = '';

      // 账期核对：只有到期日与实际付款日都齐的样本进分母
      var checked = 0, overdue = 0, major = 0, delays = [], termNodes = $$('.wt-sample', list);
      termNodes.forEach(function (node) {
        var info = syncTerms(node, false);
        if (info.delay === null) return;
        checked += 1;
        delays.push(info.delay);
        if (info.status === '逾期') {
          overdue += 1;
          if (info.delay > MAJOR_DAYS) major += 1;
        }
      });
      var onTimeRate = checked ? Math.round((checked - overdue) / checked * 100) : null;
      // 与后端 terms_stats 一致：单笔逾期不按比例判失效，至少 2 笔才看占比
      var termsFail = major > 0 || (overdue >= 2 && (overdue / checked) >= BAD_RATIO);
      var maxDelay = delays.length ? Math.max.apply(null, delays) : null;
      var avgDelay = delays.length
        ? (delays.reduce(function (a, b) { return a + b; }, 0) / delays.length)
        : null;

      var rateEl = $('[data-ts-rate]', root);
      var odEl = $('[data-ts-overdue]', root);
      var avgEl = $('[data-ts-avg]', root);
      var maxEl = $('[data-ts-max]', root);
      if (rateEl) rateEl.textContent = onTimeRate === null ? '—' : onTimeRate + '%';
      if (odEl) odEl.textContent = String(overdue);
      if (avgEl) avgEl.textContent = avgDelay === null ? '—' : avgDelay.toFixed(1) + ' 天';
      if (maxEl) {
        maxEl.textContent = maxDelay === null ? '—'
          : (maxDelay > 0 ? maxDelay + ' 天' : '无逾期');
      }

      if (!total) { conclusion = ''; reason = '尚未录入样本'; }
      else if (keyFail.length) { conclusion = '部分有效'; reason = '关键控制点未执行：' + keyFail.join('、'); }
      else if (termsFail) {
        conclusion = '部分有效';
        reason = '账期执行偏差：' + overdue + ' 笔逾期（最长 ' + maxDelay + ' 天）';
      }
      else if (defects) { conclusion = '基本有效'; reason = '存在 ' + defects + ' 项执行缺陷'; }
      else if (overdue) {
        conclusion = '基本有效';
        reason = '存在 ' + overdue + ' 笔逾期付款（最长 ' + maxDelay + ' 天），控制点本身执行到位';
      }
      else { conclusion = '有效'; reason = '控制点全部执行且有证据支持'; }

      var conclEl = $('[data-wt-conclusion]', root);
      var reasonEl2 = $('[data-wt-reason]', root);
      var defectEl = $('[data-wt-defects]', root);
      var countEl = $('[data-wt-count]', root);

      if (conclEl) {
        conclEl.textContent = conclusion || '—';
        conclEl.className = 'cr-grade' + (conclusion === '有效' ? ' cr-grade--a'
          : conclusion === '基本有效' ? ' cr-grade--b'
            : conclusion === '部分有效' ? ' cr-grade--c'
              : conclusion === '无效' ? ' cr-grade--d' : '');
      }
      if (reasonEl2) reasonEl2.textContent = reason;
      if (defectEl) defectEl.textContent = String(defects);
      if (countEl) countEl.textContent = String(total);

      var conclInput = $('#id_conclusion');
      if (conclInput) conclInput.value = conclusion;
      hidden.value = JSON.stringify(samples);
    }

    list.addEventListener('input', function (event) {
      // 改「约定账期」或「起算日」时自动补到期日，改其它字段只刷新统计
      var field = event.target.getAttribute && event.target.getAttribute('data-f');
      var node = event.target.closest('.wt-sample');
      if (node && field === 'due_date') {
        // 手工改到期日：解除自动标记，之后不再被推算覆盖
        var dueInput = $('[data-f="due_date"]', node);
        if (dueInput) dueInput.removeAttribute('data-auto');
      }
      if (node && (field === 'terms_days' || field === 'base_date')) syncTerms(node, true);
      refreshStats();
    });
    list.addEventListener('change', refreshStats);
    list.addEventListener('click', function (event) {
      var btn = event.target.closest('.wt-remove');
      if (!btn) return;
      var node = btn.closest('.wt-sample');
      if (!node) return;
      if ($$('.wt-sample', list).length <= 1) {
        if (window.showToast) window.showToast('至少保留一笔样本');
        return;
      }
      node.remove();
      renumber();
      refreshStats();
    });

    var addBtn = $('[data-add-sample]', root);
    if (addBtn) {
      addBtn.addEventListener('click', function () {
        var node = addSample(null);
        var first = $('.wt-step', node);
        if (first) first.scrollIntoView({ block: 'center', behavior: 'smooth' });
      });
    }

    // 初始样本：服务端已保存的直接回填；新增时给一笔空白样本
    var initial = readJSON('initial-samples', []);
    if (initial.length) {
      initial.forEach(function (s) { addSample(s); });
    } else {
      addSample(null);
    }

    var form = $('form', root);
    if (form) {
      form.addEventListener('submit', function () { hidden.value = JSON.stringify(collect()); });
    }

    // 表头「约定账期」改动后，未单独填天数的样本同步换算出新到期日
    var headerTerms = document.getElementById('id_terms_days');
    if (headerTerms) {
      headerTerms.addEventListener('input', function () {
        defaultTerms = num(headerTerms.value);
        $$('.wt-sample', list).forEach(function (node) { syncTerms(node, true); });
        refreshStats();
      });
    }
    refreshStats();
  }

  /* ------------------------------------------------------------------ */
  document.addEventListener('DOMContentLoaded', function () {
    initScoreForm();
    initAuditForm();
    initWalkthrough();
  });
})();
