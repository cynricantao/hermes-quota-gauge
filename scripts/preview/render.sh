#!/usr/bin/env bash
# Render the pane with a fixture and screenshot it in headless Chromium.
#   HERMES_REPO=~/.hermes/hermes-agent CHROME=/path/to/chrome scripts/preview/render.sh [fixture.json] [outdir]
# HERMES_REPO must be a Hermes checkout with node_modules and a built apps/desktop/dist (for the
# app's real CSS and theme variables). Default fixture: fixture.demo.json (synthetic data).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=${HERMES_REPO:?set HERMES_REPO to a Hermes checkout}
chrome=${CHROME:-$(command -v chromium || command -v google-chrome || true)}
fixture=${1:-$here/fixture.demo.json}
out=${2:-$here/../../docs/images}
work=$(mktemp -d)
cp "$fixture" "$here/fixture.json"
NODE_PATH="$repo/node_modules" "$repo/node_modules/.bin/esbuild" "$here/main.mjs" --bundle --format=iife \
  --outfile="$work/out.js" --alias:@hermes/plugin-sdk="$here/sdk-stub.mjs" \
  --define:process.env.NODE_ENV='"production"' --log-level=warning
rm -f "$here/fixture.json"
links=""
for css in "$repo"/apps/desktop/dist/assets/{index,sdk,tooltip}-*.css; do links+="<link rel=\"stylesheet\" href=\"file://$css\">"; done
cat > "$work/index.html" <<HTML
<!doctype html><html><head><meta charset="utf-8">$links
<style>:root.dark{--theme-foreground:#e6edf3;--theme-midground:#4a84fe;--theme-primary:#4a84fe;--theme-background-seed:#0d1117;--theme-sidebar-seed:#010409;--theme-card-seed:#010409;--theme-elevated-seed:#161b22;--theme-bubble-seed:#07162c}html,body{margin:0;background:var(--ui-bg-editor,var(--background))}#root{width:340px;height:1040px}</style>
</head><body><div id="root"></div><script src="out.js"></script></body></html>
HTML
mkdir -p "$out"
shot() { TZ=UTC "$chrome" --headless=new --no-sandbox --disable-gpu --hide-scrollbars --allow-file-access-from-files \
  --virtual-time-budget=4000 --window-size=340,1040 --screenshot="$out/$1.png" "file://$work/index.html?$2" >/dev/null 2>&1; }
shot quota-gauge-light ""
shot quota-gauge-dark "dark=1"
shot quota-gauge-hover "dark=1&hover=15"
shot quota-gauge-7d "dark=1&range=7d&hover=4"
rm -rf "$work"
ls -1 "$out"/quota-gauge-*.png
