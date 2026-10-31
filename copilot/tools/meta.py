"""元工具：让模型先知道"能查什么、字段叫什么"，再决定怎么查。"""

from .. import catalog, registry
from .common import labels


@registry.tool(
    'list_modules',
    '列出当前账号可查询的业务模块。回答任何业务问题前可先调用它确认范围。',
    params=(),
    examples=('你能查哪些数据',),
)
def list_modules(ctx):
    modules = registry.available_modules(ctx.user, ctx.is_local, ctx.policy)
    data = [
        {'module': m.key, 'label': m.label, 'date_field': m.date_field}
        for m in modules
    ]
    return registry.ToolResult(ok=True, data=data, row_count=len(data))


@registry.tool(
    'schema_of_module',
    '查询某个模块的字段中文名、可分组字段、可求和字段、可排序字段。',
    params=(
        registry.Param('module', 'module', required=True, description='模块 key'),
    ),
    module_param=True,
    examples=('客诉表有哪些字段',),
)
def schema_of_module(ctx, module):
    spec = catalog.get_module(module)
    if spec is None:
        return registry.ToolResult(ok=False, error='UNKNOWN_MODULE',
                                   message='未知模块')
    payload = catalog.schema(spec)
    payload['detail_fields'] = labels(spec, spec.detail_fields)
    payload['order_fields'] = list(spec.order_fields)
    return registry.ToolResult(ok=True, data=payload)
