# Как поднять tg-ws-proxy

Этот README только про запуск `tg-ws-proxy`. Publisher-бот и основной пайплайн здесь не описаны.

## 1. Зачем нужен proxy

`tg-ws-proxy` поднимается локально и используется Telethon-клиентом как MTProto proxy:

```text
service / Telethon -> 127.0.0.1:1443 -> Telegram
```

В проекте параметры proxy читаются из окружения или из `tools/.env.tools`:

```bash
TG_PROXY_HOST="127.0.0.1"
TG_PROXY_PORT="1443"
TG_PROXY_SECRET="32_hex_chars_secret"
```

`TG_PROXY_SECRET` должен быть строкой из 32 hex-символов.

## 2. Установка из исходников

```bash
cd ~
git clone https://github.com/Flowseal/tg-ws-proxy.git
cd tg-ws-proxy
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Проверь доступный entrypoint:

```bash
python -m proxy.tg_ws_proxy --help
```

Если модульный запуск в твоей версии не работает, проверь прямой файл:

```bash
python proxy/tg_ws_proxy.py --help
```

## 3. Генерация secret

```bash
python - <<'PY'
import secrets
print(secrets.token_hex(16))
PY
```

Скопируй результат в `tools/.env.tools`:

```bash
TG_PROXY_SECRET="сюда_сгенерированный_secret"
```

## 4. Запуск proxy

```bash
cd ~/tg-ws-proxy
source .venv/bin/activate

export TG_PROXY_HOST="127.0.0.1"
export TG_PROXY_PORT="1443"
export TG_PROXY_SECRET="сюда_сгенерированный_secret"

python -m proxy.tg_ws_proxy \
  --host "$TG_PROXY_HOST" \
  --port "$TG_PROXY_PORT" \
  --secret "$TG_PROXY_SECRET"
```

Если в твоей версии установлен console-script `tg-ws-proxy`, можно запустить так:

```bash
tg-ws-proxy \
  --host 127.0.0.1 \
  --port 1443 \
  --secret "$TG_PROXY_SECRET"
```

Proxy должен оставаться запущенным всё время, пока сервис или бот обращается к Telegram.

## 5. Проверка, что порт слушается

В другом терминале:

```bash
ss -ltnp | grep 1443
```

Ожидаемо увидеть процесс, который слушает `127.0.0.1:1443`.

## 6. Частые проблемы

### `proxy argument will be ignored`

Для Telethon нужен пакет `python-socks[asyncio]`:

```bash
pip install 'python-socks[asyncio]'
```

### `Connection refused`

Proxy не запущен, упал или слушает другой порт. Проверь:

```bash
ss -ltnp | grep 1443
```

### Неверный secret

Сгенерируй новый secret из 32 hex-символов и укажи один и тот же `TG_PROXY_SECRET` и при запуске proxy, и в `tools/.env.tools` проекта.
