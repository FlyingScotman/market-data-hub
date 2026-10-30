# Обновление до 0.6.0 (внутридневные данные для монитора фьючерсов RGBI)

Для владельца. Записка готовится вместе с PR задачи FlyingScotman/market-data-hub#95 (эпик
FlyingScotman/market-data-hub#94). Исходное состояние: production = 0.5.0 (после релиза
FlyingScotman/market-data-hub#83) или 0.4.0 — промежуточных шагов нет, `min_previous_version` не
менялся. Агент не ставил тег, не публиковал релиз и не трогал production: всё ниже делает владелец.

## Версия

Тег после слияния: **`v0.6.0`** (новые таблицы, задания и эндпоинты — минорная версия хаба по
правилу `docs/DEVELOPING.md`, «Version»). Контракт `market_snapshot` не меняется (1.6): новые ряды
в срезы не входят.

## Что приходит

- `futures_intraday` берёт, кроме валютных, фьючерсы активов `MDHUB_FUTURES_RATES_ASSETS`
  (по умолчанию RGBI — `RB*`, вечный `RGBIF`, `KEYRATE` — `KK*`, `RUONIA` — `RF*`) и их сделки
  (`md.futures_trade`); расписание с 07:00 (было с 08:50). Контракты, которых нет в ответе APIM,
  читаются с публичного ISS (`latency=delayed`), запуск `partial` с текстом «APIM answer lacks».
- Новые задания: `zcyc_securities` (раз в минуту 06:50–18:59, 6 опросов по 10 с),
  `index_intraday` (раз в минуту 07:00–18:59 и раз в 10 минут вечером), `index_constituents`
  (20:10 и 07:40), `repo_ccp_bond` и `repo_gc_term` (раз в 10 минут 07:00–19:05, EOD 19:30/19:35,
  final 07:35/07:40), `bond_close_quote` (19:10).
- Эндпоинты (scope `read`): `/v1/intraday/{futures,futures_trades,bonds,zcyc_securities,index,
  index_candles,repo,latency}`, `/v1/index/constituents`, `/v1/repo/{ccp,gc}`,
  `/v1/history/bond_close` — справочник `docs/INTRADAY_API.md`.

## Миграции

Три новых, аддитивных; данные не переписываются:

- `0280_md_intraday_rgbi` — столбцы `bid_depth`, `offer_depth` в `md.futures_intraday` и таблицы
  `md.futures_trade`, `md.zcyc_security_quote`, `md.index_intraday`, `md.index_candle`,
  `md.index_constituent`, `md.repo_intraday`, `md.repo_eod`, `md.bond_close_quote`;
- `0560_intraday_known_at` — процедура `ops.create_known_at_ts_function` и читатели
  «как было известно на момент» для новых таблиц, `md.futures_intraday` и `md.bond_quote_intraday`;
- `0670_job_source_intraday` — задания → источник `moex_iss` в `ops.job_source`.

Номера меньше уже применённых — это нормально: `mdhub migrate` применяет все неприменённые файлы
по имени. После установки `/health/ready`: `applied` 49, `known` 49, `pending` 0,
`checksum_mismatch` 0.

## Настройки

Новые переменные необязательны (значения по умолчанию подходят); в `deploy/env.example` они
перечислены закомментированными:

| переменная | по умолчанию | смысл |
|---|---|---|
| `MDHUB_FUTURES_RATES_ASSETS` | `RGBI,RGBIF,KEYRATE,RUONIA` | коды активов FORTS кроме валютных |
| `MDHUB_INTRADAY_INDICES` | `RGBI,RGBITR` | индексы для внутридневных значений и минутных свечей |
| `MDHUB_CONSTITUENT_INDICES` | `RGBI` | индексы, чей состав и веса грузятся ежедневно (подындексы `RUGBI*` тоже работают) |
| `MDHUB_BOND_CLOSE_CUT` | `18:45` | время закрывающего снимка bid/ask облигаций, МСК |

Новых секретов нет. `MOEX_API_KEY` прежний; покрывает ли подписка APIM фьючерсы RGBI, KEYRATE и
RUONIA — проверка в FlyingScotman/market-data-hub#96 (без неё данные всё равно идут, с задержкой
15 минут).

## Выпуск и установка (штатный механизм Updates)

Как в `UPGRADE_TO_0.5.0.md`, «Выпуск и установка»: слить PR, дождаться зелёного CI, поставить
тег `v0.6.0` на коммит слияния, одобрить `sign` в workflow `release`, установить 0.6.0 в консоли
→ **Updates**. На странице подтверждения — три миграции из списка выше.

## После установки: бэкфиллы

Плановые задания начнут писать внутридневные ряды сами. История нужна для трёх рядов. Задача
`quality` (08:30 и 20:30) сама поставит в очередь недостающие дни (`ops.backfill_task`) для
`index_constituent` (с 2016-12-13), `index_candle`, `repo_ccp_eod`, `repo_gc_eod` (с 2024-01-03),
и `backfill_worker` выполнит их порциями. Ускорить можно вручную, на сервере хаба функцией `dc`
(обёртка над `docker compose` с файлами установки, как в прошлых записках):

```bash
dc run --rm --no-deps worker mdhub run index_constituents -p date_from=2016-12-13 -p date_to=2018-01-31
dc run --rm --no-deps worker mdhub run index_intraday -p date_from=2026-01-05 -p date_to=2026-06-30
dc run --rm --no-deps worker mdhub run repo_ccp_bond -p mode=final -p date_from=2026-09-01 -p date_to=2026-10-28
dc run --rm --no-deps worker mdhub run repo_gc_term -p mode=final -p date_from=2026-09-01 -p date_to=2026-10-28
```

- `dc` — `docker compose` с compose-файлами текущей установки;
- `run` — выполнить команду в новом разовом контейнере сервиса; `--rm` — удалить контейнер после
  завершения; `--no-deps` — не запускать зависимые сервисы (база уже работает);
- `worker` — сервис, в образе которого есть CLI `mdhub` и доступ к базе;
- `mdhub run <задание>` — выполнить задание один раз (строка в `ops.job_run`, trigger `cli`);
- `-p` (= `--param`) — параметр задания в виде `ключ=значение`: `date_from` / `date_to` — первый и
  последний день диапазона включительно (берутся только рабочие дни `MOEX_STOCK`); `mode=final` —
  режим чтения дневной истории репо.

Ограничения одного запуска: `index_constituents` и `index_intraday` — не более 400 рабочих дней,
`repo_*` — не более 120, поэтому длинную историю грузить несколькими командами с соседними
диапазонами (или оставить очереди `quality`/`backfill_worker`). Состав RGBI — 2 запроса на день
(~2 500 дней с 2016 года), минутные свечи — 2 запроса на индекс и день, репо — 5 страниц на
борд и день (ccp) и 1 (gcc). ISS рвёт соединение примерно на каждом 3–4-м запросе; клиент
повторяет запросы с экспоненциальной задержкой и случайным разбросом, ошибка дня видна в
`/v1/runs`.

## Проверка

Через read-API (токен в переменной окружения, не в команде), в торговые часы:

```bash
H="Authorization: Bearer $MDHUB_TOKEN"; B="https://<hub host>"
curl -sS "$B/health/ready"
curl -sS -H "$H" "$B/v1/intraday/futures?from=$(date +%F)&secid=RGBIF,RBZ6&fields=bid,offer,last,source,latency&limit=5"
curl -sS -H "$H" "$B/v1/intraday/zcyc_securities?from=$(date +%F)&limit=3"
curl -sS -H "$H" "$B/v1/intraday/latency"
curl -sS -H "$H" "$B/v1/runs?job=futures_intraday&status=partial&limit=3"
```

- `H=…; B=…` — две переменные оболочки: заголовок авторизации (токен из `$MDHUB_TOKEN`) и адрес
  хаба (`<hub host>` подставить); `;` разделяет команды.
- `curl` — HTTP-запрос; `-sS` — без индикатора прогресса, но с текстом ошибки; `-H "$H"` —
  добавить заголовок; аргумент в кавычках — URL (кавычки нужны из-за `?` и `&`).
- `$(date +%F)` — подстановка сегодняшней даты в формате `ГГГГ-ММ-ДД` (`date` — дата, `+%F` —
  формат); дата сервера должна быть московской или передать её явно.
- `from=` — начало диапазона (дата = с 00:00 МСК), `secid=` — инструменты через запятую,
  `fields=` — нужные столбцы, `limit=` — строк на страницу; `job=`, `status=` — фильтры журнала
  запусков.

Ожидание:

- `/health/ready`: `"version":"0.6.0"`, `migrations`: `applied` 49, `pending` 0;
- `/v1/intraday/futures` — строки `RGBIF`/`RBZ6`; `source` `moex_apim` и `latency` `realtime`,
  если подписка покрывает контракты, иначе `moex_iss` / `delayed` (записать в #96);
- `/v1/intraday/zcyc_securities` — строки ОФЗ с `latency` `realtime`;
- `/v1/intraday/latency` — по каждому набору `age_s` и `lag_p50_s`: секунды для realtime, ~900
  для delayed;
- в `/v1/runs?job=futures_intraday&status=partial` нет новых строк (или только «APIM answer
  lacks» — см. #96).

Результаты записать в задачу релиза 0.6.0 (завести по образцу #83).
