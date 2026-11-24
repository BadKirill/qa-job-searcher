# Объединение обновлений зависимостей

24 ноября 2026. По запросу владельца изменения всех 12 открытых PR собраны
в ветку `codex/consolidate-open-prs`. Исходные коммиты сохранены merge-коммитами;
каждый исходный head является предком итоговой ветки. Закрытие заменённых PR
выполняется после успешного обязательного CI общего PR.

| Исходные PR | Изменения |
| --- | --- |
| #4 | @eslint/js 10.0.1 |
| #5, #7, #9 | Расширенные допустимые major-версии pypdf, MCP и ReportLab legacy |
| #10, #19 | Node typings 26.6.6 в обоих frontend |
| #16, #17, #18 | Те же допустимые major-версии для платформы |
| #25 | Vite 8.3.8, React plugin 6.3.0, Vitest/coverage 5.1.1 |
| #26 | React/React DOM 19.3.1, Playwright 1.65.1, testing-library и React typings |
| #27 | Ruff 0.17.6, pytest 9.2.0 для CI; SQLAlchemy 2.1.9, psycopg 3.3.9, Alembic 1.20.1 |

Конфликты деклараций разрешены сохранением изменений каждого PR. Оба npm lock-файла
пересобраны по совокупным версиям, после чего выполнена чистая установка `npm ci`.
Python locks синхронизированы с изменёнными декларациями и содержат SHA-256 hashes.
Без этой синхронизации установка через lock продолжала бы использовать прежние
SQLAlchemy/psycopg/Alembic и инструменты CI, несмотря на обновлённые декларации.

Расширение диапазона MCP/ReportLab/pypdf само по себе не обновляет закреплённую
runtime-версию. Совместимые существующие pins сохраняются; переход на следующий
major требует отдельного изменения lock и его проверки.

Команды синхронизации Python locks:

```sh
uv pip compile multiuser/pyproject.toml --extra dev --python-version 3.12 --generate-hashes --no-annotate --no-header --output-file multiuser/requirements.lock
uv pip compile ci/requirements.in --python-version 3.12 --generate-hashes --no-annotate --no-header --output-file ci/requirements.lock
```

Общий PR проходит прежний обязательный CI: отдельные полные quality gate обеих
версий, реальные PostgreSQL/API/browser E2E, Docker recovery rehearsal, CodeQL,
dependency/secret/IaC/image scans и проверки документов в ARM64 runtime.
Пороги 90% строк и 90% ветвлений, ноль skipped/flaky и запрет test retries сохранены.
Обновления остаются в PR; эта операция не сливает их в master.
