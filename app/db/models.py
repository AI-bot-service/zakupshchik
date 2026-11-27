"""Схема БД (ARCHITECTURE.md §8, §7.3). Одна модель сделки для всех площадок."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, ClassVar, NewType

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

PlatformId = NewType("PlatformId", int)
DealId = NewType("DealId", int)
CatalogItemId = NewType("CatalogItemId", int)
ManagerId = NewType("ManagerId", int)


class AdapterKind(StrEnum):
    API_JSON = "A"
    HTML = "B"
    HTML_GENERIC = "B2"
    EXTENSION = "C"
    MOCK = "D"


class PlatformStatus(StrEnum):
    ON = "on"
    NEEDS_SESSION = "needs_session"
    NO_PUBLIC = "no_public"
    OFF = "off"


class Law(StrEnum):
    FZ44 = "44"
    FZ223 = "223"
    COMMERCIAL = "commercial"
    UNKNOWN = "unknown"


class Procedure(StrEnum):
    """Способ закупки (§6.1): отсекаем конкурентные, берём быстрые."""

    REQUEST_KP = "request_kp"
    SINGLE_SUPPLIER = "single_supplier"
    SMALL_PURCHASE = "small_purchase"
    QUOTATION_SESSION = "quotation_session"
    TENDER = "tender"
    AUCTION = "auction"
    REQUEST_QUOTES = "request_quotes"
    REQUEST_PROPOSALS = "request_proposals"
    UNKNOWN = "unknown"


class PaymentTerms(StrEnum):
    PREPAYMENT = "prepayment"
    POSTPAYMENT = "postpayment"
    ON_DELIVERY = "on_delivery"
    UNKNOWN = "unknown"


class ContractFormat(StrEnum):
    NONE = "none"
    SUPPLIER_TEMPLATE = "supplier_template"
    CUSTOMER_TEMPLATE = "customer_template"
    UNKNOWN = "unknown"


class TriState(StrEnum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class DealStatus(StrEnum):
    NEW = "new"
    FILTERED_OUT = "filtered_out"
    ANALYZED = "analyzed"
    HOT = "hot"
    TAKEN = "taken"
    SUBMITTED = "submitted"
    SKIPPED = "skipped"
    EXPIRED = "expired"


class MatchKind(StrEnum):
    EXACT = "exact"
    ANALOG = "analog"
    NONE = "none"


class FindingKind(StrEnum):
    CONFLICT = "conflict"
    RISK = "risk"
    REQUIREMENT = "requirement"


class ProposalStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    SENT = "sent"


class Actor(StrEnum):
    MANAGER = "manager"
    ROBOT = "robot"


class Channel(StrEnum):
    STAND = "stand"
    EXTENSION = "extension"
    WORKER = "worker"
    EMAIL = "email"  # кнопка «Подготовить КП» в письме


class NotifyChannel(StrEnum):
    EMAIL_DEAL = "email_deal"
    EMAIL_DAILY = "email_daily"
    EXTENSION = "extension"


class NotifyStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


def _enum[E: StrEnum](cls: type[E]) -> Enum:
    # Храним значение строкой с CHECK: новые значения добавляются миграцией без ALTER TYPE.
    return Enum(
        cls,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda e: [m.value for m in e],
    )


NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSONB}


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Platform(Timestamps, Base):
    __tablename__ = "platform"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    adapter_kind: Mapped[AdapterKind] = mapped_column(_enum(AdapterKind))
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[PlatformStatus] = mapped_column(_enum(PlatformStatus), default=PlatformStatus.ON)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class Deal(Timestamps, Base):
    __tablename__ = "deal"
    __table_args__ = (
        UniqueConstraint("platform_id", "external_id"),
        Index("ix_deal_status_end_at", "status", "end_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    platform_id: Mapped[int] = mapped_column(ForeignKey("platform.id", ondelete="RESTRICT"))
    external_id: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    law: Mapped[Law] = mapped_column(_enum(Law), default=Law.UNKNOWN)
    procedure: Mapped[Procedure] = mapped_column(_enum(Procedure), default=Procedure.UNKNOWN)
    procedure_raw: Mapped[str | None] = mapped_column(Text)
    customer_inn: Mapped[str | None] = mapped_column(String(12))
    customer_name: Mapped[str | None] = mapped_column(Text)
    region: Mapped[str | None] = mapped_column(String(255))
    nmck: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kp_min_valid_days: Mapped[int | None] = mapped_column(Integer)
    payment_terms: Mapped[PaymentTerms] = mapped_column(_enum(PaymentTerms), default=PaymentTerms.UNKNOWN)
    payment_days: Mapped[int | None] = mapped_column(Integer)
    contract_format: Mapped[ContractFormat] = mapped_column(
        _enum(ContractFormat), default=ContractFormat.UNKNOWN
    )
    has_tz: Mapped[TriState] = mapped_column(_enum(TriState), default=TriState.UNKNOWN)
    smb_only: Mapped[TriState] = mapped_column(_enum(TriState), default=TriState.UNKNOWN)
    smb_evidence: Mapped[str | None] = mapped_column(Text)
    # Только после маскирования ПДн (белый список полей адаптера + маскирование свободного текста).
    raw_masked: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[DealStatus] = mapped_column(_enum(DealStatus), default=DealStatus.NEW)
    conditions_met: Mapped[int | None] = mapped_column(Integer)
    score: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    score_breakdown: Mapped[dict[str, Any]] = mapped_column(default=dict)
    rules_version: Mapped[str | None] = mapped_column(String(64))
    filter_reason: Mapped[str | None] = mapped_column(Text)

    items: Mapped[list[DealItem]] = relationship(back_populates="deal", cascade="all, delete-orphan")


class DealItem(Timestamps, Base):
    __tablename__ = "deal_item"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("deal.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(Text)
    okpd: Mapped[str | None] = mapped_column(String(32))
    qty: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    unit: Mapped[str | None] = mapped_column(String(64))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))

    deal: Mapped[Deal] = relationship(back_populates="items")


class DealFile(Timestamps, Base):
    __tablename__ = "deal_file"
    __table_args__ = (UniqueConstraint("deal_id", "sha256"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("deal.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    mime: Mapped[str] = mapped_column(String(128))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    source_url: Mapped[str | None] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text)


class CatalogItem(Timestamps, Base):
    __tablename__ = "catalog_item"
    __table_args__ = (
        Index(
            "ix_catalog_item_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(Integer, unique=True)
    sku: Mapped[str] = mapped_column(String(128), index=True)
    vendor: Mapped[str] = mapped_column(String(64), index=True)
    manufacturer: Mapped[str | None] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(Text)
    edition: Mapped[str | None] = mapped_column(Text)
    license_type: Mapped[str | None] = mapped_column(String(128))
    term: Mapped[str | None] = mapped_column(String(64))
    client_type: Mapped[str | None] = mapped_column(String(64))
    min_qty: Mapped[int | None] = mapped_column(Integer)
    max_qty: Mapped[int | None] = mapped_column(Integer)
    price: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    price_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rf_registry_no: Mapped[str | None] = mapped_column(String(64))
    delivery_days: Mapped[str | None] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(Text)
    synonyms: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)


class Match(Timestamps, Base):
    __tablename__ = "match"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    deal_item_id: Mapped[int] = mapped_column(ForeignKey("deal_item.id", ondelete="CASCADE"), index=True)
    catalog_item_id: Mapped[int | None] = mapped_column(ForeignKey("catalog_item.id", ondelete="SET NULL"))
    kind: Mapped[MatchKind] = mapped_column(_enum(MatchKind))
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    reason: Mapped[str] = mapped_column(Text)


class Finding(Timestamps, Base):
    __tablename__ = "finding"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("deal.id", ondelete="CASCADE"), index=True)
    kind: Mapped[FindingKind] = mapped_column(_enum(FindingKind))
    text: Mapped[str] = mapped_column(Text)
    source_field: Mapped[str | None] = mapped_column(String(128))
    quote: Mapped[str | None] = mapped_column(Text)


class Manager(Timestamps, Base):
    __tablename__ = "manager"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255))


class ExtKey(Base):
    """Ключ плагина: по одному на устройство, хранится только хэш. Новый ключ старые не отзывает."""

    __tablename__ = "ext_key"

    id: Mapped[int] = mapped_column(primary_key=True)
    manager_id: Mapped[int] = mapped_column(ForeignKey("manager.id", ondelete="CASCADE"), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    manager: Mapped[Manager] = relationship()


class Proposal(Timestamps, Base):
    __tablename__ = "proposal"
    __table_args__ = (UniqueConstraint("deal_id", "version"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("deal.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    items: Mapped[dict[str, Any]] = mapped_column(default=dict)
    total: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    status: Mapped[ProposalStatus] = mapped_column(_enum(ProposalStatus), default=ProposalStatus.DRAFT)
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("manager.id", ondelete="SET NULL"))
    file_path: Mapped[str | None] = mapped_column(Text)


class DealStatusLog(Base):
    """Журнал смены статуса: кто (менеджер или робот), откуда, с чем на руках (N из 6, КП, правила)."""

    __tablename__ = "deal_status_log"
    __table_args__ = (Index("ix_deal_status_log_at", "at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("deal.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    from_status: Mapped[DealStatus | None] = mapped_column(_enum(DealStatus))
    to_status: Mapped[DealStatus] = mapped_column(_enum(DealStatus))
    actor: Mapped[Actor] = mapped_column(_enum(Actor))
    actor_name: Mapped[str] = mapped_column(String(255))  # логин менеджера или шаг робота
    channel: Mapped[Channel] = mapped_column(_enum(Channel))
    device: Mapped[str | None] = mapped_column(String(80))  # подпись ключа плагина
    reason: Mapped[str | None] = mapped_column(Text)
    conditions_met: Mapped[int | None] = mapped_column(Integer)
    rules_version: Mapped[str | None] = mapped_column(String(64))
    proposal_id: Mapped[int | None] = mapped_column(ForeignKey("proposal.id", ondelete="SET NULL"))
    proposal_version: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    deal: Mapped[Deal] = relationship()


class AgentStep(Base):
    """Журнал шага конвейера: вход (хэш), выход, модель, токены, стоимость, время, ошибка."""

    __tablename__ = "agent_step"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("deal.id", ondelete="CASCADE"), index=True)
    step: Mapped[str] = mapped_column(String(64))
    input_hash: Mapped[str | None] = mapped_column(String(64))
    output: Mapped[dict[str, Any]] = mapped_column(default=dict)
    model: Mapped[str | None] = mapped_column(String(128))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_rub: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal(0))
    ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Notification(Timestamps, Base):
    __tablename__ = "notification"
    __table_args__ = (
        UniqueConstraint("channel", "recipient", "deal_id", name="uq_notification_deal"),
        UniqueConstraint("channel", "recipient", "report_date", name="uq_notification_daily"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel: Mapped[NotifyChannel] = mapped_column(_enum(NotifyChannel))
    recipient: Mapped[str] = mapped_column(String(255))
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("deal.id", ondelete="CASCADE"))
    report_date: Mapped[date | None] = mapped_column(Date)
    attachments_sha256: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)
    status: Mapped[NotifyStatus] = mapped_column(_enum(NotifyStatus), default=NotifyStatus.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    # Какое письмо ушло: Message-ID и тема (миграция 0004)
    message_id: Mapped[str | None] = mapped_column(String(255))
    subject: Mapped[str | None] = mapped_column(Text)


class RulesVersion(Base):
    """Версия правил отбора и оценки. Только добавление: действует последняя, откат = копия старой."""

    __tablename__ = "rules_version"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(String(12), index=True)
    profile: Mapped[dict[str, Any]] = mapped_column()
    author: Mapped[str] = mapped_column(String(255))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
