import pytest

from catalog.models import (
    MOVE_CANCEL,
    MOVE_WAIT,
    JobDependency,
    JobEvent,
    DependencyPolicy,
    ParseJob,
)
from catalog.seed import ensure_seed


@pytest.fixture
def login(client):
    client.post("/login", {"username": "admin", "password": "123456"})
    return client


def make_job(code, status="待解析", **extra):
    return ParseJob.objects.create(
        title=code, code=code, status=status, source_uri=f"mock://{code}", **extra
    )


def link(downstream, *upstreams):
    for up in upstreams:
        JobDependency.objects.create(downstream=downstream, upstream=up)


# ---------- 依赖声明 ----------

@pytest.mark.django_db
def test_downstream_must_declare_at_least_one_upstream(login):
    job = make_job("solo")
    r = login.post(f"/jobs/{job.id}/deps/", {"upstream_ids": []})
    assert r.status_code == 302
    assert JobDependency.objects.filter(downstream=job).count() == 0
    # 显式断言：库里仍无任何依赖边
    assert not JobDependency.objects.exists()


@pytest.mark.django_db
def test_empty_dependency_post_is_rejected(login):
    job = make_job("solo2")
    r = login.post(f"/jobs/{job.id}/deps/", {"upstream_ids": ""})
    assert r.status_code == 302
    assert not JobDependency.objects.exists()


@pytest.mark.django_db
def test_dependency_save_records_upstream(login):
    up = make_job("up1")
    down = make_job("down1")
    r = login.post(f"/jobs/{down.id}/deps/", {"upstream_ids": [str(up.id)]})
    assert r.status_code == 302
    assert list(down.upstream_jobs()) == [up]
    event = JobEvent.objects.filter(job=down, kind="依赖设置").first()
    assert event and "up1" in event.message


@pytest.mark.django_db
def test_self_and_cycle_dependency_are_blocked(login):
    a = make_job("cyc_a")
    b = make_job("cyc_b")
    # 自依赖
    login.post(f"/jobs/{a.id}/deps/", {"upstream_ids": [str(a.id)]})
    assert not JobDependency.objects.exists()
    # 先建 b -> a，再建 a -> b 即成环
    JobDependency.objects.create(downstream=b, upstream=a)
    login.post(f"/jobs/{a.id}/deps/", {"upstream_ids": [str(b.id)]})
    assert JobDependency.objects.filter(downstream=a).count() == 0
    assert JobDependency.objects.filter(downstream=b).count() == 1


# ---------- 上游未齐禁止开析（后台强制）----------

@pytest.mark.django_db
def test_parse_blocked_when_upstream_not_ready(login):
    up = make_job("raw_up", status="待解析")
    down = make_job("raw_down")
    link(down, up)
    r = login.post(f"/jobs/{down.id}/parse/")
    assert r.status_code == 302
    down.refresh_from_db()
    assert down.status == "待解析"
    evt = JobEvent.objects.filter(job=down, kind="走法拦截").first()
    assert evt and "上游未齐" in evt.message


@pytest.mark.django_db
def test_parse_entry_hidden_while_upstream_reading(login):
    up = make_job("hid_up", status="待解析")
    down = make_job("hid_down")
    link(down, up)
    r = login.get(f"/jobs/{down.id}/")
    body = r.content.decode("utf-8")
    assert "/parse/" not in body
    assert "下游读盘入口保持不可见" in body


@pytest.mark.django_db
def test_parse_allowed_once_upstream_complete(login):
    up = make_job("done_up", status="已解析待入库")
    down = make_job("done_down")
    link(down, up)
    r = login.post(f"/jobs/{down.id}/parse/")
    assert r.status_code == 302
    down.refresh_from_db()
    assert down.status == "已解析待入库"
    assert JobEvent.objects.filter(job=down, kind="开析").exists()


@pytest.mark.django_db
def test_landed_upstream_counts_as_complete(login):
    up = make_job("landed_up", status="已入库")
    down = make_job("landed_down")
    link(down, up)
    login.post(f"/jobs/{down.id}/parse/")
    down.refresh_from_db()
    assert down.status == "已解析待入库"


# ---------- 上游挂掉：等待舱走法 ----------

@pytest.mark.django_db
def test_dead_upstream_sends_downstream_to_waiting_bay(login):
    DependencyPolicy.objects.create(move=MOVE_WAIT, locked_date=None)
    up = make_job("dead_up", status="解析失败")
    down = make_job("wait_down")
    link(down, up)
    r = login.post(f"/jobs/{down.id}/parse/")
    assert r.status_code == 302
    down.refresh_from_db()
    assert down.status == "等待舱"
    assert JobEvent.objects.filter(job=down, kind="进舱").exists()


@pytest.mark.django_db
def test_force_parse_requires_reason_and_waits_for_manual_confirm(login):
    DependencyPolicy.objects.create(move=MOVE_WAIT, locked_date=None)
    up = make_job("dead_up2", status="解析失败")
    down = make_job("wait_down2", status="等待舱")
    link(down, up)
    # 无缘由：拦
    r = login.post(f"/jobs/{down.id}/force-parse/", {"reason": "  "})
    assert r.status_code == 302
    down.refresh_from_db()
    assert down.status == "等待舱"
    # 有缘由：人工确认后强析
    r = login.post(f"/jobs/{down.id}/force-parse/", {"reason": "上游已人工补齐"})
    assert r.status_code == 302
    down.refresh_from_db()
    assert down.status == "已解析待入库"
    assert down.force_reason == "上游已人工补齐"
    assert JobEvent.objects.filter(job=down, kind="强析").exists()


@pytest.mark.django_db
def test_normal_parse_blocked_in_waiting_bay(login):
    down = make_job("wb_only", status="等待舱")
    r = login.post(f"/jobs/{down.id}/parse/")
    assert r.status_code == 302
    down.refresh_from_db()
    assert down.status == "等待舱"
    assert JobEvent.objects.filter(job=down, kind="走法拦截").exists()


# ---------- 上游挂掉：取消走法 ----------

@pytest.mark.django_db
def test_dead_upstream_cancels_downstream_with_interrupt(login):
    DependencyPolicy.objects.create(move=MOVE_CANCEL, locked_date=None)
    up = make_job("dead_up3", status="解析失败")
    down = make_job("cancel_down")
    link(down, up)
    r = login.post(f"/jobs/{down.id}/parse/")
    assert r.status_code == 302
    down.refresh_from_db()
    assert down.status == "依赖中断"
    assert JobEvent.objects.filter(job=down, kind="中断").exists()


@pytest.mark.django_db
def test_force_parse_blocked_under_cancel_policy(login):
    DependencyPolicy.objects.create(move=MOVE_CANCEL, locked_date=None)
    down = make_job("cancel_down2", status="等待舱")
    r = login.post(f"/jobs/{down.id}/force-parse/", {"reason": "想强开"})
    assert r.status_code == 302
    down.refresh_from_db()
    assert down.status == "等待舱"


@pytest.mark.django_db
def test_interrupted_job_parse_is_blocked(login):
    down = make_job("inter_only", status="依赖中断")
    login.post(f"/jobs/{down.id}/parse/")
    down.refresh_from_db()
    assert down.status == "依赖中断"
    assert JobEvent.objects.filter(job=down, kind="走法拦截").exists()


# ---------- 走法偏离拦截 / 策略当日锁 ----------

@pytest.mark.django_db
def test_policy_locked_same_day(login):
    from django.utils import timezone
    today = timezone.localdate()
    DependencyPolicy.objects.create(move=MOVE_WAIT, locked_date=today)
    r = login.post("/dependencies/policy/", {"move": MOVE_CANCEL})
    assert r.status_code == 302
    assert DependencyPolicy.get_solo().move == MOVE_WAIT


@pytest.mark.django_db
def test_policy_can_be_set_when_not_locked(login):
    r = login.post("/dependencies/policy/", {"move": MOVE_CANCEL})
    assert r.status_code == 302
    p = DependencyPolicy.get_solo()
    assert p.move == MOVE_CANCEL and p.locked_date is not None
    # 落定后同日再改仍被拦
    login.post("/dependencies/policy/", {"move": MOVE_WAIT})
    assert DependencyPolicy.get_solo().move == MOVE_CANCEL


# ---------- 下游争用：只留先到 ----------

@pytest.mark.django_db
def test_contention_only_first_wins(login):
    up = make_job("cont_up", status="已入库")
    down = make_job("cont_down")
    link(down, up)
    r1 = login.post(f"/jobs/{down.id}/parse/")
    r2 = login.post(f"/jobs/{down.id}/parse/")
    assert r1.status_code == 302 and r2.status_code == 302
    down.refresh_from_db()
    assert down.status == "已解析待入库"
    assert JobEvent.objects.filter(job=down, kind="开析").count() == 1
    assert JobEvent.objects.filter(job=down, kind="争用失败").count() == 1


# ---------- 依赖列表展示 ----------

@pytest.mark.django_db
def test_board_shows_upstream_state_waiting_interrupt_count_and_move(login):
    DependencyPolicy.objects.create(move=MOVE_WAIT, locked_date=None)
    up = make_job("board_up", status="解析失败")
    down = make_job("board_down", status="等待舱")
    link(down, up)
    r = login.get("/dependencies/")
    assert r.status_code == 200
    body = r.content.decode("utf-8")
    assert "board_down" in body and "board_up" in body
    assert "等待舱" in body and "中断数" in body and MOVE_WAIT in body
    assert "上游未齐" in body


@pytest.mark.django_db
def test_detail_shows_force_reason_and_contention_failure(login):
    up = make_job("ev_up", status="已入库")
    down = make_job("ev_down", force_reason="人工补齐数据")
    link(down, up)
    JobEvent.objects.create(job=down, kind="争用失败", message="下游争用失败：只留先到")
    r = login.get(f"/jobs/{down.id}/")
    body = r.content.decode("utf-8")
    assert "人工补齐数据" in body and "争用失败" in body and "只留先到" in body


# ---------- 种子与基线 ----------

@pytest.mark.django_db
def test_seed_builds_dependency_chain(login):
    ensure_seed()
    report = ParseJob.objects.get(code="report_merge")
    codes = sorted(u.code for u in report.upstream_jobs())
    assert codes == ["index_json", "notice_html"]
    r = login.get("/jobs/")
    assert r.status_code == 200
    r = login.get("/dependencies/")
    assert r.status_code == 200


@pytest.mark.django_db
def test_anonymous_is_redirected(client):
    r = client.get("/dependencies/")
    assert r.status_code == 302
    assert r["Location"] == "/login"
