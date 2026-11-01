"""Python 计算沙箱 —— **子进程**执行，不在进程内 `exec`。

历史实现（`app/agent/tools.py:165-279`）有三个结构性毛病：

1. `os.chdir(_CHARTS_DIR)`（`:205`）改的是**整个进程**的工作目录 ——
   沙箱代码跑图表的同时，别的请求正在按相对路径读配置或写缓存；
2. `signal.alarm(15)`（`:200`）只对主线程有效，在工作线程里调用直接抛
   `ValueError`；而且它只打断 Python 字节码，`numpy` 的长循环或 C 层
   分配照样跑到底；
3. 受限内建函数是**进程级共享**的，`_lazy_module_cache` 也是全局可变状态。

强制使用 bubblewrap 的文件系统、进程与网络命名空间隔离；无隔离环境时拒绝执行。
超时或取消会终止进程组，内存/CPU/文件大小用 `resource.setrlimit` 限制。输出
落在临时目录，图表产出后再搬进 `Settings.artifacts_dir`。

模块白名单语义（`_PRELOADED_MODULES` / `_LAZY_MODULES` / `_restricted_import`）
与图表 GC（保留最新 50 张）刻意与历史实现保持一致。
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import math
import os
import shutil
import signal
import stat
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import anyio

from dbchat.domain.query import Artifact

logger = logging.getLogger(__name__)

__all__ = [
    "ARTIFACT_ROUTE_PREFIX",
    "PYTHON_BOOTSTRAP",
    "SandboxResult",
    "cleanup_artifacts",
    "run_python_code",
]

#: 图表等产物对外的下载路径前缀。API 层按这个前缀提供静态文件。
ARTIFACT_ROUTE_PREFIX = "/api/artifacts"

#: 沙箱保留的图表数量上限（历史实现同样是 50）。
KEEP_ARTIFACTS = 50

#: 子进程地址空间上限。给 matplotlib + numpy 留足虚拟内存，
#: 同时让 runaway 分配（如 `[0] * 10**9`）在几百毫秒内撞墙退出。
DEFAULT_ADDRESS_SPACE_BYTES = 2 * 1024 * 1024 * 1024

#: 预加载模块 —— 沙箱代码 import 这些一定成功，不依赖懒加载。
PRELOADED_MODULES: tuple[str, ...] = (
    "math",
    "statistics",
    "json",
    "decimal",
    "fractions",
    "collections",
    "itertools",
    "functools",
    "random",
    "datetime",
    "time",
)

#: 懒加载模块 —— 只有沙箱代码真的 import 时才载入（matplotlib 导入很贵）。
LAZY_MODULES: tuple[str, ...] = ("matplotlib", "matplotlib.pyplot")

#: 允许 import 的顶层模块名（错误信息里也用它列清单）。
ALLOWED_MODULES: tuple[str, ...] = tuple(sorted(set(PRELOADED_MODULES) | {m.split(".")[0] for m in LAZY_MODULES}))


@dataclass(slots=True)
class SandboxResult:
    """一次沙箱执行的结果。"""

    ok: bool
    stdout: str = ""
    stderr: str = ""
    #: `result` 变量的渲染文本。
    result_text: str = ""
    #: 沙箱产出的图表（已搬到 artifacts 目录）。
    artifacts: tuple[Artifact, ...] = ()
    #: 失败原因（面向用户）。
    error: str = ""
    #: 原始细节（只进日志/调试）。
    error_detail: str = ""
    execution_ms: int = 0
    extra: dict[str, object] = field(default_factory=dict)


#: 子进程引导脚本。
#:
#: 必须自包含：它由 `sys.executable -c` 直接执行，不 import 项目里的任何模块
#: （否则沙箱进程会带着整个应用上下文，隔离就没意义了）。
PYTHON_BOOTSTRAP = r'''
import io
import json
import os
import sys

_ARGS = sys.argv[1:]
_PAYLOAD_PATH, _OUT_PATH, _WORKDIR, _CPU_S, _ADDR_LIMIT = (
    _ARGS[0], _ARGS[1], _ARGS[2], int(_ARGS[3]), int(_ARGS[4]),
)

# ---- 资源限制：CPU 硬上限 + 地址空间上限 ----
try:
    import resource

    _cpu = max(1, _CPU_S)
    resource.setrlimit(resource.RLIMIT_CPU, (_cpu, _cpu + 1))
    if _ADDR_LIMIT > 0:
        resource.setrlimit(resource.RLIMIT_AS, (_ADDR_LIMIT, _ADDR_LIMIT))
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))
except Exception as _exc:  # noqa: BLE001 - 拿不到 rlimit 不算致命，超时兜底仍在
    sys.stderr.write("sandbox: rlimit unavailable: %r\n" % (_exc,))

# ---- 工作目录在**子进程**里设置，绝不碰父进程的 cwd ----
os.chdir(_WORKDIR)

_PRELOADED = {}
_LAZY = set()
with open(_PAYLOAD_PATH, "r", encoding="utf-8") as _fh:
    _payload = json.load(_fh)
_CODE = _payload.get("code", "")
_INPUT_DATA = _payload.get("input_data", {})
for _name in _payload.get("preloaded", ()):
    __import__(_name)
for _name in _payload.get("lazy", ()):
    _LAZY.add(_name.split(".")[0])

_LAZY_CACHE = {}
_ALLOWED = set(_PRELOADED) | set(_payload.get("preloaded", ())) | _LAZY


def _restricted_import(name, *args, **kwargs):
    """只允许导入白名单模块；matplotlib 按需懒加载。"""
    top = name.split(".")[0]
    if top not in _ALLOWED:
        raise ImportError(
            "不允许导入模块: %s（仅允许 %s）"
            % (name, ", ".join(sorted(_ALLOWED)))
        )
    if name in _LAZY_CACHE:
        return _LAZY_CACHE[name]
    module = __import__(name, *args, **kwargs)
    if top in _LAZY:
        _LAZY_CACHE[name] = module
    return module


def _restricted_open(file, mode="r", *args, **kwargs):
    """只允许**写入**工作目录内的文件；任何读取都被拒绝。"""
    write_modes = {"w", "wb", "a", "ab", "x", "xb"}
    if mode not in write_modes:
        raise PermissionError(
            "不允许读取文件。仅支持写入图表文件（mode in %s）" % (sorted(write_modes),)
        )
    target = os.path.abspath(os.path.join(_WORKDIR, str(file)))
    if target != _WORKDIR and not target.startswith(_WORKDIR + os.sep):
        raise PermissionError("不允许写入工作目录之外的文件: %s" % (file,))
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return open(target, mode, *args, **kwargs)


def _build_builtins():
    import builtins as _b

    safe = {
        "__import__": _restricted_import,
        "open": _restricted_open,
        "True": True, "False": False, "None": None,
        "abs": abs, "all": all, "any": any, "ascii": ascii,
        "bin": bin, "bool": bool, "bytearray": bytearray, "bytes": bytes,
        "chr": chr, "complex": complex, "dict": dict, "divmod": divmod,
        "enumerate": enumerate, "filter": filter, "float": float,
        "format": format, "frozenset": frozenset, "getattr": getattr,
        "hasattr": hasattr, "hash": hash, "hex": hex, "id": id,
        "int": int, "isinstance": isinstance, "issubclass": issubclass,
        "iter": iter, "len": len, "list": list, "map": map, "max": max,
        "min": min, "next": next, "object": object, "oct": oct,
        "ord": ord, "pow": pow, "print": print, "range": range,
        "repr": repr, "reversed": reversed, "round": round, "set": set,
        "slice": slice, "sorted": sorted, "str": str, "sum": sum,
        "tuple": tuple, "type": type, "zip": zip,
        "ValueError": ValueError, "TypeError": TypeError, "KeyError": KeyError,
        "IndexError": IndexError, "ZeroDivisionError": ZeroDivisionError,
        "MemoryError": MemoryError, "Exception": Exception,
        "RuntimeError": RuntimeError, "ArithmeticError": ArithmeticError,
    }
    # 刻意不放 eval / exec / compile / __loader__ / globals / locals
    return safe, _b


_SAFE_BUILTINS, _builtins = _build_builtins()

_globals = {"__builtins__": _SAFE_BUILTINS, "__name__": "__sandbox__"}
for _name in _payload.get("preloaded", ()):
    _globals[_name.split(".")[0]] = __import__(_name)
_locals = {"input_data": _INPUT_DATA, "result": None}

_out = {"ok": False, "stdout": "", "stderr": "", "result": None, "error": "", "traceback": ""}
_stdout_buf = io.StringIO()
_stderr_buf = io.StringIO()
_real_stdout, _real_stderr = sys.stdout, sys.stderr

try:
    _code_obj = compile(_CODE, "<run_python>", "exec")
except SyntaxError as _exc:
    _out["error"] = "Python 语法错误：%s（第 %s 行）" % (_exc.msg, _exc.lineno)
else:
    sys.stdout, sys.stderr = _stdout_buf, _stderr_buf
    try:
        exec(_code_obj, _globals, _locals)
        _out["ok"] = True
    except BaseException as _exc:  # noqa: BLE001 - 沙箱里的任何异常都要变成结果
        import traceback as _tb
        _out["error"] = "执行错误：%s: %s" % (type(_exc).__name__, _exc)
        _out["traceback"] = _tb.format_exc()[-2000:]
    finally:
        sys.stdout, sys.stderr = _real_stdout, _real_stderr

_out["stdout"] = _stdout_buf.getvalue()
_out["stderr"] = _stderr_buf.getvalue()
_value = _locals.get("result")
if isinstance(_value, (dict, list, tuple)):
    _out["result"] = json.dumps(_value, ensure_ascii=False, indent=2, default=str)
elif _value is None:
    _out["result"] = ""
else:
    _out["result"] = str(_value)

with open(_OUT_PATH, "w", encoding="utf-8") as _fh:
    json.dump(_out, _fh, ensure_ascii=False, default=str)

# matplotlib 的退出钩子可能很慢，直接退出，避免父进程等一个没有意义的收尾。
sys.stdout.flush()
os._exit(0 if _out["ok"] else 1)
'''


async def run_python_code(
    code: str,
    *,
    input_data: dict | None = None,
    artifacts_dir: Path,
    timeout_s: float,
    address_space_bytes: int = DEFAULT_ADDRESS_SPACE_BYTES,
) -> SandboxResult:
    """在子进程里执行一段 Python 代码。

    Args:
        code: 待执行代码。
        input_data: 注入沙箱的 `input_data` 变量（查询结果）。
        artifacts_dir: 图表等产物的落地目录。
        timeout_s: 硬超时（秒）；到点杀子进程，不指望它自己停。
        address_space_bytes: 子进程地址空间上限（0 表示不限制）。

    Returns:
        `SandboxResult`。**不抛异常** —— 沙箱失败也是一种结果。
    """
    started = time.perf_counter()
    # Python object introspection defeats builtins allowlists. Only run untrusted
    # code inside a mandatory OS namespace sandbox; never fall back to plain Python.
    bwrap = shutil.which("bwrap")
    if bwrap is None or sys.platform != "linux":
        return SandboxResult(ok=False, error="计算隔离环境不可用：需要 Linux 和 bubblewrap")
    workdir = Path(tempfile.mkdtemp(prefix="dbchat-sandbox-"))
    payload_path = workdir / "_payload.json"
    out_path = workdir / "_result.json"
    payload = json.dumps(
        {
            "code": code,
            "input_data": input_data or {},
            "preloaded": list(PRELOADED_MODULES),
            "lazy": list(LAZY_MODULES),
        },
        ensure_ascii=False,
        default=str,
    )
    # 文件 IO 一律下线程池：沙箱是异步路径，别让磁盘把事件循环卡住。
    await anyio.to_thread.run_sync(_write_text, payload_path, payload)

    env = _sandbox_env(workdir)
    proc: asyncio.subprocess.Process | None = None
    timed_out = False
    try:
        command = await anyio.to_thread.run_sync(_isolation_command, bwrap, workdir)
        proc = await asyncio.create_subprocess_exec(
            *command,
            "-c",
            PYTHON_BOOTSTRAP,
            str(payload_path),
            str(out_path),
            str(workdir),
            str(max(1, math.ceil(timeout_s))),
            str(int(address_space_bytes)),
            cwd=str(workdir),
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            process_stderr = await asyncio.wait_for(_wait_with_stderr(proc), timeout=timeout_s)
        except TimeoutError:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
        except asyncio.CancelledError:
            with anyio.CancelScope(shield=True):
                if proc.returncode is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
                await anyio.to_thread.run_sync(_rmtree, workdir)
            raise
    except Exception as exc:  # noqa: BLE001 - 起进程失败也要变成结果
        logger.warning("启动计算沙箱失败: %s: %s", type(exc).__name__, exc, exc_info=True)
        await anyio.to_thread.run_sync(_rmtree, workdir)
        return SandboxResult(
            ok=False,
            error="计算沙箱启动失败",
            error_detail=f"{type(exc).__name__}: {exc}",
            execution_ms=_elapsed_ms(started),
        )

    try:
        if timed_out:
            return SandboxResult(
                ok=False,
                error=f"Python 代码执行超时（超过 {timeout_s:g} 秒限制）",
                error_detail=f"子进程超过 {timeout_s:g}s 未结束，已强制终止",
                execution_ms=_elapsed_ms(started),
            )

        raw = await anyio.to_thread.run_sync(_read_result, out_path)
        if raw is None:
            return SandboxResult(
                ok=False,
                error="Python 代码执行失败（子进程未返回结果）",
                error_detail=f"exit_code={proc.returncode}: {process_stderr.decode(errors="replace")[-2000:]}",
                execution_ms=_elapsed_ms(started),
            )

        artifacts = await anyio.to_thread.run_sync(_collect_artifacts, workdir, artifacts_dir)
        extra: dict[str, object] = {}
        if raw.get("traceback"):
            extra["traceback"] = raw["traceback"]
        return SandboxResult(
            ok=bool(raw.get("ok")),
            stdout=str(raw.get("stdout") or ""),
            stderr=str(raw.get("stderr") or ""),
            result_text=str(raw.get("result") or ""),
            artifacts=artifacts,
            error=str(raw.get("error") or ""),
            error_detail=str(raw.get("traceback") or raw.get("error") or ""),
            execution_ms=_elapsed_ms(started),
            extra=extra,
        )
    finally:
        await anyio.to_thread.run_sync(_rmtree, workdir)


async def _wait_with_stderr(proc: asyncio.subprocess.Process) -> bytes:
    """Drain diagnostic output without letting untrusted writes grow parent RAM."""
    tail = b""
    assert proc.stderr is not None
    while chunk := await proc.stderr.read(8192):
        tail = (tail + chunk)[-2000:]
    await proc.wait()
    return tail


def _isolation_command(bwrap: str, workdir: Path) -> list[str]:
    """Expose only runtime dependencies and this job's writable directory."""
    command = [
        bwrap, "--unshare-all", "--unshare-user", "--die-with-parent", "--new-session",
        "--disable-userns", "--cap-drop", "ALL", "--proc", "/proc",
        "--dev", "/dev", "--tmpfs", "/tmp",
    ]
    # Mount Python/venv at their original paths to preserve pyvenv.cfg and .so RPATHs.
    runtime_paths = {
        sys.base_prefix, sys.prefix, str(Path(sys.executable).resolve().parent.parent),
        "/nix/store", "/usr/lib", "/usr/lib64", "/lib", "/lib64",
        "/usr/share/fonts", "/etc/fonts",
    }
    for path in sorted(runtime_paths, key=lambda p: (len(p), p)):
        if Path(path).exists():
            command.extend(["--ro-bind", path, path])
    command.extend(["--bind", str(workdir), str(workdir), "--chdir", str(workdir), str(Path(sys.executable).resolve())])
    return command


def _sandbox_env(workdir: Path) -> dict[str, str]:
    """子进程环境变量。

    必须**最小化**：`HOME` 与 `MPLCONFIGDIR` 指到工作目录，否则 matplotlib
    会往真实 HOME 写字体缓存（沙箱的副作用就漏到进程外了）。
    """
    config_dir = workdir / ".mplconfig"
    config_dir.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(workdir),
        "TMPDIR": str(workdir),
        "MPLCONFIGDIR": str(config_dir),
        # 保持与父进程一致的库搜索路径（NixOS 上 numpy/matplotlib 依赖它）
        "PYTHONHASHSEED": "0",
        "PYTHONPATH": str(Path(sys.prefix) / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", ""),
        "PYTHONIOENCODING": "utf-8",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
    }
    nix_loader = Path("/run/current-system/sw/share/nix-ld/lib/ld.so")
    if nix_loader.exists():
        env["NIX_LD"] = str(nix_loader.resolve())
    if os.environ.get("LD_LIBRARY_PATH"):
        env["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH"]
    return env


def _rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _write_text(path: Path, payload: str) -> None:
    path.write_text(payload, encoding="utf-8")


def _read_result(out_path: Path) -> dict | None:
    if not out_path.exists() or out_path.is_symlink():
        return None
    try:
        info = out_path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4 * 1024 * 1024:
            return None
        result = json.loads(out_path.read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("读取沙箱结果失败: %s: %s", type(exc).__name__, exc)
        return None


def _collect_artifacts(workdir: Path, artifacts_dir: Path) -> tuple[Artifact, ...]:
    """把沙箱产出的图表搬进 artifacts 目录。

    先搬后 GC：否则刚生成的图表可能被"保留最新 50 张"的逻辑按时间戳淘汰。
    """
    produced: list[Artifact] = []
    try:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        for source in sorted(glob.glob(str(workdir / "*.png"))):
            if Path(source).is_symlink() or not Path(source).is_file():
                continue
            name = _unique_name(artifacts_dir, Path(source).name)
            target = artifacts_dir / name
            shutil.move(source, target)
            produced.append(
                Artifact(
                    artifact_id=Path(name).stem,
                    kind="chart",
                    url=f"{ARTIFACT_ROUTE_PREFIX}/{name}",
                    title=Path(name).stem,
                )
            )
    except OSError as exc:
        logger.warning("搬运沙箱产物失败: %s: %s", type(exc).__name__, exc)
    finally:
        cleanup_artifacts(artifacts_dir)
    return tuple(produced)


def _unique_name(directory: Path, name: str) -> str:
    """避免同名图表互相覆盖（模型很爱把图都叫 chart.png）。"""
    candidate = directory / name
    if not candidate.exists():
        return name
    stem, suffix = Path(name).stem, Path(name).suffix
    for index in range(1, 1000):
        alt = f"{stem}_{index}{suffix}"
        if not (directory / alt).exists():
            return alt
    return f"{stem}_{int(time.time() * 1000)}{suffix}"


def cleanup_artifacts(directory: Path, keep: int = KEEP_ARTIFACTS) -> None:
    """只保留最新的 `keep` 张图表（历史 `_cleanup_charts` 的语义）。

    清理失败不影响本次结果，但必须记日志 —— 磁盘被图表塞满是真实发生过的
    问题（历史实现把它 `except: pass` 掉了）。
    """
    if not directory.is_dir():
        return
    try:
        files = [p for p in directory.glob("*.png")]
    except OSError as exc:
        logger.warning("列举图表目录失败: %s: %s", type(exc).__name__, exc)
        return
    if len(files) <= keep:
        return
    try:
        files.sort(key=lambda p: p.stat().st_mtime)
    except OSError as exc:
        logger.warning("图表按时间排序失败，跳过清理: %s: %s", type(exc).__name__, exc)
        return
    for path in files[:-keep]:
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("删除旧图表 %s 失败: %s", path.name, exc)


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
