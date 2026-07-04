#!/bin/zsh
cd /Users/imkimhk/Project/nanobot-queen
pkill -f "nanobot.queen.gateway" 2>/dev/null
pkill -f "nanobot.queen.telegram_runner" 2>/dev/null
sleep 1
./start-queen.sh

# Restart Telegram runner after gateway restart.
# Use the project venv Python explicitly so Python 3.11+ / tomllib is available.
QUEEN_GATEWAY_URL=http://127.0.0.1:8900 QUEEN_USER_KEY=user-key \
  nohup /Users/imkimhk/Project/nanobot-queen/.venv/bin/python -m nanobot.queen.telegram_runner \
  > /tmp/nbq-telegram.log 2>&1 &
disown
