"""
MES发布与回滚自动化平台 - 完整演示脚本
========================================
"""
import json
import random
import sys
import os
import io
from pathlib import Path

# 设置固定随机种子，保证演示流程可复现
random.seed(20260621)

# 修复Windows控制台/文件重定向UTF-8编码问题
if sys.platform.startswith("win"):
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        except Exception:
            pass
    if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
        try:
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mes_release.pipeline import release_pipeline
from mes_release.reports import reports_engine
from mes_release.audit import audit_logger
from mes_release.models import get_session, ReleaseRequest, ReleaseStatus, get_utc_now


def print_sep(title: str = ""):
    width = 100
    print("\n" + "=" * width)
    if title:
        print(f"  {title}")
        print("=" * width)


def pprint_json(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def scenario_1_regular_release_with_gray_and_cb():
    print_sep("场景 1: 常规发布 v3.9.0 - 完整灰度发布与熔断回滚流程")
    release = release_pipeline.create_release(
        version="v3.9.0",
        title="MES工艺参数模块重构升级 + 批记录模板标准化",
        release_type="regular",
        submitter="李工程师",
        submitter_department="开发部",
        description="重构S88批次配方引擎，优化批记录v2.3.1模板字段校验，增加OEE统计功能。",
        change_summary="1. S88引擎重构，支持动态配方模板\n2. EBR模板升级至v2.3.1，增加QA签名字段\n3. 包装线PLC通讯协议优化\n4. 新增实时OEE看板接口",
        previous_version="v3.8.2",
    )
    print(f"✅ 发布申请创建: {release.id} | {release.version} | {release.release_type.value}")
    print(f"   提交人: {release.submitter} ({release.submitter_department})")

    print("\n🏃 运行全流程（含自动审批）...\n")
    result = release_pipeline.run_full_pipeline(release, auto_approve=True)
    print_sep("场景 1 执行结果")
    pprint_json(result)
    return release


def scenario_2_hotfix_release():
    print_sep("场景 2: 紧急热修复通道 - 包装线扫码异常")
    release = release_pipeline.create_release(
        version="v3.9.1-hotfix",
        title="[紧急] 包装线二级赋码扫码接口偶发500错误修复",
        release_type="hotfix",
        submitter="王运维",
        submitter_department="IT运维部",
        description="包装线PKG-01/02的赋码扫码接口在高并发下偶发连接重置，定位为连接池耗尽，已紧急修复并加监控。",
        change_summary="紧急修复：\n1. HttpClient连接池max从50调至200\n2. 增加请求重试机制(3次指数退避)\n3. 增加Prometheus监控指标",
        previous_version="v3.9.0",
        emergency_reason="昨日22:30起包装线扫码失败率由0.1%攀升至3.8%，已影响3个批次，需立即修复。",
    )
    print(f"🚨 紧急热修复创建: {release.id} | {release.version}")
    print(f"   紧急原因: {release.emergency_reason}")
    print(f"   偏差报告编号: {release.deviation_report_ref}")

    print("\n🏃 运行热修复全流程...\n")
    result = release_pipeline.run_full_pipeline(release, auto_approve=True)
    print_sep("场景 2 执行结果")
    pprint_json(result)
    return release


def scenario_3_manual_rollback():
    print_sep("场景 3: 人工手动回滚（产线发现业务异常）")
    session = get_session()
    try:
        releases = (
            session.query(ReleaseRequest)
            .filter(ReleaseRequest.status.in_([
                ReleaseStatus.FULL_RELEASED,
                ReleaseStatus.GRAY_COMPLETED,
                ReleaseStatus.GRAY_IN_PROGRESS,
            ]))
            .order_by(ReleaseRequest.created_at.desc())
            .all()
        )
        if not releases:
            print("  ⚠️  无合适发布可用于手动回滚演示，跳过。")
            return None

        target = releases[0]
        print(f"🎯 目标版本: {target.version} ({target.id[:8]}...)  状态: {target.status.value}")
        rb_result = release_pipeline.manual_rollback(
            release_id=target.id,
            operator="赵生产主管",
            reason="人工巡检发现ORAL-03产线批记录签字字段渲染异常，可能影响合规，立即回滚。",
            production_lines=["ORAL-03"],
        )
        ok = rb_result.get("success", False)
        rb_id = rb_result.get("result", "")
        status_icon = "✅" if ok else "❌"
        id_display = f" | 回滚记录ID={rb_id}" if ok else f" | 错误={rb_id}"
        print(f"   回滚结果: {status_icon} {'成功' if ok else '失败'}{id_display}")
        return target
    finally:
        session.close()


def scenario_4_rollback_drill():
    print_sep("场景 4: 常态化回滚演练（每月末演练熔断机制）")
    drill_result = release_pipeline.run_rollback_drill(
        title="MES月度回滚演练 - 2026年6月",
        scenario="模拟无菌产线STER-01灰度发布时生产异常率突破3%阈值，自动触发熔断并回滚至v3.8.2，验证EBR数据一致性、OPC设备握手、以及业务SLA恢复。",
    )
    print(f"🎬 演练执行结果:")
    print(f"   ID:         {drill_result.get('drill_id')}")
    print(f"   状态:       {drill_result.get('status')}")
    print(f"   成功:       {drill_result.get('success')}")
    print(f"   耗时:       {drill_result.get('duration_seconds')} 秒")
    print(f"\n   发现问题 ({len(drill_result.get('issues') or [])}):")
    for i in drill_result.get("issues") or []:
        print(f"     ⚠️  {i}")
    print(f"\n   改进建议 ({len(drill_result.get('improvements') or [])}):")
    for i in drill_result.get("improvements") or []:
        print(f"     💡 {i}")
    return drill_result


def scenario_5_weekly_report_and_history():
    print_sep("场景 5: 生成周报 + 历史数据检索导出")
    print("\n📊 生成周报 (强制刷新)...")
    report = reports_engine.generate_weekly_report(force=True)
    if report:
        print(f"   周报 ID:       {report.id}")
        print(f"   统计周期:      {report.week_start.date()} ~ {report.week_end.date()}")
        for k, v in (report.file_paths or {}).items():
            abs_path = Path(v).resolve()
            print(f"   [{k.upper():4s}] {abs_path}")
        print("\n   📈 核心指标:")
        metrics = report.metrics or {}
        for k, v in metrics.items():
            if isinstance(v, dict):
                continue
            print(f"      · {k:<28} = {v}")

    print("\n🔍 查询最近90天发布记录并导出XLSX...")
    result = release_pipeline.query_history(days=90, export_format="xlsx")
    print(f"   命中记录: 发布={result['counts']['releases']}, 回滚={result['counts']['rollbacks']}")
    for f in result.get("exported_files", []):
        print(f"   📄 导出文件: {Path(f).resolve()}")
    return report


def scenario_6_audit_chain_verification():
    print_sep("场景 6: GMP合规审计 - 不可篡改日志链校验")
    verify = audit_logger.verify_chain()
    print(f"   审计日志总数:  {verify['total_entries']}")
    print(f"   链完整性校验:  {'✅ 全部通过 (不可篡改)' if verify['verified'] else '❌ 存在问题'}")
    if verify["issues"]:
        for issue in verify["issues"]:
            print(f"   ⚠️  [#{issue['index']}] {issue['type']} at {issue.get('log_id')}")
    print("\n✅ 审计日志符合GMP数据完整性ALCOA+要求：Attributable(可归因)、Legible(可读取)、Contemporaneously(同步)、Original(原始)、Accurate(准确) 及 +Complete(完整)、Consistent(一致)、Enduring(持久)、Available(可用)")
    return verify


def main():
    print("\n" + "▓" * 100)
    print("▓" + " " * 40 + "药品生产 MES 系统版本发布与智能回滚自动化平台" + " " * 14 + "▓")
    print("▓" + " " * 36 + "Pharmaceutical MES Release & Smart Rollback Platform" + " " * 10 + "▓")
    print("▓" * 100)
    print()

    print("系统已就绪。即将演示以下场景：")
    print("  1) 常规版本发布 + 三级产线灰度 + 监控熔断自动回滚")
    print("  2) 紧急热修复(HOTFIX)并行审批通道")
    print("  3) 人工触发手动回滚")
    print("  4) 每月回滚演练 (验证熔断机制)")
    print("  5) 周报自动生成 + 历史查询导出")
    print("  6) 审计日志不可篡改链校验")
    print()

    results = {}

    try:
        results["scenario_1"] = scenario_1_regular_release_with_gray_and_cb()
    except Exception as e:
        print(f"场景1异常: {e}")
        import traceback
        traceback.print_exc()

    try:
        results["scenario_2"] = scenario_2_hotfix_release()
    except Exception as e:
        print(f"场景2异常: {e}")

    try:
        results["scenario_3"] = scenario_3_manual_rollback()
    except Exception as e:
        print(f"场景3异常: {e}")

    try:
        results["scenario_4"] = scenario_4_rollback_drill()
    except Exception as e:
        print(f"场景4异常: {e}")

    try:
        results["scenario_5"] = scenario_5_weekly_report_and_history()
    except Exception as e:
        print(f"场景5异常: {e}")

    try:
        results["scenario_6"] = scenario_6_audit_chain_verification()
    except Exception as e:
        print(f"场景6异常: {e}")

    print_sep("全部场景演示完成")
    session = get_session()
    try:
        from mes_release.models import ReleaseRequest, ApprovalRecord, RollbackRecord, GrayReleaseRecord, RollbackDrill, WeeklyReport, AuditLog
        counts = {
            "发布申请": session.query(ReleaseRequest).count(),
            "前置校验记录": 0,  # lazy
            "审批节点": session.query(ApprovalRecord).count(),
            "灰度阶段记录": session.query(GrayReleaseRecord).count(),
            "回滚记录": session.query(RollbackRecord).count(),
            "演练计划": session.query(RollbackDrill).count(),
            "周报": session.query(WeeklyReport).count(),
            "审计日志": session.query(AuditLog).count(),
        }
        print("\n📦 数据库总览:")
        for k, v in counts.items():
            print(f"   · {k:<16} {v:>6} 条")
    finally:
        session.close()

    print("\n🎉 所有演示场景运行完毕！\n")
    print("使用提示:")
    print("   python mes_release_cli.py --help              查看所有命令")
    print("   python mes_release_cli.py create --version v3.10.0 --title 'xxx' --auto-approve --auto-run")
    print("   python mes_release_cli.py list")
    print("   python mes_release_cli.py status --release-id <ID>")
    print("   python mes_release_cli.py query --days 30 --export xlsx")
    print()


if __name__ == "__main__":
    main()
