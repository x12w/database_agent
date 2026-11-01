"""Archive the verified application directory with bounded compression time."""
import json
import tarfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
version = json.loads((root / "package.json").read_text())["version"]
source = root / "release" / "linux-unpacked"
archive = root / "release" / f"dbchat-desktop-{version}.tar.gz"
temporary = archive.with_suffix(".gz.tmp")
try:
    with tarfile.open(temporary, "w:gz", compresslevel=1) as package:
        package.add(source, arcname="dbchat")
    temporary.replace(archive)
finally:
    temporary.unlink(missing_ok=True)
print(f"Linux application archive: {archive}")
