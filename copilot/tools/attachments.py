"""附件清单工具：只暴露元信息，不给下载地址与存储路径。"""

from django.contrib.contenttypes.models import ContentType

from .. import catalog, registry
from ..models import CopilotPolicy


@registry.tool(
    'list_attachments',
    '查询某条业务记录挂载的附件清单（文件名、分类、大小、上传人、时间）。不返回文件内容。',
    params=(
        registry.Param('module', 'module', required=True),
        registry.Param('record_id', 'int', required=True, minimum=1),
    ),
    module_param=True,
    examples=('这条客诉有附件吗',),
)
def list_attachments(ctx, module, record_id):
    from attachments.models import MODEL_MODULE_MAP, Attachment

    spec = catalog.get_module(module)
    model = catalog.model_of(spec)
    key = (model._meta.app_label, model._meta.model_name)
    if key not in MODEL_MODULE_MAP:
        return registry.ToolResult(
            ok=False, error='NO_ATTACHMENTS',
            message='该模块未接入附件能力',
        )

    obj = model.objects.filter(pk=record_id).first()
    if obj is None:
        return registry.ToolResult(ok=False, error='NOT_FOUND', message='未找到该记录')

    content_type = ContentType.objects.get_for_model(model)
    rows = []
    for att in Attachment.objects.filter(
        content_type=content_type, object_id=record_id
    ).select_related('uploaded_by')[:20]:
        rows.append({
            'id': att.pk,
            'name': att.original_name,
            'category': att.category_label or '未分类',
            'size_kb': round((att.size or 0) / 1024, 1),
            'uploader': str(att.uploaded_by or ''),
            'uploaded_at': att.created_at.strftime('%Y-%m-%d %H:%M') if att.created_at else '',
        })

    ref = catalog.build_ref(spec, obj)
    if ref.get('url'):
        ref['url'] = f"{ref['url']}#attachments"
        ref['label'] = f'{ref["label"]} · 附件'

    policy = ctx.policy or CopilotPolicy.current()
    return registry.ToolResult(
        ok=True,
        data={
            'module': spec.key,
            'record_id': record_id,
            'total': len(rows),
            'rows': rows,
            'masked': bool(policy.mask_enabled),
        },
        row_count=len(rows),
        refs=[ref] if ref.get('url') else [],
    )
