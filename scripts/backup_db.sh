#!/usr/bin/env bash
# Ночная копия БД Закупщика: data/backups/zakupshchik-ГГГГ-ММ-ДД.dump (формат pg_dump -Fc), хранение 14 дней.
# Запуск по cron на сервере стенда:  30 3 * * * /opt/zakupshchik/scripts/backup_db.sh >> /tmp/zakupshchik-backup.log 2>&1
# Восстановление на новом сервере (пустая БД zakupshchik и роли из .env уже созданы):
#   docker exec -i shared-infra-postgres pg_restore -U postgres -d zakupshchik --no-owner --role=zakupshchik_owner < файл.dump
# Повторный запуск безопасен: файл за день перезаписывается.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)/data/backups"
PG="${PG_CONTAINER:-shared-infra-postgres}"
DB="${PG_DATABASE:-zakupshchik}"
KEEP_DAYS="${KEEP_DAYS:-14}"
mkdir -p "$DIR"
out="$DIR/zakupshchik-$(TZ=Europe/Moscow date +%F).dump"
tmp="$out.part"

docker exec "$PG" pg_dump -U postgres -d "$DB" -Fc > "$tmp"
# Копия годная, только если pg_restore читает её оглавление
docker exec -i "$PG" pg_restore --list < "$tmp" > /dev/null
mv "$tmp" "$out"
chmod 600 "$out"
find "$DIR" -name 'zakupshchik-*.dump' -mtime +"$KEEP_DAYS" -delete
echo "$(date -Is) ok $(du -h "$out" | cut -f1) $out"
