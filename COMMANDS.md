sqlite3 data/cache.db .schema > data/backup.schema
sqlite3 data/cache.db .dump | split -d -l 10000 /dev/stdin data/backup-
git add data/backup-* data/backup.schema
git commit -m "Periodic backup" data/backup-* data/backup.schema
git push
