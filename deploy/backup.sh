#!/usr/bin/env bash
# Nightly backup of the database and all uploaded/generated files (keeps 30 days).
# Cron (as user shk):  30 2 * * * /opt/shk/deploy/backup.sh
set -euo pipefail
mkdir -p /home/shk/backups
stamp=$(date +%F)
sqlite3 /opt/shk/data/shk.db ".backup /tmp/shk-$stamp.db"
tar -czf "/home/shk/backups/shk-$stamp.tar.gz" -C /tmp "shk-$stamp.db" -C /opt/shk data/quotes data/mtc data/scans 2>/dev/null || true
rm -f "/tmp/shk-$stamp.db"
find /home/shk/backups -name 'shk-*.tar.gz' -mtime +30 -delete
