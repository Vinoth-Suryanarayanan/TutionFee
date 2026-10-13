"""Database tables, written as Python classes.

Step 2a: the roster -- who the families are, how to reach them, and who is enrolled in what.
Step 2b: the money -- what families owe (charges), what arrived (transactions),
         and how payments are matched to charges (allocations).
Step 2c: the agents -- what they ran (agent_runs), what they called (llm_calls),
         and what they sent to parents (reminders).

Money is always Numeric(10, 2) in the database and Decimal in Python -- never float.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tuition.db.base import Base, str_enum, utcnow


class Channel(StrEnum):
    """How a guardian prefers to be contacted."""

    EMAIL = "email"
    SMS = "sms"


class Family(Base):
    """A household. Payments and reminders are per family, not per student."""

    __tablename__ = "families"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_key: Mapped[str] = mapped_column(String(32), unique=True)
    display_name: Mapped[str] = mapped_column(String(120))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    guardians: Mapped[list[Guardian]] = relationship(
        back_populates="family", cascade="all, delete-orphan"
    )
    students: Mapped[list[Student]] = relationship(
        back_populates="family", cascade="all, delete-orphan"
    )
    charges: Mapped[list[Charge]] = relationship(
        back_populates="family", cascade="all, delete-orphan"
    )
    transactions: Mapped[list[Transaction]] = relationship(
        back_populates="family", passive_deletes=True
    )
    reminders: Mapped[list[Reminder]] = relationship(
        back_populates="family", cascade="all, delete-orphan"
    )


class Guardian(Base):
    """A parent or guardian: the people we match payments to and send reminders to."""

    __tablename__ = "guardians"

    id: Mapped[int] = mapped_column(primary_key=True)
    family_id: Mapped[int] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"))
    full_name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(254))
    phone: Mapped[str | None] = mapped_column(String(32))
    relation_to_student: Mapped[str | None] = mapped_column(String(32))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    preferred_channel: Mapped[Channel] = mapped_column(str_enum(Channel), default=Channel.EMAIL)

    family: Mapped[Family] = relationship(back_populates="guardians")
    reminders: Mapped[list[Reminder]] = relationship(
        back_populates="guardian", passive_deletes=True
    )


class Student(Base):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_key: Mapped[str] = mapped_column(String(32), unique=True)
    family_id: Mapped[int] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"))
    first_name: Mapped[str] = mapped_column(String(60))
    last_name: Mapped[str] = mapped_column(String(60))

    family: Mapped[Family] = relationship(back_populates="students")
    enrollments: Mapped[list[Enrollment]] = relationship(
        back_populates="student", cascade="all, delete-orphan"
    )


class Enrollment(Base):
    """A student in a program at a monthly fee, from start_date until end_date.

    end_date empty  -> still enrolled (gets charged and reminded)
    end_date filled -> withdrawn after that date (no new charges or reminders)
    """

    __tablename__ = "enrollments"
    __table_args__ = (
        CheckConstraint("monthly_fee >= 0", name="fee_non_negative"),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="dates_ordered"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"))
    program: Mapped[str] = mapped_column(String(80))
    monthly_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)

    student: Mapped[Student] = relationship(back_populates="enrollments")
    charges: Mapped[list[Charge]] = relationship(
        back_populates="enrollment", cascade="all, delete-orphan"
    )

    def is_active_on(self, day: date) -> bool:
        """Was this enrollment in effect on the given day?"""
        return self.start_date <= day and (self.end_date is None or day <= self.end_date)


# -- Step 2b: Money tables ----------------------------------------------------


class ChargeStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    PARTIAL = "partial"
    WAIVED = "waived"


class Charge(Base):
    """What a family owes for one enrollment for one billing period (month)."""

    __tablename__ = "charges"
    __table_args__ = (
        UniqueConstraint("enrollment_id", "billing_period", name="uq_charge_enrollment_period"),
        CheckConstraint("amount_due >= 0", name="charge_amount_non_negative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id", ondelete="CASCADE"))
    family_id: Mapped[int] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"))
    billing_period: Mapped[date] = mapped_column(Date)
    amount_due: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    status: Mapped[ChargeStatus] = mapped_column(
        str_enum(ChargeStatus), default=ChargeStatus.PENDING
    )
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    enrollment: Mapped[Enrollment] = relationship(back_populates="charges")
    family: Mapped[Family] = relationship(back_populates="charges")
    allocations: Mapped[list[Allocation]] = relationship(
        back_populates="charge", cascade="all, delete-orphan"
    )

    @property
    def amount_paid(self) -> Decimal:
        return sum((a.amount for a in self.allocations), Decimal("0.00"))

    @property
    def amount_remaining(self) -> Decimal:
        return self.amount_due - self.amount_paid


class TransactionSource(StrEnum):
    PAYPAL = "paypal"
    BANK = "bank"
    MANUAL = "manual"


class Transaction(Base):
    """A payment that actually arrived in our PayPal or bank account."""

    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_transaction_source_external"),
        CheckConstraint("amount > 0", name="txn_amount_positive"),
        Index("ix_transactions_family_id", "family_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    family_id: Mapped[int | None] = mapped_column(ForeignKey("families.id", ondelete="SET NULL"))
    source: Mapped[TransactionSource] = mapped_column(str_enum(TransactionSource))
    external_id: Mapped[str] = mapped_column(String(120))
    paid_on: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    payer_name: Mapped[str | None] = mapped_column(String(200))
    memo: Mapped[str | None] = mapped_column(String(500))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    family: Mapped[Family | None] = relationship(back_populates="transactions")
    allocations: Mapped[list[Allocation]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan"
    )

    @property
    def amount_allocated(self) -> Decimal:
        return sum((a.amount for a in self.allocations), Decimal("0.00"))

    @property
    def amount_unallocated(self) -> Decimal:
        return self.amount - self.amount_allocated


class Allocation(Base):
    """Links a Transaction to a Charge -- how a payment covers what is owed."""

    __tablename__ = "allocations"
    __table_args__ = (
        CheckConstraint("amount > 0", name="alloc_amount_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE")
    )
    charge_id: Mapped[int] = mapped_column(ForeignKey("charges.id", ondelete="CASCADE"))
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    confirmed_by_human: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    transaction: Mapped[Transaction] = relationship(back_populates="allocations")
    charge: Mapped[Charge] = relationship(back_populates="allocations")


# -- Step 2c: Agent observability tables --------------------------------------


class AgentStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentRun(Base):
    """One execution of a named agent (ingestion, reconciliation, reminder, etc.)."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_agent_runs_agent_name", "agent_name"),
        Index("ix_agent_runs_started_at", "started_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_name: Mapped[str] = mapped_column(String(80))
    label: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[AgentStatus] = mapped_column(
        str_enum(AgentStatus), default=AgentStatus.RUNNING
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)

    llm_calls: Mapped[list[LlmCall]] = relationship(
        back_populates="agent_run", cascade="all, delete-orphan"
    )
    reminders: Mapped[list[Reminder]] = relationship(
        back_populates="agent_run", cascade="all, delete-orphan"
    )

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def total_cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.llm_calls if c.cost_usd is not None)


class LlmCall(Base):
    """One call to the Claude API made by an agent -- raw data for the cost dashboard."""

    __tablename__ = "llm_calls"
    __table_args__ = (
        CheckConstraint("input_tokens >= 0", name="llm_input_tokens_non_negative"),
        CheckConstraint("output_tokens >= 0", name="llm_output_tokens_non_negative"),
        CheckConstraint("cost_usd >= 0", name="llm_cost_non_negative"),
        Index("ix_llm_calls_agent_run_id", "agent_run_id"),
        Index("ix_llm_calls_called_at", "called_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"))
    model: Mapped[str] = mapped_column(String(80))
    purpose: Mapped[str | None] = mapped_column(String(120))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    called_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    agent_run: Mapped[AgentRun] = relationship(back_populates="llm_calls")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class ReminderStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"


class Reminder(Base):
    """A payment reminder sent (or queued to send) to a guardian."""

    __tablename__ = "reminders"
    __table_args__ = (
        Index("ix_reminders_family_id", "family_id"),
        Index("ix_reminders_sent_at", "sent_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"))
    family_id: Mapped[int] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"))
    guardian_id: Mapped[int | None] = mapped_column(
        ForeignKey("guardians.id", ondelete="SET NULL")
    )
    channel: Mapped[Channel] = mapped_column(str_enum(Channel))
    body: Mapped[str] = mapped_column(Text)
    charge_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[ReminderStatus] = mapped_column(
        str_enum(ReminderStatus), default=ReminderStatus.PENDING
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    agent_run: Mapped[AgentRun] = relationship(back_populates="reminders")
    family: Mapped[Family] = relationship(back_populates="reminders")
    guardian: Mapped[Guardian | None] = relationship(back_populates="reminders")

    def get_charge_ids(self) -> list[int]:
        import json
        return json.loads(self.charge_ids_json)

    def set_charge_ids(self, ids: list[int]) -> None:
        import json
        self.charge_ids_json = json.dumps(ids)
