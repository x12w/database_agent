"""计算沙箱 —— 子进程隔离。

历史实现（`app/agent/tools.py:_execute_python`）三处结构性毛病都在这里被断言：

* `os.chdir(_CHARTS_DIR)` 改的是**整个进程**的工作目录 —— 这里断言父进程的
  cwd 在沙箱跑完后**完全没变**；
* `signal.alarm(15)` 只对主线程有效 —— 这里断言超时真的把子进程杀了，
  而且是在工作线程/事件循环里调用（`asyncio.wait_for` + `kill`）；
* 受限内建是进程级共享的 —— 这里断言子进程里的 `open` / `import` 受限，
  而父进程的 `open` 照样能用。

另外两条：产物必须落进 `Settings.artifacts_dir`（而不是某个模块级全局目录），
失控的内存分配必须在子进程里撞墙，不能把父进程一起拖死。
"""

from __future__ import annotations

import asyncio
import math
import os
import sys
from pathlib import Path

import pytest

from dbchat.tools.sandbox import (
    DEFAULT_ADDRESS_SPACE_BYTES,
    cleanup_artifacts,
    run_python_code,
)


@pytest.fixture()
def artifacts_dir(tmp_path: Path) -> Path:
    path = tmp_path / "artifacts"
    path.mkdir()
    return path


def _png_names(directory: Path) -> list[str]:
    """目录里的图表文件名（测试断言用的小工具）。"""
    return sorted(path.name for path in directory.glob("*.png"))


def _entry_names(directory: Path) -> list[str]:
    """目录里的全部条目名。"""
    return sorted(path.name for path in directory.iterdir())


def _native_env_ok() -> bool:
    """matplotlib 需要系统库（NixOS 上靠 LD_LIBRARY_PATH）。"""
    return bool(os.environ.get("LD_LIBRARY_PATH"))


@pytest.mark.asyncio
async def test_timeout_kills_the_child(artifacts_dir: Path) -> None:
    """死循环必须被硬超时杀掉，且报出超时而不是别的错。"""
    started = asyncio.get_running_loop().time()
    result = await run_python_code(
        "while True:\n    pass\n",
        artifacts_dir=artifacts_dir,
        timeout_s=1.0,
    )
    elapsed = asyncio.get_running_loop().time() - started

    assert result.ok is False
    assert "超时" in result.error
    assert elapsed < 10.0, "超时没有及时生效"


@pytest.mark.asyncio
async def test_server_process_cwd_is_unchanged(artifacts_dir: Path) -> None:
    """沙箱改工作目录不许影响父进程 —— 这是历史实现最严重的副作用。"""
    before = os.getcwd()
    stray = Path(before) / "sandbox_relative_write.txt"
    assert not stray.exists()

    result = await run_python_code(
        # 相对路径写在**子进程**的 cwd 里；父进程的 cwd 必须纹丝不动。
        "handle = open('sandbox_relative_write.txt', 'w')\n"
        "handle.write('x')\n"
        "handle.close()\n"
        "result = 'ok'\n",
        artifacts_dir=artifacts_dir,
        timeout_s=20.0,
    )

    assert result.ok is True, result.error
    assert os.getcwd() == before
    assert stray.name not in _entry_names(Path(before))


@pytest.mark.asyncio
async def test_open_is_denied(artifacts_dir: Path) -> None:
    """读文件被拒绝（`open('/etc/passwd')` 是历史实现里的经典绕过尝试）。"""
    result = await run_python_code(
        "open('/etc/passwd')",
        artifacts_dir=artifacts_dir,
        timeout_s=20.0,
    )
    assert result.ok is False
    assert "PermissionError" in result.error
    assert "不允许读取文件" in result.error


@pytest.mark.asyncio
async def test_write_outside_workdir_is_denied(artifacts_dir: Path, tmp_path: Path) -> None:
    """写到工作目录之外也被拒绝。"""
    target = tmp_path / "escaped.txt"
    result = await run_python_code(
        f"open({str(target)!r}, 'w').write('x')",
        artifacts_dir=artifacts_dir,
        timeout_s=20.0,
    )
    assert result.ok is False
    assert "PermissionError" in result.error
    assert not target.exists()


@pytest.mark.asyncio
async def test_restricted_imports(artifacts_dir: Path) -> None:
    """只有白名单模块能导入（`os` / `subprocess` / `sys` 都不行）。"""
    for module in ("os", "subprocess", "sys", "shutil"):
        result = await run_python_code(
            f"import {module}",
            artifacts_dir=artifacts_dir,
            timeout_s=20.0,
        )
        assert result.ok is False, module
        assert "不允许导入模块" in result.error, module


@pytest.mark.asyncio
async def test_runaway_allocation_is_contained(artifacts_dir: Path) -> None:
    """失控分配在子进程里撞内存上限，父进程与事件循环照常。"""
    result = await run_python_code(
        "data = [0] * (10 ** 9)\nresult = len(data)",
        artifacts_dir=artifacts_dir,
        timeout_s=30.0,
    )
    assert result.ok is False
    assert "MemoryError" in result.error or "超时" in result.error
    # 父进程没被拖死：还能正常跑一个沙箱
    again = await run_python_code("result = 1 + 1", artifacts_dir=artifacts_dir, timeout_s=20.0)
    assert again.ok is True
    assert again.result_text == "2"


@pytest.mark.asyncio
async def test_input_data_and_result(artifacts_dir: Path) -> None:
    """输入注入与 `result` 取值，以及 stdout 的回传。"""
    result = await run_python_code(
        "print('hi')\nresult = sum(q['row_count'] for q in input_data['queries'])",
        input_data={"queries": [{"row_count": 2}, {"row_count": 3}]},
        artifacts_dir=artifacts_dir,
        timeout_s=20.0,
    )
    assert result.ok is True
    assert result.result_text == "5"
    assert result.stdout.strip() == "hi"


@pytest.mark.asyncio
async def test_syntax_error_is_reported(artifacts_dir: Path) -> None:
    result = await run_python_code("x = (", artifacts_dir=artifacts_dir, timeout_s=20.0)
    assert result.ok is False
    assert "语法错误" in result.error


@pytest.mark.asyncio
async def test_chart_lands_in_artifacts_dir(artifacts_dir: Path) -> None:
    """图表必须落到传给沙箱的 artifacts_dir，并产出可展示的 Artifact。"""
    if not _native_env_ok():
        pytest.skip("matplotlib 需要 LD_LIBRARY_PATH（见 run.sh 的检测片段）")

    code = (
        "import matplotlib\n"
        "matplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "plt.figure()\n"
        "plt.plot([1, 2, 3], [3, 2, 1])\n"
        "plt.savefig('chart_test.png')\n"
        "plt.close()\n"
        "result = {'charts': ['chart_test.png']}\n"
    )
    result = await run_python_code(code, artifacts_dir=artifacts_dir, timeout_s=120.0)

    assert result.ok is True, result.error
    # 产物目录里只有图表 —— 沙箱的临时文件（payload/result json）不许漏进来
    assert _entry_names(artifacts_dir) == ["chart_test.png"]
    assert _png_names(artifacts_dir) == ["chart_test.png"]
    assert len(result.artifacts) == 1
    artifact = result.artifacts[0]
    assert artifact.kind == "chart"
    assert artifact.url.endswith("/chart_test.png")


@pytest.mark.asyncio
async def test_cleanup_keeps_newest_charts(artifacts_dir: Path) -> None:
    """图表 GC：只保留最新 N 张（历史 `_cleanup_charts` 的语义）。"""
    import time

    for index in range(8):
        path = artifacts_dir / f"chart_{index}.png"
        path.write_bytes(b"x")
        os.utime(path, (time.time() + index, time.time() + index))

    cleanup_artifacts(artifacts_dir, keep=3)

    assert _png_names(artifacts_dir) == ["chart_5.png", "chart_6.png", "chart_7.png"]


@pytest.mark.asyncio
async def test_default_address_space_is_finite() -> None:
    """默认地址空间上限是有限值 —— 没有它，失控分配会把整机拖垮。"""
    assert 0 < DEFAULT_ADDRESS_SPACE_BYTES <= 8 * 1024 * 1024 * 1024


@pytest.mark.asyncio
async def test_child_mutation_does_not_leak_to_the_parent(artifacts_dir: Path) -> None:
    """沙箱是**另一个进程**：它改模块级状态不会污染服务进程。

    历史实现在进程内 `exec`，`import math; math.pi = 3` 会真的改掉服务进程
    的 `math.pi` —— 同一个进程里所有请求一起遭殃。
    """
    original = math.pi
    result = await run_python_code(
        "import math\nmath.pi = 3\nresult = math.pi",
        artifacts_dir=artifacts_dir,
        timeout_s=20.0,
    )
    assert result.ok is True
    assert result.result_text == "3"      # 子进程里确实改了
    assert math.pi == original            # 父进程里纹丝不动
    assert sys.executable                 # 子进程用同一个解释器启动
