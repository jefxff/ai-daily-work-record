"""校验 requirements.txt（直接依赖）与 requirements.lock（全量锁定）是否一致。

为什么需要：锁文件是"升级流程"的一部分，很容易出现「改了 requirements.txt 忘了
重新生成 lock」的静默漂移 —— 开发装的是新版本、部署按 lock 装的是旧版本，
问题只在生产暴露。CI 里跑本脚本可让这种漂移直接变红。

用法：
    python scripts/check_lock_sync.py
退出码：0 一致 / 1 不一致 / 2 文件缺失
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIN_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*==\s*([^\s#;]+)")
HEADER_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*==\s*([^\s#;\\]+)")


def _read_pins(path: Path) -> dict[str, str]:
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        m = PIN_RE.match(line)
        if m:
            pins[m.group(1).lower().replace("_", "-")] = m.group(2)
    return pins


def main() -> int:
    req = ROOT / "requirements.txt"
    lock = ROOT / "requirements.lock"
    for f in (req, lock):
        if not f.is_file():
            print(f"缺少文件：{f.name}（请先运行依赖锁定流程）", file=sys.stderr)
            return 2
    direct = _read_pins(req)
    locked = _read_pins(lock)

    problems = []
    for name, ver in sorted(direct.items()):
        if name not in locked:
            problems.append(f"{name}=={ver} 不在 requirements.lock 中")
        elif locked[name] != ver:
            problems.append(f"{name}: requirements.txt={ver} ≠ lock={locked[name]}")

    if problems:
        print("依赖锁与直接依赖不一致（请重新生成 requirements.lock）：", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        return 1

    extra = sorted(set(locked) - set(direct))
    print(f"依赖锁一致：直接依赖 {len(direct)} 个，锁定包 {len(locked)} 个"
          f"（含传递依赖 {len(extra)} 个）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
