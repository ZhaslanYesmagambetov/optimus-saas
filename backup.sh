#!/bin/bash
# Директория для бэкапов
BACKUP_DIR="/root/optimus_bot_postgre/backups"
mkdir -p "$BACKUP_DIR"

# Генерируем имя файла с текущей датой
DATE=$(date +"%Y-%m-%d_%H-%M")
FILE_NAME="$BACKUP_DIR/optimus_db_$DATE.sql"

# Делаем дамп базы (Убедись, что юзер и база совпадают с твоим .env)
# Если в .env у тебя POSTGRES_USER=postgres и POSTGRES_DB=optimus_db:
docker exec optimus_home_postgres pg_dump -U postgres optimus_db > "$FILE_NAME"

# Очищаем старые бэкапы (оставляем только за последние 7 дней)
find "$BACKUP_DIR" -type f -name "*.sql" -mtime +7 -exec rm {} \;

echo "✅ Backup completed: $FILE_NAME"
