from datetime import datetime
from typing import Any, Dict, Optional

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
                    issues.append({
                        "index": i,
                        "log_id": log.id,
                        "type": "hash_mismatch",
                        "expected": expected_hash,
                        "actual": log.entry_hash,
                    })
                if log.previous_hash != prev_hash:
                    issues.append({
                        "index": i,
                        "log_id": log.id,
                        "type": "chain_break",
                        "expected_prev": prev_hash,
                        "actual_prev": log.previous_hash,
                    })
                prev_hash = log.entry_hash
            return {
                "total_entries": len(logs),
                "verified": len(issues) == 0,
                "issues": issues,
            }
        finally:
            session.close()


audit_logger = AuditLogger()
