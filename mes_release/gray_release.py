import random
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .audit import audit_logger
from .config import config
from .models import (
    CircuitBreakerState,
    ensure_utc,
    GrayReleaseRecord,
    ReleaseRequest,
    ReleaseStatus,
    RollbackRecord,
    get_session,
    get_utc_now,
)
from .notifier import NotificationSeverity, notifier


class MetricsSnapshot:
    def __init__(
        self,
        timestamp: datetime,
        production_anomaly_rate: float,
        ebr_error_rate: float,
        device_comm_latency_ms: float,
        interface_error_rate: float,
        active_batches: List[str],
    ):
        self.timestamp = timestamp
        self.production_anomaly_rate = production_anomaly_rate
        self.ebr_error_rate = ebr_error_rate
        self.device_comm_latency_ms = device_comm_latency_ms
        self.interface_error_rate = interface_error_rate
        self.active_batches = active_batches

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "production_anomaly_rate": self.production_anomaly_rate,
            "ebr_error_rate": self.ebr_error_rate,
            "device_comm_latency_ms": self.device_comm_latency_ms,
            "interface_error_rate": self.interface_error_rate,
            "active_batches": self.active_batches,
        }


class CircuitBreakerMetricsResult:
    def __init__(self):
        self.red_breaches: List[Dict[str, Any]] = []
        self.yellow_warnings: List[Dict[str, Any]] = []
        self.snapshot: Optional[MetricsSnapshot] = None


class GrayReleaseEngine:
    def __init__(self):
        self.gray_cfg = config.get("gray_release", {})
        self.phases_cfg = self.gray_cfg.get("phases", [])
        self.monitor_cfg = self.gray_cfg.get("monitoring", {})
        self.rollback_cfg = self.gray_cfg.get("rollback", {})
        self.cb_cfg = self.monitor_cfg.get("circuit_breaker", {})
        self._active_monitors: Dict[str, threading.Event] = {}
        self._circuit_states: Dict[str, CircuitBreakerState] = {}
        self._circuit_failure_counts: Dict[str, int] = {}

    def start_gray_release(self, release_id: str) -> Tuple[bool, str]:
        session = get_session()
        try:
            release = session.query(ReleaseRequest).filter_by(id=release_id).first()
            if not release:
                return False, "发布申请不存在"
            if release.status != ReleaseStatus.APPROVAL_PASSED:
                return False, f"状态不正确: {release.status.value}，需审批通过后才能灰度"

            existing_gray = (
                session.query(GrayReleaseRecord)
                .filter_by(release_id=release.id)
                .order_by(GrayReleaseRecord.phase_order.asc())
                .all()
            )
            if not existing_gray:
                for phase in self.phases_cfg:
                    rec = GrayReleaseRecord(
                        release_id=release.id,
                        phase_order=phase["order"],
                        phase_name=phase["name"],
                        production_lines=list(phase.get("production_lines", [])),
                        status="pending",
                    )
                    session.add(rec)
                session.commit()

            release.status = ReleaseStatus.GRAY_IN_PROGRESS
            release.current_phase = 1
            session.commit()

            audit_logger.log(
                actor="system",
                action="启动灰度发布",
                category="gray_release",
                release_id=release.id,
                details={
                    "version": release.version,
                    "phases_count": len(self.phases_cfg),
                },
            )

            notifier.send(
                title="🚀 MES灰度发布启动",
                content=(
                    f"版本 **{release.version}** - {release.title}\n"
                    f"发布类型: {release.release_type.value}\n"
                    f"灰度阶段共 {len(self.phases_cfg)} 级\n"
                    f"即将部署至: 第1阶段 - {self.phases_cfg[0]['name']}\n"
                    f"产线: {', '.join(self.phases_cfg[0].get('production_lines', []))}"
                ),
                severity=NotificationSeverity.INFO,
                extra={"发布申请ID": release.id},
            )

            session.close()
            self._deploy_and_monitor(release_id)
            return True, "灰度发布已启动"
        finally:
            pass

    def _deploy_and_monitor(self, release_id: str):
        session = get_session()
        try:
            release = session.query(ReleaseRequest).filter_by(id=release_id).first()
            if not release:
                return

            while release.current_phase and release.current_phase <= len(self.phases_cfg):
                phase_idx = release.current_phase - 1
                phase_cfg = self.phases_cfg[phase_idx]

                phase_record = (
                    session.query(GrayReleaseRecord)
                    .filter_by(release_id=release.id, phase_order=release.current_phase)
                    .first()
                )
                if not phase_record:
                    break

                phase_record.status = "deploying"
                phase_record.started_at = get_utc_now()
                phase_record.deploy_result = self._deploy_to_production_lines(
                    release, phase_cfg.get("production_lines", [])
                )
                deploy_ok = phase_record.deploy_result.get("success", False)

                if not deploy_ok:
                    phase_record.status = "deploy_failed"
                    phase_record.completed_at = get_utc_now()
                    session.commit()
                    self._trigger_rollback(
                        release,
                        trigger_source="deploy_failure",
                        reason=f"第{release.current_phase}阶段部署失败: {phase_record.deploy_result.get('error')}",
                        affected_lines=phase_cfg.get("production_lines", []),
                        anomaly_details=phase_record.deploy_result,
                    )
                    session.refresh(release)
                    return

                phase_record.status = "monitoring"
                session.commit()

                monitor_interval_min = self.monitor_cfg.get("interval_min", 5)
                duration_min = phase_cfg.get("duration_min", 30)
                total_checks = max(1, int(duration_min / monitor_interval_min))
                monitor_interval_s = min(1.0, monitor_interval_min * 0.02)

                consecutive_red = 0
                phase_failed = False

                for _ in range(total_checks):
                    time.sleep(monitor_interval_s)
                    snapshot = self._collect_metrics_snapshot(release, phase_cfg.get("production_lines", []))
                    phase_record.monitor_snapshots = (phase_record.monitor_snapshots or []) + [snapshot.to_dict()]
                    session.commit()

                    cb_result = self._check_circuit_breaker_thresholds(snapshot)
                    if cb_result.yellow_warnings:
                        notifier.send(
                            title="⚠️ 灰度监控黄色预警",
                            content=(
                                f"版本 **{release.version}**\n"
                                f"阶段: 第{release.current_phase}级 - {phase_cfg['name']}\n"
                                + "\n".join(
                                    f"- {w['metric']}: {w['value']}{w['unit']} (阈值黄:{w['threshold']})"
                                    for w in cb_result.yellow_warnings
                                )
                            ),
                            severity=NotificationSeverity.WARNING,
                            extra={"发布申请ID": release.id},
                        )

                    if cb_result.red_breaches:
                        consecutive_red += 1
                        threshold_count = self.cb_cfg.get("consecutive_failures", 2)
                        if consecutive_red >= threshold_count:
                            phase_record.circuit_breaker_triggered = True
                            phase_record.circuit_breaker_reason = "; ".join(
                                f"{b['metric']}={b['value']}{b['unit']}" for b in cb_result.red_breaches
                            )
                            session.commit()
                            batches = snapshot.active_batches if snapshot else []
                            self._trigger_rollback(
                                release,
                                trigger_source="circuit_breaker",
                                reason=f"熔断触发: {phase_record.circuit_breaker_reason}",
                                affected_lines=phase_cfg.get("production_lines", []),
                                affected_batches=batches,
                                anomaly_details={
                                    "breaches": cb_result.red_breaches,
                                    "warnings": cb_result.yellow_warnings,
                                    "snapshot": snapshot.to_dict() if snapshot else None,
                                },
                            )
                            session.refresh(release)
                            phase_record.status = "circuit_breaker"
                            phase_record.completed_at = get_utc_now()
                            session.commit()
                            phase_failed = True
                            break
                    else:
                        consecutive_red = 0

                if phase_failed:
                    return

                phase_record.status = "monitoring_done"
                session.commit()

                if phase_cfg.get("require_manual_confirm"):
                    notifier.send(
                        title="🔐 MES灰度阶段需人工确认",
                        content=(
                            f"版本 **{release.version}**\n"
                            f"阶段: 第{release.current_phase}级 - {phase_cfg['name']} 监控完成\n"
                            f"下一阶段为高风险产线，需高级主管手动确认推进。\n"
                            f"产线: {', '.join(self.phases_cfg[min(phase_idx + 1, len(self.phases_cfg)-1)].get('production_lines', []))}"
                        ),
                        severity=NotificationSeverity.WARNING,
                        extra={"发布申请ID": release.id},
                    )
                    return

                if phase_cfg.get("auto_advance", True):
                    phase_record.advanced_to_next = True
                    phase_record.completed_at = get_utc_now()
                    release.current_phase += 1
                    if release.current_phase > len(self.phases_cfg):
                        release.status = ReleaseStatus.GRAY_COMPLETED
                    session.commit()
                else:
                    break

                session.refresh(release)

            if release.status == ReleaseStatus.GRAY_COMPLETED:
                release.status = ReleaseStatus.FULL_RELEASED
                session.commit()
                audit_logger.log(
                    actor="system",
                    action="灰度发布完成，全量上线",
                    category="gray_release",
                    release_id=release.id,
                    details={"version": release.version},
                )
                notifier.send(
                    title="🎉 MES版本全量发布成功",
                    content=(
                        f"版本 **{release.version}** - {release.title}\n"
                        f"所有灰度阶段已顺利完成，版本已全量上线。\n"
                        f"请继续关注生产运行状况。"
                    ),
                    severity=NotificationSeverity.SUCCESS,
                    extra={"发布申请ID": release.id},
                )
        finally:
            session.close()

    def _deploy_to_production_lines(
        self, release: ReleaseRequest, production_lines: List[str]
    ) -> Dict[str, Any]:
        results: Dict[str, Any] = {"lines": {}}
        all_ok = True
        for line in production_lines:
            time.sleep(random.uniform(0.1, 0.3))
            success = random.random() > 0.03
            results["lines"][line] = {
                "success": success,
                "deployment_id": f"DEP-{release.id[:8]}-{line}-{random.randint(1000,9999)}",
                "completed_at": get_utc_now().isoformat(),
                "error": None if success else "部署脚本执行超时",
            }
            if not success:
                all_ok = False
        results["success"] = all_ok
        if not all_ok:
            failed = [k for k, v in results["lines"].items() if not v["success"]]
            results["error"] = f"{len(failed)}条产线部署失败: {', '.join(failed)}"
        return results

    def _collect_metrics_snapshot(
        self, release: ReleaseRequest, production_lines: List[str]
    ) -> MetricsSnapshot:
        return MetricsSnapshot(
            timestamp=get_utc_now(),
            production_anomaly_rate=round(random.uniform(0.05, 3.5), 2),
            ebr_error_rate=round(random.uniform(0.02, 2.2), 2),
            device_comm_latency_ms=random.randint(80, 2500),
            interface_error_rate=round(random.uniform(0.01, 2.5), 2),
            active_batches=[
                f"B{random.randint(2026060000, 2026069999)}"
                for _ in range(random.randint(2, 8))
            ],
        )

    def _check_circuit_breaker_thresholds(self, snapshot: Optional[MetricsSnapshot]) -> CircuitBreakerMetricsResult:
        result = CircuitBreakerMetricsResult()
        if not snapshot:
            return result
        result.snapshot = snapshot

        metric_thresholds = self.monitor_cfg.get("metrics", {})
        metric_values = {
            "production_anomaly_rate": snapshot.production_anomaly_rate,
            "ebr_error_rate": snapshot.ebr_error_rate,
            "device_comm_latency_ms": snapshot.device_comm_latency_ms,
            "interface_error_rate": snapshot.interface_error_rate,
        }

        for key, cfg in metric_thresholds.items():
            value = metric_values.get(key, 0)
            name = cfg.get("name", key)
            unit = cfg.get("unit", "")
            yellow = cfg.get("threshold_yellow")
            red = cfg.get("threshold_red")

            if red is not None and value >= red:
                result.red_breaches.append({
                    "key": key,
                    "metric": name,
                    "value": value,
                    "threshold": red,
                    "unit": unit,
                    "level": "red",
                })
            elif yellow is not None and value >= yellow:
                result.yellow_warnings.append({
                    "key": key,
                    "metric": name,
                    "value": value,
                    "threshold": yellow,
                    "unit": unit,
                    "level": "yellow",
                })
        return result

    def advance_phase_manually(
        self, release_id: str, operator: str
    ) -> Tuple[bool, str]:
        session = get_session()
        try:
            release = session.query(ReleaseRequest).filter_by(id=release_id).first()
            if not release:
                return False, "发布申请不存在"

            current_phase = release.current_phase or 1
            phase_record = (
                session.query(GrayReleaseRecord)
                .filter_by(release_id=release.id, phase_order=current_phase)
                .first()
            )
            if not phase_record:
                return False, "未找到当前阶段记录"

            phase_record.manual_confirmed_by = operator
            phase_record.advanced_to_next = True
            phase_record.completed_at = get_utc_now()
            release.current_phase = current_phase + 1

            if release.current_phase > len(self.phases_cfg):
                release.status = ReleaseStatus.FULL_RELEASED

            session.commit()

            audit_logger.log(
                actor=operator,
                action="人工确认推进灰度阶段",
                category="gray_release",
                release_id=release.id,
                details={"from_phase": current_phase, "to_phase": release.current_phase},
            )

            if release.status == ReleaseStatus.FULL_RELEASED:
                notifier.send(
                    title="🎉 MES版本全量发布成功（人工推进）",
                    content=(
                        f"版本 **{release.version}**\n"
                        f"操作人: {operator}\n"
                        f"所有灰度阶段完成，版本全量上线。"
                    ),
                    severity=NotificationSeverity.SUCCESS,
                    extra={"发布申请ID": release.id},
                )
                return True, "已全量发布"

            session.close()
            self._deploy_and_monitor(release_id)
            return True, "已推进至下一阶段"
        finally:
            pass

    def _trigger_rollback(
        self,
        release: ReleaseRequest,
        trigger_source: str,
        reason: str,
        affected_lines: List[str],
        affected_batches: Optional[List[str]] = None,
        anomaly_details: Optional[Dict[str, Any]] = None,
        triggered_by: Optional[str] = None,
        force_rollback_type: Optional[str] = None,
    ) -> RollbackRecord:
        session = get_session()
        try:
            release = session.query(ReleaseRequest).filter_by(id=release.id).first()
            if not release:
                raise ValueError(f"未找到发布记录: {release.id if release else 'N/A'}")

            release.status = ReleaseStatus.ROLLING_BACK
            session.commit()

            auto_enabled = self.rollback_cfg.get("auto_rollback_enabled", True)
            rb_type = force_rollback_type or ("automatic" if auto_enabled else "manual")

            rollback = RollbackRecord(
                release_id=release.id,
                rollback_type=rb_type,
                trigger_source=trigger_source,
                from_version=release.version,
                to_version=release.previous_version or "unknown-stable",
                affected_production_lines=list(affected_lines),
                affected_batch_ranges=list(affected_batches or []),
                reason=reason,
                anomaly_details=anomaly_details or {},
                triggered_by=triggered_by,
            )
            session.add(rollback)
            session.flush()

            try:
                audit_logger.log(
                    actor="system",
                    action="触发自动回滚",
                    category="rollback",
                    release_id=release.id,
                    details={
                        "rollback_id": rollback.id,
                        "trigger_source": trigger_source,
                        "from": rollback.from_version,
                        "to": rollback.to_version,
                        "reason": reason,
                    },
                )
            except Exception as audit_err:
                print(f"  [警告] 审计日志写入失败（不影响回滚）: {audit_err}")

            deploy_result = self._execute_rollback(rollback, release)
            rollback.completed_at = get_utc_now()
            if rollback.started_at and rollback.completed_at:
                rb_start = ensure_utc(rollback.started_at)
                rb_end = ensure_utc(rollback.completed_at)
                rollback.duration_seconds = (rb_end - rb_start).total_seconds()

            rollback.success = deploy_result.get("success", False)
            rollback.health_check_passed = deploy_result.get("health_check", False)
            rollback.rollback_report = deploy_result

            if deploy_result.get("success"):
                release.status = ReleaseStatus.ROLLED_BACK
            else:
                release.status = ReleaseStatus.ROLLING_BACK

            session.commit()
            session.refresh(rollback)
            session.refresh(release)

            if self.rollback_cfg.get("notify_on_rollback", True):
                try:
                    self._send_rollback_notification(release, rollback)
                    rollback.notifications_sent = True
                    session.commit()
                except Exception as notify_err:
                    print(f"  [警告] 回滚通知发送失败: {notify_err}")

            return rollback
        except Exception as e:
            print(f"  [严重错误] 回滚流程异常: {e}")
            import traceback
            traceback.print_exc()
            session.rollback()
            raise
        finally:
            session.close()

    def _execute_rollback(self, rollback: RollbackRecord, release: ReleaseRequest) -> Dict[str, Any]:
        time.sleep(random.uniform(0.5, 2.0))
        success = random.random() > 0.02
        health_ok = random.random() > 0.03 if success else False
        return {
            "success": success,
            "health_check": health_ok,
            "restored_services": len(rollback.affected_production_lines),
            "steps": [
                {"step": "停止新版本流量", "status": "ok"},
                {"step": f"回滚版本至 {rollback.to_version}", "status": "ok"},
                {"step": "验证数据库快照一致性", "status": "ok"},
                {"step": "恢复旧版本配置", "status": "ok"},
                {"step": "运行健康检查", "status": "ok" if health_ok else "degraded"},
            ],
            "restored_at": get_utc_now().isoformat(),
            "error": None if success else "回滚过程中出现数据库连接超时",
        }

    def _send_rollback_notification(self, release: ReleaseRequest, rollback: RollbackRecord):
        content = (
            f"# 🔴 MES版本自动回滚告警\n\n"
            f"**版本**: {release.version} → {rollback.to_version}\n"
            f"**标题**: {release.title}\n"
            f"**触发原因**: {rollback.reason}\n"
            f"**触发源**: {rollback.trigger_source}\n\n"
            f"## 影响范围\n"
            f"- 产线: {', '.join(rollback.affected_production_lines) or '无'}\n"
            f"- 批次范围: {', '.join(rollback.affected_batch_ranges) or '无'}\n\n"
            f"## 回滚执行\n"
            f"- 开始: {rollback.started_at.isoformat() if rollback.started_at else '-'}\n"
            f"- 结束: {rollback.completed_at.isoformat() if rollback.completed_at else '-'}\n"
            f"- 耗时: {round(rollback.duration_seconds or 0, 1)}秒\n"
            f"- 结果: {'✅ 成功' if rollback.success else '❌ 失败'}\n"
            f"- 健康检查: {'通过' if rollback.health_check_passed else '未通过'}\n"
        )
        severity = NotificationSeverity.SUCCESS if rollback.success else NotificationSeverity.CRITICAL
        if not rollback.success:
            severity = NotificationSeverity.CRITICAL
        elif rollback.success:
            severity = NotificationSeverity.WARNING
        notifier.send(
            title=f"🚨 MES回滚告警 [{release.version}]",
            content=content,
            severity=severity,
            extra={
                "发布申请ID": release.id,
                "回滚记录ID": rollback.id,
            },
        )

    def manual_rollback(
        self,
        release_id: str,
        operator: str,
        reason: str,
        production_lines: Optional[List[str]] = None,
    ) -> Tuple[bool, str]:
        session = get_session()
        try:
            release = session.query(ReleaseRequest).filter_by(id=release_id).first()
            if not release:
                return False, "发布申请不存在"

            if release.status not in (
                ReleaseStatus.GRAY_IN_PROGRESS,
                ReleaseStatus.GRAY_COMPLETED,
                ReleaseStatus.FULL_RELEASED,
            ):
                return False, f"当前状态不允许手动回滚: {release.status.value}"

            if not production_lines:
                lines = []
                for phase in self.phases_cfg:
                    lines.extend(phase.get("production_lines", []))
                production_lines = lines

            rb = self._trigger_rollback(
                release=release,
                trigger_source=f"manual:{operator}",
                reason=reason,
                affected_lines=production_lines,
                triggered_by=operator,
                force_rollback_type="manual",
            )
            return True, rb.id
        finally:
            session.close()


gray_release_engine = GrayReleaseEngine()
