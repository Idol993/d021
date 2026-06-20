from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .audit import audit_logger
from .config import config
from .models import get_session, get_utc_now, ensure_utc
from .notifier import NotificationSeverity, notifier
from .reports import reports_engine


class ReleaseScheduler:
    _instance = None
    _scheduler: Optional[BackgroundScheduler] = None
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.timezone = config.get("system.timezone", "Asia/Shanghai")

    def start(self) -> Dict[str, Any]:
        if self._scheduler and self._scheduler.running:
            return {
                "running": True,
                "already_running": True,
                "jobs": self._list_jobs_info(),
                "message": "调度器已在运行中，未重复创建任务",
            }

        self._scheduler = BackgroundScheduler(timezone=self.timezone)

        weekly_cron = config.get("reports.weekly.schedule", "0 9 * * 1")
        drill_cron = config.get("rollback_drill.schedule", "0 14 28 * *")

        self._scheduler.add_job(
            func=self._weekly_report_job,
            trigger=CronTrigger.from_crontab(weekly_cron, timezone=self.timezone),
            id="weekly_report_job",
            name="周报自动生成",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
        )

        self._scheduler.add_job(
            func=self._rollback_drill_job,
            trigger=CronTrigger.from_crontab(drill_cron, timezone=self.timezone),
            id="rollback_drill_job",
            name="月度回滚演练",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
        )

        self._scheduler.start()

        audit_logger.log(
            actor="system",
            action="调度器启动",
            category="scheduler",
            details={
                "weekly_cron": weekly_cron,
                "drill_cron": drill_cron,
                "timezone": self.timezone,
                "jobs": self._list_jobs_info(),
            },
        )

        return {
            "running": True,
            "already_running": False,
            "jobs": self._list_jobs_info(),
            "message": "调度器已启动，2个定时任务已注册",
        }

    def stop(self) -> Dict[str, Any]:
        if not self._scheduler or not self._scheduler.running:
            return {"running": False, "already_stopped": True, "message": "调度器未在运行"}

        jobs = self._list_jobs_info()
        self._scheduler.shutdown(wait=True)
        self._scheduler = None

        audit_logger.log(
            actor="system",
            action="调度器停止",
            category="scheduler",
            details={"jobs_stopped": jobs},
        )

        return {
            "running": False,
            "already_stopped": False,
            "jobs": jobs,
            "message": "调度器已停止",
        }

    def status(self) -> Dict[str, Any]:
        if not self._scheduler:
            return {"running": False, "jobs": []}
        return {
            "running": self._scheduler.running,
            "jobs": self._list_jobs_info(),
        }

    def trigger_now(self, job_id: str) -> Dict[str, Any]:
        if not self._scheduler or not self._scheduler.running:
            return {"success": False, "message": "调度器未在运行"}

        job = self._scheduler.get_job(job_id)
        if not job:
            return {"success": False, "message": f"任务不存在: {job_id}"}

        job.modify(next_run_time=get_utc_now())
        return {"success": True, "message": f"任务已立即触发: {job.name}"}

    def _list_jobs_info(self) -> Dict[str, Any]:
        if not self._scheduler:
            return {}
        jobs = self._scheduler.get_jobs()
        result = {}
        for job in jobs:
            trigger = job.trigger
            next_run = ensure_utc(job.next_run_time) if job.next_run_time else None
            result[job.id] = {
                "name": job.name,
                "cron": str(trigger),
                "next_run": next_run.isoformat() if next_run else None,
                "misfire_grace_time": job.misfire_grace_time,
            }
        return result

    def _weekly_report_job(self):
        try:
            print(f"[{get_utc_now().isoformat()}] 📊 执行定时任务：生成周报...")
            report = reports_engine.generate_weekly_report(force=False)
            if report:
                fps = report.file_paths or {}
                notifier.send(
                    title="📊 MES运营周报已自动生成",
                    content=(
                        f"**周期**: {report.week_start.date()} ~ {report.week_end.date()}\n"
                        f"**发布次数**: {report.metrics.get('total_releases', 0)}\n"
                        f"**成功率**: {report.metrics.get('success_rate', 0) * 100:.1f}%\n"
                        f"**回滚次数**: {report.metrics.get('rollback_count', 0)}\n\n"
                        + (f"📄 [XLSX]({fps.get('xlsx', '')})\n" if "xlsx" in fps else "")
                        + (f"📄 [PDF]({fps.get('pdf', '')})\n" if "pdf" in fps else "")
                    ),
                    severity=NotificationSeverity.INFO,
                )
                audit_logger.log(
                    actor="scheduler",
                    action="周报自动生成",
                    category="report",
                    details={"report_id": report.id, "files": fps},
                )
                print(f"  ✅ 周报生成完成: {report.id}")
            else:
                print(f"  ⚠️  本周无数据，跳过周报生成")
        except Exception as e:
            print(f"  ❌ 周报生成失败: {e}")
            notifier.send(
                title="🔴 定时任务异常：周报生成失败",
                content=f"错误: {str(e)}",
                severity=NotificationSeverity.CRITICAL,
            )

    def _rollback_drill_job(self):
        try:
            print(f"[{get_utc_now().isoformat()}] 🎬 执行定时任务：月度回滚演练...")
            from datetime import datetime as dt

            title = f"月度回滚演练 - {dt.now().strftime('%Y年%m月')}"
            drill = reports_engine.schedule_rollback_drill(
                scheduled_at=get_utc_now() + timedelta(minutes=1),
                title=title,
                scenario="定期月度演练，验证熔断机制与回滚流程有效性",
            )

            notify_before = config.get("rollback_drill.notify_before_hours", 72)
            notifier.send(
                title="🎬 回滚演练预告",
                content=(
                    f"**演练ID**: {drill.id}\n"
                    f"**名称**: {title}\n"
                    f"**计划时间**: {drill.scheduled_at.strftime('%Y-%m-%d %H:%M')}\n"
                    f"**参与人员**: {', '.join([p.get('role', '') for p in (drill.participants or [])])}\n\n"
                    f"请相关人员于{notify_before}小时内确认参与。"
                ),
                severity=NotificationSeverity.WARNING,
            )

            ok, status = reports_engine.execute_rollback_drill(drill.id)

            session = get_session()
            try:
                from .models import RollbackDrill
                drill_refresh = session.query(RollbackDrill).filter_by(id=drill.id).first()
                if drill_refresh:
                    audit_logger.log(
                        actor="scheduler",
                        action="回滚演练自动执行",
                        category="drill",
                        details={
                            "drill_id": drill_refresh.id,
                            "status": drill_refresh.status,
                            "success": drill_refresh.success,
                            "duration_seconds": drill_refresh.duration_seconds,
                        },
                    )
                    if drill_refresh.success:
                        notifier.send(
                            title="✅ 月度回滚演练成功",
                            content=(
                                f"**演练ID**: {drill_refresh.id}\n"
                                f"**耗时**: {drill_refresh.duration_seconds:.1f}秒\n"
                                f"**问题**: {len(drill_refresh.issues_found or [])}个\n"
                                f"**改进项**: {len(drill_refresh.improvement_actions or [])}项\n\n"
                                + "\n".join(f"- {imp}" for imp in (drill_refresh.improvement_actions or []))
                            ),
                            severity=NotificationSeverity.SUCCESS,
                        )
                    else:
                        notifier.send(
                            title="🔴 月度回滚演练失败",
                            content=(
                                f"**演练ID**: {drill_refresh.id}\n"
                                f"**状态**: {drill_refresh.status}\n"
                                f"**问题**: {'; '.join(drill_refresh.issues_found or [])}"
                            ),
                            severity=NotificationSeverity.CRITICAL,
                        )
                    print(f"  ✅ 演练完成: status={drill_refresh.status}, success={drill_refresh.success}")
            finally:
                session.close()

        except Exception as e:
            print(f"  ❌ 回滚演练失败: {e}")
            import traceback
            traceback.print_exc()
            notifier.send(
                title="🔴 定时任务异常：回滚演练失败",
                content=f"错误: {str(e)}",
                severity=NotificationSeverity.CRITICAL,
            )


release_scheduler = ReleaseScheduler()
