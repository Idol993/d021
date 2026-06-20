import json
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .audit import audit_logger
from .config import config
from .models import get_session, get_utc_now, ensure_utc
from .notifier import NotificationSeverity, notifier
from .reports import reports_engine


_STATE_DIR = Path(config.get("database.path", "./data/mes_release.db")).parent
_STATE_FILE = _STATE_DIR / "scheduler_state.json"


def _is_pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError, PermissionError):
        return False


def _read_state() -> Optional[Dict[str, Any]]:
    if not _STATE_FILE.exists():
        return None
    try:
        data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        return data
    except Exception:
        return None


def _write_state(state: Dict[str, Any]):
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = get_utc_now().isoformat()
    _STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _remove_state():
    try:
        if _STATE_FILE.exists():
            _STATE_FILE.unlink()
    except Exception:
        pass


def _check_external_scheduler() -> Optional[Dict[str, Any]]:
    state = _read_state()
    if not state:
        return None
    pid = state.get("pid")
    if not pid:
        _remove_state()
        return None
    if _is_pid_alive(pid):
        return state
    _remove_state()
    return None


class ReleaseScheduler:
    _instance = None
    _scheduler = None
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

    def start(self, blocking: bool = False) -> Dict[str, Any]:
        if self._scheduler and self._scheduler.running:
            return {
                "running": True,
                "already_running": True,
                "jobs": self._list_jobs_info(),
                "message": "调度器已在当前进程运行中，未重复创建任务",
            }

        external = _check_external_scheduler()
        if external:
            return {
                "running": True,
                "already_running": True,
                "external_pid": external["pid"],
                "jobs": external.get("jobs", {}),
                "message": f"调度器已在运行中（PID={external['pid']}），未重复创建任务",
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

        jobs_info = self._list_jobs_info()
        state = {
            "pid": os.getpid(),
            "running": True,
            "started_at": get_utc_now().isoformat(),
            "jobs": jobs_info,
        }
        _write_state(state)

        try:
            audit_logger.log(
                actor="system",
                action="调度器启动",
                category="scheduler",
                details={
                    "weekly_cron": weekly_cron,
                    "drill_cron": drill_cron,
                    "timezone": self.timezone,
                    "jobs": jobs_info,
                    "pid": os.getpid(),
                },
            )
        except Exception:
            pass

        msg = "调度器已启动，2个定时任务已注册"
        if blocking:
            msg += "（常驻运行中，按 Ctrl+C 停止）"

        return {
            "running": True,
            "already_running": False,
            "jobs": jobs_info,
            "message": msg,
        }

    def stop(self) -> Dict[str, Any]:
        if self._scheduler and self._scheduler.running:
            jobs = self._list_jobs_info()
            self._scheduler.shutdown(wait=True)
            self._scheduler = None
            _remove_state()
            try:
                audit_logger.log(
                    actor="system",
                    action="调度器停止",
                    category="scheduler",
                    details={"jobs_stopped": jobs},
                )
            except Exception:
                pass
            return {
                "running": False,
                "already_stopped": False,
                "jobs": jobs,
                "message": "调度器已停止",
            }

        external = _check_external_scheduler()
        if external:
            pid = external["pid"]
            jobs = external.get("jobs", {})
            try:
                if sys.platform == "win32":
                    import subprocess
                    subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                                   capture_output=True, timeout=10)
                else:
                    os.kill(pid, signal.SIGTERM)
                for _ in range(30):
                    time.sleep(0.2)
                    if not _is_pid_alive(pid):
                        break
            except PermissionError:
                return {
                    "running": True,
                    "already_stopped": False,
                    "message": f"无权限停止调度器进程 (PID={pid})，请手动终止",
                }
            except Exception as e:
                return {
                    "running": True,
                    "already_stopped": False,
                    "message": f"停止调度器进程失败: {e}",
                }

            if _is_pid_alive(pid):
                _remove_state()
                return {
                    "running": True,
                    "already_stopped": False,
                    "message": f"调度器进程 (PID={pid}) 仍在运行，请手动终止",
                }

            _remove_state()
            return {
                "running": False,
                "already_stopped": False,
                "jobs": jobs,
                "message": f"调度器已停止（已终止进程 PID={pid}）",
            }

        _remove_state()
        return {"running": False, "already_stopped": True, "message": "调度器未在运行"}

    def status(self) -> Dict[str, Any]:
        if self._scheduler and self._scheduler.running:
            jobs = self._list_jobs_info()
            state = {"pid": os.getpid(), "running": True, "jobs": jobs}
            _write_state(state)
            return {"running": True, "jobs": jobs, "pid": os.getpid()}

        external = _check_external_scheduler()
        if external:
            pid = external["pid"]
            try:
                state = _read_state() or {}
                jobs = state.get("jobs", external.get("jobs", {}))
            except Exception:
                jobs = external.get("jobs", {})
            return {"running": True, "jobs": jobs, "pid": pid}

        _remove_state()
        return {"running": False, "jobs": {}, "pid": None}

    def trigger_now(self, job_id: str) -> Dict[str, Any]:
        job_funcs = {
            "weekly_report_job": self._weekly_report_job,
            "rollback_drill_job": self._rollback_drill_job,
        }
        if job_id not in job_funcs:
            return {"success": False, "message": f"任务不存在: {job_id}"}

        print(f"[{get_utc_now().isoformat()}] ⚡ 手动触发任务: {job_id}")
        try:
            result = job_funcs[job_id]()
            if self._scheduler and self._scheduler.running:
                job = self._scheduler.get_job(job_id)
                if job:
                    try:
                        job.modify(next_run_time=get_utc_now())
                    except Exception:
                        pass
            return result
        except Exception as e:
            import traceback
            err_detail = traceback.format_exc()
            print(f"  ❌ 任务执行失败: {e}\n{err_detail}")
            return {"success": False, "message": f"任务执行失败: {str(e)}", "error_detail": err_detail}

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

    def _weekly_report_job(self) -> Dict[str, Any]:
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
                try:
                    audit_logger.log(
                        actor="scheduler",
                        action="周报自动生成",
                        category="report",
                        details={"report_id": report.id, "files": fps},
                    )
                except Exception:
                    pass
                print(f"  ✅ 周报生成完成: {report.id}")
                return {
                    "success": True,
                    "message": f"周报生成成功 (ID: {report.id})",
                    "report_id": report.id,
                    "files": fps,
                }
            else:
                print(f"  ⚠️  本周无数据，跳过周报生成")
                return {
                    "success": False,
                    "message": "本周无数据，周报未生成",
                }
        except Exception as e:
            import traceback
            err_detail = traceback.format_exc()
            print(f"  ❌ 周报生成失败: {e}\n{err_detail}")
            try:
                notifier.send(
                    title="🔴 定时任务异常：周报生成失败",
                    content=f"错误: {str(e)}",
                    severity=NotificationSeverity.CRITICAL,
                )
            except Exception:
                pass
            return {
                "success": False,
                "message": f"周报生成失败: {str(e)}",
                "error_detail": err_detail,
            }

    def _rollback_drill_job(self) -> Dict[str, Any]:
        try:
            print(f"[{get_utc_now().isoformat()}] 🎬 执行定时任务：月度回滚演练...")
            title = f"月度回滚演练 - {datetime.now().strftime('%Y年%m月')}"
            drill = reports_engine.schedule_rollback_drill(
                scheduled_at=get_utc_now() + timedelta(minutes=1),
                title=title,
                scenario="定期月度演练，验证熔断机制与回滚流程有效性",
            )

            notify_before = config.get("rollback_drill.notify_before_hours", 72)
            try:
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
            except Exception:
                pass

            ok, drill_status = reports_engine.execute_rollback_drill(drill.id)

            session = get_session()
            try:
                from .models import RollbackDrill
                drill_refresh = session.query(RollbackDrill).filter_by(id=drill.id).first()
                if drill_refresh:
                    try:
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
                    except Exception:
                        pass
                    if drill_refresh.success:
                        try:
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
                        except Exception:
                            pass
                    else:
                        issues_str = "; ".join(drill_refresh.issues_found or [])
                        try:
                            notifier.send(
                                title="🔴 月度回滚演练失败",
                                content=(
                                    f"**演练ID**: {drill_refresh.id}\n"
                                    f"**状态**: {drill_refresh.status}\n"
                                    f"**问题**: {issues_str}"
                                ),
                                severity=NotificationSeverity.CRITICAL,
                            )
                        except Exception:
                            pass
                    print(f"  ✅ 演练完成: status={drill_refresh.status}, success={drill_refresh.success}")
                    return {
                        "success": bool(drill_refresh.success),
                        "message": f"演练{'成功' if drill_refresh.success else '失败'} (ID: {drill_refresh.id}, status: {drill_refresh.status})",
                        "drill_id": drill_refresh.id,
                        "drill_status": drill_refresh.status,
                        "drill_success": bool(drill_refresh.success),
                        "duration_seconds": drill_refresh.duration_seconds,
                        "issues_found": drill_refresh.issues_found or [],
                    }
            finally:
                session.close()

            return {
                "success": ok,
                "message": f"演练执行{'成功' if ok else '失败'} (status: {drill_status})",
                "drill_status": drill_status,
            }

        except Exception as e:
            import traceback
            err_detail = traceback.format_exc()
            print(f"  ❌ 回滚演练失败: {e}\n{err_detail}")
            try:
                notifier.send(
                    title="🔴 定时任务异常：回滚演练失败",
                    content=f"错误: {str(e)}",
                    severity=NotificationSeverity.CRITICAL,
                )
            except Exception:
                pass
            return {
                "success": False,
                "message": f"回滚演练执行异常: {str(e)}",
                "error_detail": err_detail,
            }


release_scheduler = ReleaseScheduler()
