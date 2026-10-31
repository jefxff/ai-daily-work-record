"""Copilot 端到端冒烟：登录 → bootstrap → ask → 校验引用与留痕。

用法（服务需已启动；建议用 DJANGO_COPILOT_INLINE=1 启动，接口会直接返回答案）：

    set COPILOT_SMOKE_PASSWORD=你的密码
    python scripts/smoke_copilot.py

可选环境变量：
    COPILOT_SMOKE_BASE      默认 http://127.0.0.1:8080
    COPILOT_SMOKE_USER      默认 admin
    COPILOT_SMOKE_QUESTION  默认「东方新材料有哪些客户客诉」
"""

import os
import re
import sys

import requests

BASE = os.environ.get('COPILOT_SMOKE_BASE', 'http://127.0.0.1:8080').rstrip('/')
USERNAME = os.environ.get('COPILOT_SMOKE_USER', 'admin')
PASSWORD = os.environ.get('COPILOT_SMOKE_PASSWORD', '')
QUESTION = os.environ.get('COPILOT_SMOKE_QUESTION', '东方新材料有哪些客户客诉')


def main():
    if not PASSWORD:
        print('请先设置 COPILOT_SMOKE_PASSWORD')
        return 2

    session = requests.Session()
    # 本机若存在 HTTP_PROXY，requests 会走代理，Django 收到的 PATH_INFO 会变成
    # 绝对 URI（http://...），登录中间件据此判定路径不匹配并重定向到登录页。
    # 冒烟脚本只打本机，直接绕开代理。
    session.trust_env = False

    login_page = session.get(f'{BASE}/accounts/login/', timeout=10)
    match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', login_page.text)
    if not match:
        print(f'FAIL: 登录页未找到 csrf token（status={login_page.status_code}）')
        return 1
    token = match.group(1)
    resp = session.post(
        f'{BASE}/accounts/login/',
        data={'username': USERNAME, 'password': PASSWORD,
              'csrfmiddlewaretoken': token},
        headers={'Referer': f'{BASE}/accounts/login/'},
        timeout=10, allow_redirects=False,
    )
    if resp.status_code not in (301, 302) or resp.headers.get('Location') == '/accounts/login/':
        print(f'FAIL: 登录失败 status={resp.status_code}')
        return 1
    print('OK 登录成功')

    headers = {'X-Requested-With': 'XMLHttpRequest', 'Content-Type': 'application/json',
               'X-CSRFToken': session.cookies.get('csrftoken') or token}

    boot = session.get(f'{BASE}/copilot/bootstrap/', headers=headers, timeout=10).json()
    if not boot.get('ok'):
        print('FAIL: bootstrap', boot)
        return 1
    data = boot['data']
    print(f"OK bootstrap：模块 {len(data['modules'])} 个，模型已配置={data['configured']}，"
          f"本地={data['is_local']}，示例 {len(data['examples'])} 条")

    resp = session.post(f'{BASE}/copilot/ask/', headers=headers, timeout=180,
                        json={'question': QUESTION})
    try:
        payload = resp.json()
    except ValueError:
        print('FAIL: ask 返回非 JSON', resp.status_code, resp.text[:300])
        return 1
    if resp.status_code != 200 or not payload.get('ok'):
        print('FAIL: ask', resp.status_code, payload)
        return 1

    result = payload['data']
    print(f"OK ask：状态={result['status']}，error_code={result['error_code'] or '-'}，"
          f"引用 {len(result['refs'])} 条")
    print(f"   回答：{(result['answer'] or '')[:120]}")
    for ref in result['refs'][:3]:
        print(f"   引用：{ref['module_label']} / {ref['label']} → {ref['url']}")
    if result.get('steps'):
        print(f"   步骤：{result['steps']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
