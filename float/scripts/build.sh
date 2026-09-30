#!/bin/sh
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
task_name="Codex Float"
task_bundle_id="io.github.chengjun023.codex-float"
task_demo="false"
if [ "${1:-}" = "--demo" ]; then
  task_name="Token Inspector Demo"
  task_bundle_id="io.github.chengjun023.token-inspector-demo"
  task_demo="true"
fi
task_app="$task_root/build/$task_name.app"
mkdir -p "$task_app/Contents/MacOS" "$task_app/Contents/Resources/backend" "$task_app/Contents/Resources/data"
if [ -n "${ADAPTIVE_ROUTER_SOURCE:-}" ]; then
  python3 "$task_root/scripts/sync_router.py" --source "$ADAPTIVE_ROUTER_SOURCE"
fi
xcrun swiftc -parse-as-library -swift-version 5 -target "$(uname -m)-apple-macos14.0" -O \
  -framework SwiftUI -framework AppKit "$task_root/app/CodexFloat.swift" -o "$task_app/Contents/MacOS/CodexFloat"
cp "$task_root/backend/monitor.py" "$task_root/backend/meter.py" "$task_root/backend/difficulty.py" \
  "$task_root/backend/usage_ledger.py" "$task_root/backend/model_registry.py" "$task_app/Contents/Resources/backend/"
cp "$task_root/data/models.bundled.json" "$task_root/data/router-vendor.json" "$task_app/Contents/Resources/data/"
cat > "$task_app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>$task_name</string>
<key>CFBundleDisplayName</key><string>$task_name</string>
<key>CFBundleIdentifier</key><string>$task_bundle_id</string>
<key>CFBundleExecutable</key><string>CodexFloat</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>0.5.0</string>
<key>CFBundleVersion</key><string>5</string>
<key>LSMinimumSystemVersion</key><string>14.0</string>
<key>TokenInspectorDemo</key><$task_demo/>
<key>LSUIElement</key><true/>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
cp "$task_root/demo/demo-snapshot.json" "$task_app/Contents/Resources/"
codesign --force --sign - "$task_app"
printf '%s\n' "$task_app"
