#!/bin/bash
# ===================================================================
#  Mac_Create desktop shortcut.command  (macOS)
#
#  Double-click this ONCE from the oTree Lab Launcher folder. It works
#  out its own location, then creates a proper Finder ALIAS
#
#      Start oTree Lab Launcher
#
#  on your Desktop, pointing at the default launcher in THIS folder
#      Mac_Start oTree Lab Launcher.command
#
#  This mirrors the Windows "Start oTree Lab Launcher.lnk": a real
#  Finder alias (not an app bundle). Double-clicking it opens the
#  .command in Terminal with your full login environment, which starts
#  the launcher in your default browser.
#
#  Nothing to edit by hand: the target is an absolute path computed
#  from where THIS file lives.
# ===================================================================
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
TARGET="$HERE/Mac_Start oTree Lab Launcher.command"
DESKTOP="$HOME/Desktop"
ALIAS_NAME="Start oTree Lab Launcher"
ALIAS_PATH="$DESKTOP/$ALIAS_NAME"

echo
echo "Creating desktop shortcut (Finder alias)..."
echo "  Shortcut : $ALIAS_PATH"
echo "  Target   : $TARGET"
echo

if [ ! -f "$TARGET" ]; then
  echo "ERROR: cannot find the launcher next to this file:"
  echo "       $TARGET"
  echo "Make sure this .command is inside the oTree Lab Launcher folder."
  read -r -p "Press return to close." _
  exit 1
fi

# Make sure the launcher itself is executable so the alias runs it in Terminal.
chmod +x "$TARGET" 2>/dev/null || true

# Remove any previous alias we made (Finder refuses to make a second alias to the
# same target, and we want a clean, correctly-named one).
rm -f "$ALIAS_PATH" 2>/dev/null || true

# Ask Finder to make the alias, then rename it to the friendly name. Using
# POSIX file <path> keeps spaces in the path safe. osascript returns non-zero and
# prints the AppleScript error if anything fails, which "set -e" will surface.
osascript - "$TARGET" "$ALIAS_NAME" <<'APPLESCRIPT'
on run argv
    set targetPOSIX to item 1 of argv
    set aliasName to item 2 of argv
    tell application "Finder"
        set newAlias to make alias file to POSIX file targetPOSIX at (path to desktop folder)
        set name of newAlias to aliasName
    end tell
end run
APPLESCRIPT

# Optional branding: a Finder alias shows the target's icon (a script icon here).
# Setting a custom icon on an alias reliably needs resource-fork tools that are
# not guaranteed present, so we leave the default icon.
if [ -f "$HERE/app/branding/logo.icns" ]; then
  echo "Note: leaving the default alias icon (a custom icon on a Finder alias is"
  echo "      not set reliably without extra tools; app/branding/logo.icns unused)."
fi

echo
echo "Done. Look on your Desktop for \"$ALIAS_NAME\"."
echo "(If macOS blocks it the first time, right-click the alias and choose Open.)"
echo
read -r -p "Press return to close." _
