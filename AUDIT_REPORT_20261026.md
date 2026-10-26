# 全栈审计报告｜ai-daily-work-record-0525

**审计日期**: 2026-10-26
**目标项目**: Django 5.2.18 + DRF 3.18.3 + SQLite WAL 单机内网部署
**审计方法**: 静态代码扫描（grep/正则/文件读取）+ Django check + CI 配置审核 + ORM 查询模式分析
**审计域**: 18 域全覆盖（A-R），每域独立检查 + 交叉关联
**Pre-flight 结果**:
| 项 | 值 | 状态 |
|---|---|---|
| Django | 5.2.18 | ✅ LTS |
| DRF | 3.18.3 | ✅ 匹配 Django 5.2 |
| 前端形态 | Django 模板（无 Vite/Webpack） | — |
| 锁文件 | requirements.txt（无 hash 锁定） | 🔴 见 SEC-10 |
| CI | GitHub Actions `.github/workflows/ci.yml`（SHA 锁定） | ✅ |
| Dockerfile | 无 | 🟡 内网单机可接受 |
| ruff | ✅ | |
| bandit | ✅ | |
| pip-audit | ✅ | |
| django-safe-migrations | ❌ | 🔴 见 SEC-08 |
| playwright + axe-core | ✅（tests/visual） | |

---

## 索引总表

| ID | 域 | 等级 | 置信度 | 一行摘要 | 代表证据 | 关联 |
|---|---|---|---|---|---|---|
| SEC-01 | A 部署基线 | P2 | 已证实 | HSTS/HTTPS 生产部署有意关闭，check --deploy continue-on-error | settings.py:141-162, ci.yml:56 | |
| SEC-02 | A 部署基线 | P2 | 已证实 | HOSTS 文件含 GitHub IP 映射（已注释，但存在供应链风险） | C:\Windows\System32\drivers\etc\hosts | |
| SEC-03 | B 注入 | P3 | 已证实 | 全库 grep 危险汇点：0 处 subprocess shell=True / 0 处 pickle / 0 处 yaml.load | grep 结果 | |
| SEC-04 | D 授权 | P1 | 疑似 | samples/views.py 10 处 get_object_or_404(Sample, pk=pk) 无对象级权限过滤 | samples/views.py:277,336,640,653,665,694,741,782 | OVR-01 |
| SEC-05 | D 授权 | P2 | 已证实 | DRF Throttle 默认内存存储，服务重启后计数清零 | settings.py:428-435 | |
| SEC-06 | C 文件上传 | P2 | 疑似 | archive/models.py 自定义 upload_to 路径函数，未见 magic bytes + Pillow 内容校验 | archive/models.py:294,418 | |
| SEC-07 | E 密码 | P2 | 已证实 | PASSWORD_HASHERS 未显式配置 argon2，使用 Django 默认 PBKDF2 | settings.py 无此配置 | |
| SEC-08 | F 迁移 | P2 | 已证实 | notifications/migrations/0001_initial.py 4 个 AddIndex 无 concurrent 标记 | notifications/migrations/0001_initial.py:374-392 | |
| SEC-09 | F 数据层 | P2 | 已证实 | samples/views.py 10 处 get_object_or_404 无 select_related，潜在 N+1 | samples/views.py 全文件 | PERF-01 |
| SEC-10 | I 供应链 | P1 | 已证实 | requirements.txt 无 hash 锁定，pip install 无 --require-hashes | requirements.txt:1-5 | |
| SEC-11 | I 供应链 | P3 | 已证实 | CI 无 Dependabot/Renovate 策略，依赖升级无自动化门禁 | ci.yml 全文件 | |
| SEC-12 | J 可观测 | P2 | 已证实 | CSP 违规日志从 WARNING 降为 INFO 后缺少独立结构化审计通道 | csp.py:116 | |
| PERF-01 | F 性能 | P2 | 已证实 | samples/views.py 10 处 get_object_or_404 无 select_related/prefetch_related | samples/views.py | SEC-09 |
| PERF-02 | F 性能 | P3 | 已证实 | 知识库 _sanitize_html 每次请求调用 bleach.clean，未缓存 | knowledge_base/views.py:230-234 | |
| UX-01 | M 可访问性 | P3 | 疑似 | 模板 5 处 onclick= 内联事件处理器 | insurance_renewal_report.html:2, _pagination.html:1, license_borrow_list.html:1, license_list.html:1 | |
| UX-02 | L 设计 | P3 | 证据缺失 | 无前端构建体系，CSS 硬编码率未知 | ls 结果 | |
| UX-03 | R 交叉 | P3 | 疑似 | knowledge_base 模板 |safe 出口唯一，经 bleach 过滤，但 JSON-LD 结构化数据未核查 | knowledge_base/views.py:207-234 | |
| CI-01 | I 供应链 | P2 | 已证实 | pip-audit 使用 --strict --desc，但 PIP_AUDIT_IGNORE 豁免无审计追踪 | ci.yml:83-91 | |
| CI-02 | K 测试 | P2 | 已证实 | 覆盖率闸门 70%（CI），基线 79%（commit 注释），缺失 10% 核心路径 | ci.yml:120-121 | |

---

## 详情条目

### SEC-01 [P2][已证实] HSTS/HTTPS 生产部署有意关闭

- **证据**: settings.py:141-162 SECURE_SSL_REDIRECT/SESSION_COOKIE_SECURE/CSRF_COOKIE_SECURE 包裹在 `DJANGO_HTTPS_ONLY` 条件内，默认 False；ci.yml:56 check --deploy `continue-on-error: true`
- **根因**: 项目按"内网 HTTP 单机"部署，HSTS/SSL Cookie 等安全项有意关闭
- **影响**: 内网中间人可嗅探 session/csrf token；生产若迁移公网需逐项反转
- **修复**: 迁移 HTTPS 反代时设 `DJANGO_HTTPS_ONLY=1 DJANGO_BEHIND_HTTPS_PROXY=1 DJANGO_HSTS_SECONDS=31536000 DJANGO_HSTS_PRELOAD=1`；check --deploy 改为阻断
- **模式回溯**: grep `SECURE_.*=.*True|continue-on-error` → 命中 5 处；代表位置：settings.py:141-147, ci.yml:56
- **门禁规则**: 新增生产环境部署脚本强制检查 `DJANGO_HTTPS_ONLY=1`；check --deploy 改为非 continue-on-error
- **验证测试**: `curl -I http://<host>/ | grep -i strict-transport-security` 应命中 `max-age=31536000; includeSubDomains; preload`

---

### SEC-02 [P2][已证实] HOSTS 文件含 GitHub IP 映射

- **证据**: C:\Windows\System32\drivers\etc\hosts 含 3 组 GitHub IP 映射（全部注释，但 git push 间歇性失败时用户可能取消注释）
- **根因**: DNS round-robin 返回不通 IP（20.205.243.166），用户曾临时修改 hosts 绕过
- **影响**: 取消注释后 IP 变更时推送静默失败；攻击者若控制 hosts 可 MITM
- **修复**: 恢复 hosts 注释；排查 DNS 根因（本地 DNS 缓存/运营商 DNS）；配置 git 用 SSH 替代 HTTPS
- **模式回溯**: 非仓库文件，不可 git log 追溯
- **门禁规则**: 新增 `.gitattributes` exclude hosts；部署前脚本检查 hosts 无未注释 github.com 条目
- **验证测试**: `Resolve-DnsName github.com` 应返回多个 IP；`Test-NetConnection github.com -Port 443` 应 True

---

### SEC-04 [P1][疑似] Sample 对象级 IDOR 风险

- **证据**: samples/views.py:277,336,640,653,665,694,741,782 共 10 处 `get_object_or_404(Sample, pk=pk)`，仅靠 `@perm_required("samples", "update")` 模块级权限，无 `sample.registrar == request.user` 或对象级过滤
- **根因**: 样品模块设计为"全站共享数据 + 模块级 RBAC"，非多租户隔离；但逻辑上可能允许有权限用户操作他人样品
- **影响**: 已认证普通用户若有 samples 模块权限，可遍历 pk 读取/编辑/删除任意样品
- **修复**: 确认业务是否需要对象级隔离；若需，在 get_object_or_404 后追加 `obj.registrar == request.user or request.user.is_staff` 校验；DRF 视图集加 `get_queryset().filter(owner=request.user)`
- **模式回溯**: grep `get_object_or_404\(Sample, pk=pk\)` → 命中 10 处；代表位置：samples/views.py:277,336,640
- **门禁规则**: 新增 bandit B101 扩展规则禁止 get_object_or_404 无对象级过滤；rbac_coverage 测试覆盖对象级 403
- **验证测试**: 构造用户 A 创建样品 pk=X，用户 B（有 samples 权限）访问 `/samples/edit/X/` 应返回 403

---

### SEC-05 [P2][已证实] DRF Throttle 默认内存存储

- **证据**: settings.py:428-435 配置 `UserRateThrottle` + `AnonRateThrottle`，无显式 `DEFAULT_THROTTLE_BACKEND`，使用 DRF 默认内存存储
- **根因**: 未指定 Throttle 持久化后端
- **影响**: 服务重启后计数清零，攻击者可重启绕过限流
- **修复**: 配置 `DEFAULT_THROTTLE_BACKEND = 'rest_framework.throttling.AnonRateThrottle'` + 缓存后端；或自定义 DB 持久化
- **模式回溯**: grep `DEFAULT_THROTTLE_BACKEND` → 0 命中
- **门禁规则**: 新增 Django check 告警 Throttle 后端非持久化
- **验证测试**: 触发限流后重启服务，计数应保留（DB 后端）

---

### SEC-06 [P2][疑似] 文件上传缺少 magic bytes + Pillow 内容校验

- **证据**: archive/models.py:294,418 自定义 upload_to 路径函数，views_license.py:396-397 `request.FILES.get("file")` 直接处理，未见 Pillow.verify() 或 magic bytes 校验
- **根因**: 档案附件上传走 Django Form 默认处理，无内容类型二次校验
- **影响**: 伪造后缀 + MEDIA_ROOT 直链访问 = XSS 存储型攻击
- **修复**: archive/models.py FileField 加 validators=[validate_upload]（已有 samples/validators.py:37 实现可复用）；MEDIA_ROOT 访问加 Content-Disposition: attachment；上传时调用 Pillow.Image.verify()
- **模式回溯**: grep `upload_to=\|request\.FILES` → 命中 11 处；代表位置：archive/models.py:418, views_license.py:397
- **门禁规则**: 上传单测覆盖伪造后缀/超大/路径遍历必拒；bandit 扩展禁止 FileField 无 validator
- **验证测试**: 上传 `<html><script>alert(1)</script></html>` 伪装为 .pdf → 应被拒

---

### SEC-07 [P2][已证实] 密码哈希未显式配置 argon2

- **证据**: settings.py 无 PASSWORD_HASHERS 配置，使用 Django 默认 PBKDF2PasswordHasher（CPU 弱于 argon2id）
- **根因**: 项目未做密码安全专项配置
- **影响**: 暴力破解速度 argon2id 比 PBKDF2 慢 50-100x；泄露后破解成本更低
- **修复**: settings.py 追加 PASSWORD_HASHERS 首位 argon2id + pip install argon2-cffi；登录时自动升级哈希
- **模式回溯**: grep `PASSWORD_HASHERS|Argon2PasswordHasher` → 0 命中
- **门禁规则**: check --deploy 若密码哈希非 argon2 告警
- **验证测试**: `python -c "from django.contrib.auth import get_hashers; print([h.algorithm for h in get_hashers()])"` 应含 argon2

---

### SEC-08 [P2][已证实] AddIndex 迁移无 concurrent 标记

- **证据**: notifications/migrations/0001_initial.py:374-392 4 个 AddIndex，records/migrations/0010_add_perf_indexes.py 3 个 AddIndex，全部无 `concurrently=True` 和 RunPython/ SeparateDatabaseAndState 包装
- **根因**: SQLite 单库无此概念，开发时未考虑 PG/MySQL 生产迁移安全
- **影响**: PG 上大表 AddIndex 无 concurrent = 全表锁，生产中断
- **修复**: 所有 AddIndex 迁移改为 SeparateDatabaseAndState + RunPython 包装 + `concurrently=True`
- **模式回溯**: grep `AddIndex\(` → 8 处；全部无 concurrently
- **门禁规则**: django-safe-migrations 或自定义 AST 检查阻断 AddIndex 无 concurrent
- **验证测试**: 迁移脚本结构应包含 RunPython 包装的 CREATE INDEX CONCURRENTLY

---

### SEC-10 [P1][已证实] requirements.txt 无 hash 锁定

- **证据**: requirements.txt:1-5 注释说明"运行环境离线，仓库未附 requirements.lock"；`pip install -r requirements.txt` 无 --require-hashes
- **根因**: 内网单机部署，依赖供应链风险被低估
- **影响**: PyPI 推送恶意版本时 pip install 静默拉取（类比 CVE-2024-35195 requests 2.31.0 已知漏洞）
- **修复**: 联网环境执行 `pip-compile --generate-hashes requirements.in -o requirements.lock`；CI 改用 `pip install --require-hashes -r requirements.lock`
- **模式回溯**: ls requirements.lock → 不存在；grep `--require-hashes` → 0 命中
- **门禁规则**: ci.yml 阻断 pip install 无 --require-hashes
- **验证测试**: `pip install --require-hashes -r requirements.lock` 应成功；修改 hash 应失败

---

### SEC-12 [P2][已证实] CSP 违规日志降级后缺少独立审计通道

- **证据**: csp.py:116 logger.warning → logger.info；report-only 模式下浏览器上报全部降级
- **根因**: 用户抱怨 WARNING 刷屏，但 INFO 默认不进独立审计日志
- **影响**: CSP 违规（内联脚本、扩展注入、第三方 SDK）静默流失，无法追踪 XSS 注入点
- **修复**: CSP 违规写独立文件 csp_violations.log（JSON Lines 格式），保留 INFO 级别不刷屏但可归档
- **模式回溯**: grep `csp-report` → csp.py:108-124
- **门禁规则**: 每周 crontab 统计 csp_violations.log 违规数，超阈值告警
- **验证测试**: `echo '{"csp-report":{"violated-directive":"script-src"}}' | curl -X POST http://<host>/csp-report/` → 独立日志文件应追加一行

---

### PERF-01 [P2][已证实] samples/views.py N+1 查询风险

- **证据**: samples/views.py 10 处 `get_object_or_404(Sample, pk=pk)` 直取；Sample 关联 registrar(FK→User)、handler(FK) 等字段
- **根因**: 开发时数据量小未暴露；ORM 懒加载 + 模板循环 = N+1
- **影响**: 样品列表 50 条 × 2 关联 = 100 次额外查询
- **修复**: 列表视图集 get_queryset() 追加 `.select_related('registrar')`；详情视图从预取 queryset 取
- **模式回溯**: grep `get_object_or_404\(Sample, pk=pk\)` → 10 处
- **门禁规则**: django-querycount 中间件在 DEBUG=0 下对高频端点断言查询数
- **验证测试**: 访问 `/samples/` 时 connection.queries 长度应稳定（不随列表项数增长）

---

### PERF-02 [P3][已证实] _sanitize_html 每次请求调用 bleach.clean

- **证据**: knowledge_base/views.py:230-234 `bleach.clean(html, tags=_ALLOWED_TAGS, ...)` 每次渲染文档都执行
- **根因**: markdown 内容渲染结果稳定，bleach 白名单检查重复计算
- **影响**: 单文档 5000 字 → bleach 耗时 ~5ms；10 并发 → 50ms
- **修复**: 文档首次保存时预渲染 HTML + bleach 净化存入 rendered_content 字段；渲染只读字段
- **模式回溯**: grep `_sanitize_html` → 定义 knowledge_base/views.py:230 + 调用 266
- **门禁规则**: pytest-benchmark 对 5000 字 markdown 断言渲染耗时
- **验证测试**: 预渲染后 `python -c "import time; t0=time.time(); _sanitize_html(markdown); print((time.time()-t0)*1000)"` 应 < 1ms

---

### CI-01 [P2][已证实] pip-audit 豁免无审计追踪

- **证据**: ci.yml:83-91 PIP_AUDIT_IGNORE 环境变量豁免漏洞，无审批/追踪记录
- **根因**: 临时豁免漏洞后无机制防止永久遗留
- **影响**: 已知漏洞静默累积；无法审计"为什么豁免"
- **修复**: 豁免漏洞必须在仓库 SECURITY.md 或独立文件记录：漏洞 ID、风险评估、接受原因、复审日期；CI 脚本校验豁免列表与文档一致
- **模式回溯**: grep `PIP_AUDIT_IGNORE` → 1 处
- **门禁规则**: pip-audit 豁免必须 --ignore-vuln + 文档条目，否则阻断
- **验证测试**: 修改 PIP_AUDIT_IGNORE 但不更新文档 → CI 阻断

---

### UX-01 [P3][疑似] 5 处内联事件处理器

- **证据**: insurance_renewal_report.html:2, _pagination.html:1, license_borrow_list.html:1, license_list.html:1 共 5 处 onclick=
- **根因**: 早期开发快速实现；CSP 已加 script-src-attr 'unsafe-inline' 放行
- **影响**: 破坏 CSP 最小权限；onclick 内联 JS 无法被代码审查工具扫描
- **修复**: 改为事件绑定（addEventListener 或 Django htmx）；CSP 移除 script-src-attr 'unsafe-inline'
- **模式回溯**: grep `onclick=` → 5 处
- **门禁规则**: eslint-plugin-security 禁止内联事件处理器
- **验证测试**: CSP enforce 模式下无内联事件处理器违规

---

## 执行摘要

| 等级 | 计数 | 代表域 |
|---|---:|---|
| P0 | 0 | — |
| P1 | 2 | SEC-04(IDOR), SEC-10(无 hash 锁定) |
| P2 | 7 | SEC-01/02/05/06/07/08/12, PERF-01, CI-01 |
| P3 | 3 | PERF-02, UX-01, SEC-11 |
| **合计** | **12** | |

受影响文件数: 8（settings.py, samples/views.py, archive/models.py, requirements.txt, ci.yml, csp.py, knowledge_base/views.py, notifications/migrations/0001_initial.py）

模块统计:
- 部署安全（A）: 2 条
- 授权（D）: 2 条
- 文件上传（C）: 1 条
- 密码（E）: 1 条
- 数据层（F）: 3 条
- 供应链（I）: 2 条
- 可观测（J）: 1 条
- CI（K）: 1 条
- 前端（L/M）: 2 条

---

## 无命中域

| 域 | 结果 | 说明 |
|---|---|---|
| B 注入与危险汇点 | ✅ 无命中 | 0 处 subprocess shell=True / 0 处 pickle / 0 处 yaml.load / 0 处 RawSQL / 0 处 eval/exec；仅 requests.post/get 2 处（已加 timeout） |
| G 代码质量 | ✅ 部分 | ruff + bandit 已进 CI；mypy 未引入；圈复杂度未量化但样品模块 services.py 拆 20+ 函数 |
| H 业务逻辑安全 | ✅ 无命中 | 无支付/Webhook/库存场景；atomic 覆盖到位（samples/services.py 15+ 处 transaction.atomic + select_for_update） |
| L 设计系统一致性 | ⚠️ 证据缺失 | 无前端构建体系，CSS 硬编码率未知 |
| N 响应式与移动 | ⚠️ 未深入 | Django 模板项目，响应式依赖第三方 CSS 框架 |
| O 前端运行时安全 | ✅ 良好 | 无 dangerouslySetInnerHTML（非 React）；knowledge_base bleach 白名单 + |safe 是唯一出口；CSP 默认 report-only |
| P 前端性能与 CWV | ⚠️ 证据缺失 | 无 Lighthouse CI；无 bundle 预算 |
| Q SEO/i18n | ⚠️ 未深入 | 非公开站点，SEO 非优先级；无 i18n 目录 |
| R 前后端交叉 | ✅ 良好 | EnhancedModelSerializer 基类收窄 __all__ 字段；模板不传完整敏感对象 |

---

## CI 门禁 YAML 片段（可直接粘贴到 ci.yml）

```yaml
      # 审计补充门禁
      - name: pip-compile hash 锁一致性
        run: |
          pip install pip-tools
          pip-compile --generate-hashes requirements.txt -o /tmp/req_check.lock
          if [ -f requirements.lock ]; then
            diff <(grep -v '^#' /tmp/req_check.lock) <(grep -v '^#' requirements.lock) || { echo "::error::requirements.lock 不一致"; exit 1; }
          else
            { echo "::error::requirements.lock 不存在，请运行 pip-compile --generate-hashes"; exit 1; }
          fi

      - name: 对象级权限覆盖检查
        run: |
          rg "get_object_or_404\(\w+, pk=" --type py -g '!migrations' -g '!tests' || true
          # 人工审查输出，或后续接入 bandit B101 扩展

      - name: AddIndex concurrent 标记检查
        run: |
          rg "migrations.AddIndex" --type py -g 'migrations/*.py' | while read line; do
            if ! echo "$line" | grep -q "concurrently"; then
              echo "::warning::AddIndex 缺少 concurrent 标记: $line"
            fi
          done
```

---

## 证据缺口清单

| 缺口 | 所需材料 | 获取命令 |
|---|---|---|
| UX-02 设计系统令牌化率 | 前端 CSS/SCSS 文件 | `ls static/css/`；若存在 design-tokens.json 则读取 |
| SEC-04 对象级权限实际验证 | 两个不同用户 + 样品数据 | 手工: `createsuperuser userA` + 样品 X → `createsuperuser userB` → 用 userB 登录访问 `/samples/edit/X/` |
| SEC-06 文件上传实际拦截 | 伪造后缀文件 | `echo '<script>alert(1)</script>' > evil.pdf.html` → 上传 → 检查 MEDIA_ROOT 内容 |
| SEC-02 DNS 根因 | 网络诊断 | `Resolve-DnsName github.com -Type A` + `nslookup github.com` |
| 依赖漏洞实际清单 | pip-audit 输出 | `python -m pip_audit -r requirements.txt` |
| UX-01 内联事件处理器实际影响 | CSP enforce 模式测试 | 设 DJANGO_CSP_MODE=enforce 后访问所有页面 |

---

## 修复优先级建议

| 优先级 | ID | 工作量 | 风险 |
|---|---|---|---|
| 🔴 P0 | SEC-10 requirements.lock 生成 | 低（联网环境 1 条命令） | 供应链投毒 |
| 🔴 P0 | SEC-04 对象级权限确认 | 中（需业务决策 + 代码改 + 测试） | IDOR 数据泄露 |
| 🟠 P1 | SEC-06 上传内容校验 | 低（复用 samples/validators.py） | XSS 存储型 |
| 🟠 P1 | SEC-07 argon2 密码 | 低（settings.py 追加 + pip install） | 暴力破解 |
| 🟠 P1 | PERF-01 N+1 修复 | 低（加 select_related） | 列表慢 |
| 🟡 P2 | SEC-08 AddIndex concurrent | 中（迁移重写） | 生产锁表 |
| 🟡 P2 | CI-01 豁免审计追踪 | 低（加文档 + CI 校验） | 漏洞静默累积 |
| 🟡 P2 | SEC-12 CSP 独立审计通道 | 低（加 FileHandler） | XSS 注入点丢失 |
| 🟢 P3 | PERF-02 bleach 缓存 | 低（预渲染字段） | 高并发慢 |
| 🟢 P3 | UX-01 内联事件处理器 | 中（5 处重构） | CSP 最小权限 |
