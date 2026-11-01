"""Build a Linux runtime without shipping developer environments or credentials.

Use a relocatable CPython 3.13 (e.g. uv-managed python). Install only runtime
requirements; vector embeddings are optional and fall back to keyword search.
"""

import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "desktop" / "runtime"


def main():
    if sys.platform != "linux" or sys.version_info[:2] != (3, 13):
        raise SystemExit("当前打包脚本需要 Linux 和可迁移的 CPython 3.13（uv python install 3.13）")
    base = Path(sys.base_prefix).resolve()
    if not (base / "bin" / "python3.13").is_file() or str(base).startswith(("/usr", "/nix/store")):
        raise SystemExit("请在 uv 管理的可迁移 Python 3.13 环境内运行此脚本")
    # Only generated runtime output is replaced; never copy .env, var or the dev venv.
    if TARGET.exists():
        shutil.rmtree(TARGET)
    shutil.copytree(
        base,
        TARGET / "python",
        symlinks=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copytree(
        ROOT / "src",
        TARGET / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
    )
    # Retain application metadata alongside the bundled source.
    shutil.copy2(ROOT / "pyproject.toml", TARGET / "pyproject.toml")
    packages = TARGET / "python" / "lib" / "python3.13" / "site-packages"
    uv = shutil.which("uv")
    command = (
        [uv, "pip", "install", "--python", sys.executable]
        if uv
        else [sys.executable, "-m", "pip", "install"]
    )
    subprocess.run(
        [
            *command,
            "--target",
            str(packages),
            "-r",
            str(ROOT / "desktop" / "requirements-runtime.txt"),
        ],
        check=True,
    )
    (TARGET / "platform.txt").write_text(
        f"{sys.platform} {platform.machine()} Python {platform.python_version()}\n"
    )
    print(f"Runtime ready: {TARGET}")


if __name__ == "__main__":
    main()
