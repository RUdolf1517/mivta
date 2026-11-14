# Развёртывание и эксплуатация

## 1. Автоматическая установка на Ubuntu / Debian (`install.sh`)

Интерактивная установка или обновление одной командой:

```bash
curl -fsSL https://raw.githubusercontent.com/RUdolf1517/mivta/main/install.sh | sudo bash
```

Что делает скрипт:
- проверяет наличие Python `>= 3.11` с `venv` и `git` (при нехватке ставит пакеты через `apt`);
- клонирует или атомарно обновляет код в `/opt/mivta`, собирает изолированный `/opt/mivta/.venv`;
- создаёт системного пользователя `mivta` без shell-доступа и разводит права:
  - код, `.venv` и `install.sh` принадлежат `root:root` (`0755`) — процесс бота не может менять исполняемые файлы;
  - `/opt/mivta/.env` принадлежит `root:mivta` (`0640`);
  - `/opt/mivta/data` принадлежит `mivta:mivta` (`0700`, `UMask=0077`);
- регистрирует и запускает `mivta.service` с `systemd`-песочницей (`ProtectSystem=strict`, `NoNewPrivileges=true`, `PrivateTmp=true`) и ежедневный таймер автообновления `mivta-update.timer` на `06:00`.

### Неинтерактивная установка и флаги `install.sh`

```bash
curl -fsSL https://raw.githubusercontent.com/RUdolf1517/mivta/main/install.sh | sudo bash -s -- \
  --non-interactive \
  --token "123456:ABC..." \
  --provider groq \
  --api-key "gsk_..." \
  --swap-lang ru
```

Основные флаги:
- `--token <TOKEN>`, `--provider <NAME>`, `--api-key <KEY>`, `--api-url <URL>`, `--model <MODEL>`
- `--swap-lang <CODE>` — парный язык авто-разворота по умолчанию
- `--webhook-url <HTTPS>`, `--webhook-secret <SEC>`, `--webhook-port <PORT>` — включить режим Webhook
- `--auto-update-time <HH:MM>` / `--no-auto-update` — настроить или отключить ежедневный таймер обновления
- `--owner-id <ID>` — числовой Telegram ID владельца, которому доступны `/status`, `/update`, `/restart` и `/logs`
- `--no-remote-update` — не создавать триггер `mivta-update.path` (запретить обновление по кнопке из Telegram)
- `--self-update` — безопасное обновление с автоматическим откатом на предыдущий коммит при ошибке проверки пакета
- `--uninstall` / `--uninstall --purge` — удалить `systemd`-юниты (с `--purge` также удаляются `/opt/mivta` и пользователь `mivta`)

---

## 2. Полезные команды `systemd`

```bash
# Статус и последние логи сервиса
sudo systemctl status mivta
sudo journalctl -u mivta -n 50 --no-pager

# Проверить расписание ежедневного автообновления (06:00 по времени сервера)
sudo systemctl list-timers mivta-update.timer

# Принудительно запустить проверку обновлений прямо сейчас
sudo systemctl start mivta-update.service
```

### Обновление, перезапуск и логи прямо из Telegram (`/update`, `/restart`, `/logs`)

Если в `.env` указан `OWNER_ID`, владелец бота может обновлять и перезапускать сервис из личного чата — ssh больше не нужен:

| Команда | Что делает |
| :--- | :--- |
| `/update` | Проверяет новую ревизию/релиз и показывает кнопку «Обновить и перезапустить» |
| `/restart` | Просит подтверждение и поднимает процесс заново (~2 секунды) |
| `/logs` | Показывает последние записи WARNING/ERROR из памяти процесса (кнопки «🔄 Обновить» и «🧹 Очистить»); то же — кнопкой «📋 Логи ▸» в `/status` |

Как это работает без прав root и без sudo:

- **Перезапуск.** Бот останавливает приём апдейтов, закрывает соединения и подменяет сам себя через `os.execv` — PID и systemd-юнит сохраняются, `.env` перечитывается заново. Для Docker и других схем задайте `MIVTA_RESTART_CMD` (например `docker restart mivta`).
- **Обновление в обычной установке `install.sh`.** Код принадлежит root, поэтому бот лишь кладёт маркер `/opt/mivta/data/.update-request-<время>`, а привилегированный наблюдатель `mivta-update.path` (создаётся установщиком) запускает `mivta-update.service` → `install.sh --self-update` → `systemctl restart mivta`. Через 1–2 минуты обновлённый бот сам пишет владельцу в то же сообщение: «обновлено v7.4 → v7.5». Если код не обновился — приходит предупреждение с подсказкой проверить `journalctl -u mivta-update`.
- **Обновление, если бот владелец своего каталога** (Docker, `--no-systemd`, dev-чек-аут): бот сам делает `git fetch` + fast-forward `checkout` и перезапускается. Если новая ревизия меняет `requirements.txt` / `pyproject.toml`, бот честно просит запустить `sudo /opt/mivta/install.sh --self-update` — зависимости должен переустановить root.

Отключить функцию можно полностью (`SELF_UPDATE=false`, `SELF_RESTART=false`) или только триггер systemd (переустановка с `--no-remote-update`, либо `MIVTA_UPDATE_TRIGGER=0`).

> Уже установленный сервер: юниты `mivta-update.path` и новые переменные окружения в `mivta.service` появляются при перезапуске установщика — просто выполните `curl -fsSL https://raw.githubusercontent.com/RUdolf1517/mivta/main/install.sh | sudo bash` (`.env` и `data/` сохраняются), либо укажите `--owner-id <ID>` сразу.

---

## 3. Режим Webhook за Nginx / Caddy

По умолчанию `mivta` слушает `127.0.0.1:8080/webhook` и требует заголовок `X-Telegram-Bot-Api-Secret-Token` на каждом POST-запросе от Telegram (`HTTP 401` при отсутствии или несовпадении токена).

Пример блока для **Caddy**:

```caddyfile
bot.example.com {
    handle /webhook {
        reverse_proxy 127.0.0.1:8080
    }
}
```

Локальная проверка живости процесса (для мониторинга или оркестратора):

```bash
curl -fsS http://127.0.0.1:8080/healthz
# {"status": "ok", "version": "7.4"}
```

---

## 4. Docker Compose и готовый образ GHCR

При каждом релизе публикуется мультиархитектурный образ `ghcr.io/rudolf1517/mivta:latest` (`linux/amd64` и `linux/arm64`):

```bash
cp .env.example .env
# Заполните BOT_TOKEN в .env
docker compose pull && docker compose up -d
# Либо локальная сборка из исходников: docker compose up -d --build
```

Контейнер работает от непривилегированного пользователя `mivta` (`UID 10001`), монтирует `./data` в `/app/data` и использует встроенный `HEALTHCHECK` (`python -m mivta --check`).

### Логи в Telegram (`/logs`)

Команда `/logs` (и кнопка «📋 Логи ▸» в `/status`) показывает хвост служебного журнала уровня WARNING и выше: ошибки провайдеров перевода, срабатывания circuit breaker, предупреждения конфигурации, перезапуски. Буфер живёт только в памяти процесса (кольцевой, 200 записей, строка обрезается до 300 символов) и очищается кнопкой «🧹 Очистить», поэтому на диск и в Telegram не попадает ничего лишнего: **тексты сообщений, inline-запросы и переводы в логи не пишутся по-прежнему**. Если бот только что запустился и warnings не было, `/logs` честно отвечает «Логи пусты».
