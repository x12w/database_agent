#!/usr/bin/env bash
# (set -e removed — dual-server mode needs graceful handling of port conflicts)

cd "$(dirname "$0")"

# Create venv if not exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate venv
source venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install -q -r requirements.txt

# Dynamically locate system libraries on NixOS (store paths change with every update)
# numpy/matplotlib wheels from pip need: libstdc++, libgcc_s, libz, libglib, etc.
detect_nix_libraries() {
    local lib_paths=()
    local lib names found

    # List of critical libraries and their nix store name patterns
    # Format: "soname:package_pattern"
    names=(
        "libstdc++.so.6:gcc-*-lib"
        "libgcc_s.so.1:gcc-*-lib"
        "libz.so.1:zlib-*"
        "libglib-2.0.so.0:glib-*"
    )

    for entry in "${names[@]}"; do
        lib="${entry%%:*}"
        # Search nix store with a generous maxdepth; pick the latest version
        found=$(find /nix/store -maxdepth 6 -name "$lib" 2>/dev/null | grep -v fhsenv | sort -V | tail -1)
        if [ -n "$found" ] && [ -f "$found" ]; then
            lib_paths+=("$(dirname "$found")")
        fi
    done

    # Print unique paths, newline-separated
    if [ ${#lib_paths[@]} -gt 0 ]; then
        printf '%s\n' "${lib_paths[@]}" | sort -u
    fi
}

NIX_LIBS=$(detect_nix_libraries)
if [ -n "$NIX_LIBS" ]; then
    while IFS= read -r dir; do
        [ -d "$dir" ] && export LD_LIBRARY_PATH="${dir}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    done <<< "$NIX_LIBS"
    echo "  [nix] Added library paths to LD_LIBRARY_PATH"
fi

# Create data directories
mkdir -p data/clarifications
mkdir -p app/static/charts

# Check for .env file
if [ ! -f ".env" ]; then
    echo ""
    echo "⚠  .env file not found. Creating template..."
    cp .env.example .env 2>/dev/null || true
    echo "   The agent will run in limited mode (keyword-based classification, template SQL)."
    echo "   Edit .env to configure your LLM API key."
    echo ""
fi

# Kill any existing processes on our ports
echo "Checking for existing processes on ports 8000/8001..."
for port in 8000 8001; do
    existing=$(ss -tlnp 2>/dev/null | grep -oP "(?<=:)$port\s" | head -1)
    if [ -n "$existing" ]; then
        pid=$(ss -tlnp 2>/dev/null | grep ":$port " | grep -oP 'pid=\K\d+' | head -1)
        if [ -n "$pid" ]; then
            echo "  Killing existing process on port $port (pid $pid)..."
            kill "$pid" 2>/dev/null
            sleep 0.5
        fi
    fi
done

# Start both servers
echo ""
echo "Starting chat server on http://localhost:8000 ..."
echo "Starting admin panel on http://localhost:8001 ..."
echo ""

# Start main chat app in background
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload &
MAIN_PID=$!
sleep 1

# Check main app started
if ! kill -0 $MAIN_PID 2>/dev/null; then
    echo "ERROR: Main chat server failed to start (port 8000 may be in use)"
    exit 1
fi

# Start admin panel app in background
uvicorn app.admin_app:admin_app --host 0.0.0.0 --port 8001 --reload &
ADMIN_PID=$!

# Cleanup on exit
cleanup() {
    echo ""
    echo "Shutting down servers..."
    kill $MAIN_PID 2>/dev/null
    kill $ADMIN_PID 2>/dev/null
    wait $MAIN_PID 2>/dev/null
    wait $ADMIN_PID 2>/dev/null
    echo "Servers stopped."
}
trap cleanup EXIT INT TERM

# Wait for either process to exit
wait -n $MAIN_PID $ADMIN_PID
