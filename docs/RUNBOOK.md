# RUNBOOK — Кузя (telegram-ai-bot)

Операционный runbook для self-hosted деплоя. Секреты и токены сюда не писать.

## Deploy (systemd)

Путь продукта: `/opt/bots/telegram-ai-bot`  
Unit: `kuzya-bot.service` (`Restart=on-failure`)

```bash
cd /opt/bots/telegram-ai-bot
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
# заполнить .env (TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, …) — не коммитить
sudo systemctl daemon-reload
sudo systemctl enable --now kuzya-bot.service
sudo systemctl status kuzya-bot.service
sudo journalctl -u kuzya-bot.service -f
```

Перезапуск после обновления кода:

```bash
cd /opt/bots/telegram-ai-bot
git pull
./venv/bin/pip install -r requirements.txt
sudo systemctl restart kuzya-bot.service
```

Проверки: `systemctl is-active kuzya-bot.service`, в Telegram `/status` или `/selftest` (админ).

## Backup / restore (SQLite)

Файлы:

- `bot_database.db` — диалоги, настройки, редактор, шаблоны/якоря (если включены)
- `bot_statistics.db` — статистика и баны

Backup (короткое окно обслуживания):

```bash
sudo systemctl stop kuzya-bot.service
cp /opt/bots/telegram-ai-bot/bot_database.db /backup/bot_database_$(date +%F).db
cp /opt/bots/telegram-ai-bot/bot_statistics.db /backup/bot_statistics_$(date +%F).db
sudo systemctl start kuzya-bot.service
```

Restore:

```bash
sudo systemctl stop kuzya-bot.service
cp /backup/bot_database_YYYY-MM-DD.db /opt/bots/telegram-ai-bot/bot_database.db
cp /backup/bot_statistics_YYYY-MM-DD.db /opt/bots/telegram-ai-bot/bot_statistics.db
sudo systemctl start kuzya-bot.service
```

## Ротация ключей

1. Выпустить новый ключ у провайдера (BotFather / OpenAI / Tavily).
2. Обновить `/opt/bots/telegram-ai-bot/.env` и локальный vault (не в git).
3. `sudo systemctl restart kuzya-bot.service`
4. Smoke: `/start`, короткий поисковый запрос, `/status`.
5. Отозвать старый ключ у провайдера после успешного smoke.

## Инциденты

| Симптом | Куда смотреть | Действие |
|---------|---------------|----------|
| Crash-loop / Token invalid | `journalctl -u kuzya-bot` | Проверить `TELEGRAM_BOT_TOKEN` |
| Нет веб-поиска | логи Tavily, `.env` | `TAVILY_API_KEY`, квота |
| OpenAI timeout / 5xx | логи `APIStatusError` | Повтор, прокси `PROXY_*` |
| Нет блока «Источники» | запрос без триггера поиска | Маркеры актуальности / кнопка веб-поиска |
| Concierge кнопка при OFF | `CONCIERGE_ENABLED` | Должна скрываться в `send_ai_reply` |
| Высокая нагрузка autofetch | флаги редактора | Держать `CONTENT_EDITOR_AUTOFETCH_ENABLED=false` |

Self-heal: фоновая диагностика (`SelfDiagnostics`), heartbeat middleware; при критических сбоях — уведомление `ADMIN_ID` / безопасный restart по политике кода.

## Rollback

```bash
cd /opt/bots/telegram-ai-bot
git log --oneline -5
git checkout <known_good_sha>
./venv/bin/pip install -r requirements.txt
sudo systemctl restart kuzya-bot.service
```

При откате схемы SQLite — восстановить backup `.db` за ту же дату, что и код.

## Связанные документы

- README продукта (паспорт, scope, env)
- `docs/editorial-qualities.md` — качества редактора
- Библиотека overlays: [kartuznik/bot-components](https://github.com/kartuznik/bot-components)
