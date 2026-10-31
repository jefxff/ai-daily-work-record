"""引用注入。

服务端是引用的唯一来源：工具返回时带上 ``build_ref`` 结构，执行器汇总去重，
前端照着渲染。模型输出的 id / 链接一律不采信（``security.sanitize_answer``
会把非白名单链接降级为纯文本），避免幻觉指向别人的数据。
"""

MAX_REFS = 12


def merge(existing, new_refs, limit=MAX_REFS):
    merged = list(existing or [])
    seen = {(r.get('module'), r.get('id'), r.get('url')) for r in merged}
    for ref in new_refs or []:
        key = (ref.get('module'), ref.get('id'), ref.get('url'))
        if key in seen:
            continue
        seen.add(key)
        merged.append(ref)
        if len(merged) >= limit:
            break
    return merged


def allowed_urls(refs):
    return {r.get('url') for r in (refs or []) if r.get('url')}
