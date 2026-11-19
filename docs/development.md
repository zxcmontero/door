# Руководство разработчика

## 1. Порядок чтения

1. [task-guide.md](task-guide.md) — ID задачи и обязательные источники.
2. [whitepaper.md](whitepaper.md) — продуктовые требования.
3. [architecture.md](architecture.md) — компоненты и структура кода.
4. [security.md](security.md) — безопасность и decision engine.
5. [access-contracts.md](access-contracts.md) и [database.md](database.md) — реализационные контракты.
6. [testing.md](testing.md) — обязательные проверки.
7. `ROADMAP.md` и `AGENTS.md` в корне — этапы и правила завершения.

## 2. Окружение

- Python 3.11+.
- Linux для целевого Docker/systemd deployment.
- NVIDIA RTX 3080, совместимый драйвер и NVIDIA Container Toolkit для GPU.
- SQLite и OpenCV.
- Системные библиотеки FFmpeg/GStreamer только для декодирования видео, не для создания архива.

Текущие runtime-зависимости находятся в `requirements.txt`. Development-окружение устанавливается через `requirements/dev.txt`, который включает runtime-зависимости и фиксированный `pytest`:

```bash
python -m pip install -r requirements/dev.txt
```

Целевое разделение на `requirements/base.txt`, `requirements/gpu.txt` и `requirements/dev.txt` выполняется отдельно: `base.txt` предназначен для приложения, SQLite, Telegram, HTTP, OpenCV и ZXing, `gpu.txt` — для совместимых InsightFace, `onnxruntime-gpu` и CUDA/cuDNN constraints, `dev.txt` — для pytest и вспомогательных test doubles.

Версии GPU-стека фиксируются после проверки совместимости на целевом сервере. Официальные источники и hashes/constraints используются для воспроизводимости; установщики не хранятся в репозитории.

## 3. Правила модулей

- CV не вызывает actuator.
- Decision engine не зависит от Telegram/HTTP.
- HTTP и Telegram используют общий access validator.
- Только воркер вызывает actuator.
- Бот не загружает CV/liveness.
- Синхронные SQLite/OpenCV/файлы не блокируют event loop.
- Внешние интерфейсы не содержат собственных правил списания.
- Видеоархив и отправка медиа не добавляются.

Подробная структура пакетов находится в [architecture.md](architecture.md).

## 4. Рабочий процесс

1. Проверить `git status` и актуальный этап ROADMAP.
2. Зафиксировать изменяемый контракт в профильном документе.
3. Написать негативный и положительный тесты.
4. Реализовать изменение без заглушек.
5. Добавить миграцию при изменении SQL.
6. Выполнить соответствующий уровень pytest.
7. Обновить README только если изменилась навигация, а не дублировать контракт.
8. Обновить ROADMAP отдельно для реализации и приемки.
9. Проверить итоговый diff и удалить временные артефакты.

## 5. Изменение контрактов

При изменении HTTP обновляются [access-contracts.md](access-contracts.md), тесты HTTP и документация конфигурации.

При изменении IPC обновляются [access-contracts.md](access-contracts.md), [database.md](database.md), миграции и тесты TTL/возвратов.

При изменении схемы обновляются [database.md](database.md), миграция, fixtures и тесты конкурентности.

При изменении liveness/N/T обновляются [security.md](security.md), ROC/DET-методика и регрессионный набор.

При изменении ретенции или персональных данных обновляются [privacy.md](privacy.md), [database.md](database.md) и [operations.md](operations.md).

## 6. Тестовые данные и секреты

- Используются синтетические эмбеддинги и кадры.
- Реальные лица, QR, база и журналы не копируются в fixtures.
- `.env` не читается тестами.
- Логи проверяются на отсутствие секретов.
- Test actuator не выполняет внешних вызовов.

## 7. Definition of Done

Изменение завершено только если:

- реализован полный пользовательский и негативный сценарий;
- постоянные тесты добавлены и проходят;
- профильная документация синхронизирована;
- отсутствуют секреты и временные файлы в diff;
- ограничения аппаратной проверки явно перечислены;
- ROADMAP не содержит неподтвержденных отметок приемки.
