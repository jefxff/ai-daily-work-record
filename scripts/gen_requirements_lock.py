"""生成带哈希的 requirements.lock（审计 SUP-02）。

必须联网（PyPI 或镜像可达），并先安装 pip-tools：
    python -m pip install pip-tools
    python scripts/gen_requirements_lock.py

生成后安装：
    python -m pip install --require-hashes -r requirements.lock

说明：项目运行环境曾长期离线，因此 requirements.lock 未随仓库提供；
本脚本保证升级流程有可执行的落地方式，而不是文档里指向不存在的文件。
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    try:
        import piptools  # noqa: F401
    except ImportError:
        print("未安装 pip-tools：python -m pip install pip-tools", file=sys.stderr)
        return 2
    cmd = [
        sys.executable,
        "-m",
        "piptools",
        "compile",
        "--generate-hashes",
        "--resolver=backtracking",
        "--output-file",
        str(ROOT / "requirements.lock"),
        str(ROOT / "requirements.txt"),
    ]
    print("执行：", " ".join(cmd))
    return subprocess.call(cmd, cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main())
