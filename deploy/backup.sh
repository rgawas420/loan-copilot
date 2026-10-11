#!/usr/bin/env sh
# Nightly backup of everything the app cannot rebuild: the SQLite databases (consistent copies, taken while the
# app runs), attachments, ad images and the knowledge-base documents. Run from the project folder on the server:
#   crontab -e   ->   30 2 * * * cd /opt/urban-money && ./deploy/backup.sh >> backups/backup.log 2>&1
set -eu
STAMP=$(date +%Y%m%d-%H%M%S)
DEST="backups/$STAMP"
mkdir -p "$DEST/db"

for db in storage/*.db; do
  [ -f "$db" ] || continue
  # SQLite's online backup: safe while the app is writing
  docker compose exec -T app python -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close()" \
    "/srv/var/$(basename "$db")" "/srv/var/.backup-$(basename "$db")"
  mv "storage/.backup-$(basename "$db")" "$DEST/db/$(basename "$db")"
  gzip "$DEST/db/$(basename "$db")"          # the question log makes these large; text compresses ~10x
done

tar -czf "$DEST/files.tar.gz" --exclude='storage/kb_cache' --exclude='storage/*.db' --exclude='storage/*.db-wal' \
    --exclude='storage/*.db-shm' storage kb
find backups -maxdepth 1 -type d -name '20*' -mtime +14 -exec rm -rf {} +    # keep two weeks
echo "backup $STAMP done"
