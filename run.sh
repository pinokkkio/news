#!/usr/bin/env bash
# Управление ботом через Docker. Использование: ./run.sh <команда>
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Создан файл .env — впишите в него TELEGRAM_BOT_TOKEN и TELEGRAM_CHANNEL_ID и запустите снова."
  exit 1
fi

bot() { docker compose run --rm --build bot python bot.py "$@"; }

case "${1:-help}" in
  test)    docker compose up -d ollama && bot --test-model ;;
  feeds)   bot --check-feeds ;;
  preview) docker compose up -d ollama && bot --dry-run ;;
  start)   docker compose up -d --build && echo "Бот запущен. Логи: ./run.sh logs" ;;
  logs)    docker compose logs -f bot ;;
  stop)    docker compose down ;;
  restart) docker compose up -d --build --force-recreate bot ;;
  *)
    cat <<'HELP'
Команды:
  ./run.sh test     проверить нейросеть: как она оценивает важность тестовых новостей
  ./run.sh feeds    проверить, какие RSS-ленты доступны
  ./run.sh preview  показать текущие главные сюжеты и пример поста (без публикации)
  ./run.sh start    запустить бота в фоне
  ./run.sh logs     смотреть, что делает бот
  ./run.sh restart  перезапустить (после изменения .env или feeds.txt)
  ./run.sh stop     остановить
HELP
    ;;
esac
