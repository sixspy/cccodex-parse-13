from django.db import models
from django.utils import timezone

# 任务状态
STATUS_READY = "待解析"
STATUS_FAILED = "解析失败"
STATUS_PARSED = "已解析待入库"
STATUS_LANDED = "已入库"
STATUS_WAITING = "等待舱"
STATUS_INTERRUPTED = "依赖中断"

# 上游已读盘完成（已解析或已入库）即视为齐套
UPSTREAM_FINISHED_STATUSES = {STATUS_PARSED, STATUS_LANDED}
# 上游挂掉：解析失败或已被标依赖中断
UPSTREAM_DEAD_STATUSES = {STATUS_FAILED, STATUS_INTERRUPTED}
# 允许发起开析的任务态
OPENABLE_STATUSES = {STATUS_READY, STATUS_FAILED}

# 依赖走法（上游挂掉后下游去向），一经落定当天不许再改
MOVE_WAIT = "进等待舱"
MOVE_CANCEL = "取消并标依赖中断"
POLICY_MOVE_CHOICES = [
    (MOVE_WAIT, "下游进入等待舱，经人工确认后方可强行开析"),
    (MOVE_CANCEL, "下游一律取消并标注依赖中断"),
]

# 读盘详情事件种类
EVENT_PARSE = "开析"
EVENT_FORCE = "强析"
EVENT_WAIT = "进舱"
EVENT_INTERRUPT = "中断"
EVENT_CONTEND = "争用失败"
EVENT_BLOCK = "走法拦截"
EVENT_DEPS = "依赖设置"


class ParseJob(models.Model):
    title = models.CharField(max_length=120)
    code = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=32, default=STATUS_READY)
    source_uri = models.CharField(max_length=255)
    parsed = models.IntegerField(default=0)
    landed = models.IntegerField(default=0)
    force_reason = models.CharField("强析缘由", max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.code} · {self.title}"

    def upstream_jobs(self):
        return [link.upstream for link in self.upstream_links.select_related("upstream")]

    def has_upstream(self) -> bool:
        return self.upstream_links.exists()

    def upstream_all_complete(self) -> bool:
        ups = self.upstream_jobs()
        return bool(ups) and all(u.status in UPSTREAM_FINISHED_STATUSES for u in ups)

    def pending_upstreams(self):
        """上游仍在读盘（未完成且未挂掉）。"""
        return [
            u for u in self.upstream_jobs()
            if u.status not in UPSTREAM_FINISHED_STATUSES and u.status not in UPSTREAM_DEAD_STATUSES
        ]

    def dead_upstreams(self):
        return [u for u in self.upstream_jobs() if u.status in UPSTREAM_DEAD_STATUSES]

    def parse_entry_visible(self) -> bool:
        """上游仍在读盘时，下游读盘入口保持不可见。"""
        if self.status not in OPENABLE_STATUSES:
            return False
        ups = self.upstream_jobs()
        if not ups:
            return True
        return all(u.status in UPSTREAM_FINISHED_STATUSES for u in ups)


class JobDependency(models.Model):
    """依赖图有向边：downstream 依赖 upstream。"""

    downstream = models.ForeignKey(
        ParseJob, related_name="upstream_links", on_delete=models.CASCADE
    )
    upstream = models.ForeignKey(
        ParseJob, related_name="downstream_links", on_delete=models.CASCADE
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        unique_together = ("downstream", "upstream")

    def __str__(self):
        return f"{self.downstream.code} 依赖 {self.upstream.code}"


class DependencyPolicy(models.Model):
    """依赖策略单例：上游挂掉后下游去向，落定当日锁定。"""

    move = models.CharField(max_length=32, choices=POLICY_MOVE_CHOICES, default=MOVE_WAIT)
    locked_date = models.DateField("落定日期", null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"依赖走法：{self.move}"

    @classmethod
    def get_solo(cls) -> "DependencyPolicy":
        solo = cls.objects.order_by("id").first()
        if solo is None:
            # 尚未落定：返回未入库的默认实例（默认进等待舱）
            return cls(move=MOVE_WAIT, locked_date=None)
        return solo

    def locked_today(self) -> bool:
        return self.locked_date is not None and self.locked_date == timezone.localdate()


class JobEvent(models.Model):
    """读盘详情流水：强析缘由、下游争用失败、走法拦截等都写在这里。"""

    job = models.ForeignKey(ParseJob, related_name="events", on_delete=models.CASCADE)
    kind = models.CharField(max_length=32)
    message = models.CharField(max_length=512)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        return f"[{self.kind}] {self.message}"
