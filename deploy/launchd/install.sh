#!/bin/bash
# Install/reload the four launchd agents (3 pollers + dashboard) for the
# current user. Idempotent — safe to re-run after editing a plist.
# Use when setting up the engine on a macOS host, or after changing a unit.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
LABELS=(pinnacle-poller kalshi-poller polymarket-poller ev-dashboard)
failed=0

mkdir -p "$AGENTS" "$REPO/data"

for name in "${LABELS[@]}"; do
    src="$REPO/deploy/launchd/com.pmev.$name.plist"
    dst="$AGENTS/com.pmev.$name.plist"
    # Rewrite the repo path so the plists work from any checkout location.
    sed "s|__REPO__|$REPO|g" "$src" > "$dst"
    launchctl bootout "gui/$UID/com.pmev.$name" 2>/dev/null || true
    # bootout is async: bootstrapping while the old job is still tearing down
    # fails with "Input/output error" (5). Wait for the label to disappear.
    for _ in $(seq 1 50); do
        launchctl print "gui/$UID/com.pmev.$name" >/dev/null 2>&1 || break
        sleep 0.2
    done
    # Don't let one bad unit abort the rest of the install.
    if launchctl bootstrap "gui/$UID" "$dst" 2>/dev/null; then
        echo "loaded com.pmev.$name"
    else
        echo "FAILED com.pmev.$name" >&2
        failed=1
    fi
done

echo
echo "status:"
launchctl list | grep pmev || echo "  (none running)"
exit "$failed"
