import argparse
import json
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import audit_logger
from .config import config
from .models import (
    ApprovalRecord,
    ApprovalStatus,
    PreCheckResult,
    ReleaseRequest,
    ReleaseStatus,
    ReleaseType,
    RollbackDrill,
    init_db,
    get_session,
    get_utc_now,
)
from .precheck import pre_check_engine
from .approval import approval_engine
from .gray_release import gray_release_engine
from .reports import reports_engine
from .scheduler import release_scheduler
from .notifier import NotificationSeverity, notifier


class ReleasePipeline:
    def __init__(self):
        init_db()

    def create_release(
        self,
        version: str,
        title: str,
        release_type: str = "regular",
        submitter: str = "system",
        submitter_department: str = "IT部",
        description: Optional[str] = None,
        change_summary: Optional[str] = None,
        previous_version: Optional[str] = None,
        emergency_reason: Optional[str] = None,
        artifacts: Optional[Dict[str, Any]] = None,
    ) -> ReleaseRequest:
        session = get_session()
        try:
            rtype = ReleaseType.HOTFIX if release_type.lower() == "hotfix" else ReleaseType.REGULAR
            release = ReleaseRequest(
                version=version,
                release_type=rtype,
                title=title,
                description=description,
                change_summary=change_summary,
                submitter=submitter,
                submitter_department=submitter_department,
                previous_version=previous_version,
                emergency_reason=emergency_reason,
                artifacts=artifacts or self._default_artifacts(rtype),
            )
            if rtype == ReleaseType.HOTFIX:
                if not emergency_reason:
                    release.emergency_reason = "紧急生产问题热修复 - 详细偏差报告见附件"
                if not release.deviation_report_ref:
                    release.deviation_report_ref = f"DEV-{get_utc_now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"

            session.add(release)
            session.commit()
            session.refresh(release)

            audit_logger.log(
                actor=submitter,
                action="创建发布申请",
                category="release",
                release_id=release.id,
                details={
                    "version": version,
                    "type": rtype.value,
                    "title": title,
                },
            )
            return release
        finally:
            session.close()

    def _default_artifacts(self, rtype: ReleaseType) -> Dict[str, Any]:
        if rtype == ReleaseType.HOTFIX:
            return {
                "gmp_documents": {
                    "验证报告(V报告)": f"V-RPT-HOTFIX-{get_utc_now().strftime('%Y%m%d')}",
                    "变更控制记录": f"CR-HOT-{uuid.uuid4().hex[:8].upper()}",
                    "风险评估报告": "已备案（紧急通道，事后72小时补全签字）",
                    "回归测试报告": "冒烟测试通过（紧急）",
                },
                "change_controls": [
                    {"id": f"CC-HOT-{uuid.uuid4().hex[:8].upper()}", "status": "closed", "note": "紧急热修复-事后补全文档"}
                ],
                "ebr": {
                    "templates": [
                        {
                            "name": "口服固体制剂批记录",
                            "version": "v2.3.1",
                            "fields": ["batch_no", "product_code", "production_line", "start_time", "end_time", "operator", "qa_sign"],
                            "content": {"schema_version": 2, "sections": ["配料", "制粒", "压片", "包衣", "包装"]},
                            "hash": "",
                        }
                    ]
                },
                "device_connectivity": {},
            }
        return {
            "gmp_documents": {
                "验证报告(V报告)": f"V-RPT-{get_utc_now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}",
                "变更控制记录": f"CR-{uuid.uuid4().hex[:8].upper()}",
                "风险评估报告": f"RA-{uuid.uuid4().hex[:8].upper()}",
                "回归测试报告": f"RT-{uuid.uuid4().hex[:8].upper()}",
            },
            "change_controls": [
                {"id": f"CC-{uuid.uuid4().hex[:8].upper()}", "status": "closed"},
                {"id": f"CC-{uuid.uuid4().hex[:8].upper()}", "status": "closed"},
            ],
            "ebr": {
                "templates": [
                    {
                        "name": "口服固体制剂批记录",
                        "version": "v2.3.1",
                        "fields": ["batch_no", "product_code", "production_line", "start_time", "end_time", "operator", "qa_sign"],
                        "content": {"schema_version": 2, "sections": ["配料", "制粒", "压片", "包衣", "包装"]},
                        "hash": "",
                    },
                    {
                        "name": "无菌注射液批记录",
                        "version": "v2.3.1",
                        "fields": ["batch_no", "product_code", "production_line", "start_time", "end_time", "operator", "qa_sign", "sterility_result"],
                        "content": {"schema_version": 2, "sections": ["洗瓶", "配液", "灌封", "灭菌", "灯检", "包装"]},
                        "hash": "",
                    },
                ]
            },
            "device_connectivity": {},
        }

    def run_full_pipeline(
        self,
        release: ReleaseRequest,
        auto_approve: bool = False,
    ) -> Dict[str, Any]:
        release_id = release.id
        audit_logger.log(
            actor=release.submitter,
            action="提交发布并启动全流程管道",
            category="release",
            release_id=release_id,
        )

        print(f"[1/5] 运行发布前置校验... (版本: {release.version})")
        passed, check_results = pre_check_engine.run_all(release_id)
        for r in check_results:
            icon = "✅" if r.passed else "❌"
            print(f"  {icon} {r.check_item.value} -> 得分: {r.score}  问题数: {len(r.issues_found or [])}")
            if r.suggestions:
                for s in r.suggestions:
                    print(f"     💡 建议: {s}")

        if not passed:
            print("\n❌ 前置校验未通过，发布被阻断。请修复后重试。")
            return {"step": "pre_check", "status": "blocked", "release_id": release_id}

        print("\n[2/5] 初始化审批流程...")
        approval_records = approval_engine.initialize_approvals(release_id)
        print(f"  审批链路: {len(approval_records)} 级审批")

        if auto_approve:
            print("  🚀 自动审批模式: 自动通过所有审批节点...")
            session = get_session()
            try:
                all_records = (
                    session.query(ApprovalRecord)
                    .filter_by(release_id=release_id)
                    .order_by(ApprovalRecord.approval_order.asc())
                    .all()
                )
                for rec in all_records:
                    check_points = {k: True for k in (rec.check_points_results or {}).keys()}
                    ok, msg = approval_engine.submit_approval(
                        release_id=release_id,
                        approval_record_id=rec.id,
                        approver_name=f"AUTO-{rec.approver_role}",
                        approved=True,
                        check_points=check_points,
                        comments="自动化演示 - 系统自动审批通过",
                    )
                    print(f"    [{rec.approval_order}] {rec.approver_department}/{rec.approver_role}: 通过")
            finally:
                session.close()
        else:
            print("  (提示: 生产环境中需各审批人登录系统完成审批)")

        approval_status = approval_engine.get_approval_status(release_id)
        if approval_status.get("status") != ReleaseStatus.APPROVAL_PASSED.value:
            print(f"\n⏸️ 审批未完成，当前状态: {approval_status.get('status')}")
            print(f"   审批进度: {approval_status.get('summary', {}).get('progress_pct', 0)}%")
            return {"step": "approval", "status": "waiting_approval", "release_id": release_id, "approval": approval_status}

        print("\n[3/5] 启动灰度发布（含监控与熔断）...")
        ok, msg = gray_release_engine.start_gray_release(release_id)
        if not ok:
            print(f"❌ 灰度启动失败: {msg}")
            return {"step": "gray_release", "status": "failed", "release_id": release_id, "error": msg}

        session = get_session()
        try:
            release_db = session.query(ReleaseRequest).filter_by(id=release_id).first()
            status = release_db.status.value if release_db else "unknown"
            current_phase = release_db.current_phase if release_db else None
            print(f"\n[4/5] 灰度发布与监控完成，最终状态: {status}")
            print(f"      到达阶段: 第{current_phase or '-'}阶段")

            if release_db and release_db.status == ReleaseStatus.ROLLED_BACK:
                rollbacks = sorted(release_db.rollback_records, key=lambda r: r.started_at)
                if rollbacks:
                    rb = rollbacks[-1]
                    print(f"\n      ⚠️  触发自动回滚:")
                    print(f"         原因: {rb.reason}")
                    print(f"         版本: {rb.from_version} → {rb.to_version}")
                    print(f"         产线: {', '.join(rb.affected_production_lines) or '-'}")
                    print(f"         批次: {', '.join(rb.affected_batch_ranges) or '-'}")
                    print(f"         耗时: {round(rb.duration_seconds or 0, 1)}s")
                    print(f"         结果: {'成功' if rb.success else '失败'}")
        finally:
            session.close()

        print("\n[5/5] 生成周报与归档...")
        report = reports_engine.generate_weekly_report(force=False)
        if report:
            print(f"   📊 周报ID: {report.id}")
            for k, v in (report.file_paths or {}).items():
                print(f"      [{k.upper()}] {v}")

        audit_logger.log(
            actor="system",
            action="全流程管道执行完成",
            category="release",
            release_id=release_id,
        )

        session = get_session()
        try:
            release_db = session.query(ReleaseRequest).filter_by(id=release_id).first()
            return {
                "step": "complete",
                "status": release_db.status.value if release_db else "unknown",
                "release_id": release_id,
                "version": release_db.version if release_db else version,
            }
        finally:
            session.close()

    def get_release_status(self, release_id: str) -> Dict[str, Any]:
        session = get_session()
        try:
            release = session.query(ReleaseRequest).filter_by(id=release_id).first()
            if not release:
                return {}
            return {
                "id": release.id,
                "version": release.version,
                "type": release.release_type.value,
                "title": release.title,
                "status": release.status.value,
                "current_phase": release.current_phase,
                "submitter": release.submitter,
                "submitted_at": release.submitted_at.isoformat() if release.submitted_at else None,
                "pre_checks": [
                    {
                        "item": p.check_item.value,
                        "passed": p.passed,
                        "score": p.score,
                        "issues": len(p.issues_found or []),
                    }
                    for p in release.pre_checks
                ],
                "approval": approval_engine.get_approval_status(release.id),
                "gray_phases": [
                    {
                        "order": g.phase_order,
                        "name": g.phase_name,
                        "status": g.status,
                        "circuit_breaker": g.circuit_breaker_triggered,
                        "production_lines": g.production_lines,
                    }
                    for g in release.gray_records
                ],
                "rollbacks": [
                    {
                        "id": r.id,
                        "from": r.from_version,
                        "to": r.to_version,
                        "trigger": r.trigger_source,
                        "success": r.success,
                        "duration_s": r.duration_seconds,
                    }
                    for r in release.rollback_records
                ],
            }
        finally:
            session.close()

    def list_releases(self, limit: int = 20) -> List[Dict[str, Any]]:
        session = get_session()
        try:
            releases = (
                session.query(ReleaseRequest)
                .order_by(ReleaseRequest.created_at.desc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "id": r.id,
                    "version": r.version,
                    "type": r.release_type.value,
                    "title": r.title,
                    "status": r.status.value,
                    "submitter": r.submitter,
                    "submitted_at": r.submitted_at.isoformat() if r.submitted_at else None,
                }
                for r in releases
            ]
        finally:
            session.close()

    def manual_rollback(
        self,
        release_id: str,
        operator: str,
        reason: str,
        production_lines: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        ok, result = gray_release_engine.manual_rollback(release_id, operator, reason, production_lines)
        return {"success": ok, "result": result}

    def audit_verify(self) -> Dict[str, Any]:
        return audit_logger.verify_chain()

    def run_rollback_drill(
        self,
        title: Optional[str] = None,
        scenario: Optional[str] = None,
    ) -> Dict[str, Any]:
        scheduled = get_utc_now() + timedelta(minutes=1)
        drill = reports_engine.schedule_rollback_drill(
            scheduled_at=scheduled,
            title=title,
            scenario=scenario,
        )
        print(f"🎬 创建回滚演练计划: {drill.id} - {drill.title}")
        print("   立即执行演练...")
        ok, status = reports_engine.execute_rollback_drill(drill.id)
        session = get_session()
        try:
            drill = session.query(RollbackDrill).filter_by(id=drill.id).first()
            if drill:
                return {
                    "drill_id": drill.id,
                    "status": drill.status,
                    "success": drill.success,
                    "duration_seconds": drill.duration_seconds,
                    "issues": drill.issues_found,
                    "improvements": drill.improvement_actions,
                    "results": drill.results,
                }
            return {"drill_id": drill.id, "executed": ok, "status": status}
        finally:
            session.close()

    def query_history(
        self,
        days: int = 30,
        version: Optional[str] = None,
        production_line: Optional[str] = None,
        export_format: Optional[str] = None,
    ) -> Dict[str, Any]:
        end = get_utc_now()
        start = end - timedelta(days=days)
        return reports_engine.query_history(
            start_time=start,
            end_time=end,
            version=version,
            production_line=production_line,
            export_format=export_format,
        )

    def scheduler_start(self) -> Dict[str, Any]:
        return release_scheduler.start()

    def scheduler_stop(self) -> Dict[str, Any]:
        return release_scheduler.stop()

    def scheduler_status(self) -> Dict[str, Any]:
        return release_scheduler.status()

    def scheduler_trigger(self, job_id: str) -> Dict[str, Any]:
        return release_scheduler.trigger_now(job_id)


release_pipeline = ReleasePipeline()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mes-release",
        description="药品生产MES系统版本发布与智能回滚自动化管理平台",
    )
    sub = parser.add_subparsers(dest="command", help="可用命令")

    create_p = sub.add_parser("create", help="创建发布申请")
    create_p.add_argument("--version", required=True, help="版本号,如v3.9.0")
    create_p.add_argument("--title", required=True, help="发布标题")
    create_p.add_argument("--type", choices=["regular", "hotfix"], default="regular")
    create_p.add_argument("--submitter", default="张开发")
    create_p.add_argument("--dept", default="开发部")
    create_p.add_argument("--description", default="")
    create_p.add_argument("--change-summary", default="")
    create_p.add_argument("--previous-version", default="")
    create_p.add_argument("--emergency-reason", default="")
    create_p.add_argument("--auto-approve", action="store_true", help="演示用:自动审批")
    create_p.add_argument("--auto-run", action="store_true", help="创建后自动运行全流程")

    run_p = sub.add_parser("run", help="运行指定发布申请的全流程")
    run_p.add_argument("--release-id", required=True)
    run_p.add_argument("--auto-approve", action="store_true")

    list_p = sub.add_parser("list", help="列出发布申请")
    list_p.add_argument("--limit", type=int, default=20)

    status_p = sub.add_parser("status", help="查看发布详情")
    status_p.add_argument("--release-id", required=True)

    rb_p = sub.add_parser("rollback", help="手动触发回滚")
    rb_p.add_argument("--release-id", required=True)
    rb_p.add_argument("--operator", required=True)
    rb_p.add_argument("--reason", required=True)
    rb_p.add_argument("--line", action="append", help="指定产线，可重复")

    drill_p = sub.add_parser("drill", help="执行回滚演练")
    drill_p.add_argument("--title", default="")
    drill_p.add_argument("--scenario", default="")

    report_p = sub.add_parser("report", help="生成周报")
    report_p.add_argument("--force", action="store_true")

    query_p = sub.add_parser("query", help="查询历史记录并可导出")
    query_p.add_argument("--days", type=int, default=90)
    query_p.add_argument("--version", default="")
    query_p.add_argument("--line", default="")
    query_p.add_argument("--export", choices=["csv", "xlsx"], default=None)

    audit_p = sub.add_parser("audit-verify", help="审计日志完整性校验")

    sched_p = sub.add_parser("scheduler", help="调度器管理")
    sched_sub = sched_p.add_subparsers(dest="sched_command", required=True)
    sched_start = sched_sub.add_parser("start", help="启动调度器（每周一9点周报+每月28日14点演练）")
    sched_stop = sched_sub.add_parser("stop", help="停止调度器")
    sched_status = sched_sub.add_parser("status", help="查看调度器状态")
    sched_trigger = sched_sub.add_parser("trigger", help="立即触发指定任务")
    sched_trigger.add_argument("--job", required=True, choices=["weekly_report_job", "rollback_drill_job"])

    return parser


def main(argv: Optional[List[str]] = None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.command == "create":
        release = release_pipeline.create_release(
            version=args.version,
            title=args.title,
            release_type=args.type,
            submitter=args.submitter,
            submitter_department=args.dept,
            description=args.description or None,
            change_summary=args.change_summary or None,
            previous_version=args.previous_version or None,
            emergency_reason=args.emergency_reason or None,
        )
        print(f"✅ 创建发布申请成功:")
        print(f"   ID:       {release.id}")
        print(f"   版本:     {release.version}")
        print(f"   类型:     {release.release_type.value}")
        print(f"   标题:     {release.title}")
        print(f"   提交人:   {release.submitter} ({release.submitter_department})")
        if args.auto_run:
            print()
            result = release_pipeline.run_full_pipeline(release, auto_approve=args.auto_approve)
            print(f"\n🏁 管道执行结果: {json.dumps(result, ensure_ascii=False, indent=2)}")
        else:
            print(f"\n提示: 使用 `mes-release run --release-id {release.id} --auto-approve` 运行全流程")

    elif args.command == "run":
        session = get_session()
        try:
            release = session.query(ReleaseRequest).filter_by(id=args.release_id).first()
            if not release:
                print("❌ 发布申请不存在")
                sys.exit(1)
            result = release_pipeline.run_full_pipeline(release, auto_approve=args.auto_approve)
            print(f"\n🏁 管道执行结果:\n{json.dumps(result, ensure_ascii=False, indent=2)}")
        finally:
            session.close()

    elif args.command == "list":
        items = release_pipeline.list_releases(args.limit)
        print(f"共 {len(items)} 条发布记录:\n")
        print(f"{'ID':<8}  {'版本':<10} {'类型':<7} {'状态':<20} {'提交人':<10} 标题")
        print("-" * 100)
        for it in items:
            print(
                f"{it['id'][:8]:<8}  {it['version']:<10} {it['type']:<7} {it['status']:<20} {it['submitter']:<10} {it['title'][:40]}"
            )

    elif args.command == "status":
        st = release_pipeline.get_release_status(args.release_id)
        if not st:
            print("❌ 不存在")
            sys.exit(1)
        print(f"版本 {st['version']} ({st['type']}) - {st['title']}")
        print(f"整体状态: {st['status']}  |  当前灰度阶段: 第{st.get('current_phase') or '-'}阶段")
        print()
        print("--- 前置校验 ---")
        for p in st.get("pre_checks", []):
            icon = "✅" if p["passed"] else "❌"
            print(f"  {icon} {p['item']:<20}  得分 {p['score'] or 0}  问题 {p['issues']}")
        print()
        approval = st.get("approval", {})
        summary = approval.get("summary", {})
        print(f"--- 审批流程 ({summary.get('progress_pct', 0)}%) ---")
        for a in approval.get("approvals", []):
            icon = {"pending": "⏳", "approved": "✅", "rejected": "❌", "post_signed": "📝"}.get(a["status"], "?")
            print(f"  {icon} [{a['order']}] {a['department']}/{a['role']:<10} -> {a['status']}" + (f" ({a['approver_name']})" if a["approver_name"] else ""))
        print()
        print("--- 灰度阶段 ---")
        for g in st.get("gray_phases", []):
            cb = "🔥" if g["circuit_breaker"] else ""
            print(f"  [{g['order']}] {g['name']:<16} 状态={g['status']:<16} 产线={','.join(g['production_lines']) or '-'} {cb}")
        print()
        print("--- 回滚记录 ---")
        for r in st.get("rollbacks", []):
            sc = "✅" if r["success"] else "❌"
            print(f"  {sc} {r['from']} → {r['to']}  触发={r['trigger']}  耗时={r['duration_s'] or 0}s")

    elif args.command == "rollback":
        result = release_pipeline.manual_rollback(args.release_id, args.operator, args.reason, args.line)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.command == "drill":
        result = release_pipeline.run_rollback_drill(args.title or None, args.scenario or None)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.command == "report":
        r = reports_engine.generate_weekly_report(force=args.force)
        if r:
            print(f"✅ 周报ID: {r.id}")
            for k, v in (r.file_paths or {}).items():
                print(f"   [{k.upper()}] {v}")
            print(f"   核心指标: {json.dumps(r.metrics, ensure_ascii=False, indent=6)}")

    elif args.command == "query":
        result = release_pipeline.query_history(
            days=args.days,
            version=args.version or None,
            production_line=args.line or None,
            export_format=args.export,
        )
        print(f"查询条件: {json.dumps(result['query'], ensure_ascii=False)}")
        print(f"结果: 发布 {result['counts']['releases']} 条, 回滚 {result['counts']['rollbacks']} 条")
        if result.get("exported_files"):
            print(f"导出文件:")
            for f in result["exported_files"]:
                print(f"   📄 {f}")

    elif args.command == "audit-verify":
        result = release_pipeline.audit_verify()
        print(f"日志总数: {result['total_entries']}")
        print(f"校验结果: {'✅ 通过' if result['verified'] else '❌ 存在问题'}")
        if result["issues"]:
            for issue in result["issues"]:
                print(f"   ⚠️  [{issue['index']}] {issue['type']}: {issue.get('log_id')}")

    elif args.command == "scheduler":
        if args.sched_command == "start":
            result = release_pipeline.scheduler_start()
            print(f"{'✅' if result['running'] else '❌'} {result['message']}")
            if result.get("already_running"):
                print("   (重复启动已拦截，未创建重复任务)")
            for jid, jinfo in (result.get("jobs") or {}).items():
                print(f"   🕒 {jinfo['name']}: {jinfo['cron']} | 下次: {jinfo.get('next_run', '-')}")
        elif args.sched_command == "stop":
            result = release_pipeline.scheduler_stop()
            print(f"{'✅' if not result['running'] else '❌'} {result['message']}")
        elif args.sched_command == "status":
            result = release_pipeline.scheduler_status()
            print(f"调度器状态: {'✅ 运行中' if result['running'] else '⏹  已停止'}")
            if result.get("jobs"):
                for jid, jinfo in result["jobs"].items():
                    print(f"   🕒 {jinfo['name']}: {jinfo['cron']} | 下次: {jinfo.get('next_run', '-')}")
        elif args.sched_command == "trigger":
            result = release_pipeline.scheduler_trigger(args.job)
            print(f"{'✅' if result['success'] else '❌'} {result['message']}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
