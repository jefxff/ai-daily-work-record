"""知识库文件夹树服务层（方案 04，批次 6）。

定位：文件夹是「个人书架」，不是权限边界——
- 可见性只在文档级（private/public），文件夹不叠加权限矩阵；
- 文件夹按 owner 隔离，每个人只看到自己的树；别人的公开文档走列表/搜索，不经过树；
- 公开文档仍在 owner 自己的树里（归档是个人组织行为，公开是传播行为，正交）。

两条不变量（与幕布大纲的语义一致）：
- **删除 = 内容上提**：删中间文件夹，子文件夹与文档上提一层；顶层删除后变「未归档」；
- **环检测**：move_folder 沿 parent 链向上走，目标出现在祖先链上即拒绝。

视图层不许自己写文件夹查询，一律走本模块。
"""
from django.db import transaction

from . import access
from .models import KnowledgeDocument, KnowledgeFolder

MAX_DEPTH = 8
MAX_NAME_LEN = 50

NOT_FOUND = '文件夹不存在或没有权限'


class FolderError(Exception):
    """文件夹业务错误（视图捕获后返回 400 + message）。"""


def visible_folders(user):
    """本人文件夹集（侧栏、计数从这里起手）。"""
    return KnowledgeFolder.objects.filter(owner=user)


def _normalize_parent_id(folder_id):
    """归一前端传参：None/''/0/'0' → 顶层（None）；其余原样。"""
    if folder_id in (None, '', 'null', '0', 0):
        return None
    return folder_id


def _owned_folder(user, folder_id):
    """存在性 + 归属一步校验；「不存在」与「别人的」同一文案，不暴露 ID 空间。

    folder_id 为空（顶层/未归档语义）时返回 None，调用方需区分
    「显式给了 id 但校验失败」（应报错）与「本来就是要顶层」（返回 None）。
    """
    if folder_id in (None, ''):
        return None
    try:
        pk = int(folder_id)
    except (TypeError, ValueError):
        return None
    return KnowledgeFolder.objects.filter(pk=pk, owner=user).first()


def _require_owned(user, folder_id):
    folder = _owned_folder(user, folder_id)
    if folder is None:
        raise FolderError(NOT_FOUND)
    return folder


def _chain_depth(folder):
    """文件夹所在深度：顶层为 1。"""
    depth = 0
    node = folder
    while node is not None:
        depth += 1
        node = node.parent
    return depth


def _subtree_depth(folder):
    """子树层数（含自身）：叶子为 1。"""
    return 1 + max((_subtree_depth(c) for c in folder.children.all()), default=0)


def create_folder(user, name, parent_id=None):
    """新建文件夹：父必须是本人的；深度上限 MAX_DEPTH。"""
    name = (name or '').strip()
    if not name:
        raise FolderError('文件夹名不能为空')
    if len(name) > MAX_NAME_LEN:
        raise FolderError(f'文件夹名不能超过 {MAX_NAME_LEN} 个字')
    parent = None
    if _normalize_parent_id(parent_id) is not None:
        parent = _require_owned(user, parent_id)
        if _chain_depth(parent) + 1 > MAX_DEPTH:
            raise FolderError(f'文件夹最多 {MAX_DEPTH} 层')
    return KnowledgeFolder.objects.create(name=name, owner=user, parent=parent)


def rename_folder(user, folder_id, name):
    """重命名：非空、≤MAX_NAME_LEN 字。"""
    folder = _require_owned(user, folder_id)
    name = (name or '').strip()
    if not name:
        raise FolderError('文件夹名不能为空')
    if len(name) > MAX_NAME_LEN:
        raise FolderError(f'文件夹名不能超过 {MAX_NAME_LEN} 个字')
    folder.name = name
    folder.save(update_fields=['name'])
    return folder


def move_folder(user, folder_id, new_parent_id):
    """移动文件夹（拖拽）：环检测 + 深度上限，二者都过才落库。"""
    folder = _require_owned(user, folder_id)
    parent = None
    if _normalize_parent_id(new_parent_id) is not None:
        parent = _require_owned(user, new_parent_id)
        # 环检测：目标在 folder 自己/子孙链上即拒绝（含移到自己）
        node = parent
        while node is not None:
            if node.pk == folder.pk:
                raise FolderError('不能把文件夹移动到它自己或它的子文件夹里')
            node = node.parent
        # 深度校验：parent 深度 + folder 子树层数不得超过上限
        if _chain_depth(parent) + _subtree_depth(folder) > MAX_DEPTH:
            raise FolderError(f'文件夹最多 {MAX_DEPTH} 层')
    folder.parent = parent
    folder.save(update_fields=['parent'])
    return folder


def delete_folder(user, folder_id):
    """只删壳：子文件夹与文档上提一层，文档永不跟着删。返回受影响文档数。"""
    folder = _require_owned(user, folder_id)
    with transaction.atomic():
        parent = folder.parent
        moved_docs = KnowledgeDocument.objects.filter(folder=folder).update(folder=parent)
        folder.children.update(parent=parent)
        folder.delete()
    return moved_docs


def move_doc(user, doc, target_folder_id):
    """文档归档/移出：文档要 can_edit（owner 专属），目标文件夹须在本人树里。

    target_folder_id 为空 → 移出为「未归档」；别人的文件夹 → 同一文案 400。
    公开文档对他人只读的规则由 access.can_edit 自动继承。
    """
    if not access.can_edit(doc, user):
        raise FolderError('只能移动自己创建的文档')
    target = None
    if _normalize_parent_id(target_folder_id) is not None:
        target = _require_owned(user, target_folder_id)
    doc.folder = target
    doc.save(update_fields=['folder'])
    return doc


def folder_tree(user):
    """一次返回整棵树 + 未归档文档（个人树很小，不分页、不懒加载）。

    树只含本人文件夹；文件夹下挂的文档 = 本人文档（move_doc 的 owner 专属
    保证不会出现别人的文档挂在我的树上）。
    """
    folders = list(visible_folders(user))
    docs = list(
        KnowledgeDocument.objects
        .filter(owner=user)
        .only('id', 'title', 'visibility', 'folder_id', 'updated_at')
        .order_by('-updated_at')
    )
    by_folder = {}
    unarchived = []
    for d in docs:
        if d.folder_id is None:
            unarchived.append(d)
        else:
            by_folder.setdefault(d.folder_id, []).append(d)

    def doc_row(d):
        return {
            'id': d.pk,
            'title': d.title,
            'visibility': d.visibility,
        }

    nodes = {f.pk: {'id': f.pk, 'name': f.name, 'children': [],
                    'docs': [doc_row(d) for d in by_folder.get(f.pk, [])]}
             for f in folders}
    roots = []
    for f in folders:
        node = nodes[f.pk]
        if f.parent_id and f.parent_id in nodes:
            nodes[f.parent_id]['children'].append(node)
        else:
            roots.append(node)
    return {
        'tree': roots,
        'unarchived': [doc_row(d) for d in unarchived],
        'unarchived_count': len(unarchived),
    }


def breadcrumbs(folder_id, user):
    """详情页路径：root → 当前文件夹；他人的文件夹一律返回 []（不泄露存在性）。"""
    folder = _owned_folder(user, folder_id)
    if folder is None:
        return []
    chain = []
    node = folder
    while node is not None:
        chain.append({'id': node.pk, 'name': node.name})
        node = node.parent
    chain.reverse()
    return chain
