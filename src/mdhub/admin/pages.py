"""Console pages (ADMIN_DESIGN §4) and the operator / admin actions behind them.

Read pages (Overview, Jobs, Data, Sources, API traffic, Updates) need ``viewer``; operator
actions (request a job run, retry a failed backfill task, mark a gap ``not_expected``) need
``operator``. They only write to the database: a requested run is executed by the worker
(``ops.job_request``, :mod:`mdhub.job_requests`), so the console holds no source credentials.
Tokens, Users and Audit need ``admin``, and their changes a step-up. Every action is written
to ``admin.audit_log``. Results come back as a fixed message key (``?done=``), never as text
from the URL.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Final

import psycopg
from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, Form, Query
from fastapi.responses import RedirectResponse, Response

from mdhub import __version__
from mdhub.admin import store
from mdhub.admin.web import Ctx, Denied, render, require
from mdhub.api.auth import SCOPES, TokenError, create_token, list_tokens, revoke_token
from mdhub.core.bizday import DbCalendar
from mdhub.db.pool import DictConnection
from mdhub.hostmon.status import Machine, format_value, machine_status, worst
from mdhub.jobs import REGISTRY
from mdhub.quality.health import job_health
from mdhub.schedule import RULES, Rule, rule_applies

__all__ = ["router"]

router = APIRouter()

viewer = require("viewer")
operator = require("operator")
admin = require("admin")
admin_step_up = require("admin", step_up=True)

MESSAGES: Final = {
    "job_requested": "The run is queued; the worker starts it within a few seconds.",
    "request_cancelled": "The queued run was cancelled.",
    "task_retried": "The backfill task is queued again; the backfill worker picks it up.",
    "gap_marked": "The gap is marked not expected.",
    "token_revoked": "The token was revoked. The API stops accepting it within 30 seconds.",
    "token_expiry": "The token expiry was changed.",
    "user_updated": "The user was updated.",
    "user_unlocked": "The login was unlocked.",
    "sessions_revoked": "The user's sessions were revoked.",
}


def _done(location: str, key: str) -> RedirectResponse:
    return RedirectResponse(f"{location}?done={key}", status_code=303)


def _flash(done: str | None) -> str | None:
    return MESSAGES.get(done or "")


# ---------------------------------------------------------------------------------- overview


def _light(ok: bool, warn: bool = False) -> str:
    return "ok" if ok else ("warn" if warn else "bad")


def overview_data(ctx: Ctx) -> dict[str, Any]:
    tz = ctx.settings.zone
    with ctx.db.connection() as conn:
        now: datetime = _one(conn, "SELECT now() AS v")
        beats = conn.execute(
            """
            SELECT DISTINCT ON (process) process, host, version, beat_at, stopped_at,
                   running_jobs
            FROM ops.worker_heartbeat ORDER BY process, beat_at DESC
            """
        ).fetchall()
        db_size = _one(conn, "SELECT pg_database_size(current_database()) AS v")
        backup_at = _one(
            conn,
            """
            SELECT max(finished_at) AS v FROM ops.job_run
            WHERE job = 'maintenance' AND params ->> 'mode' = 'backup' AND status = 'succeeded'
            """,
        )
        sources = conn.execute(
            "SELECT source, name, ok, auth, probed_at, tls_expires_at, error "
            "FROM ops.v_source_status ORDER BY source"
        ).fetchall()
        coverage = conn.execute(
            "SELECT dataset, is_stale, open_gaps, unfillable_gaps, problem FROM ops.v_coverage"
        ).fetchall()
        health = job_health(conn, now=now.astimezone(tz), calendar=DbCalendar(ctx.db), tz=tz)
        failed_tasks = _one(
            conn, "SELECT count(*) AS v FROM ops.backfill_task WHERE status = 'failed'"
        )
        machine = machine_status(conn, ctx.settings)
    problems: list[str] = []
    worker = next((b for b in beats if b["process"] == "worker"), None)
    worker_age = (now - worker["beat_at"]).total_seconds() if worker else None
    if worker is None:
        problems.append("No worker heartbeat recorded: is the scheduler running?")
    elif worker["stopped_at"] is not None or (worker_age or 0) > 300:
        problems.append(f"Worker heartbeat is {int((worker_age or 0) // 60)} min old.")
    if backup_at is None or now - backup_at > timedelta(hours=36):
        problems.append("No successful backup in the last 36 hours.")
    bad_sources = [s for s in sources if s["ok"] is False or s["auth"] == "rejected"]
    unprobed = [s for s in sources if s["ok"] is None]
    for s in bad_sources:
        what = "credentials rejected" if s["auth"] == "rejected" else "unreachable"
        problems.append(f"Source {s['source']}: {what}.")
    job_problems = [j for j in health if j.problem]
    for j in job_problems:
        problems.append(f"Job {j.job}: {j.status}.")
    stale = [c for c in coverage if c["is_stale"]]
    open_gaps = sum(int(c["open_gaps"] or 0) for c in coverage)
    unfillable = sum(int(c["unfillable_gaps"] or 0) for c in coverage)
    if stale:
        problems.append(
            f"{len(stale)} stale dataset(s): {', '.join(c['dataset'] for c in stale[:5])}."
        )
    if failed_tasks:
        problems.append(f"{failed_tasks} backfill task(s) failed.")
    tls = [s["tls_expires_at"] for s in sources if s["tls_expires_at"] is not None]
    tls_min = min(tls) if tls else None
    if tls_min is not None and tls_min - now < timedelta(days=14):
        problems.append("A source TLS certificate expires within 14 days.")
    versions = sorted({b["version"] for b in beats if b["stopped_at"] is None})
    if any(v != __version__ for v in versions):
        problems.append(
            f"Version mismatch: console {__version__}, processes {', '.join(versions)}."
        )
    lights = [
        (
            "Scheduler",
            _light(
                worker_age is not None and worker_age <= 90,
                worker_age is not None and worker_age <= 300,
            ),
            _age_text(worker["beat_at"] if worker else None, now),
        ),
        ("Database", "ok", _size(db_size)),
        (
            "Last backup",
            _light(
                backup_at is not None and now - backup_at <= timedelta(hours=26),
                backup_at is not None and now - backup_at <= timedelta(hours=36),
            ),
            _age_text(backup_at, now),
        ),
        (
            "TLS (sources)",
            _light(
                tls_min is None or tls_min - now > timedelta(days=30),
                tls_min is not None and tls_min - now > timedelta(days=14),
            ),
            f"earliest expiry {tls_min:%Y-%m-%d}" if tls_min else "no data",
        ),
        (
            "Sources",
            _light(not bad_sources and not unprobed, not bad_sources),
            f"{len(sources) - len(bad_sources) - len(unprobed)}/{len(sources)} ok"
            + (f", {len(unprobed)} not probed" if unprobed else ""),
        ),
        ("Jobs", _light(not job_problems), f"{len(job_problems)} problem(s)"),
        (
            "Data",
            _light(not stale and not open_gaps, not stale),
            f"{len(stale)} stale, {open_gaps} open gaps, {unfillable} unfillable",
        ),
        ("Version", "ok" if not any(v != __version__ for v in versions) else "warn", __version__),
        *_machine_lights(machine),
    ]
    for c in machine.problems():
        problems.append(f"Machine: {c.area} {c.label} {format_value(c.value, c.unit)} ({c.state}).")
    if not machine.containers_reporting:
        problems.append("Machine: the updater is not reporting container status.")
    return {"lights": lights, "problems": problems, "beats": beats, "now": now}


def _machine_lights(machine: Machine) -> list[tuple[str, str, str]]:
    def light(label: str, areas: tuple[str, ...]) -> tuple[str, str, str]:
        bad = [c for c in machine.problems() if c.area in areas]
        state = worst([machine.area_state(a) for a in areas])
        return label, state, ", ".join(c.label for c in bad[:3]) or "ok"

    containers = machine.area_state("Containers")
    return [
        light("Machine", ("CPU", "Memory", "Network", "System")),
        light("Disks", ("Disks",)),
        light("PostgreSQL", ("PostgreSQL",)),
        (
            "Containers",
            containers,
            "updater not reporting"
            if not machine.containers_reporting
            else f"{sum(s == 'ok' for s in machine.container_states.values())}"
            f"/{len(machine.container_states)} ok",
        ),
    ]


def _one(conn: DictConnection, query: str) -> Any:
    row = conn.execute(query).fetchone()
    return row["v"] if row else None


def _size(n: int | None) -> str:
    if n is None:
        return "-"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return str(n)


def _age_text(ts: datetime | None, now: datetime) -> str:
    if ts is None:
        return "never"
    minutes = int((now - ts).total_seconds() // 60)
    if minutes < 2:
        return "just now"
    if minutes < 120:
        return f"{minutes} min ago"
    return f"{minutes // 60} h ago"


@router.get("/")
def overview(ctx: Annotated[Ctx, Depends(viewer)]) -> Response:
    return render(ctx, "overview.html", **overview_data(ctx))


@router.get("/overview/body")
def overview_body(ctx: Annotated[Ctx, Depends(viewer)]) -> Response:
    return render(ctx, "_overview_body.html", **overview_data(ctx))


# -------------------------------------------------------------------------------------- jobs


def next_fire(rules: tuple[Rule, ...], now: datetime, calendar: DbCalendar) -> datetime | None:
    """Next cron fire of any rule whose day filter applies (searching 8 days ahead)."""
    best: datetime | None = None
    for rule in rules:
        trigger = CronTrigger.from_crontab(rule.cron, timezone=now.tzinfo)
        t: datetime | None = trigger.get_next_fire_time(None, now)
        for _ in range(500):
            if t is None or t - now > timedelta(days=8):
                break
            if rule_applies(rule, t.date(), calendar):
                if best is None or t < best:
                    best = t
                break
            t = trigger.get_next_fire_time(t, t + timedelta(seconds=1))
    return best


@router.get("/jobs")
def jobs_page(
    ctx: Annotated[Ctx, Depends(viewer)],
    job: Annotated[str | None, Query(max_length=64)] = None,
    status: Annotated[str | None, Query(max_length=16)] = None,
    done: str | None = None,
) -> Response:
    tz = ctx.settings.zone
    calendar = DbCalendar(ctx.db)
    with ctx.db.connection() as conn:
        now: datetime = _one(conn, "SELECT now() AS v").astimezone(tz)
        health = job_health(conn, now=now, calendar=calendar, tz=tz)
        running = conn.execute(
            "SELECT id, job, started_at, trigger, host FROM ops.job_run "
            "WHERE status = 'running' ORDER BY started_at"
        ).fetchall()
        failures = conn.execute(
            """
            SELECT id, job, status, started_at, finished_at, error FROM ops.job_run
            WHERE status IN ('failed', 'partial') AND started_at > now() - interval '24 hours'
            ORDER BY started_at DESC LIMIT 100
            """
        ).fetchall()
        requests = conn.execute(
            """
            SELECT q.id, q.job, q.state, q.requested_login, q.requested_at, q.picked_at,
                   q.finished_at, q.attempts, q.job_run_id, q.error, r.status AS run_status
            FROM ops.job_request q LEFT JOIN ops.job_run r ON r.id = q.job_run_id
            ORDER BY q.requested_at DESC, q.id DESC LIMIT 50
            """
        ).fetchall()
        history = conn.execute(
            """
            SELECT id, job, status, trigger, started_at, finished_at, rows_inserted,
                   rows_updated, left(error, 300) AS error
            FROM ops.job_run
            WHERE (%(job)s::text IS NULL OR job = %(job)s)
              AND (%(status)s::text IS NULL OR status = %(status)s)
            ORDER BY started_at DESC LIMIT 200
            """,
            {"job": job or None, "status": status or None},
        ).fetchall()
    rows = []
    for h in health:
        rules = RULES.get(h.job, ())
        rows.append(
            {
                "h": h,
                "next": next_fire(rules, now, calendar) if rules else None,
                "crons": ", ".join(r.cron for r in rules) or "manual",
            }
        )
    return render(
        ctx,
        "jobs.html",
        jobs=rows,
        requests=requests,
        running=running,
        failures=failures,
        history=history,
        now=now,
        filter_job=job or "",
        filter_status=status or "",
        flash=_flash(done),
        registered=set(REGISTRY.names()),
        statuses=("running", "succeeded", "partial", "failed", "skipped"),
    )


@router.post("/jobs/{name}/run")
def job_run_now(ctx: Annotated[Ctx, Depends(operator)], name: str) -> Response:
    """Queue a run for the worker (``ops.job_request``); the console runs no job itself."""
    if name not in REGISTRY:
        raise Denied(404, "Unknown job.")
    with ctx.db.connection() as conn:
        try:
            with conn.transaction():
                row = conn.execute(
                    """
                    INSERT INTO ops.job_request (job, requested_by, requested_login)
                    VALUES (%s, %s, %s) RETURNING id
                    """,
                    (name, ctx.user.id, ctx.user.login),
                ).fetchone()
        except psycopg.errors.UniqueViolation:
            raise Denied(409, "A run of this job is already queued or running.") from None
        assert row is not None
        ctx.audit(conn, "job.request", target=f"job:{name}", details={"request_id": row["id"]})
    return _done("/jobs", "job_requested")


@router.post("/jobs/requests/{request_id}/cancel")
def job_request_cancel(ctx: Annotated[Ctx, Depends(operator)], request_id: int) -> Response:
    with ctx.db.connection() as conn:
        row = conn.execute(
            """
            UPDATE ops.job_request SET state = 'cancelled', finished_at = now()
            WHERE id = %s AND state = 'queued' RETURNING job
            """,
            (request_id,),
        ).fetchone()
        if row is None:
            raise Denied(404, "No queued request with that id.")
        ctx.audit(
            conn,
            "job.request_cancel",
            target=f"job:{row['job']}",
            details={"request_id": request_id},
        )
    return _done("/jobs", "request_cancelled")


# -------------------------------------------------------------------------------------- data


@router.get("/data")
def data_page(ctx: Annotated[Ctx, Depends(viewer)], done: str | None = None) -> Response:
    with ctx.db.connection() as conn:
        coverage = conn.execute("SELECT * FROM ops.v_coverage ORDER BY dataset").fetchall()
        queue = conn.execute(
            "SELECT * FROM ops.v_backfill_queue ORDER BY status, created_at LIMIT 300"
        ).fetchall()
        counts = conn.execute(
            "SELECT status, count(*) AS n FROM ops.v_backfill_queue GROUP BY status"
        ).fetchall()
        gaps = conn.execute(
            """
            SELECT g.dataset, g.key::text AS key, g.gap_date, g.status, g.note, g.detected_at
            FROM ops.data_gap g WHERE g.status IN ('open', 'unfillable')
            ORDER BY g.status, g.dataset, g.gap_date DESC LIMIT 300
            """
        ).fetchall()
    return render(
        ctx,
        "data.html",
        coverage=coverage,
        queue=queue,
        counts={r["status"]: r["n"] for r in counts},
        gaps=gaps,
        flash=_flash(done),
    )


@router.post("/data/tasks/{task_id}/retry")
def task_retry(ctx: Annotated[Ctx, Depends(operator)], task_id: int) -> Response:
    with ctx.db.connection() as conn:
        try:
            with conn.transaction():
                row = conn.execute(
                    """
                    UPDATE ops.backfill_task SET status = 'pending', attempts = 0,
                           not_before = NULL, done_at = NULL
                    WHERE id = %s AND status = 'failed'
                    RETURNING dataset, params
                    """,
                    (task_id,),
                ).fetchone()
        except psycopg.errors.UniqueViolation:
            raise Denied(409, "An open task for the same range exists already.") from None
        if row is None:
            raise Denied(404, "No failed task with that id.")
        ctx.audit(
            conn,
            "backfill.retry",
            target=f"task:{task_id}",
            details={"dataset": row["dataset"], "params": row["params"]},
        )
    return _done("/data", "task_retried")


@router.post("/data/gaps/not-expected")
def gap_not_expected(
    ctx: Annotated[Ctx, Depends(operator)],
    dataset: Annotated[str, Form(max_length=128)],
    key: Annotated[str, Form(max_length=2000)],
    gap_date: Annotated[date, Form()],
    reason: Annotated[str, Form(min_length=3, max_length=500)],
) -> Response:
    with ctx.db.connection() as conn:
        row = conn.execute(
            """
            UPDATE ops.data_gap SET status = 'not_expected', note = %s
            WHERE dataset = %s AND key = %s::jsonb AND gap_date = %s
              AND status IN ('open', 'unfillable')
            RETURNING status
            """,
            (f"not expected ({ctx.user.login}): {reason.strip()}", dataset, key, gap_date),
        ).fetchone()
        if row is None:
            raise Denied(404, "No open or unfillable gap with that key.")
        ctx.audit(
            conn,
            "gap.not_expected",
            target=f"gap:{dataset}:{gap_date}",
            details={"key": key, "reason": reason.strip()},
        )
    return _done("/data", "gap_marked")


# ----------------------------------------------------------------------------------- sources


@router.get("/sources")
def sources_page(ctx: Annotated[Ctx, Depends(viewer)]) -> Response:
    with ctx.db.connection() as conn:
        rows = conn.execute("SELECT * FROM ops.v_source_status ORDER BY source").fetchall()
        now = _one(conn, "SELECT now() AS v")
    return render(ctx, "sources.html", sources=rows, now=now)


# ----------------------------------------------------------------------------------- traffic


@router.get("/traffic")
def traffic_page(
    ctx: Annotated[Ctx, Depends(viewer)], hours: Annotated[int, Query(ge=1, le=2160)] = 24
) -> Response:
    window = timedelta(hours=hours)
    with ctx.db.connection() as conn:

        def q(sql: str) -> list[dict[str, Any]]:
            return [dict(r) for r in conn.execute(sql, {"w": window}).fetchall()]

        totals = q(
            """
            SELECT count(*) AS requests,
                   count(*) FILTER (WHERE status BETWEEN 200 AND 399) AS ok,
                   count(*) FILTER (WHERE status = 401) AS s401,
                   count(*) FILTER (WHERE status = 403) AS s403,
                   count(*) FILTER (WHERE status = 429) AS s429,
                   count(*) FILTER (WHERE status >= 500) AS s5xx
            FROM ops.api_request WHERE ts > now() - %(w)s
            """
        )[0]
        consumers = q(
            """
            SELECT coalesce(token_name, '(none)') AS consumer, count(*) AS requests,
                   count(*) FILTER (WHERE status >= 400) AS errors,
                   round(avg(duration_ms)::numeric, 1) AS avg_ms,
                   round(percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)::numeric, 1)
                       AS p95_ms,
                   max(ts) AS last_at, count(DISTINCT client_ip) AS ips
            FROM ops.api_request WHERE ts > now() - %(w)s
            GROUP BY 1 ORDER BY requests DESC LIMIT 50
            """
        )
        ips = q(
            """
            SELECT host(client_ip) AS ip, count(*) AS requests,
                   count(*) FILTER (WHERE status = 401) AS s401,
                   count(*) FILTER (WHERE status = 403) AS s403,
                   count(*) FILTER (WHERE status = 429) AS s429,
                   count(*) FILTER (WHERE status >= 500) AS s5xx,
                   string_agg(DISTINCT token_name, ', ') AS consumers, max(ts) AS last_at
            FROM ops.api_request WHERE ts > now() - %(w)s
            GROUP BY client_ip ORDER BY requests DESC LIMIT 50
            """
        )
        endpoints = q(
            """
            SELECT method, path, count(*) AS requests,
                   count(*) FILTER (WHERE status >= 400) AS errors,
                   round(avg(duration_ms)::numeric, 1) AS avg_ms,
                   round(max(duration_ms)::numeric, 1) AS max_ms,
                   sum(response_bytes) AS bytes
            FROM ops.api_request WHERE ts > now() - %(w)s
            GROUP BY method, path ORDER BY requests DESC LIMIT 50
            """
        )
        unknown = q(
            """
            SELECT host(client_ip) AS ip, count(*) AS attempts, min(ts) AS first_at,
                   max(ts) AS last_at, max(user_agent) AS user_agent
            FROM ops.api_request
            WHERE ts > now() - %(w)s AND status = 401 AND token_name IS NULL
            GROUP BY client_ip ORDER BY attempts DESC LIMIT 50
            """
        )
        slow = q(
            """
            SELECT ts, coalesce(token_name, '(none)') AS consumer, host(client_ip) AS ip,
                   method, path, status, duration_ms, response_bytes
            FROM ops.api_request WHERE ts > now() - %(w)s
            ORDER BY duration_ms DESC LIMIT 20
            """
        )
    return render(
        ctx,
        "traffic.html",
        hours=hours,
        totals=totals,
        consumers=consumers,
        ips=ips,
        endpoints=endpoints,
        unknown=unknown,
        slow=slow,
    )


# ------------------------------------------------------------------------------------ tokens


def _tokens(ctx: Ctx, status_code: int = 200, **values: Any) -> Response:
    with ctx.db.connection() as conn:
        tokens = list_tokens(conn, include_revoked=True)
    return render(
        ctx,
        "tokens.html",
        status_code=status_code,
        tokens=tokens,
        scopes=SCOPES,
        **values,
    )


@router.get("/tokens")
def tokens_page(ctx: Annotated[Ctx, Depends(admin)], done: str | None = None) -> Response:
    return _tokens(ctx, flash=_flash(done))


@router.post("/tokens")
def token_create(
    ctx: Annotated[Ctx, Depends(admin_step_up)],
    name: Annotated[str, Form(max_length=64)],
    scope: Annotated[list[str] | None, Form()] = None,
    expires_days: Annotated[int | None, Form(ge=1, le=3650)] = None,
) -> Response:
    expires_at = datetime.now(UTC) + timedelta(days=expires_days) if expires_days else None
    with ctx.db.connection() as conn:
        try:
            token, row = create_token(
                conn,
                name.strip(),
                scope or ["read"],
                expires_at=expires_at,
                created_by=ctx.user.login,
            )
        except TokenError as exc:
            conn.rollback()
            return _tokens(ctx, 400, error=str(exc))
        ctx.audit(
            conn,
            "token.create",
            target=f"token:{row['id']}",
            details={
                "name": row["name"],
                "scopes": row["scopes"],
                "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
            },
        )
    return _tokens(ctx, created={"token": token, **row})


@router.post("/tokens/{token_id}/revoke")
def token_revoke(ctx: Annotated[Ctx, Depends(admin_step_up)], token_id: int) -> Response:
    with ctx.db.connection() as conn:
        rows = revoke_token(conn, str(token_id))
        if not rows:
            raise Denied(404, "No live token with that id.")
        ctx.audit(
            conn, "token.revoke", target=f"token:{token_id}", details={"name": rows[0]["name"]}
        )
    return _done("/tokens", "token_revoked")


@router.post("/tokens/{token_id}/expiry")
def token_expiry(
    ctx: Annotated[Ctx, Depends(admin_step_up)],
    token_id: int,
    expires_days: Annotated[int | None, Form(ge=1, le=3650)] = None,
) -> Response:
    with ctx.db.connection() as conn:
        row = conn.execute(
            """
            UPDATE admin.api_token
            SET expires_at = CASE WHEN %(d)s::int IS NULL THEN NULL
                                  ELSE now() + make_interval(days => %(d)s::int) END
            WHERE id = %(id)s AND revoked_at IS NULL RETURNING name, expires_at
            """,
            {"d": expires_days, "id": token_id},
        ).fetchone()
        if row is None:
            raise Denied(404, "No live token with that id.")
        ctx.audit(
            conn,
            "token.expiry",
            target=f"token:{token_id}",
            details={"name": row["name"], "expires_days": expires_days},
        )
    return _done("/tokens", "token_expiry")


# ------------------------------------------------------------------------------------- users


def _users(ctx: Ctx, status_code: int = 200, **values: Any) -> Response:
    with ctx.db.connection() as conn:
        users = conn.execute(
            """
            SELECT u.id, u.login, u.display_name, u.role, u.disabled_at, u.created_at,
                   u.created_by, u.passkey_only, u.password_hash IS NOT NULL AS has_password,
                   (SELECT count(*) FROM admin.webauthn_credential c WHERE c.user_id = u.id
                     AND c.suspect_at IS NULL) AS passkeys,
                   EXISTS (SELECT 1 FROM admin.totp_secret t WHERE t.user_id = u.id
                            AND t.confirmed_at IS NOT NULL) AS totp,
                   (SELECT count(*) FROM admin.recovery_code r WHERE r.user_id = u.id
                     AND r.used_at IS NULL) AS recovery_left,
                   (SELECT count(*) FROM admin.session s WHERE s.user_id = u.id
                     AND s.stage = 'full' AND s.revoked_at IS NULL AND s.expires_at > now()
                     AND s.last_seen_at > now() - %s) AS sessions,
                   (SELECT max(ts) FROM admin.login_attempt a WHERE a.login = u.login
                     AND a.outcome = 'success') AS last_login
            FROM admin.user_account u ORDER BY u.login
            """,
            (store.IDLE_TIMEOUT,),
        ).fetchall()
        locked = {
            u["login"]
            for u in users
            if store.login_failures(conn, u["login"]) >= store.LOCK_THRESHOLD
        }
    return render(
        ctx,
        "users.html",
        status_code=status_code,
        users=users,
        locked=locked,
        roles=store.ROLES,
        **values,
    )


@router.get("/users")
def users_page(ctx: Annotated[Ctx, Depends(admin)], done: str | None = None) -> Response:
    return _users(ctx, flash=_flash(done))


def _link(ctx: Ctx, token: str) -> str:
    return f"{ctx.cfg.origin}/enroll/{token}"


@router.post("/users")
def user_invite(
    ctx: Annotated[Ctx, Depends(admin_step_up)],
    login: Annotated[str, Form(max_length=64)],
    role: Annotated[str, Form(max_length=16)],
    display_name: Annotated[str, Form(max_length=128)] = "",
) -> Response:
    with ctx.db.connection() as conn:
        try:
            user = store.create_user(
                conn, login, role, display_name=display_name, created_by=ctx.user.login
            )
        except store.UserError as exc:
            conn.rollback()
            return _users(ctx, 400, error=str(exc))
        token = store.create_enrollment(conn, user.id, "invite", ctx.user.login)
        ctx.audit(conn, "user.create", target=user.login, details={"role": user.role})
    return _users(ctx, link={"login": user.login, "url": _link(ctx, token)})


def _target(conn: DictConnection, ctx: Ctx, user_id: int, *, not_self: bool = False) -> store.User:
    user = store.user_by_id(conn, user_id)
    if user is None:
        raise Denied(404, "No such user.")
    if not_self and user.id == ctx.user.id:
        raise Denied(400, "You cannot do this to your own account.")
    return user


@router.post("/users/{user_id}/role")
def user_role(
    ctx: Annotated[Ctx, Depends(admin_step_up)],
    user_id: int,
    role: Annotated[str, Form(max_length=16)],
) -> Response:
    if role not in store.ROLES:
        raise Denied(400, "Unknown role.")
    with ctx.db.connection() as conn:
        user = _target(conn, ctx, user_id, not_self=True)
        conn.execute("UPDATE admin.user_account SET role = %s WHERE id = %s", (role, user.id))
        store.revoke_user_sessions(conn, user.id)  # privilege change: log in again
        ctx.audit(conn, "user.role", target=user.login, details={"from": user.role, "to": role})
    return _done("/users", "user_updated")


@router.post("/users/{user_id}/disable")
def user_disable(ctx: Annotated[Ctx, Depends(admin_step_up)], user_id: int) -> Response:
    with ctx.db.connection() as conn:
        user = _target(conn, ctx, user_id, not_self=True)
        conn.execute(
            "UPDATE admin.user_account SET disabled_at = coalesce(disabled_at, now()) "
            "WHERE id = %s",
            (user.id,),
        )
        store.revoke_user_sessions(conn, user.id)
        ctx.audit(conn, "user.disable", target=user.login)
    return _done("/users", "user_updated")


@router.post("/users/{user_id}/enable")
def user_enable(ctx: Annotated[Ctx, Depends(admin_step_up)], user_id: int) -> Response:
    with ctx.db.connection() as conn:
        user = _target(conn, ctx, user_id)
        conn.execute("UPDATE admin.user_account SET disabled_at = NULL WHERE id = %s", (user.id,))
        ctx.audit(conn, "user.enable", target=user.login)
    return _done("/users", "user_updated")


@router.post("/users/{user_id}/unlock")
def user_unlock(ctx: Annotated[Ctx, Depends(admin_step_up)], user_id: int) -> Response:
    with ctx.db.connection() as conn:
        user = _target(conn, ctx, user_id)
        store.record_attempt(conn, user.login, ctx.ip, "unlocked")
        ctx.audit(conn, "user.unlock", target=user.login)
    return _done("/users", "user_unlocked")


@router.post("/users/{user_id}/reset")
def user_reset(ctx: Annotated[Ctx, Depends(admin_step_up)], user_id: int) -> Response:
    with ctx.db.connection() as conn:
        user = _target(conn, ctx, user_id, not_self=True)
        token = store.create_enrollment(conn, user.id, "reset", ctx.user.login)
        store.revoke_user_sessions(conn, user.id)
        ctx.audit(conn, "user.reset", target=user.login)
    return _users(ctx, link={"login": user.login, "url": _link(ctx, token), "reset": True})


@router.post("/users/{user_id}/sessions/revoke")
def user_sessions_revoke(ctx: Annotated[Ctx, Depends(admin_step_up)], user_id: int) -> Response:
    with ctx.db.connection() as conn:
        user = _target(conn, ctx, user_id, not_self=True)
        n = store.revoke_user_sessions(conn, user.id)
        ctx.audit(conn, "user.sessions_revoke", target=user.login, details={"sessions": n})
    return _done("/users", "sessions_revoked")


# ------------------------------------------------------------------------------------- audit


@router.get("/audit")
def audit_page(
    ctx: Annotated[Ctx, Depends(admin)],
    action: Annotated[str | None, Query(max_length=64)] = None,
    login: Annotated[str | None, Query(max_length=64)] = None,
    before: Annotated[int | None, Query(ge=1)] = None,
) -> Response:
    with ctx.db.connection() as conn:
        rows = conn.execute(
            """
            SELECT id, ts, login, session_ref, host(ip) AS ip, action, target, details, outcome
            FROM admin.audit_log
            WHERE (%(action)s::text IS NULL OR action LIKE %(action)s || '%%')
              AND (%(login)s::text IS NULL OR login = %(login)s)
              AND (%(before)s::bigint IS NULL OR id < %(before)s)
            ORDER BY id DESC LIMIT 200
            """,
            {"action": action or None, "login": login or None, "before": before},
        ).fetchall()
    return render(
        ctx,
        "audit.html",
        rows=rows,
        action=action or "",
        login=login or "",
        older=rows[-1]["id"] if len(rows) == 200 else None,
    )


# ----------------------------------------------------------------------------------- updates


@router.get("/updates")
def updates_page(ctx: Annotated[Ctx, Depends(viewer)]) -> Response:
    with ctx.db.connection() as conn:
        beats = conn.execute(
            """
            SELECT DISTINCT ON (process, host) process, host, version, started_at, beat_at
            FROM ops.worker_heartbeat WHERE stopped_at IS NULL
            ORDER BY process, host, beat_at DESC
            """
        ).fetchall()
    return render(ctx, "updates.html", beats=beats)
