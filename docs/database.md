# SQLite: схема и транзакции

## 1. Общие правила

- Python 3.11+, SQLite WAL.
- Sync API используется воркером, async API — интерфейсами.
- Каждое подключение выполняет:

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;
```

- Время — Unix timestamp `REAL`.
- `time.monotonic()` используется только внутри процесса и не сохраняется.
- Миграции имеют монотонный номер версии и применяются до запуска сервисов.
- Секретные токены и курьерские коды хранятся как hash.

## 2. Таблицы

### `users`

| Поле | Тип | Правило |
|---|---|---|
| `telegram_id` | INTEGER PK | Telegram ID или локальный ID |
| `username` | TEXT NULL | Необязательный username |
| `full_name` | TEXT | Отображаемое имя |
| `role` | TEXT | `admin`, `resident`, `blocked` |
| `created_at` | REAL | Время создания |
| `updated_at` | REAL | Время изменения |

### `access_rules`

Правила ACL и расписаний. Отсутствие разрешающего правила означает запрет. `blocked` и `admin_disabled` имеют приоритет над любыми правилами. Явный `deny` всегда сильнее `allow`.

| Поле | Тип | Правило |
|---|---|---|
| `id` | INTEGER PK | ID правила |
| `user_id` | INTEGER NULL FK | Конкретный пользователь; NULL — все пользователи |
| `door_id` | TEXT | Идентификатор двери |
| `method` | TEXT | Метод из контракта доступа |
| `effect` | TEXT | `allow` или `deny` |
| `priority` | INTEGER | Большее значение имеет больший приоритет |
| `valid_from` | REAL NULL | Начало действия |
| `valid_until` | REAL NULL | Окончание действия |
| `weekdays_mask` | INTEGER | Биты дней недели: Пн=1, Вт=2, …, Вс=64 |
| `start_local_time` | TEXT NULL | Начало интервала `HH:MM` |
| `end_local_time` | TEXT NULL | Конец интервала `HH:MM` |
| `timezone` | TEXT | IANA timezone, например `Europe/Moscow` |
| `is_active` | INTEGER | Активность правила |
| `created_at` | REAL | Создание |
| `updated_at` | REAL | Изменение |

Индекс: `(user_id, door_id, method, is_active, priority)`.

Сначала применяются `blocked`, `admin_disabled` и активные `deny`. Затем выбирается `allow` с максимальным `priority`; при равном приоритете правило конкретного пользователя сильнее общего правила с `user_id=NULL`. Дата проверяется через `valid_from`/`valid_until`, день недели — через `weekdays_mask`, локальное время — через `timezone`. Интервал `start_local_time > end_local_time` проходит через полночь. Отсутствие временных полей означает отсутствие соответствующего ограничения. До создания первого `allow` доступ запрещен.

### `face_embeddings`

| Поле | Тип | Правило |
|---|---|---|
| `id` | INTEGER PK | ID вектора |
| `user_id` | INTEGER FK | Владелец |
| `person_name` | TEXT | Имя человека |
| `embedding` | BLOB | Ровно 2048 байт |
| `is_active` | INTEGER | Пользовательский статус |
| `admin_disabled` | INTEGER | Административный запрет |
| `created_at` | REAL | Регистрация |
| `updated_at` | REAL | Изменение |

Индекс: `(user_id, is_active, admin_disabled)`. Несколько векторов одного человека допускаются для разных вариантов внешности. Периодическая дорегистрация выполняется администратором.

### `qr_tokens`

| Поле | Тип | Правило |
|---|---|---|
| `id` | INTEGER PK | ID пропуска |
| `token_hash` | TEXT UNIQUE | SHA-256 полного токена |
| `creator_id` | INTEGER FK | Владелец |
| `label` | TEXT | Название |
| `qr_type` | TEXT | `permanent`, `temporary`, `courier` |
| `max_uses` | INTEGER | 0 = безлимит |
| `used_count` | INTEGER | Текущий расход |
| `expires_at` | REAL NULL | Срок |
| `is_active` | INTEGER | Отзыв |
| `created_at` | REAL | Создание |

Индексы: `(creator_id, created_at)`, `(is_active, expires_at)`.

### `courier_sessions`

| Поле | Тип | Правило |
|---|---|---|
| `id` | INTEGER PK | ID сессии |
| `user_id` | INTEGER FK | Владелец |
| `qr_id` | INTEGER FK | Курьерский пропуск |
| `expected_code_hash` | TEXT | Hash ожидаемого кода |
| `code_salt` | TEXT | Сессионная соль |
| `status` | TEXT | `active`, `consumed`, `expired`, `cancelled` |
| `expires_at` | REAL | Срок ожидания |
| `unload_until` | REAL | Окончание разгрузки |
| `created_at` | REAL | Создание |
| `consumed_at` | REAL NULL | Успешное использование |

Индекс: `(status, expires_at)`.

### `ipc_commands`

| Поле | Тип | Правило |
|---|---|---|
| `id` | INTEGER PK | ID команды |
| `cmd_type` | TEXT | Тип команды |
| `payload` | TEXT | JSON object |
| `status` | TEXT | `pending`, `processed` |
| `result` | TEXT NULL | `ok`, `stale`, `error`, `cooldown` |
| `idempotency_key` | TEXT NULL UNIQUE | Защита повтора |
| `created_at` | REAL | Создание |
| `expires_at` | REAL | TTL |
| `processed_at` | REAL NULL | Обработка |

Индекс: `(status, id)`.

### `http_idempotency`

Хранит результат HTTP POST, чтобы повтор с тем же ключом возвращал идентичные HTTP status и тело без нового списания или IPC.

| Поле | Тип | Правило |
|---|---|---|
| `idempotency_key` | TEXT PK | Ключ клиента |
| `request_fingerprint` | TEXT | SHA-256 метода, kind, token_hash и нормализованного тела |
| `ipc_command_id` | INTEGER NULL FK | Связанная команда |
| `http_status` | INTEGER NULL | Сохраненный HTTP status |
| `response_body` | TEXT NULL | Сохраненное generic тело без секретов |
| `created_at` | REAL | Первый запрос |
| `completed_at` | REAL NULL | Формирование ответа |
| `expires_at` | REAL | Срок хранения ключа |

Индекс: `(expires_at)`.

Ключ резервируется в `BEGIN IMMEDIATE` до списания. Если ключ нов, создается запись с `request_fingerprint`. Если ключ существует с другим fingerprint, возвращается generic `409 Conflict` без изменения состояния. Если fingerprint совпадает, новый IPC не создается: после завершения возвращаются сохраненные `http_status` и `response_body`; для еще выполняющейся операции возвращается исходный `202`. Тело ответа не содержит токен, внутреннюю причину или персональные данные. Срок хранения определяется `HTTP_IDEMPOTENCY_RETENTION_SECONDS`.

### `bot_outbox`

| Поле | Тип | Правило |
|---|---|---|
| `id` | INTEGER PK | ID уведомления |
| `chat_id` | INTEGER | Получатель |
| `text` | TEXT | Только текст |
| `keyboard_json` | TEXT NULL | Inline keyboard |
| `status` | TEXT | `pending`, `sent`, `failed` |
| `attempts` | INTEGER | Сохраняется в БД |
| `next_attempt_at` | REAL | SQL-планирование |
| `last_error` | TEXT NULL | Без токенов/секретов |
| `created_at` | REAL | Создание |
| `sent_at` | REAL NULL | Отправка |

Индекс: `(status, next_attempt_at, id)`.

### `door_state`

Синглтон `id=1`: `last_motion_ts`, `last_person_ts`, `people_count`, `last_unlock_ts`. Все поля `REAL`, кроме `people_count INTEGER`.

### `access_logs`

| Поле | Тип | Назначение |
|---|---|---|
| `id` | INTEGER PK | ID решения |
| `method` | TEXT | Метод доступа |
| `user_id` | INTEGER NULL FK | Субъект/владелец |
| `outcome` | TEXT | `opened`, `rejected`, `error` |
| `reason` | TEXT | Машинная причина |
| `similarity` | REAL NULL | Сходство для лица |
| `details` | TEXT NULL | Без секретов |
| `snapshot_path` | TEXT NULL | Только если снимок разрешен |
| `created_at` | REAL | Время решения |

Индекс: `(created_at DESC, id DESC)`.

### `admin_audit`

`id`, `actor_id`, `action`, `target_type`, `target_id`, `details`, `created_at`. Фиксирует роли, блокировки, `admin_disabled`, регистрацию и удаление биометрии.

## 3. Атомарные операции

### Списание QR

```sql
BEGIN IMMEDIATE;
SELECT * FROM qr_tokens WHERE token_hash = ?;
-- проверить владельца, статус, срок, лимит и присутствие
UPDATE qr_tokens SET used_count = used_count + 1
WHERE id = ? AND used_count = ?;
COMMIT;
```

Проверка и изменение не разделяются. Параллельная попытка одноразового QR получает `exhausted`.

### Возврат QR

```sql
UPDATE qr_tokens SET used_count = MAX(0, used_count - 1)
WHERE id = ? AND used_count = ?;
```

Второй параметр — ожидаемое значение после списания. При несовпадении возврат не выполняется и создается ошибка аудита.

### Курьерская сессия

Проверка `active`, срока, владельца и ожидаемого кода выполняется в `BEGIN IMMEDIATE`. Окончательный `consumed` устанавливается только после успеха actuator. При ошибке сессия остается/возвращается в `active`, если `expires_at > now`.

### IPC

До actuator проверяются `status='pending'` и `expires_at >= now`. Обработка отмечается атомарно. Просроченная QR-команда получает `processed/stale` и возврат использования.

### Outbox

Выборка использует `status='pending' AND next_attempt_at <= now ORDER BY id LIMIT ?`. Отложенные записи не занимают рабочее окно. `sent` ставится только после подтверждения Telegram.

## 4. Валидация эмбеддингов

1. BLOB длина ровно 2048 байт.
2. `np.frombuffer(..., dtype=np.float32)` дает 512 значений.
3. Все значения конечны.
4. Норма больше нуля.
5. Для сравнения используется нормализованный `float32`.

Поврежденная запись пропускается с предупреждением, остальные загружаются.

## 5. Retention

Стартовые значения, утверждаемые в политике данных:

- processed IPC и HTTP idempotency: 24 часа;
- sent/failed outbox: 30 дней;
- истекшие/отозванные QR: 30 дней;
- access/admin audit: 90 дней;
- снимки отказов: хранение отключено; при включении — до 7 дней;
- биометрия: до отзыва/удаления по запросу.

Очистка выполняется пакетами, не удаляет активные доступы и записывает факт удаления биометрии в admin audit. Резервные копии имеют отдельный срок ротации.
