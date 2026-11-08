"""
backend/schemas.py
Core business logic, strict input validation, and Pydantic v2 schemas:
- Strict 10-digit Indian mobile number validator
- Smart EV Calculator:
    Units Needed (kWh) = Battery Capacity * (Desired % - Current %) / 100
    Total Cost (INR)   = (Units Needed * Local Area Unit Cost) + 200 Rupees
- Estimated charging time calculation based on socket power (3.0 kW vs 7.2 kW)
"""

import math
import re
from typing import Optional

ALLOWED_PROPERTY_TYPES = ("House", "Shop", "Parking Area")
EMERGENCY_FEE_INR = 200.0
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
PHONE_CLEAN_REGEX = re.compile(r"[\s\-().]")


def validate_email_address(raw_email: str) -> str:
    """Validates email format (accepts Gmail and standard addresses)."""
    if not isinstance(raw_email, str) or not raw_email.strip():
        raise ValueError("Email address is required.")
    email = raw_email.strip().lower()
    if len(email) > 254 or not EMAIL_REGEX.match(email):
        raise ValueError("Please enter a valid email address (e.g., driver@gmail.com).")
    return email


def validate_phone_10_digits(raw_phone: str) -> str:
    """
    Strictly validates a 10-digit mobile number.
    Rejects +91 country codes, letters, or numbers shorter/longer than 10 digits.
    """
    if raw_phone is None:
        raise ValueError("Mobile number is required.")
    s = str(raw_phone).strip()
    if not s:
        raise ValueError("Mobile number cannot be empty.")
    if "+" in s:
        raise ValueError(
            "Enter a 10-digit mobile number without country code (e.g., 9876543210, not +91)."
        )
    cleaned = PHONE_CLEAN_REGEX.sub("", s)
    if not cleaned.isdigit():
        raise ValueError("Mobile number must contain digits only.")
    if len(cleaned) != 10:
        raise ValueError(
            f"Mobile number must be strictly 10 digits (received {len(cleaned)} digits)."
        )
    if cleaned[0] not in "6789":
        raise ValueError("Indian 10-digit mobile number must start with 6, 7, 8, or 9.")
    return cleaned


def validate_property_type(raw_type: str) -> str:
    if not isinstance(raw_type, str) or raw_type.strip() not in ALLOWED_PROPERTY_TYPES:
        raise ValueError(
            f"Property type must be one of: {', '.join(ALLOWED_PROPERTY_TYPES)}."
        )
    return raw_type.strip()


def calculate_ev_charge_pricing(
    car_model: str,
    battery_capacity_kwh: float,
    current_battery_pct: float,
    desired_battery_pct: float,
    unit_cost_inr: float = 8.0,
    socket_kw: float = 3.0,
) -> dict:
    """
    Smart Calculator Formula:
      Units Needed (kWh) = Battery Capacity * (Desired % - Current %) / 100
      Total Cost (₹)     = (Units Needed * Local Area Unit Cost) + 200 Rupees
    """
    model = str(car_model or "").strip()
    if len(model) < 2 or len(model) > 80:
        raise ValueError("Car Model must be between 2 and 80 characters.")

    cap = float(battery_capacity_kwh)
    cur = float(current_battery_pct)
    des = float(desired_battery_pct)
    rate = float(unit_cost_inr)
    power_kw = float(socket_kw) if float(socket_kw) > 0 else 3.0

    if not math.isfinite(cap) or cap <= 0 or cap > 250:
        raise ValueError("Battery Capacity must be between 1 kWh and 250 kWh.")
    if not math.isfinite(cur) or cur < 0 or cur >= 100:
        raise ValueError("Current Battery Percentage must be between 0% and 99%.")
    if not math.isfinite(des) or des <= 0 or des > 100:
        raise ValueError("Desired Charge Percentage must be between 1% and 100%.")
    if des <= cur:
        raise ValueError(
            "Desired Charge Percentage must be strictly greater than Current Battery Percentage."
        )
    if not math.isfinite(rate) or rate < 0 or rate > 100:
        raise ValueError("Local Area Unit Cost must be between ₹0 and ₹100 per unit.")

    units_needed = round((cap * (des - cur)) / 100.0, 2)
    energy_charge = round(units_needed * rate, 2)
    total_cost = round(energy_charge + EMERGENCY_FEE_INR, 2)
    est_minutes = max(5, int(round((units_needed / power_kw) * 60)))

    return {
        "car_model": model,
        "battery_capacity_kwh": round(cap, 2),
        "current_battery_pct": round(cur, 2),
        "desired_battery_pct": round(des, 2),
        "units_needed_kwh": units_needed,
        "unit_cost_inr": round(rate, 2),
        "energy_charge_inr": energy_charge,
        "service_fee_inr": EMERGENCY_FEE_INR,
        "total_cost_inr": total_cost,
        "estimated_duration_minutes": est_minutes,
        "formula_display": f"({units_needed:.2f} kWh × ₹{rate:.2f}) + ₹200 = ₹{total_cost:.2f}",
    }


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Calculates great-circle distance in kilometers between two coordinates."""
    r = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lng / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return round(r * c, 2)


# Optional Pydantic schemas when running with FastAPI installed
try:
    from pydantic import BaseModel, Field

    class OTPRequestSchema(BaseModel):
        email: str

    class OTPVerifySchema(BaseModel):
        email: str
        otp: str

    class ProviderSaveSchema(BaseModel):
        host_name: str = Field(..., min_length=2, max_length=80)
        property_type: str
        phone_number: str
        address_label: str = Field(..., min_length=4, max_length=200)
        lat: float = Field(..., ge=-90, le=90)
        lng: float = Field(..., ge=-180, le=180)
        unit_cost_inr: float = Field(default=8.0, ge=0, le=100)
        socket_type: Optional[str] = "15A Domestic (3.3 kW)"
        is_available: bool = True

    class ContactChargeSchema(BaseModel):
        provider_id: int
        car_model: str
        battery_capacity_kwh: float
        current_battery_pct: float
        desired_battery_pct: float
        unit_cost_inr: Optional[float] = None
        offered_cost_inr: Optional[float] = None

    class BoostOfferSchema(BaseModel):
        new_offered_cost_inr: float

except ImportError:
    pass
