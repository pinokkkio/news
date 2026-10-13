#!/usr/bin/env bash
# Управление ботом через Docker. Использование: ./run.sh <команда>
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Создан файл .env — впишите в него TELEGRAM_BOT_TOKEN и TELEGRAM_CHANNEL_ID и запустите снова."
  exit 1
fi

# USE_GPU=1 в .env — запускать нейросеть на видеокарте NVIDIA
if grep -qE '^USE_GPU=1' .env; then
  compose() { docker compose -f docker-compose.yml -f docker-compose.gpu.yml "$@"; }
else
  compose() { docker compose "$@"; }
fi

bot() { compose build -q bot && compose run --rm bot python bot.py "$@"; }

gpu_check() {
  echo "1) Драйвер NVIDIA на компьютере:"
  if ! nvidia-smi --query-gpu=name,memory.total --format=csv,noheader; then
    echo "   nvidia-smi не найден — установите драйвер NVIDIA."; exit 1
  fi
  echo "2) Видеокарта видна из Docker:"
  if ! docker run --rm --gpus all --entrypoint nvidia-smi ollama/ollama:latest -L; then
    echo "   Docker не видит видеокарту — установите nvidia-container-toolkit:"
    echo "   https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html"
    exit 1
  fi
  echo "3) Где сейчас работает модель (PROCESSOR должен быть «100% GPU»):"
  compose exec ollama ollama ps 2>/dev/null \
    || echo "   ollama не запущена — выполните ./run.sh test и проверьте снова."
  grep -qE '^USE_GPU=1' .env || echo "Чтобы включить GPU, добавьте в .env строку USE_GPU=1 и выполните ./run.sh restart"
}

case "${1:-help}" in
  test)    compose up -d ollama && bot --test-model ;;
  feeds)   bot --check-feeds ;;
  preview) compose up -d ollama && bot --dry-run ;;
  start)   compose up -d --build && echo "Бот запущен. Логи: ./run.sh logs" ;;
  logs)    compose logs -f bot ;;
  stop)    compose down ;;
  restart) compose up -d --build --force-recreate ;;
  gpu)     gpu_check ;;
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
  ./run.sh gpu      проверить, работает ли нейросеть на видеокарте (USE_GPU=1 в .env)
HELP
    ;;
esac
