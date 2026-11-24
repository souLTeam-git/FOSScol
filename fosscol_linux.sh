#!/usr/bin/env bash
cd "$(dirname "$(readlink -f "$0")")"
APP_DIR="$(pwd)"
SCRIPT_PATH="$APP_DIR/$(basename "$(readlink -f "$0")")"

if [ ! -f "venv/bin/python" ]; then
    echo "[1/4] Creating Python virtual environment..."
    python3 -m venv venv

    echo "[2/4] Installing dependencies from requirements.txt..."
    venv/bin/pip install --upgrade pip
    venv/bin/pip install -r requirements.txt

    echo "[3/4] Downloading Chromium browser for Playwright..."
    venv/bin/playwright install chromium

    echo "[4/4] Creating FOSScol shortcuts on Desktop and Application Menu..."
    DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
    mkdir -p "$DESKTOP_DIR" "$HOME/.local/share/applications"

    DESKTOP_FILE="$DESKTOP_DIR/FOSScol.desktop"
    cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=FOSScol
Comment=Cross-platform web scraper FOSScol
Exec="$SCRIPT_PATH"
Path=$APP_DIR
Icon=$APP_DIR/assets/logo_fosscol.png
Terminal=false
Categories=Utility;Network;
EOF

    chmod +x "$DESKTOP_FILE"
    cp "$DESKTOP_FILE" "$HOME/.local/share/applications/FOSScol.desktop"
    gio set "$DESKTOP_FILE" metadata::trusted true 2>/dev/null || true
fi

nohup venv/bin/python main.py >/dev/null 2>&1 &