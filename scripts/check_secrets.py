"""提交前密钥门禁：扫描 Git 跟踪文件中的硬编码密钥（审计 SEC-01）。

用法：
    python scripts/check_secrets.py

命中即返回非 0，CI 与本地提交前均可运行；只读文件，不修改仓库。
历史提交中的旧密钥无法靠本脚本回收，需在部署环境轮换 SECRET_KEY（见 README）。
"""

import re
import subprocess
import sys

# 只匹配「像真密钥」的形态，避免把文档示例、变量名当成泄漏
PATTERNS = [
    (re.compile(r"django-insecure-[A-Za-z0-9_\-]{10,}"), "Django 默认 SECRET_KEY"),
    (re.compile(r"SECRET_KEY\s*=\s*['\"][^'\"]{20,}['\"]"), "硬编码 SECRET_KEY"),
    (
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        "私钥文件内容",
    ),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "疑似 API Key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "疑似 AWS Access Key"),
]

# 测试夹具明确使用假密钥，放行
ALLOWLIST = (
    "tests/",
    "scripts/check_secrets.py",
    "README.md",
)


def tracked_files():
    out = subprocess.run(
        ["git", "ls-files", "-z"], capture_output=True, check=True
    ).stdout
    for raw in out.split(b"\x00"):
        if raw:
            yield raw.decode("utf-8", "replace")


def main() -> int:
    hits = []
    for name in tracked_files():
        if name.startswith(ALLOWLIST):
            continue
        try:
            with open(name, "r", encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except OSError:
            continue
        for pattern, label in PATTERNS:
            for match in pattern.finditer(text):
                line = text[: match.start()].count("\n") + 1
                hits.append(f"{name}:{line}: {label}")
    if hits:
        print("发现疑似硬编码密钥：", file=sys.stderr)
        print("\n".join(hits), file=sys.stderr)
        return 1
    print(f"密钥扫描通过（{len(list(tracked_files()))} 个跟踪文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
