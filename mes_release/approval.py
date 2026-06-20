from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .audit import audit_logger
from .config import config
from .models import (
    ApprovalRecord,
    ApprovalStatus,
    ReleaseRequest,
    ReleaseStatus,
    ReleaseType,
    get_session,
    get_utc_now,
)
from .notifier import NotificationSeverity, notifier


class ApprovalEngine:
    def __init__(self):
        self.approval_cfg = config.get("approval.channels", {})

    def initialize_approvals(self, release_id: str) -> List[ApprovalRecord]:
        session = get_session()
        try:
            release = session.query(ReleaseRequest).filter_by(id=release_id).first()
            if not release:
                raise ValueError(f"发布申请不存在: {release_id}")

            release.status = ReleaseStatus.APPROVAL_PENDING
            session.commit()

            channel_key = "hotfix" if release.release_type == ReleaseType.HOTFIX else "regular"
            channel_cfg = self.approval_cfg.get(channel_key, {})

            existing = (
                session.query(ApprovalRecord)
                .filter_by(release_id=release.id)
                .order_by(ApprovalRecord.approval_order.asc())
                .all()
            )
            if existing:
                return existing

            records: List[ApprovalRecord] = []

            if release.release_type == ReleaseType.REGULAR:
                chain = channel_cfg.get("approvers_chain", [])
                for idx, approver_cfg in enumerate(chain):
                    sla_hours = approver_cfg.get("sla_hours", 8)
                    rec = ApprovalRecord(
                        release_id=release.id,
                        approver_role=approver_cfg["role"],
                        approver_department=approver_cfg["department"],
                        approval_order=idx + 1,
                        status=ApprovalStatus.PENDING,
                        sla_deadline=get_utc_now() + timedelta(hours=sla_hours),
                        check_points_results={cp: None for cp in approver_cfg.get("check_points", [])},
                    )
                    session.add(rec)
                    records.append(rec)
            else:
                approvers = channel_cfg.get("approvers", [])
                for idx, approver_cfg in enumerate(approvers):
                    rec = ApprovalRecord(
                        release_id=release.id,
                        approver_role=approver_cfg["role"],
                        approver_department=approver_cfg["department"],
                        approval_order=idx + 1,
                        status=ApprovalStatus.PENDING,
                        check_points_results={cp: None for cp in approver_cfg.get("check_points", [])},
                    )
                    session.add(rec)
                    records.append(rec)

            session.commit()
            for r in records:
                session.refresh(r)

            audit_logger.log(
                actor="system",
                action="初始化审批流程",
                category="approval",
                release_id=release.id,
                details={
                    "channel": channel_key,
                    "approvers_count": len(records),
                    "parallel": channel_cfg.get("parallel_approval", False),
                },
            )

            first_pending = records[0] if records else None
            if first_pending and release.release_type == ReleaseType.REGULAR:
                self._notify_approver(release, first_pending, 1)

            return records
        finally:
            session.close()

    def submit_approval(
        self,
        release_id: str,
        approval_record_id: str,
        approver_name: str,
        approved: bool,
        check_points: Optional[Dict[str, bool]] = None,
        comments: Optional[str] = None,
        is_post_sign: bool = False,
    ) -> Tuple[bool, Optional[str]]:
        session = get_session()
        try:
            release = session.query(ReleaseRequest).filter_by(id=release_id).first()
            if not release:
                return False, "发布申请不存在"
            if release.status not in (
                ReleaseStatus.APPROVAL_PENDING,
                ReleaseStatus.GRAY_IN_PROGRESS,
                ReleaseStatus.FULL_RELEASED,
            ):
                return False, f"当前状态不允许审批: {release.status.value}"

            record = (
                session.query(ApprovalRecord)
                .filter_by(id=approval_record_id, release_id=release.id)
                .first()
            )
            if not record:
                return False, "审批记录不存在"
            if record.status != ApprovalStatus.PENDING and not is_post_sign:
                return False, f"该审批已处理: {record.status.value}"

            if release.release_type == ReleaseType.REGULAR and not is_post_sign:
                prev_ok, prev_err = self._check_previous_approval_passed(session, release.id, record.approval_order)
                if not prev_ok:
                    return False, prev_err

            if check_points:
                existing_cp = record.check_points_results or {}
                for k, v in check_points.items():
                    if k in existing_cp:
                        existing_cp[k] = v
                record.check_points_results = existing_cp
                all_cp_passed = all(v is True for v in existing_cp.values() if v is not None)
            else:
                all_cp_passed = True

            record.status = ApprovalStatus.APPROVED if (approved and all_cp_passed) else ApprovalStatus.REJECTED
            record.approver_name = approver_name
            record.comments = comments
            record.submitted_at = get_utc_now()
            record.is_post_signed = is_post_sign

            session.commit()

            audit_logger.log(
                actor=approver_name,
                action="审批提交",
                category="approval",
                release_id=release.id,
                details={
                    "approval_id": record.id,
                    "role": record.approver_role,
                    "department": record.approver_department,
                    "status": record.status.value,
                    "is_post_sign": is_post_sign,
                    "comments": comments,
                },
            )

            if not approved or not all_cp_passed:
                release.status = ReleaseStatus.APPROVAL_REJECTED
                session.commit()
                notifier.send(
                    title="❌ MES发布审批被驳回",
                    content=(
                        f"版本 **{release.version}** - {release.title}\n"
                        f"审批人: {approver_name} ({record.approver_role}/{record.approver_department})\n"
                        f"审批意见: {comments or '无'}\n"
                        f"检查项结果: {record.check_points_results}"
                    ),
                    severity=NotificationSeverity.WARNING,
                    extra={"发布申请ID": release.id},
                )
                return True, None

            all_done, all_ok = self._check_all_approvals_done(session, release.id)
            if release.release_type == ReleaseType.REGULAR and not all_done:
                next_rec = self._get_next_pending_approval(session, release.id)
                if next_rec:
                    self._notify_approver(release, next_rec, next_rec.approval_order)

            if all_done and all_ok:
                release.status = ReleaseStatus.APPROVAL_PASSED
                session.commit()
                notifier.send(
                    title="✅ MES发布审批全部通过",
                    content=(
                        f"版本 **{release.version}** - {release.title}\n"
                        f"发布类型: {release.release_type.value}\n"
                        f"即将进入灰度发布阶段，请相关人员关注监控指标。"
                    ),
                    severity=NotificationSeverity.SUCCESS,
                    extra={"发布申请ID": release.id},
                )
                audit_logger.log(
                    actor="system",
                    action="审批全部通过",
                    category="approval",
                    release_id=release.id,
                    details={},
                )

            return True, None
        finally:
            session.close()

    def _check_all_approvals_done(self, session, release_id: str) -> Tuple[bool, bool]:
        records = (
            session.query(ApprovalRecord)
            .filter_by(release_id=release_id)
            .all()
        )
        if not records:
            return True, True
        statuses = [r.status for r in records]
        all_done = all(s in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED, ApprovalStatus.POST_SIGNED) for s in statuses)
        all_ok = all(s in (ApprovalStatus.APPROVED, ApprovalStatus.POST_SIGNED) for s in statuses)
        return all_done, all_ok

    def _get_next_pending_approval(self, session, release_id: str) -> Optional[ApprovalRecord]:
        return (
            session.query(ApprovalRecord)
            .filter_by(release_id=release_id, status=ApprovalStatus.PENDING)
            .order_by(ApprovalRecord.approval_order.asc())
            .first()
        )

    def _check_previous_approval_passed(self, session, release_id: str, current_order: int) -> Tuple[bool, Optional[str]]:
        if current_order <= 1:
            return True, None
        prev = (
            session.query(ApprovalRecord)
            .filter_by(release_id=release_id, approval_order=current_order - 1)
            .first()
        )
        if not prev:
            return False, f"未找到前序审批节点 (order={current_order - 1})"
        if prev.status == ApprovalStatus.PENDING:
            return False, (
                f"❌ 审批顺序错误：前序节点「{prev.approver_department}/{prev.approver_role}」(第{prev.approval_order}级) 尚未处理，"
                f"请先完成前序审批后再提交当前「{prev.approver_department.replace('部', '')}」节点"
            )
        if prev.status == ApprovalStatus.REJECTED:
            return False, (
                f"❌ 审批已被前序节点驳回：「{prev.approver_department}/{prev.approver_role}」(第{prev.approval_order}级) 已拒绝，"
                f"当前节点无法继续审批"
            )
        if prev.status not in (ApprovalStatus.APPROVED, ApprovalStatus.POST_SIGNED):
            return False, f"❌ 前序审批节点状态异常: {prev.status.value}"
        return True, None

    def _notify_approver(
        self,
        release: ReleaseRequest,
        record: ApprovalRecord,
        order: int,
    ):
        notifier.send(
            title="📋 MES发布审批待办",
            content=(
                f"版本 **{release.version}** - {release.title}\n"
                f"发布类型: {release.release_type.value}\n"
                f"审批环节: 第{order}级 - {record.approver_department}/{record.approver_role}\n"
                f"SLA截止: {record.sla_deadline.isoformat() if record.sla_deadline else '无'}\n"
                f"提交人: {release.submitter} ({release.submitter_department})\n\n"
                "请及时完成审批，检查要点：\n"
                + "\n".join(f"- {k}" for k in (record.check_points_results or {}).keys())
            ),
            severity=NotificationSeverity.INFO,
            extra={
                "发布申请ID": release.id,
                "审批记录ID": record.id,
                "变更摘要": release.change_summary or "见详细描述",
            },
        )

    def get_approval_status(self, release_id: str) -> Dict[str, Any]:
        session = get_session()
        try:
            release = session.query(ReleaseRequest).filter_by(id=release_id).first()
            if not release:
                return {}
            records = (
                session.query(ApprovalRecord)
                .filter_by(release_id=release.id)
                .order_by(ApprovalRecord.approval_order.asc())
                .all()
            )
            total = len(records)
            approved = sum(1 for r in records if r.status in (ApprovalStatus.APPROVED, ApprovalStatus.POST_SIGNED))
            pending = sum(1 for r in records if r.status == ApprovalStatus.PENDING)
            rejected = sum(1 for r in records if r.status == ApprovalStatus.REJECTED)
            channel_cfg = self.approval_cfg.get(
                "hotfix" if release.release_type == ReleaseType.HOTFIX else "regular", {}
            )

            return {
                "release_id": release.id,
                "version": release.version,
                "status": release.status.value,
                "channel": channel_cfg.get("name", ""),
                "parallel": channel_cfg.get("parallel_approval", False),
                "summary": {
                    "total": total,
                    "approved": approved,
                    "pending": pending,
                    "rejected": rejected,
                    "progress_pct": round((approved / total * 100), 1) if total else 100,
                },
                "approvals": [
                    {
                        "id": r.id,
                        "order": r.approval_order,
                        "role": r.approver_role,
                        "department": r.approver_department,
                        "approver_name": r.approver_name,
                        "status": r.status.value,
                        "check_points": r.check_points_results,
                        "comments": r.comments,
                        "submitted_at": r.submitted_at.isoformat() if r.submitted_at else None,
                        "sla_deadline": r.sla_deadline.isoformat() if r.sla_deadline else None,
                        "is_post_signed": r.is_post_signed,
                    }
                    for r in records
                ],
            }
        finally:
            session.close()


approval_engine = ApprovalEngine()
