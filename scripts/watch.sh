#!/bin/sh
# Log interface state every N seconds so a failure can be inspected afterwards.
#
# OPNsense's root shell is csh, so run this with sh:
#   sh /root/watch.sh re0 /root/watch.log &
#
# To keep it running after you log out:
#   nohup sh /root/watch.sh re0 /root/watch.log > /dev/null 2>&1 &
#
# Stop it with:  pkill -f watch.sh
#
# /root survives reboots; /tmp on OPNsense is RAM and does not.

IFACE="${1:-re0}"
LOG="${2:-/root/watch.log}"
INTERVAL="${3:-15}"

while true; do
    echo "=== $(date -u '+%a %b %d %H:%M:%S UTC %Y') ===" >> "$LOG"
    ifconfig "$IFACE" >> "$LOG" 2>&1
    netstat -i >> "$LOG" 2>&1
    sleep "$INTERVAL"
done
