"""
backend/models.py
Extremely lightweight SQLAlchemy ORM schema designed to minimize storage:
1. User: Verified account (id, email, active_session_token, created_at)
2. EmailOTP: Ephemeral single-use OTP hashes (email, otp_hash, salt, expires_at, attempts)
3. ProviderListing: Host pin & 10-digit mobile number (never exposed on public map endpoints)
4. ChargingHistory: Compact record of customer/provider charging requests and negotiated prices
"""

from datetime import datetime, timezone
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from .database import Base


def utcnow():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(254), unique=True, nullable=False, index=True)
    session_token_hash = Column(String(64), nullable=True, index=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    provider_listing = relationship(
        "ProviderListing",
        back_populates="owner",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    customer_requests = relationship(
        "ChargingHistory",
        back_populates="customer",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class EmailOTP(Base):
    __tablename__ = "email_otps"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(254), nullable=False, index=True)
    otp_hash = Column(String(64), nullable=False)
    otp_salt = Column(String(32), nullable=False)
    attempts = Column(Integer, default=0, nullable=False)
    expires_at = Column(Integer, nullable=False)  # Unix epoch seconds


class ProviderListing(Base):
    __tablename__ = "provider_listings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    host_name = Column(String(80), nullable=False)
    property_type = Column(String(20), nullable=False)  # 'House' | 'Shop' | 'Parking Area'
    phone_number = Column(String(10), nullable=False)   # Strictly validated 10-digit mobile
    address_label = Column(String(200), nullable=False)
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)
    unit_cost_inr = Column(Float, default=8.0, nullable=False)
    socket_type = Column(String(32), default="15A Domestic (3.3 kW)", nullable=False)
    is_available = Column(Boolean, default=True, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    owner = relationship("User", back_populates="provider_listing")
    charging_requests = relationship(
        "ChargingHistory",
        back_populates="provider",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ChargingHistory(Base):
    __tablename__ = "charging_history"

    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider_id = Column(
        Integer,
        ForeignKey("provider_listings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    car_model = Column(String(80), nullable=False)
    battery_capacity_kwh = Column(Float, nullable=False)
    current_battery_pct = Column(Float, nullable=False)
    desired_battery_pct = Column(Float, nullable=False)
    units_needed_kwh = Column(Float, nullable=False)
    unit_cost_inr = Column(Float, nullable=False)
    calculated_cost_inr = Column(Float, nullable=False)  # (Units * Unit Cost) + 200
    offered_cost_inr = Column(Float, nullable=False)     # Can be boosted if provider refuses
    status = Column(String(20), default="pending", nullable=False)  # pending | accepted | refused
    created_at = Column(DateTime, default=utcnow, nullable=False)

    customer = relationship("User", back_populates="customer_requests")
    provider = relationship("ProviderListing", back_populates="charging_requests")
