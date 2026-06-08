#!/usr/bin/env bash
# movers スクリーナーの HTML UI を起動する (標準ライブラリのみ)。
#   ./web/run_html.sh            # http://localhost:8765
#   PORT=9000 ./web/run_html.sh
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 web/server.py
