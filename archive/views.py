"""档案管理模块视图（门面模块）。

实际实现按业务域拆分至以下子模块，此处统一 re-export 以保持
urls.py 的 `from . import views` 引用方式与全部 URL 名称不变：

- views_ledger    台账总览 / 详情 / CRUD / 移动 / 单表与批量导入导出
- views_cabinet   档案柜 CRUD（含层格编辑）
- views_archive   档案 CRUD / 详情 / 移动 / 电子附件 / 二维码
- views_category  类别 CRUD
- views_borrow    借阅登记与归还
- views_io        档案导入导出与表级通用导入导出
- views_license   证照管理子模块（含年度审核、证照借阅、批量导入）

权限：基于 RBAC，模块 key=archive。验收前仅超管可见（registry 不授予内置角色）。
"""

from .views_archive import (  # noqa: F401
    archive_create,
    archive_delete,
    archive_detail,
    archive_edit,
    archive_file_delete,
    archive_file_thumb,
    archive_files_upload,
    archive_move,
    qrcode,
)
from .views_borrow import borrow_create, borrow_list, borrow_return  # noqa: F401
from .views_cabinet import (  # noqa: F401
    cabinet_create,
    cabinet_delete,
    cabinet_edit,
    cabinet_list,
)
from .views_category import (  # noqa: F401
    category_create,
    category_delete,
    category_edit,
    category_list,
)
from .views_io import (  # noqa: F401
    export_archives,
    import_archives,
    import_template,
    table_export,
    table_import,
    table_template,
)
from .views_ledger import (  # noqa: F401
    ledger_batch_export,
    ledger_batch_import,
    ledger_create,
    ledger_delete,
    ledger_detail,
    ledger_edit,
    ledger_export,
    ledger_import,
    ledger_move,
    overview,
)
from .views_license import (  # noqa: F401
    LICENSE_FIELD_CHOICES,
    LICENSE_KEYWORD_MAP,
    annual_review_create,
    import_licenses,
    license_borrow_create,
    license_borrow_list,
    license_borrow_return,
    license_create,
    license_delete,
    license_detail,
    license_edit,
    license_list,
)
