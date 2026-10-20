# Проверка больших snapshot bundles

20.10.2026, WEB-12 / thr_28a59h3qwf. При исторической пересборке новый static
публиковался за125–133сек наVPS, повторно используемый — примерно за25сек.
Сборка читает уже загруженную БД: сетевого скачивания в этом этапе нет.

## Причина и измерение

Изолированно проверен настоящий static47,719,471байт JSON /2,438,440байт gzip,
SHA256 `293e0e087f01b306de420a8cb359f9cc80e32aabf9e03eade9c6fae7f0249eba`.
Данные хранились только во временном каталоге внеGit; отчёт содержит агрегаты.
Mac/Python3.13: прежняя проверка JSON Schema16.7875сек, canonical serialization0.3453сек,
SHA2560.0135сек, gzip0.5338сек. Это отдельные этапы, не полный wall time публикации.

## Изменение

Один compiled validator jsonschema-rs0.59.0 вместо Python-обхода всей схемы.
Registry содержит только поставляемые схемы; offline запрещает загрузку внешних refs.
Кэширование схем и выбор static/market/manifest сохранены. Пропуска проверки нет.
Формат bundles, canonical bytes, immutable hashes, транзакции и SQL не меняются.

Прежние Python type/pattern/format checks используются как callbacks совместимости
внутри единственного native validator. Python jsonschema остаётся зависимостью для
этих проверок и эталоном differential tests; второго runtime validation path нет.
Это сохраняет в том числе прежние особенности Python regex `$`, date formats и
отношение кtuple/bool/integer. Усиление правил дат требует отдельного изменения контракта.

Повторный замер итоговой реализации с этими callbacks на том же static:1.3138сек,
примерно12.8× быстрее проверки (исследовательский прогон:1.0777сек). Он не означает15.6× ускорение всего задания:
чтение/построение bundles, хэширование и работа сБД остаются.

SnapshotSchemaError сохраняется. Формулировки/число диагностических сообщений native
validator могут отличаться; принятие документов проверяется против прежнего validator.

## Выпуск и production

Выпущен подписанный[0.1.6](https://github.com/FlyingScotman/market-data-hub/releases/tag/v0.1.6),
commit a4f27ed28d823f50877c9b39765f375bbacd86ba, key3C9E6DACD133A1D8.
Архив SHA256 `f752ccae379b2c9f6cbce3d48dde6e8c1183abbe0d1d517d98534706027356a7`;373файла,26 прежних миграций.
Release workflow39450336558:CI/build/sign/verify/publish success. Подпись отдельно проверена
наMac иVPS старым trusted verifier, образ собран из архива, фактическая версия0.1.6.

910тестов,ruff lint/format,strict mypy прошли. Единственный независимый reviewer дал
APPROVED точному снимку исходников:111 дополнительных schema/golden/snapshot tests,
8,632 differential mutations без расхождений. После review исходники не менялись.

НаVPS одинаковый static, одинаковые1CPU/1GiB, изолированные процессы без сети/БД:
validation98.0052сек на0.1.5 →8.1669сек на0.1.6, **12.0×**.
Canonical bytes иSHA256 неизменны; canonical1.865→1.7523сек,gzip1.5373→1.5793сек.
Полные старые задания122.6–131.3сек; новые29.04/30.04 —23.190/23.948сек,
примерно**5×** быстрее. Обе публикации успешны,29.04 создала новую static, а не только reuse.

Пилот29.04,run4787:3,161/3,161 исходных price rows присутствуют; все close/WAprice/НКД
сравнены точно черезDecimal сAPI на том жеknown_at. Market/static gzip/SHA256 проверены,
ноль not_in_static_snapshot. У этой даты прежде не было публикации; пилот05.01 отдельно
уже подтвердил сохранность старого immutable id/supersedes.

Старый batch мягко завершён на80датах. Новый
`mdhub-snapshots-2026-fast-20261020.service` продолжает только оставшиеся даты,
принимая валидные producer0.1.5/0.1.6, с обычными advisory locks/quality checks.
Production current/API/admin/scheduler/Postgres остаются0.1.3, timestamps контейнеров
до/после совпали; внешний HTTPS/login/API healthy. Финальный аудит202/202 —WEB-11.
