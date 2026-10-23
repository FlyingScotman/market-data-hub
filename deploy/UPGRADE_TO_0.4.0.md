# Обновление до 0.4.0 (контракт `market_snapshot/1.5`: входы деривативов для монитора)

Для владельца. Состояние 23.10.2026: production = **0.3.4** (контракт `market_snapshot/1.4`,
41 миграция; FlyingScotman/market-data-hub#20). `main` содержит PR #27 (пакет P6 монитора,
бриф TDM H10–H13, FX-форварды H1–H4) — его нет ни в одном теге. Релиз 0.4.0 его выпускает;
задача — FlyingScotman/market-data-hub#35. Агент не ставил тег, не публиковал релиз и не трогал
production: всё ниже делает владелец.

## Версия

Тег после слияния PR релиза: **`v0.4.0`**, не 0.3.5. Правило репозитория (UPGRADE_TO_0.3.0,
UPGRADE_TO_0.3.2): новые наборы контракта, таблицы, источники и задания — минорная версия;
исправления без изменения контракта — патч (0.3.1, 0.3.3). Здесь новая минорная версия
контракта (1.5), четыре миграции, новая таблица `md.fx_settle_date`, новый источник `snb` и
задание `saron_fixing`. `min_previous_version` не менялся (`0.1.0`): 0.4.0 ставится прямо поверх
0.3.4, промежуточных шагов нет.

Номер 0.4.0 раньше был обещан работе WEB-18; она вышла как v0.3.2, её документ переименован в
[UPGRADE_TO_0.3.2.md](UPGRADE_TO_0.3.2.md).

## Что приходит

Контракт `market_snapshot/1.5` (аддитивно; полный список — `schemas/market_snapshot/v1/README.md`,
строка 1.5):

- **`fx_rates`**: у строк `moex_eod` поля `settle_date` (MOEX SETTLEDATE сессии торговой даты,
  таблица `md.fx_settle_date`) и `settlement_calendars`. Дата валютирования только наблюдается,
  не вычисляется: `md.fx_settle_date` копится **с момента установки** (`fx_moex_eod`,
  `fx_moex_intraday`), более ранние строки получают `null` и метку `settle_date_not_observed`.
  Дата установки ограничивает глубину этой истории — записать её в #35.
- **`fx_swaps`** (новый набор): свопы CETS X/RUB с ценой свопа как у MOEX, `near_settle_date` /
  `far_settle_date`, `settlement_calendars` (в 1.0–1.4 они исключались с причиной `fx_swap`).
- **`futures_prices`** (EOD: все FORTS, расчётная цена, OHLC, открытый интерес, спецификация
  контракта) и **`futures_prices_intraday`** (последняя котировка FX-фьючерсов). Данные уже
  собираются (`futures_eod`, `futures_intraday`), новое — только их публикация в срезе.
- **`cpi`** (новый набор): `CPI_RU_MOM` (Росстат) и `CPI_RU_YOY` (ЦБ) с 2015-01
  (`parameters.cpi_from`), метка `published_on_unknown`. История на production уже есть
  (м/м с 2010-01, г/г с 2013-09; проверено 23.10 через `/v1/history/fixings`).
- **`fixings`**: coverage `ruonia_unpublished_days`; фиксинг `SARON` (источник `snb`, `unit = pct`).
- **Календари** `CN_INTERBANK`, `US_FED`, `CH_ZURICH` в `calendars` static-среза.

Прочее: задание `saron_fixing` (ежедневно 19:20 и 09:20 МСК, портал данных SNB), проба `snb` на
странице Sources, лимит запросов к SNB 1/с.

**Потребители.** Каждый объект схемы — `additionalProperties: false`. Потребитель, который
строго проверяет 1.3/1.4, отклонит срез 1.5; читатель «любой 1.x, неизвестное игнорировать»
остаётся корректным. Читатель 1.5 на стороне деска — в trading-desk-monitor.

## Миграции

Четыре новых, все аддитивны (номера меньше уже применённой `0820` — это нормально:
`mdhub migrate` применяет все неприменённые файлы по имени и проверяет контрольные суммы
применённых):

- `0270_md_fx_settle_date` — таблица `md.fx_settle_date` (+ история строк).
- `0428_fx_settlement_calendars` — CHECK `ref.calendar_day.calendar` расширен значениями
  `CN_INTERBANK`, `US_FED`, `CH_ZURICH` (меняются только CHECK и комментарий).
- `0550_derivative_inputs_known_at` — функции «как известно на момент» для
  `md.fx_settle_date`, `md.futures_eod`, `ref.futures_contract`.
- `0661_saron_source` — источник `snb` в `ops.source`, связь задания `saron_fixing` с ним.

После установки `/health/ready`: `applied` 45, `known` 45, `pending` 0, `checksum_mismatch` 0.
Новых настроек и секретов нет.

## Выпуск и установка (штатный механизм Updates)

1. Слить PR релиза в `main`, дождаться зелёного CI на коммите слияния.
2. Поставить тег `v0.4.0` на коммит слияния (README_DEPLOY §11) командами git. Через
   браузер **не** ставить: веб-интерфейс GitHub создаёт тег только вместе с релизом
   (Releases → Publish), а шаг `publish` workflow сам создаёт релиз (`gh release create`) и
   упадёт на уже существующем.

   ```bash
   git switch main && git pull --ff-only
   git tag -a v0.4.0 -m "market-data-hub 0.4.0"
   git push origin v0.4.0
   ```

   - `git switch main` — перейти на ветку `main`; `&&` — следующую команду выполнить, только
     если предыдущая успешна; `git pull --ff-only` — подтянуть коммит слияния только перемоткой.
   - `git tag -a v0.4.0` — аннотированный тег; из него hatch-vcs берёт версию 0.4.0; `-m "…"` —
     сообщение тега.
   - `git push origin v0.4.0` — отправить тег в `origin`; это запускает `release.yml`
     (ci → build → sign с одобрением в окружении `release` → verify → publish).
3. Actions → workflow `release` → одобрить `sign`; дождаться GitHub Release с тремя файлами
   (`.tar.gz`, `.SHA256SUMS`, `.SHA256SUMS.minisig`).
4. Консоль хаба → **Updates** → 0.4.0 → «Review and install». На странице подтверждения — четыре
   миграции выше. Updater проверит подпись, сделает дамп, остановит воркер, применит миграции,
   перезапустит сервисы и проверит `/health/ready`; при ошибке откатится сам.

## После установки

Всё — с консоли (**Jobs** → задание → «Run now», параметры по умолчанию) или ждать расписания.

1. **Sources**: проба `snb` — `ok`. Из среды разработки SNB не отвечал; если проба падает,
   `saron_fixing` будет `failed`, остальное не затрагивается (SARON — только данные, не схема).
2. **`cbr_meetings`** (по умолчанию `mode=calendar`) — пересчитает календари и запишет
   `CN_INTERBANK`, `US_FED`, `CH_ZURICH`. По расписанию это только первый рабочий день месяца;
   до запуска пары CNY/USD/CHF получают метку `foreign_calendar_unknown`.
3. **`saron_fixing`** — первая загрузка SARON (результат в `/v1/runs?job=saron_fixing`).
4. **`snapshot_static`** — static-срез 1.5 с восемью календарями (иначе плановый в 07:45).
5. Market-срезы 1.5 выйдут сами: ближайший intraday, `eod_preliminary` вечером,
   `eod_final` утром следующего рабочего дня. `settle_date` и `fx_swaps` заполнятся после
   первого `fx_moex_eod` / `fx_moex_intraday` новой версии.

## Проверка

Через read-API (токен в переменной окружения, не в команде):

```bash
H="Authorization: Bearer $MDHUB_TOKEN"; B="https://<hub host>"
curl -sS "$B/health/ready"
curl -sS -H "$H" "$B/v1/snapshots?kind=static&latest=true"
curl -sS -H "$H" "$B/v1/snapshots?kind=market&latest=true"
```

- `H=…; B=…` — две переменные оболочки: заголовок авторизации (токен берётся из `$MDHUB_TOKEN`)
  и адрес хаба; `;` разделяет команды.
- `curl` — HTTP-запрос; `-sS` — без индикатора прогресса, но с текстом ошибки; `-H "$H"` —
  добавить заголовок; аргумент в кавычках — URL (кавычки нужны из-за `?` и `&`).

Ожидание:

- `"version":"0.4.0"`, `migrations`: `applied` 45, `pending` 0, `checksum_mismatch` 0;
- последний static-срез: `schema_version` `market_snapshot/1.5`, `rows.calendars` 8;
- первый market-срез после установки: `schema_version` `market_snapshot/1.5`, в `rows` есть
  `cpi`, `futures_prices` (EOD-срезы) и, после `fx_moex_*`, `fx_swaps`; в манифесте coverage
  `fx_rates.moex_eod_with_settle_date` > 0 и `fixings.ruonia_unpublished_days`;
- `issuers_static` — последние запуски `succeeded` (`/v1/runs?job=issuers_static`).

Записать в #35: дату и время установки, номер запроса updater и run ID workflow, `snapshot_id`
первых срезов 1.5, наличие наборов. Без адресов, путей и токенов.

## Откат

Updates → «Roll back» к 0.3.4 восстанавливает pre-update дамп (миграции есть). Накопленные после
установки `md.fx_settle_date` теряются: MOEX публикует SETTLEDATE только для текущей сессии, их
не вернуть. Срезы 1.5, опубликованные до отката, неизменяемы; новые снова будут 1.4.
