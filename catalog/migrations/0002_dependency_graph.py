from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="parsejob",
            name="force_reason",
            field=models.CharField(blank=True, default="", max_length=255, verbose_name="强析缘由"),
        ),
        migrations.CreateModel(
            name="JobDependency",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "downstream",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="upstream_links",
                        to="catalog.parsejob",
                    ),
                ),
                (
                    "upstream",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="downstream_links",
                        to="catalog.parsejob",
                    ),
                ),
            ],
            options={"ordering": ["id"], "unique_together": {("downstream", "upstream")}},
        ),
        migrations.CreateModel(
            name="DependencyPolicy",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "move",
                    models.CharField(
                        choices=[
                            ("进等待舱", "下游进入等待舱，经人工确认后方可强行开析"),
                            ("取消并标依赖中断", "下游一律取消并标注依赖中断"),
                        ],
                        default="进等待舱",
                        max_length=32,
                    ),
                ),
                ("locked_date", models.DateField(blank=True, null=True, verbose_name="落定日期")),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["id"]},
        ),
        migrations.CreateModel(
            name="JobEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(max_length=32)),
                ("message", models.CharField(max_length=512)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "job",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="events",
                        to="catalog.parsejob",
                    ),
                ),
            ],
            options={"ordering": ["-id"]},
        ),
    ]
