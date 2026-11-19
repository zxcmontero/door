# Конфигурация

## 1. Профили

- `test` — значение по умолчанию, test actuator, реальное реле недоступно.
- `pilot` — Dahua actuator разрешен после стендовой приемки, ограниченная группа пользователей.
- `production` — только после завершения ROADMAP, обязательные healthcheck, backup, мониторинг и политика данных.

Переменная `APP_ENV=test|pilot|production`. Для `pilot` и `production` требуются явные калиброванные параметры распознавания и liveness.

## 2. Telegram и HTTP

| Переменная | По умолчанию | Правило |
|---|---|---|
| `BOT_TOKEN` | Нет | Обязателен для Telegram; секрет |
| `BOT_USERNAME` | Пусто | Получается через `getMe`, можно переопределить |
| `ADMIN_IDS` | Нет | Непустой список положительных ID |
| `LOG_CHAT_ID` | Пусто | Только текстовые события |
| `PUBLIC_BASE_URL` | Пусто | HTTPS для внешнего доступа |
| `HTTP_HOST` | `127.0.0.1` | За reverse proxy |
| `HTTP_PORT` | `8080` | Положительный свободный порт |
| `TRUSTED_PROXY_IPS` | Пусто | Только эти proxy задают client IP |
| `HTTP_GET_RATE_PER_MINUTE` | `30` | Положительное число |
| `HTTP_POST_RATE_PER_MINUTE` | `10` | Положительное число |
| `RATE_LIMIT_BLOCK_SECONDS` | `60` | Положительное число |
| `HTTP_IDEMPOTENCY_RETENTION_SECONDS` | `86400` | Срок хранения ответа idempotency |

## 3. Dahua и actuator

| Переменная | По умолчанию | Правило |
|---|---|---|
| `ACTUATOR_MODE` | `test` | `test` или `dahua` |
| `DAHUA_IP` | Нет | Адрес без схемы и `/` |
| `DAHUA_USER` | Нет | Отдельная учетная запись |
| `DAHUA_PASS` | Нет | Секрет |
| `DAHUA_RTSP_PORT` | `554` | Положительный порт |
| `DAHUA_CHANNEL` | `1` | Положительный канал |
| `ACTUATOR_OPEN_TTL_SECONDS` | `5` | Время удержания реле |
| `ACTUATOR_TIMEOUT_SECONDS` | `10` | Таймаут запроса |

В `APP_ENV=test` значение `ACTUATOR_MODE=dahua` считается ошибкой. Данные Dahua обязательны только для реального адаптера.

## 4. Хранение

| Переменная | По умолчанию |
|---|---|
| `DATABASE_PATH` | `data/access.db` |
| `MODEL_DIR` | `data/models` |
| `FACE_ENROLLMENT_DIR` | `data/enrollment` |
| `REJECTION_SNAPSHOT_DIR` | `data/rejections` |
| `REJECTION_SNAPSHOTS_ENABLED` | `false` |

Каталоги не содержат публично раздаваемых файлов. Права доступа минимальные. Видеоархива и временных видеокаталогов нет.

## 5. Видео и decision engine

| Переменная | По умолчанию | Ограничение |
|---|---|---|
| `RTSP_FPS` | `10` | Положительное число |
| `FRAME_MAX_AGE_SECONDS` | `2.0` | Больше нуля |
| `FACE_ANALYSIS_INTERVAL` | `0.2` | Больше нуля |
| `MIN_FACE_WIDTH_PX` | `80` | Положительное число |
| `FACE_DET_SCORE` | `0.60` | 0–1 |
| `FACE_SIMILARITY_THRESHOLD` | Нет | Обязателен вне test; 0–1, по ROC/DET |
| `FACE_CONFIRMATIONS_REQUIRED` | `4` | Положительное число |
| `FACE_CONFIRMATION_WINDOW_SECONDS` | `3.0` | Больше нуля |
| `DOOR_UNLOCK_COOLDOWN_SECONDS` | `10` | Больше нуля |
| `MOTION_TAIL_SECONDS` | `2.5` | Диапазон 2–3 |
| `PRESENCE_PERSON_SECONDS` | `5.0` | Больше нуля |
| `PRESENCE_MOTION_SECONDS` | `4.0` | Больше нуля |
| `COURIER_PRESENCE_SECONDS` | `3.0` | Больше нуля |

Порог similarity не получает универсального значения по умолчанию. Для pilot/production конфигурация без задокументированного ROC/DET не запускается.

## 6. Liveness и вычисления

| Переменная | По умолчанию | Правило |
|---|---|---|
| `LIVENESS_MODEL_PATH` | Нет | Обязателен для доступа по лицу |
| `LIVENESS_THRESHOLD` | Нет | Подбирается на наборе атак |
| `EXECUTION_PROVIDER` | `CUDA` | `CUDA` или `CPU` |
| `ALLOW_CPU_FALLBACK` | `true` | Fallback всегда создает алерт |
| `OPENCV_THREADS` | `1` | Положительное число |

При `EXECUTION_PROVIDER=CUDA` старт проверяет фактический provider. Его отсутствие при `ALLOW_CPU_FALLBACK=false` является фатальной ошибкой.

## 7. IPC, outbox и курьеры

| Переменная | По умолчанию |
|---|---|
| `IPC_COMMAND_TTL_SECONDS` | `15` |
| `OUTBOX_POLL_SECONDS` | `0.5` |
| `OUTBOX_MAX_ATTEMPTS` | `5` |
| `OUTBOX_MAX_BACKOFF_SECONDS` | `300` |
| `COURIER_FACE_TTL_SECONDS` | `600` |
| `COURIER_UNLOAD_SECONDS` | `600` |

IPC TTL должен быть в диапазоне 10–15 секунд. Outbox хранит `attempts` и `next_attempt_at` в SQLite.

## 8. Retention и эксплуатация

| Переменная | По умолчанию |
|---|---|
| `IPC_RETENTION_SECONDS` | `86400` |
| `OUTBOX_RETENTION_SECONDS` | `2592000` |
| `QR_RETENTION_SECONDS` | `2592000` |
| `AUDIT_RETENTION_SECONDS` | `7776000` |
| `REJECTION_SNAPSHOT_RETENTION_SECONDS` | `604800` |
| `LOG_LEVEL` | `INFO` |
| `METRICS_HOST` | `127.0.0.1` |
| `METRICS_PORT` | `9090` |

## 9. Валидация и секреты

- Отсутствующая обязательная переменная останавливает запуск с именем переменной.
- Пути разрешаются от каталога проекта и создаются с минимальными правами.
- Логин/пароль Dahua экранируются в RTSP URL.
- `.env` не коммитится и не читается тестами.
- Секреты не выводятся в validation errors, журналы и HTTP responses.
- Изменение конфигурации требует обновления `.env.example`, этого документа и тестов валидации.
