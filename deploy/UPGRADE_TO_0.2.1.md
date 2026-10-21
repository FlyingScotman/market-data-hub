# Обновление до 0.2.1 (WEB-16: исправления EOD по живой проверке 21.10)

Для владельца. Состояние 21.10.2026: production = **0.2.0**. Предполагаемая версия после слияния
PR ветки `claude/hub-eod-fixes` — **0.2.1** (исправления без изменения контракта: схема
`market_snapshot/1.2` та же). Агент не ставил тег, не публиковал релиз и не трогал production.
Источник — живая проверка `docs/reviews/HUB_REVIEW_FOR_DESK_MONITOR.md` (ветка
`claude/hub-review-desk-monitor`) §2.5 п. 1–6, §6.4, §6.5.

## Что приходит

| # | Дефект на production | Исправление |
|---|---|---|
| §6.4 | `bonds_eod` prelim 19:20 пишет 0 строк и `succeeded`; срез `eod_preliminary` без облигаций | ISS публикует историю D только ночью (`history/.../bonds/dates.json` вечером заканчивается на D-1). Prelim перечитывает курсор истории каждые 5 мин до 19:40, пока не наберётся ≥ 1000 строк; иначе запуск `partial` и один снимок marketdata после закрытия (с `LCLOSEPRICE`). Срез 19:45 берёт облигации из последнего intraday-состояния D: `coverage.basis = intraday_close`, `stale_reasons` |
| §6.5 | 20.10 16:30–19:00 все intraday-срезы `skipped: lock_busy` из-за ~170 ручных `snapshot_market -p mode=eod_final -p day=…` | Три замка: `snapshot_market:intraday`, `:eod` (плановые EOD), `:rebuild` (`day=` или `known_at=`). Intraday-срез ждёт замок до половины слота (≤ 10 мин); не дождался — `ops.data_gap` `snapshot_market_intraday` с ключом `cut` (`/v1/gaps`), закрывается перевыпуском этого среза |
| §2.5.3 | В `eod_final` кривая OTC — снимок ~18:00–19:00 без bid/ask | Правило EOD: последний набор **с bid/ask**, у которого часы источника в D не позже 19:00 МСК; иначе последний набор дня (`mid_only`). Одна функция для срезов и `/v1/history/curves?rule=eod` |
| §2.5.5 | CNY OIS к RUSFAR CNY от OTC — 2 снимка | Причина: до 0.2.0 неизменённая кривая не сохранялась повторно, а OTC обновляет RUSFARCNY редко (часы источника 16.10, затем 20.10 18:50). Правило «первый опрос дня сохраняет кривую» уже в 0.2.0 (21.10 17:30 записан снимок 3756). Изменений загрузчика нет, добавлен регрессионный тест; редкость обновлений — свойство источника |
| §2.5.6 | `RU_PRODUCTION` строится по наличию RUONIA: «нет RUONIA» = «выходной» | Официальный производственный календарь 2019–2025 в `calendar_curated.json` (сверен с КонсультантПлюс), он действует и для прошлых дней; строки `observed_cbr_ruonia` мягко удаляются (версии в `ops.row_history`); RUONIA только сверяется. На production выходными ошибочно были: 31.12.2019, 30.03–03.04.2020, 24.06.2020, 31.12.2020, 30.12.2021, 30.12.2022, 29.12.2023, сб 28.12.2024, 30.12.2025 — теперь это рабочие дни, а отсутствие RUONIA в них — пропуск `cbr_ruonia` |

## Миграции

- `0435_eod_fixes` — `md.bond_quote_intraday.legal_close` и `md.bond_quote_last.legal_close`
  (`numeric`, NULL). Только добавление колонок, данные не меняются.

## Новые настройки (необязательны; значения по умолчанию рабочие)

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `MDHUB_BONDS_EOD_PRELIM_MIN_ROWS` | `1000` | Сколько строк истории ISS считать «опубликованной»; меньше — срез 19:45 по intraday |
| `MDHUB_BONDS_EOD_PRELIM_DEADLINE` | `19:40` | До какого времени (МСК) prelim ждёт историю; должно быть раньше `MDHUB_SNAPSHOT_EOD_PRELIMINARY_TIME` |
| `MDHUB_BONDS_EOD_PRELIM_RETRY_MINUTES` | `5` | Интервал повторной проверки |
| `MDHUB_CURVE_EOD_CUTOFF` | `19:00` | Граница торгового окна для правила EOD-кривой (МСК) |

## Выпуск и установка

Так же, как 0.2.0 ([UPGRADE_TO_0.2.0.md](UPGRADE_TO_0.2.0.md), «Выпуск и установка»), с тегом
`v0.2.1`:

```bash
git switch main && git pull --ff-only
git tag -a v0.2.1 -m "market-data-hub 0.2.1"
git push origin v0.2.1
```

- `git switch main` — перейти на ветку `main`; `git pull --ff-only` — подтянуть коммит слияния,
  только перемоткой (без merge-коммита на машине владельца).
- `git tag -a v0.2.1` — аннотированный тег, из него hatch-vcs берёт версию; `-m` — сообщение тега.
- `git push origin v0.2.1` — отправить тег; это запускает `release.yml` (ci → build → sign с
  одобрением в окружении `release` → verify → publish).

Затем в консоли <https://marketdatahub.cc> → **Updates** → 0.2.1 → «Review and install».
Ручной шаг с `hostmon` (был только для перехода с 0.1.3) не нужен.

## После установки

Команды — на VPS под root. Функция `dc` — как в 0.2.0:

```bash
dc() { docker compose -f /opt/mdhub/current/deploy/docker-compose.yml \
                      -f /opt/mdhub/shared/compose.local.yaml "$@"; }
```

- `-f` дважды — compose-файл текущего релиза плюс постоянный override (host nginx, порты, секреты).

**1. Календарь RU_PRODUCTION** (иначе обновится сам в первый рабочий день месяца, 09:10):

```bash
dc run --rm --no-deps worker mdhub run cbr_meetings -p mode=calendar
```

- `run --rm` — разовый контейнер воркера, удаляется после выхода; `--no-deps` — не поднимать
  зависимые сервисы (они уже работают).
- `mdhub run cbr_meetings` — выполнить задание один раз (запись в `ops.job_run`, trigger `cli`).
- `-p mode=calendar` — обновить календари (запишет официальные 2019–2025, удалит
  `observed_cbr_ruonia`, сверит RUONIA за 62 дня), затем заседания ЦБ.
- Ожидание: `succeeded`; в результате `ruonia_crosscheck.ruonia_on_non_business_days = []`.

**2. Пропуски RUONIA и других рядов ЦБ на «бывших выходных»:**

```bash
dc run --rm --no-deps worker mdhub run quality \
    -p datasets=cbr_ruonia,cbr_key_rate,cbr_standing_rates,cbr_liquidity \
    -p check_jobs=false -p catch_up=false
```

- `-p datasets=…` — проверить только ряды на календаре `RU_PRODUCTION`.
- `-p check_jobs=false` — без расчёта здоровья заданий; `-p catch_up=false` — не перезапускать
  пропущенные плановые запуски.
- Ожидание: у `cbr_ruonia` откроются пропуски на датах из таблицы выше; `backfill_worker` (каждые
  10 мин) перечитает их у ЦБ и закроет как `unfillable` («ЦБ значения не публиковал») или
  `backfilled`. Посмотреть: `GET /v1/gaps?dataset=cbr_ruonia`.

**3. Перевыпуск `eod_preliminary` за последние дни** (срезы 19.10–21.10 вышли без облигаций).
Пересборка с `day=` строится «как известно сейчас»: история ISS за эти дни уже загружена, поэтому
срез выйдет с `basis = moex_history` и как исправление (`supersedes`) прежнего:

```bash
for d in 2026-10-19 2026-10-20 2026-10-21; do
  dc run --rm --no-deps worker mdhub run snapshot_market -p mode=eod_preliminary -p day=$d
done
```

- `for d in …; do …; done` — по одной дате за запуск.
- `-p mode=eod_preliminary` — тип среза; `-p day=$d` — бизнес-дата D. `cut_ts` = 19:45 D (по
  настройке), поэтому новый срез заменяет (`supersedes`) прежний того же среза.
- Запуск с `day=` держит замок `snapshot_market:rebuild` и больше не мешает плановым срезам.
- Чтобы получить именно предварительное состояние на 19:45 (а не финальную историю), добавьте
  `-p known_at=2026-10-20T19:45:00+03:00` — сборка «как было известно» в тот момент (смещение
  `+03:00` обязательно); для дней до 0.2.1 intraday-строк с `LCLOSEPRICE` нет.

**4. Перевыпуск `eod_final` по новому правилу кривых** (OTC даст набор с bid/ask, где он был):

```bash
dc run --rm --no-deps worker mdhub run snapshot_rebuild -p date_from=2026-09-04 -p date_to=2026-10-20
```

- `snapshot_rebuild` — пересобрать уже опубликованные `eod_final` в диапазоне; исправление
  (`supersedes`) выходит только если содержимое изменилось.
- `-p date_from` / `-p date_to` — включительно; 04.09 — первый день живых снимков OTC.
- Задание своё (свой замок), плановые срезы не блокирует.

**5. Проверка** (токен в переменной окружения, не в команде):

```bash
H="Authorization: Bearer $MDHUB_TOKEN"; B=https://marketdatahub.cc
curl -sS "$B/health/ready"
curl -sS -H "$H" "$B/v1/runs?job=bonds_eod&limit=3"
curl -sS -H "$H" "$B/v1/history/curves/ruonia_ois?from=2026-10-20&to=2026-10-20&source=otc_monitor&fields=quote_set_id,source_ts,quote_basis"
curl -sS -H "$H" "$B/v1/gaps?dataset=snapshot_market_intraday"
```

- `curl -sS` — без индикатора прогресса, но с текстом ошибки; `-H "$H"` — заголовок с токеном
  из переменной; `$B` — базовый URL; кавычки вокруг URL — чтобы оболочка не трогала `&` и `?`.
- Ожидание: версия `0.2.1`, миграций 32/32; вечером в будний день prelim `bonds_eod` — `partial`
  с текстом `basis=intraday_close` (пока ISS не опубликовал историю), срез 19:45 с облигациями;
  кривая OTC за 20.10 — набор 14:48 (`bid_ask_mid`), а не 18:50; пропусков intraday-срезов нет.

## Откат

Только восстановлением дампа, который updater делает перед установкой (миграция добавляет
колонки). Исправленные срезы (`supersedes`) остаются корректными и после отката кода.
