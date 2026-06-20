from datetime import datetime
from typing import Any, Dict, List, Optional

from .models import (
    AuditLog,
    ensure_utc,
    get_session,
    get_utc_now,
)


class AuditLogger:
    def __init__(self):
        self._last_hash: Optional[str] = None
        self._initialized: bool = False

    def _ensure_initialized(self):
        if self._initialized:
            return
        try:
            session = get_session()
            try:
                last_log = (
                    session.query(AuditLog)
                    .order_by(AuditLog.timestamp.desc())
                    .first()
                )
                if last_log:
                    self._last_hash = last_log.entry_hash
            finally:
                session.close()
        except Exception:
            self._last_hash = None
        self._initialized = True

    def log(
        self,
        actor: str,
        action: str,
        category: str,
        details: Optional[Dict[str, Any]] = None,
        release_id: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> AuditLog:
        self._ensure_initialized()
        session = get_session()
        try:
            log_entry = AuditLog(
                release_id=release_id,
                timestamp=get_utc_now(),
                actor=actor,
                action=action,
                category=category,
                details=details or {},
                entity_type=entity_type,
                entity_id=entity_id,
                ip_address=ip_address,
                previous_hash=self._last_hash,
                entry_hash="",
            )
            session.add(log_entry)
            session.flush()
            log_entry.entry_hash = log_entry.compute_hash()
            self._last_hash = log_entry.entry_hash
            session.commit()
            return log_entry
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def verify_chain(self) -> Dict[str, Any]:
        self._ensure_initialized()
        session = get_session()
        try:
            logs = (
                session.query(AuditLog)
                .order_by(AuditLog.timestamp.asc(), AuditLog.id.asc())
                .all()
            )
            issues = []
            prev_hash = None
            for i, log in enumerate(logs):
                expected_hash = log.compute_hash()
                if expected_hash != log.entry_hash:
                    tampered_fields = self._detect_tampered_fields(log)
                    issue = {
                        "index": i,
                        "log_id": log.id,
                        "type": "hash_mismatch",
                        "expected": expected_hash,
                        "actual": log.entry_hash,
                        "tampered_fields": tampered_fields,
                        "detail": (
                            f"审计记录第{i+1}条（ID: {log.id[:8]}...）哈希校验失败。"
                            + (f" 疑似篡改字段: {', '.join(tampered_fields)}" if tampered_fields else " 未知字段被篡改")
                        ),
                    }
                    issues.append(issue)
                if log.previous_hash != prev_hash:
                    issues.append({
                        "index": i,
                        "log_id": log.id,
                        "type": "chain_break",
                        "expected_prev": prev_hash,
                        "actual_prev": log.previous_hash,
                        "detail": (
                            f"审计记录第{i+1}条（ID: {log.id[:8]}...）哈希链断裂。"
                            f"前序哈希应为: {prev_hash[:16]}... 实际存储: {log.previous_hash[:16] if log.previous_hash else 'None'}... (可能记录被删除或顺序打乱)"
                        ),
                    })
                prev_hash = log.entry_hash
            return {
                "total_entries": len(logs),
                "verified": len(issues) == 0,
                "issues": issues,
            }
        finally:
            session.close()

    def _detect_tampered_fields(self, log: AuditLog) -> List[str]:
        hash_input_fields = [
            ("actor", "操作人(actor)"),
            ("action", "操作动作(action)"),
            ("category", "分类(category)"),
            ("release_id", "关联发布ID(release_id)"),
            ("details", "详情内容(details)"),
            ("ip_address", "来源IP地址(ip_address)"),
            ("entity_type", "操作对象类型(entity_type)"),
            ("entity_id", "操作对象ID(entity_id)"),
        ]
        suspicious = []
        for field_name, field_cn in hash_input_fields:
            val = getattr(log, field_name, None)
            if field_name == "details" and isinstance(val, dict) and not val:
                continue
            if val is None and field_name in ("release_id", "ip_address", "entity_type", "entity_id"):
                continue
            suspicious.append(field_cn)
        return suspicious


audit_logger = AuditLogger()
