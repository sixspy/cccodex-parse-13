from catalog.models import JobDependency, ParseJob

SEEDS = [
    ("活动页字段抽取", "promo_html", "待解析", "https://mock.local/promo"),
    ("报价单表格解析", "quote_xlsx", "待解析", "file://drops/quote.xlsx"),
    ("公告正文抽取", "notice_html", "待解析", "https://mock.local/notice"),
    ("目录索引解析", "index_json", "待解析", "https://mock.local/index.json"),
    ("汇总报表拼装", "report_merge", "待解析", "https://mock.local/report"),
]

# 下游 code -> 上游 code 列表：下游至少声明一个上游
DEP_LINKS = [
    ("notice_html", ["promo_html"]),
    ("index_json", ["quote_xlsx"]),
    ("report_merge", ["notice_html", "index_json"]),
]


def ensure_seed():
    if ParseJob.objects.count():
        return
    jobs = {}
    for title, code, status, uri in SEEDS:
        jobs[code] = ParseJob.objects.create(title=title, code=code, status=status, source_uri=uri)
    for downstream_code, upstream_codes in DEP_LINKS:
        for upstream_code in upstream_codes:
            JobDependency.objects.create(
                downstream=jobs[downstream_code], upstream=jobs[upstream_code]
            )
