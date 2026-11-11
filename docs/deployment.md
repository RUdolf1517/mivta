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
# {"status": "ok", "version": "0.3.1"}
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
