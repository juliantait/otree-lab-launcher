#!/bin/bash
# ===================================================================
#  Create desktop shortcut.command  (macOS)
#
#  Double-click this ONCE from the oTree Lab Launcher folder. It works
#  out its own location, then builds a small app bundle
#
#      Start oTree Lab Launcher.app
#
#  on your Desktop. That app:
#    * runs the web launcher in THIS folder
#        (Mac_Start oTree Lab Launcher (web).command)
#    * shows the lab logo (from branding/logo.icns)
#
#  A Desktop copy is no longer next to the folder, so the path to the
#  launcher is baked in as an absolute path computed from where THIS
#  file lives. Nothing to edit by hand.
# ===================================================================
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
TARGET="$HERE/Mac_Start oTree Lab Launcher (web).command"
# Single canonical logo. To rebrand, just regenerate branding/logo.icns.
ICON="$HERE/branding/logo.icns"
DESKTOP="$HOME/Desktop"
APP="$DESKTOP/Start oTree Lab Launcher.app"

echo
echo "Creating desktop shortcut..."
echo "  Shortcut : $APP"
echo "  Target   : $TARGET"
echo "  Icon     : $ICON"
echo

if [ ! -f "$TARGET" ]; then
  echo "ERROR: cannot find the web launcher next to this file:"
  echo "       $TARGET"
  echo "Make sure this .command is inside the oTree Lab Launcher folder."
  read -r -p "Press return to close." _
  exit 1
fi

# Fresh bundle (remove any previous one so we do not merge stale files).
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# Icon
if [ -f "$ICON" ]; then
  cp "$ICON" "$APP/Contents/Resources/icon.icns"
else
  echo "WARNING: icon not found ($ICON); the app will use a generic icon."
fi

# PkgInfo
printf 'APPL????' > "$APP/Contents/PkgInfo"

# Info.plist
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Start oTree Lab Launcher</string>
  <key>CFBundleDisplayName</key><string>Start oTree Lab Launcher</string>
  <key>CFBundleIdentifier</key><string>eu.juliantait.otree-lab-launcher</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleSignature</key><string>????</string>
  <key>CFBundleExecutable</key><string>start</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>LSMinimumSystemVersion</key><string>10.13</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# Start stub with the absolute target baked in.
{
  printf '#!/bin/bash\n'
  printf 'exec "%s"\n' "$TARGET"
} > "$APP/Contents/MacOS/start"
chmod +x "$APP/Contents/MacOS/start"

# Nudge Finder to pick up the new icon.
touch "$APP" 2>/dev/null || true

echo "Done. Look on your Desktop for \"Start oTree Lab Launcher\"."
echo "(If macOS blocks it the first time, right-click the app and choose Open.)"
echo
read -r -p "Press return to close." _
