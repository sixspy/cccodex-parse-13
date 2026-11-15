from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from catalog.models import (
    EVENT_BLOCK,
    EVENT_CONTEND,
    EVENT_DEPS,
    EVENT_FORCE,
    EVENT_INTERRUPT,
    EVENT_PARSE,
    EVENT_WAIT,
    JobDependency,
    JobEvent,
    MOVE_CANCEL,
    MOVE_WAIT,
    OPENABLE_STATUSES,
    ParseJob,
    DependencyPolicy,
    STATUS_INTERRUPTED,
    STATUS_LANDED,
    STATUS_PARSED,
    STATUS_WAITING,
    UPSTREAM_DEAD_STATUSES,
    UPSTREAM_FINISHED_STATUSES,
)


def _authed(request) -> bool:
    return request.session.get("user") == settings.ADMIN_USER


def _back(job_id) -> str:
    return f"/jobs/{job_id}/"


def _log(job, kind, message):
    JobEvent.objects.create(job=job, kind=kind, message=message)


def root(request):
    return HttpResponseRedirect("/jobs/" if _authed(request) else "/login")


@require_http_methods(["GET", "POST"])
def login_page(request):
    err = ""
    if request.method == "POST":
        user = request.POST.get("username", "").strip()
        pwd = request.POST.get("password", "")
        if user == settings.ADMIN_USER and pwd == settings.ADMIN_PASSWORD:
            request.session["user"] = user
            return HttpResponseRedirect("/jobs/")
        err = "账号或密码错误"
    return render(request, "login.html", {"error": err, "lab": settings.LAB_NAME})


def logout_view(request):
    request.session.flush()
    return HttpResponseRedirect("/login")


def job_list(request):
    if not _authed(request):
        return HttpResponseRedirect("/login")
    items = list(ParseJob.objects.all())
    return render(request, "jobs_list.html", {"items": items, "lab": settings.LAB_NAME, "user": request.session["user"]})


def job_detail(request, job_id: int):
    if not _authed(request):
        return HttpResponseRedirect("/login")
    job = get_object_or_404(ParseJob, pk=job_id)
    upstreams = job.upstream_jobs()
    downstream_links = job.downstream_links.select_related("downstream")
    events = job.events.all()
    context = {
        "job": job,
        "lab": settings.LAB_NAME,
        "user": request.session["user"],
        "upstreams": upstreams,
        "downstream_jobs": [link.downstream for link in downstream_links],
        "events": events,
        "all_jobs": ParseJob.objects.exclude(pk=job.id),
        "entry_visible": job.parse_entry_visible(),
        "pending_upstreams": job.pending_upstreams(),
        "dead_upstreams": job.dead_upstreams(),
        "policy": DependencyPolicy.get_solo(),
    }
    return render(request, "job_detail.html", context)


@require_POST
def job_parse(request, job_id: int):
    """点读盘开析。后台强制：上游未齐禁止开析；上游挂掉按依赖走法处置；争抢只留先到。"""
    if not _authed(request):
        return HttpResponseRedirect("/login")
    with transaction.atomic():
        job = get_object_or_404(ParseJob.objects.select_for_update(), pk=job_id)

        # 走法已定：等待舱 / 依赖中断 的任务，普通开析一律拦住（走法偏了要拦）
        if job.status in (STATUS_WAITING, STATUS_INTERRUPTED):
            _log(
                job,
                EVENT_BLOCK,
                f"走法拦截：当前状态为「{job.status}」，普通开析入口不受理，须按依赖走法处理",
            )
            messages.error(request, f"走法拦截：任务处于「{job.status}」，不能直接开析")
            return HttpResponseRedirect(_back(job.id))

        # 已被先到请求开析（已解析/已入库等）：两人同时点下游读盘只留先到
        if job.status not in OPENABLE_STATUSES:
            _log(job, EVENT_CONTEND, f"下游争用失败：任务已被先到请求开析为「{job.status}」")
            messages.error(request, "下游争用失败：已有先到请求开析，只留先到")
            return HttpResponseRedirect(_back(job.id))

        upstreams = job.upstream_jobs()
        if upstreams:
            dead = [u for u in upstreams if u.status in UPSTREAM_DEAD_STATUSES]
            pending = [
                u for u in upstreams
                if u.status not in UPSTREAM_FINISHED_STATUSES and u.status not in UPSTREAM_DEAD_STATUSES
            ]
            # 上游仍在读盘 / 未齐套：禁止开析，后台强制
            if pending:
                names = "、".join(u.code for u in pending)
                _log(job, EVENT_BLOCK, f"上游未齐，禁止开析：仍在读盘的上游 {names}")
                messages.error(request, f"上游未齐，禁止开析：{names} 仍在读盘")
                return HttpResponseRedirect(_back(job.id))
            if dead:
                policy = DependencyPolicy.get_solo()
                names = "、".join(u.code for u in dead)
                if policy.move == MOVE_CANCEL:
                    job.status = STATUS_INTERRUPTED
                    job.save(update_fields=["status"])
                    _log(job, EVENT_INTERRUPT, f"上游挂掉（{names}），按走法「取消并标依赖中断」取消下游")
                    messages.error(request, f"上游挂掉，按依赖走法取消并标注依赖中断：{names}")
                else:
                    job.status = STATUS_WAITING
                    job.save(update_fields=["status"])
                    _log(job, EVENT_WAIT, f"上游挂掉（{names}），按走法「进等待舱」入舱，等候人工确认强析")
                    messages.error(request, f"上游挂掉，下游已进入等待舱：{names}；需人工确认后方可强行开析")
                return HttpResponseRedirect(_back(job.id))

        # 齐套或无上游：执行开析
        job.parsed = 12 + (job.id * 2) % 9
        job.landed = 0
        job.force_reason = ""
        job.status = STATUS_PARSED
        job.save(update_fields=["parsed", "landed", "status", "force_reason"])
        _log(job, EVENT_PARSE, "开析成功，读盘完成")
        messages.success(request, "开析成功，读盘完成")
    return HttpResponseRedirect(_back(job.id))


@require_POST
def job_force_parse(request, job_id: int):
    """等待舱任务经人工确认、填写强析缘由后方可强行开析。"""
    if not _authed(request):
        return HttpResponseRedirect("/login")
    reason = request.POST.get("reason", "").strip()
    with transaction.atomic():
        job = get_object_or_404(ParseJob.objects.select_for_update(), pk=job_id)
        if job.status != STATUS_WAITING:
            _log(job, EVENT_BLOCK, "强析被拦：任务不在等待舱，不允许强行开析")
            messages.error(request, "强析被拦：仅等待舱任务可人工确认后强行开析")
            return HttpResponseRedirect(_back(job.id))
        policy = DependencyPolicy.get_solo()
        if policy.move != MOVE_WAIT:
            _log(job, EVENT_BLOCK, "强析被拦：当前依赖走法为取消并标依赖中断，无强析通道")
            messages.error(request, "强析被拦：依赖走法不是进等待舱")
            return HttpResponseRedirect(_back(job.id))
        if not reason:
            messages.error(request, "强析被拦：必须填写强析缘由")
            return HttpResponseRedirect(_back(job.id))

        job.parsed = 12 + (job.id * 2) % 9
        job.landed = 0
        job.status = STATUS_PARSED
        job.force_reason = reason
        job.save(update_fields=["parsed", "landed", "status", "force_reason"])
        _log(job, EVENT_FORCE, f"人工确认后强行开析，强析缘由：{reason}")
        messages.success(request, "已强行开析，强析缘由已写入读盘详情")
    return HttpResponseRedirect(_back(job.id))


@require_POST
def job_land(request, job_id: int):
    if not _authed(request):
        return HttpResponseRedirect("/login")
    job = get_object_or_404(ParseJob, pk=job_id)
    if job.status == STATUS_PARSED:
        job.landed = job.parsed
        job.status = STATUS_LANDED
        job.save(update_fields=["landed", "status"])
        messages.success(request, "入库完成")
    return HttpResponseRedirect(_back(job.id))


@require_POST
def deps_save(request, job_id: int):
    """下游声明上游：至少一个，空依赖不许保存；禁止自依赖与成环。"""
    if not _authed(request):
        return HttpResponseRedirect("/login")
    job = get_object_or_404(ParseJob, pk=job_id)
    # 复选框同名多值，或逗号分隔串，两种形态都兼容
    upstream_ids = []
    for value in request.POST.getlist("upstream_ids") + request.POST.get("upstream_ids", "").split(","):
        value = value.strip()
        if not value:
            continue
        try:
            upstream_ids.append(int(value))
        except ValueError:
            pass
    upstream_ids = list(dict.fromkeys(upstream_ids))

    if not upstream_ids:
        messages.error(request, "空依赖不许保存：下游至少声明一个上游任务")
        return HttpResponseRedirect(_back(job.id))
    if job.id in upstream_ids:
        messages.error(request, "依赖设置被拦：任务不能依赖自身")
        return HttpResponseRedirect(_back(job.id))

    upstreams = list(ParseJob.objects.filter(id__in=upstream_ids))
    if len(upstreams) != len(upstream_ids):
        messages.error(request, "依赖设置被拦：存在找不到的上游任务")
        return HttpResponseRedirect(_back(job.id))

    # 防环：若任一上游已（直接或间接）依赖本任务，则挂上后即成环
    def reaches(start_id, target_id):
        seen = set()
        stack = [start_id]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            if cur == target_id:
                return True
            for link in JobDependency.objects.filter(downstream_id=cur):
                stack.append(link.upstream_id)
        return False

    with transaction.atomic():
        for up in upstreams:
            if reaches(up.id, job.id):
                messages.error(request, f"依赖设置被拦：{up.code} 已在本任务下游，成环不允许")
                return HttpResponseRedirect(_back(job.id))
        JobDependency.objects.filter(downstream=job).delete()
        for up in upstreams:
            JobDependency.objects.create(downstream=job, upstream=up)
        names = "、".join(u.code for u in upstreams)
        _log(job, EVENT_DEPS, f"依赖设置：声明上游 {names}")
        messages.success(request, f"依赖已保存：上游 {names}")
    return HttpResponseRedirect(_back(job.id))


def dependency_board(request):
    """依赖列表：上游态、等待舱、中断数、走法名一览。"""
    if not _authed(request):
        return HttpResponseRedirect("/login")
    policy = DependencyPolicy.get_solo()
    jobs = list(ParseJob.objects.all())
    rows = []
    for job in jobs:
        ups = job.upstream_jobs()
        if not ups:
            continue
        dead = [u for u in ups if u.status in UPSTREAM_DEAD_STATUSES]
        complete = all(u.status in UPSTREAM_FINISHED_STATUSES for u in ups)
        rows.append({
            "job": job,
            "upstreams": ups,
            "complete": complete,
            "dead_count": len(dead),
            "in_waiting": job.status == STATUS_WAITING,
            "interrupted": job.status == STATUS_INTERRUPTED,
        })
    waiting_jobs = ParseJob.objects.filter(status=STATUS_WAITING)
    interrupted_jobs = ParseJob.objects.filter(status=STATUS_INTERRUPTED)
    context = {
        "lab": settings.LAB_NAME,
        "user": request.session["user"],
        "rows": rows,
        "policy": policy,
        "moves": [MOVE_WAIT, MOVE_CANCEL],
        "waiting_count": waiting_jobs.count(),
        "interrupted_count": interrupted_jobs.count(),
        "waiting_jobs": waiting_jobs,
        "interrupted_jobs": interrupted_jobs,
        "today": timezone.localdate(),
    }
    return render(request, "dependency_board.html", context)


@require_POST
def policy_set(request):
    """落定依赖走法；一经落定当天不许再改。"""
    if not _authed(request):
        return HttpResponseRedirect("/login")
    move = request.POST.get("move", "").strip()
    if move not in (MOVE_WAIT, MOVE_CANCEL):
        messages.error(request, "走法名无效")
        return HttpResponseRedirect("/dependencies/")
    with transaction.atomic():
        policy = DependencyPolicy.objects.order_by("id").select_for_update().first()
        if policy is None:
            policy = DependencyPolicy.objects.create(move=move, locked_date=timezone.localdate())
            messages.success(request, f"依赖走法已落定：{move}（当天锁定，不许再改）")
        else:
            if policy.locked_today():
                messages.error(
                    request,
                    f"依赖走法当天已落定为「{policy.move}」，一经落定当天不许再改",
                )
                return HttpResponseRedirect("/dependencies/")
            policy.move = move
            policy.locked_date = timezone.localdate()
            policy.save(update_fields=["move", "locked_date", "updated_at"])
            messages.success(request, f"依赖走法已落定：{move}（当天锁定，不许再改）")
    return HttpResponseRedirect("/dependencies/")
