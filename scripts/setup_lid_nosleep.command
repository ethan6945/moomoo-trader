#!/bin/bash
# ── One-time setup: let the web ☕ toggle also block LID-CLOSED sleep ───────────
# By default the ☕ keep-awake toggle uses `caffeinate`, which stops idle/system
# sleep but CANNOT keep a MacBook awake with the lid shut (clamshell sleep is
# firmware-level). Overriding it needs `sudo pmset -a disablesleep 1`.
#
# This installs a tightly-scoped, passwordless sudo rule so the web server (which
# runs as you, no root) can flip ONLY that one pmset setting on/off. You enter your
# Mac password ONCE here; after that the ☕ toggle handles lid-closed sleep too.
#
# ⚠️  When lid-closed sleep is disabled and you shut the lid, the Mac keeps running
#     full-tilt. In a closed bag it can OVERHEAT. Only use it on a desk / well
#     ventilated, ideally on power. Turn ☕ off (or close this rule) when done.
#
# To UNDO this rule later:  sudo rm /etc/sudoers.d/moomoo-nosleep
set -e
cd "$(dirname "$0")"

DEST="/etc/sudoers.d/moomoo-nosleep"

echo "MooMoo Trader — lid-closed keep-awake setup"
echo "-------------------------------------------"
echo "This installs a passwordless sudo rule scoped to ONLY pmset disablesleep,"
echo "so the web ☕ toggle can also block lid-closed sleep."
echo "You'll be asked for your Mac password once."
echo

# Shared installer (also used by the web toggle's GUI auth dialog).
sudo /bin/bash "./install_lid_rule.sh" "$(whoami)"

# Verify the passwordless path actually works now.
if sudo -n /usr/bin/pmset -g >/dev/null 2>&1; then
    echo "✓ Installed. The ☕ toggle will now also block lid-closed sleep."
else
    echo "✓ Rule installed at $DEST."
    echo "  (If the toggle still doesn't affect lid sleep, reopen Terminal and retry.)"
fi
echo
echo "Undo anytime with:  sudo rm $DEST"
sleep 4
