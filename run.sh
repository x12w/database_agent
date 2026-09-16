#!/usr/bin/env bash
# dbchat 启动脚本 —— 把环境准备好，然后起**一个**服务。
#
# 与重构前的区别：那时这里要拉起两个 uvicorn 进程（8000 聊天 / 8001 管理面板），
# 于是连接状态、会话、表筛选在两边各有一份。现在管理面板是同一个应用里的一条
# 路由（/admin），一个进程、一份状态。
set -euo pipefail

cd "$(dirname "$0")"

VENV="venv"
# 解释器版本是硬约束：dmPython 只发布了 cp313 的 ABI wheel，
# 换到 3.14 会让达梦（国产库）支持直接消失。
PYTHON_PIN="3.13"

# --------------------------------------------------------------- 1. 虚拟环境
venv_python_version() {
    "$VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true
}

if [ ! -x "$VENV/bin/python" ]; then
    echo "[env] 创建虚拟环境（Python $PYTHON_PIN）..."
    rm -rf "$VENV"   # 半拉子目录留着只会一直报同一个错
    if command -v uv >/dev/null 2>&1; then
        uv venv --python "$PYTHON_PIN" "$VENV"
    elif command -v "python$PYTHON_PIN" >/dev/null 2>&1; then
        "python$PYTHON_PIN" -m venv "$VENV"
    else
        echo "[env] 错误：找不到 uv 或 python$PYTHON_PIN，无法创建 $PYTHON_PIN 环境。" >&2
        exit 1
    fi
fi

found_version="$(venv_python_version)"
if [ "$found_version" != "$PYTHON_PIN" ]; then
    echo "[env] 错误：$VENV 里的 Python 是 ${found_version:-未知}，需要 $PYTHON_PIN。" >&2
    echo "      dmPython（达梦驱动）只发布了 cp313 的 ABI wheel，换版本会让达梦支持失效。" >&2
    echo "      请删除 $VENV 后重新运行本脚本。" >&2
    exit 1
fi

# --------------------------------------------------------------- 2. 依赖
echo "[env] 安装依赖..."
if command -v uv >/dev/null 2>&1; then
    uv pip install -q --python "$VENV/bin/python" -e ".[dev]"
else
    "$VENV/bin/python" -m pip install -q --upgrade pip
    "$VENV/bin/python" -m pip install -q -e ".[dev]"
fi

# --------------------------------------------------------------- 3. 前端产物
if [ ! -f "web/dist/index.html" ]; then
    if command -v npm >/dev/null 2>&1; then
        echo "[web] 未找到 web/dist，构建前端（npm ci && npm run build）..."
        ( cd web && npm ci --no-audit --no-fund && npm run build )
    else
        echo "[web] 警告：没有 npm，跳过前端构建。" >&2
        echo "      服务仍可启动，/ 会返回『前端尚未构建』的提示页，/api/* 正常。" >&2
    fi
fi

# --------------------------------------------- 4. NixOS 动态库路径
# 动态定位系统库（nix store 路径随每次更新变化）。
# pip 装的 numpy/matplotlib/torch 需要：libstdc++、libgcc_s、libz、libglib 等。
detect_nix_libraries() {
    local lib_paths=()
    local lib names found

    # Format: "soname:package_pattern"
    names=(
        "libstdc++.so.6:gcc-*-lib"
        "libgcc_s.so.1:gcc-*-lib"
        "libz.so.1:zlib-*"
        "libglib-2.0.so.0:glib-*"
    )

    for entry in "${names[@]}"; do
        lib="${entry%%:*}"
        found=$(find /nix/store -maxdepth 6 -name "$lib" 2>/dev/null | grep -v fhsenv | sort -V | tail -1)
        if [ -n "$found" ] && [ -f "$found" ]; then
            lib_paths+=("$(dirname "$found")")
        fi
    done

    if [ ${#lib_paths[@]} -gt 0 ]; then
        printf '%s\n' "${lib_paths[@]}" | sort -u
    fi
}

NIX_LIBS=$(detect_nix_libraries || true)
if [ -n "$NIX_LIBS" ]; then
    while IFS= read -r dir; do
        [ -d "$dir" ] && export LD_LIBRARY_PATH="${dir}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    done <<< "$NIX_LIBS"
    echo "  [nix] 已把系统库路径加入 LD_LIBRARY_PATH"
fi

# --------------------------------------------------------------- 5. .env
if [ ! -f ".env" ]; then
    echo ""
    echo "⚠  未找到 .env，正在从模板创建..."
    cp .env.example .env 2>/dev/null || true
    echo "   服务会以受限模式启动（没有模型 key 时只能回答结构类问题）。"
    echo "   请编辑 .env 配置模型服务与默认数据库。"
    echo ""
fi

# --------------------------------------------------------------- 6. 端口
PORT="${PORT:-8000}"
if command -v ss >/dev/null 2>&1; then
    existing_pid="$(ss -tlnp 2>/dev/null | grep ":$PORT " | grep -oP 'pid=\K[0-9]+' | head -1 || true)"
    if [ -n "$existing_pid" ]; then
        echo "[port] 端口 $PORT 已被进程 $existing_pid 占用，先停掉它..."
        kill "$existing_pid" 2>/dev/null || true
        sleep 1
    fi
fi

# --------------------------------------------------------------- 7. 启动
mkdir -p var
echo ""
echo "启动 dbchat: http://localhost:${PORT}  （管理面板：/admin）"
echo ""
exec "$VENV/bin/python" -m dbchat
