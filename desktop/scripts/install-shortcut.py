"""Register this built application in the current user's Linux application menu."""

import os
from pathlib import Path

root = Path(__file__).resolve().parents[2]
launcher = root / "desktop" / "release" / "linux-unpacked" / "start-dbchat.sh"
if not launcher.is_file():
    raise SystemExit("请先运行 npm --prefix desktop run pack")
applications = (
    Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "applications"
)
applications.mkdir(parents=True, exist_ok=True)
entry = applications / "org.dbchat.desktop"
quoted_launcher = (
    str(launcher).replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
)
content = (
    "[Desktop Entry]\nType=Application\nName=dbchat\nName[zh_CN]=dbchat 数据库助手\n"
    "Comment=使用自然语言查询数据库，在应用中配置模型与连接\n"
    f'Exec="{quoted_launcher}"\n'
    f"Icon={root / 'desktop/assets/icon.png'}\n"
    "Terminal=false\nCategories=Development;Database;\nStartupWMClass=dbchat\n"
)
if entry.exists() and entry.read_text(encoding="utf-8") != content:
    raise SystemExit(f"保留已有的启动入口，未覆盖：{entry}")
entry.write_text(content, encoding="utf-8")
entry.chmod(0o755)
print(f"已注册应用菜单入口：{entry}")
