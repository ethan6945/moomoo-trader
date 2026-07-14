#!/bin/bash
# Installs the passwordless `pmset disablesleep` sudoers rule. MUST run as root —
# invoked either by the web ☕ toggle (via a macOS admin-auth dialog) or by
# setup_lid_nosleep.command (via `sudo`). Arg 1 = username to grant the rule to.
#
# The rule is scoped to ONLY `pmset -a disablesleep 0|1` — nothing else.
set -e
USER_NAME="${1:?username required}"
RULE="$USER_NAME ALL=(root) NOPASSWD: /usr/bin/pmset -a disablesleep 0, /usr/bin/pmset -a disablesleep 1"
DEST="/etc/sudoers.d/moo-nosleep"

# Validate in a temp file first so a typo can never corrupt the sudoers config.
TMP="$(mktemp)"
printf '%s\n' "$RULE" > "$TMP"
if ! /usr/sbin/visudo -cf "$TMP" >/dev/null 2>&1; then
    rm -f "$TMP"
    exit 3
fi
cp "$TMP" "$DEST"
chown root:wheel "$DEST"
chmod 440 "$DEST"
rm -f "$TMP"
