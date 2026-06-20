import hashlib
import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    JSON,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .config import config


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


class ReleaseStatus(str, Enum):
    DRAFT = "draft"
    PRE_CHECK_PENDING = "pre_check_pending"
    PRE_CHECK_FAILED = "pre_check_failed"
    PRE_CHECK_PASSED = "pre_check_passed"
    APPROVAL_PENDING = "approval_pending"
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_PASSED = "approval_passed"
    GRAY_IN_PROGRESS = "gray_in_progress"
    GRAY_PAUSED = "gray_paused"
    GRAY_COMPLETED = "gray_completed"
    FULL_RELEASED = "full_released"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ReleaseType(str, Enum):
    REGULAR = "regular"
    HOTFIX = "hotfix"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    POST_SIGNED = "post_signed"


class CircuitBreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class ReleaseRequest(Base):
    __tablename__ = "release_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    release_type: Mapped[ReleaseType] = mapped_column(SAEnum(ReleaseType), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    change_summary: Mapped[Optional[str]] = mapped_column(Text)
    submitter: Mapped[str] = mapped_column(String(100), nullable=False)
    submitter_department: Mapped[str] = mapped_column(String(100), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=get_utc_now)
    status: Mapped[ReleaseStatus] = mapped_column(SAEnum(ReleaseStatus), default=ReleaseStatus.DRAFT)
    current_phase: Mapped[Optional[int]] = mapped_column(Integer)
    target_production_lines: Mapped[Optional[List[str]]] = mapped_column(JSON, default=list)
    previous_version: Mapped[Optional[str]] = mapped_column(String(50))
    emergency_reason: Mapped[Optional[str]] = mapped_column(Text)
    deviation_report_ref: Mapped[Optional[str]] = mapped_column(String(100))
    artifacts: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=get_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=get_utc_now, onupdate=get_utc_now)

    pre_checks: Mapped[List["PreCheckResult"]] = relationship(back_populates="release", cascade="all, delete-orphan")
    approvals: Mapped[List["ApprovalRecord"]] = relationship(back_populates="release", cascade="all, delete-orphan")
    gray_records: Mapped[List["GrayReleaseRecord"]] = relationship(back_populates="release", cascade="all, delete-orphan")
    rollback_records: Mapped[List["RollbackRecord"]] = relationship(back_populates="release", cascade="all, delete-orphan")
    audit_logs: Mapped[List["AuditLog"]] = relationship(back_populates="release", cascade="all, delete-orphan")


class PreCheckItem(str, Enum):
    GMP_COMPLIANCE = "gmp_compliance"
    EBR_INTEGRITY = "ebr_integrity"
    DEVICE_CONNECTIVITY = "device_connectivity"
    PERFORMANCE_TEST = "performance_test"


class PreCheckResult(Base):
    __tablename__ = "pre_check_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    release_id: Mapped[str] = mapped_column(String(36), ForeignKey("release_requests.id"), nullable=False)
    check_item: Mapped[PreCheckItem] = mapped_column(SAEnum(PreCheckItem), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    score: Mapped[Optional[float]] = mapped_column(Float)
    details: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, default=dict)
    issues_found: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(JSON, default=list)
    suggestions: Mapped[Optional[List[str]]] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=get_utc_now)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    executor: Mapped[str] = mapped_column(String(100), default="system")

    release: Mapped["ReleaseRequest"] = relationship(back_populates="pre_checks")


class ApprovalRecord(Base):
    __tablename__ = "approval_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    release_id: Mapped[str] = mapped_column(String(36), ForeignKey("release_requests.id"), nullable=False)
    approver_role: Mapped[str] = mapped_column(String(100), nullable=False)
    approver_department: Mapped[str] = mapped_column(String(100), nullable=False)
    approver_name: Mapped[Optional[str]] = mapped_column(String(100))
    approval_order: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[ApprovalStatus] = mapped_column(SAEnum(ApprovalStatus), default=ApprovalStatus.PENDING)
    check_points_results: Mapped[Optional[Dict[str, bool]]] = mapped_column(JSON, default=dict)
    comments: Mapped[Optional[str]] = mapped_column(Text)
    is_post_signed: Mapped[bool] = mapped_column(Boolean, default=False)
    sla_deadline: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=get_utc_now)

    release: Mapped["ReleaseRequest"] = relationship(back_populates="approvals")


class GrayReleaseRecord(Base):
    __tablename__ = "gray_release_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    release_id: Mapped[str] = mapped_column(String(36), ForeignKey("release_requests.id"), nullable=False)
    phase_order: Mapped[int] = mapped_column(Integer, nullable=False)
    phase_name: Mapped[str] = mapped_column(String(100), nullable=False)
    production_lines: Mapped[List[str]] = mapped_column(JSON, default=list)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(50), default="pending")
    deploy_result: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, default=dict)
    monitor_snapshots: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(JSON, default=list)
    circuit_breaker_triggered: Mapped[bool] = mapped_column(Boolean, default=False)
    circuit_breaker_reason: Mapped[Optional[str]] = mapped_column(Text)
    advanced_to_next: Mapped[bool] = mapped_column(Boolean, default=False)
    manual_confirmed_by: Mapped[Optional[str]] = mapped_column(String(100))

    release: Mapped["ReleaseRequest"] = relationship(back_populates="gray_records")


class RollbackRecord(Base):
    __tablename__ = "rollback_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    release_id: Mapped[str] = mapped_column(String(36), ForeignKey("release_requests.id"), nullable=False)
    rollback_type: Mapped[str] = mapped_column(String(50), nullable=False)
    trigger_source: Mapped[str] = mapped_column(String(50), nullable=False)
    triggered_by: Mapped[str] = mapped_column(String(100), default="system")
    from_version: Mapped[str] = mapped_column(String(50), nullable=False)
    to_version: Mapped[str] = mapped_column(String(50), nullable=False)
    affected_production_lines: Mapped[List[str]] = mapped_column(JSON, default=list)
    affected_batch_ranges: Mapped[Optional[List[str]]] = mapped_column(JSON, default=list)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    anomaly_details: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=get_utc_now)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float)
    success: Mapped[Optional[bool]] = mapped_column(Boolean)
    health_check_passed: Mapped[Optional[bool]] = mapped_column(Boolean)
    rollback_report: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, default=dict)
    notifications_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    is_drill: Mapped[bool] = mapped_column(Boolean, default=False)
    drill_id: Mapped[Optional[str]] = mapped_column(String(36))

    release: Mapped["ReleaseRequest"] = relationship(back_populates="rollback_records")


class RollbackDrill(Base):
    __tablename__ = "rollback_drills"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="scheduled")
    participants: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, default=list)
    target_release_id: Mapped[Optional[str]] = mapped_column(String(36))
    scenario_description: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float)
    success: Mapped[Optional[bool]] = mapped_column(Boolean)
    results: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, default=dict)
    issues_found: Mapped[Optional[List[str]]] = mapped_column(JSON, default=list)
    improvement_actions: Mapped[Optional[List[str]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=get_utc_now)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    release_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("release_requests.id"))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=get_utc_now, index=True)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    details: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, default=dict)
    entity_type: Mapped[Optional[str]] = mapped_column(String(100))
    entity_id: Mapped[Optional[str]] = mapped_column(String(100))
    ip_address: Mapped[Optional[str]] = mapped_column(String(50))
    previous_hash: Mapped[Optional[str]] = mapped_column(String(64))
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    release: Mapped[Optional["ReleaseRequest"]] = relationship(back_populates="audit_logs")

    def compute_hash(self) -> str:
        ts = ensure_utc(self.timestamp)
        ts_epoch_ms = int(ts.timestamp() * 1000) if ts else 0
        data = {
            "id": self.id,
            "release_id": self.release_id,
            "timestamp_ms": ts_epoch_ms,
            "actor": self.actor,
            "action": self.action,
            "category": self.category,
            "details": self.details,
            "ip_address": self.ip_address,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "previous_hash": self.previous_hash,
        }
        json_str = json.dumps(data, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(json_str.encode("utf-8")).hexdigest()


class WeeklyReport(Base):
    __tablename__ = "weekly_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    week_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    week_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=get_utc_now)
    metrics: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    release_details: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, default=list)
    rollback_details: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, default=list)
    file_paths: Mapped[Dict[str, str]] = mapped_column(JSON, default=dict)
    generated_by: Mapped[str] = mapped_column(String(100), default="system")


_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        db_type = config.get("database.type", "sqlite")
        db_path = config.get("database.path", "./data/mes_release.db")
        echo = config.get("database.echo", False)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        if db_type == "sqlite":
            _engine = create_engine(f"sqlite:///{db_path}", echo=echo, future=True)
        else:
            _engine = create_engine(f"sqlite:///{db_path}", echo=echo, future=True)
    return _engine


def get_session():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autoflush=False, autocommit=False, future=True)
    return _SessionLocal()


def init_db():
    engine = get_engine()
    Base.metadata.create_all(engine)
