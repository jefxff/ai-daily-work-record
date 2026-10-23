"""生成 CycloneDX 1.5 SBOM（审计 P3-4）。

只读取当前 Python 环境的已安装包，不联网、不安装额外依赖：
    python scripts/gen_sbom.py [输出路径，默认 sbom.cdx.json]
"""

import importlib.metadata as md
import json
import sys
from datetime import datetime, timezone

OUT = sys.argv[1] if len(sys.argv) > 1 else "sbom.cdx.json"


def main() -> int:
    components = []
    for dist in sorted(md.distributions(), key=lambda d: (d.metadata["Name"] or "").lower()):
        name = dist.metadata["Name"]
        if not name:
            continue
        components.append(
            {
                "type": "library",
                "name": name,
                "version": dist.version,
                "purl": f"pkg:pypi/{name.lower()}@{dist.version}",
            }
        )
    doc = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tools": [{"name": "gen_sbom.py"}],
        },
        "components": components,
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    print(f"已写入 {OUT}（{len(components)} 个组件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
