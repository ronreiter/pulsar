#!/bin/sh
# Build Pulsar Studio.app (needs only the Xcode command line tools).
set -e
cd "$(dirname "$0")"
swift build -c release
APP="build/Pulsar Studio.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp .build/release/PulsarStudio "$APP/Contents/MacOS/PulsarStudio"
[ -f Resources/AppIcon.icns ] || swift make_icon.swift Resources/icon_1024.png
cp Resources/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
CLI="${PULSAR_BIN:-$(cd .. && pwd)/.venv/bin/pulsar}"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Pulsar Studio</string>
  <key>CFBundleDisplayName</key><string>Pulsar Studio</string>
  <key>CFBundleIdentifier</key><string>dev.pulsar.studio</string>
  <key>CFBundleExecutable</key><string>PulsarStudio</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundleIconName</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>PulsarCLI</key><string>$CLI</string>
  <key>CFBundleDocumentTypes</key><array><dict>
    <key>CFBundleTypeName</key><string>Pulsar Song</string>
    <key>CFBundleTypeRole</key><string>Editor</string>
    <key>CFBundleTypeExtensions</key><array><string>pulse</string></array>
    <key>LSHandlerRank</key><string>Owner</string>
  </dict></array>
</dict></plist>
PLIST
codesign --force --sign - "$APP" >/dev/null 2>&1 || true
echo "built $APP (CLI: $CLI)"
