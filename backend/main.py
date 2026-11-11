"""
backend/main.py
Production FastAPI + SQLAlchemy (Neon PostgreSQL / SQLite) backend for ChargeGrid P2P.
- Zero fake demo hosts: Only real registered providers appear.
- Live OpenStreetMap Overpass query for real nearby EV charging stations.
- Permanent cloud database support via DATABASE_URL (Neon PostgreSQL).
"""

import hashlib
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from fastapi import Cookie, FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .mailer import generate_otp_bundle, send_otp_email, verify_otp_hash
from .models import Base, ChargingHistory, EmailOTP, ProviderListing, User
from .schemas import (
    calculate_ev_charge_pricing,
    haversine_km,
    validate_email_address,
    validate_phone_10_digits,
    validate_property_type,
)

SECRET_KEY = os.getenv("SECRET_KEY", "chargegrid-p2p-default-secret-key-change-in-prod")
SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "chargegrid_session")
OTP_EXPIRY_SECONDS = int(os.getenv("OTP_EXPIRY_MINUTES", "10")) * 60
OTP_MAX_ATTEMPTS = int(os.getenv("OTP_MAX_ATTEMPTS", "5"))

# -----------------------------------------------------------------------------
# Database Connection (Supports Neon PostgreSQL via DATABASE_URL or local SQLite)
# -----------------------------------------------------------------------------
raw_db_url = os.getenv("DATABASE_URL", "sqlite:///./chargegrid.db").strip()
if raw_db_url.startswith("postgres://"):
    raw_db_url = raw_db_url.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    raw_db_url,
    connect_args={"check_same_thread": False} if raw_db_url.startswith("sqlite") else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Automatically create tables in Neon PostgreSQL (or local SQLite)
Base.metadata.create_all(bind=engine)


def _hash_token(token: str) -> str:
    return hashlib.sha256(f"{SECRET_KEY}:{token}".encode("utf-8")).hexdigest()


def fetch_real_osm_ev_stations(lat: float, lng: float) -> list[dict]:
    """Fetches real-world public EV charging stations from OpenStreetMap Overpass API."""
    overpass_query = (
        f'[out:json][timeout:12];'
        f'(node["amenity"="charging_station"](around:50000,{lat},{lng});'
        f'way["amenity"="charging_station"](around:50000,{lat},{lng}););'
        f'out center 25;'
    )
    try:
        data_bytes = urllib.parse.urlencode({"data": overpass_query}).encode("utf-8")
        req = urllib.request.Request(
            "https://overpass-api.de/api/interpreter",
            data=data_bytes,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "User-Agent": "ChargeGridP2P/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            elements = payload.get("elements", [])

        stations = []
        for el in elements:
            s_lat = el.get("lat") or (el.get("center") or {}).get("lat")
            s_lng = el.get("lon") or (el.get("center") or {}).get("lon")
            if s_lat is None or s_lng is None:
                continue
            tags = el.get("tags") or {}
            name = (
                tags.get("name")
                or tags.get("operator")
                or tags.get("brand")
                or "Public EV Charging Station"
            )
            operator = tags.get("operator") or tags.get("brand") or "OpenStreetMap Verified"
            addr_parts = [
                tags.get("addr:street"),
                tags.get("addr:suburb") or tags.get("addr:city"),
            ]
            address = ", ".join([p for p in addr_parts if p]) or f"GPS: {s_lat:.4f}, {s_lng:.4f}"
            connectors = (
                "CCS2 DC Fast"
                if tags.get("socket:ccs")
                else "Type-2 AC"
                if tags.get("socket:type2")
                else "Public EV Charger"
            )
            dist = haversine_km(lat, lng, float(s_lat), float(s_lng))
            stations.append(
                {
                    "id": f"osm-{el.get('id')}",
                    "station_name": name,
                    "operator": operator,
                    "address": address,
                    "lat": float(s_lat),
                    "lng": float(s_lng),
                    "connectors": connectors,
                    "power_kw": 50.0,
                    "tariff_inr_per_kwh": 18.0,
                    "hours": tags.get("opening_hours") or "24/7 Open",
                    "is_operational": True,
                    "distance_km": dist,
                }
            )
        stations.sort(key=lambda x: x["distance_km"])
        return stations
    except Exception as exc:
        print(f"[OVERPASS API INFO]: {exc}")
        return []


# -----------------------------------------------------------------------------
# FastAPI Application & Routes
# -----------------------------------------------------------------------------
app = FastAPI(title="ChargeGrid P2P Emergency EV Charging & Station Locator", version="2.0.0")
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


def _get_current_user(db, session_token: Optional[str]) -> Optional[User]:
    if not session_token:
        return None
    token_hash = _hash_token(session_token)
    return db.query(User).filter(User.session_token_hash == token_hash).first()


def _require_user(db, session_token: Optional[str]) -> User:
    user = _get_current_user(db, session_token)
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Please sign in with your Gmail OTP.",
        )
    return user


def _serialize_listing(listing: Optional[ProviderListing]) -> Optional[dict]:
    if not listing:
        return None
    return {
        "id": listing.id,
        "user_id": listing.user_id,
        "host_name": listing.host_name,
        "property_type": listing.property_type,
        "phone_number": listing.phone_number,
        "address_label": listing.address_label,
        "lat": listing.lat,
        "lng": listing.lng,
        "unit_cost_inr": listing.unit_cost_inr,
        "socket_type": listing.socket_type,
        "is_available": bool(listing.is_available),
    }


@app.get("/api/providers/public")
def api_public_providers(lat: float = 30.7418, lng: float = 76.7825):
    with SessionLocal() as db:
        rows = db.query(ProviderListing).all()
        results = []
        for r in rows:
            results.append(
                {
                    "id": r.id,
                    "host_name": r.host_name,
                    "property_type": r.property_type,
                    "address_label": r.address_label,
                    "lat": r.lat,
                    "lng": r.lng,
                    "unit_cost_inr": r.unit_cost_inr,
                    "socket_type": r.socket_type,
                    "is_available": bool(r.is_available),
                    "distance_km": haversine_km(lat, lng, r.lat, r.lng),
                }
            )
        results.sort(key=lambda x: (not x["is_available"], x["distance_km"]))
        return {"ok": True, "providers": results}


@app.get("/api/ev-stations/nearby")
def api_nearby_ev_stations(lat: float = 30.7418, lng: float = 76.7825):
    return {"ok": True, "stations": fetch_real_osm_ev_stations(lat, lng)}


@app.post("/api/calculate")
async def api_calculate(request: Request):
    body = await request.json()
    try:
        calc = calculate_ev_charge_pricing(
            car_model=body.get("car_model", ""),
            battery_capacity_kwh=body.get("battery_capacity_kwh", 0),
            current_battery_pct=body.get("current_battery_pct", 0),
            desired_battery_pct=body.get("desired_battery_pct", 0),
            unit_cost_inr=body.get("unit_cost_inr", 8.0),
            socket_kw=body.get("socket_kw", 3.0),
        )
        return {"ok": True, "calculation": calc}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/auth/request-otp")
async def api_request_otp(request: Request):
    body = await request.json()
    try:
        email = validate_email_address(body.get("email", ""))
        otp_code, salt, otp_hash = generate_otp_bundle(SECRET_KEY)
        expires_at = int(time.time()) + OTP_EXPIRY_SECONDS

        with SessionLocal() as db:
            db.query(EmailOTP).filter(EmailOTP.email == email).delete()
            db.add(
                EmailOTP(
                    email=email,
                    otp_hash=otp_hash,
                    otp_salt=salt,
                    attempts=0,
                    expires_at=expires_at,
                )
            )
            db.commit()

        delivery = send_otp_email(email, otp_code, expiry_minutes=OTP_EXPIRY_SECONDS // 60)
        return {
            "ok": True,
            "email": email,
            "mode": delivery["mode"],
            "dev_otp_preview": delivery["dev_otp_preview"],
            "message": delivery["message"],
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/auth/verify-otp")
async def api_verify_otp(request: Request, response: Response):
    body = await request.json()
    try:
        email = validate_email_address(body.get("email", ""))
        otp = str(body.get("otp") or "").strip()
        if len(otp) != 6 or not otp.isdigit():
            raise ValueError("Please enter a valid 6-digit numeric OTP.")

        with SessionLocal() as db:
            row = (
                db.query(EmailOTP)
                .filter(EmailOTP.email == email)
                .order_by(EmailOTP.id.desc())
                .first()
            )
            if not row:
                raise ValueError("No active OTP found for this email. Please request a new code.")

            if int(time.time()) > row.expires_at:
                db.delete(row)
                db.commit()
                raise ValueError("This OTP has expired. Please request a new code.")

            if row.attempts >= OTP_MAX_ATTEMPTS:
                db.delete(row)
                db.commit()
                raise ValueError("Maximum verification attempts reached. Request a new OTP.")

            if not verify_otp_hash(otp, row.otp_salt, row.otp_hash, SECRET_KEY):
                row.attempts += 1
                db.commit()
                raise HTTPException(status_code=401, detail="Invalid OTP code. Please try again.")

            db.delete(row)
            raw_token = secrets.token_hex(32)
            token_hash = _hash_token(raw_token)

            user = db.query(User).filter(User.email == email).first()
            if not user:
                user = User(email=email, session_token_hash=token_hash)
                db.add(user)
            else:
                user.session_token_hash = token_hash
            db.commit()
            db.refresh(user)

            user_payload = {
                "id": user.id,
                "email": user.email,
                "provider_listing": _serialize_listing(user.provider_listing),
            }

        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=raw_token,
            httponly=True,
            samesite="lax",
            max_age=7 * 24 * 3600,
        )
        return {"ok": True, "user": user_payload}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/auth/session")
def api_get_session(
    session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME)
):
    with SessionLocal() as db:
        user = _get_current_user(db, session_token)
        if not user:
            return {"ok": True, "authenticated": False, "user": None}
        return {
            "ok": True,
            "authenticated": True,
            "user": {
                "id": user.id,
                "email": user.email,
                "provider_listing": _serialize_listing(user.provider_listing),
            },
        }


@app.post("/api/auth/logout")
def api_logout(
    response: Response,
    session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
):
    with SessionLocal() as db:
        user = _require_user(db, session_token)
        user.session_token_hash = None
        db.commit()
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"ok": True, "message": "Logged out."}


@app.delete("/api/auth/account")
def api_delete_account(
    response: Response,
    session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
):
    with SessionLocal() as db:
        user = _require_user(db, session_token)
        db.query(EmailOTP).filter(EmailOTP.email == user.email).delete()
        db.delete(user)
        db.commit()
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"ok": True, "message": "Account and all data permanently deleted."}


@app.post("/api/providers/save")
async def api_save_provider(
    request: Request,
    session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
):
    body = await request.json()
    try:
        host_name = str(body.get("host_name") or "").strip()
        if len(host_name) < 2 or len(host_name) > 80:
            raise ValueError("Host / Provider Name must be between 2 and 80 characters.")
        prop_type = validate_property_type(body.get("property_type", ""))
        phone = validate_phone_10_digits(body.get("phone_number", ""))
        address_label = str(body.get("address_label") or "").strip()
        if len(address_label) < 4 or len(address_label) > 200:
            raise ValueError("Address must be between 4 and 200 characters.")
        lat = float(body.get("lat"))
        lng = float(body.get("lng"))
        unit_cost = float(body.get("unit_cost_inr", 8.0))
        socket_type = str(body.get("socket_type") or "15A Domestic (3.3 kW)").strip()
        is_available = bool(body.get("is_available", True))

        with SessionLocal() as db:
            user = _require_user(db, session_token)
            listing = (
                db.query(ProviderListing)
                .filter(ProviderListing.user_id == user.id)
                .first()
            )
            if not listing:
                listing = ProviderListing(
                    user_id=user.id,
                    host_name=host_name,
                    property_type=prop_type,
                    phone_number=phone,
                    address_label=address_label,
                    lat=lat,
                    lng=lng,
                    unit_cost_inr=unit_cost,
                    socket_type=socket_type,
                    is_available=is_available,
                )
                db.add(listing)
            else:
                listing.host_name = host_name
                listing.property_type = prop_type
                listing.phone_number = phone
                listing.address_label = address_label
                listing.lat = lat
                listing.lng = lng
                listing.unit_cost_inr = unit_cost
                listing.socket_type = socket_type
                listing.is_available = is_available
            db.commit()
            db.refresh(listing)
            return {"ok": True, "listing": _serialize_listing(listing)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/charging/contact")
async def api_contact_for_charge(
    request: Request,
    session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
):
    body = await request.json()
    try:
        provider_id = int(body.get("provider_id", 0))
        with SessionLocal() as db:
            user = _require_user(db, session_token)
            provider = (
                db.query(ProviderListing)
                .filter(ProviderListing.id == provider_id)
                .first()
            )
            if not provider:
                raise ValueError("Selected electricity provider was not found.")

            calc = calculate_ev_charge_pricing(
                car_model=body.get("car_model", ""),
                battery_capacity_kwh=body.get("battery_capacity_kwh", 0),
                current_battery_pct=body.get("current_battery_pct", 0),
                desired_battery_pct=body.get("desired_battery_pct", 0),
                unit_cost_inr=float(provider.unit_cost_inr),
            )
            offered_cost = calc["total_cost_inr"]

            record = ChargingHistory(
                customer_id=user.id,
                provider_id=provider.id,
                car_model=calc["car_model"],
                battery_capacity_kwh=calc["battery_capacity_kwh"],
                current_battery_pct=calc["current_battery_pct"],
                desired_battery_pct=calc["desired_battery_pct"],
                units_needed_kwh=calc["units_needed_kwh"],
                unit_cost_inr=calc["unit_cost_inr"],
                calculated_cost_inr=calc["total_cost_inr"],
                offered_cost_inr=offered_cost,
                status="pending",
            )
            db.add(record)
            db.commit()
            db.refresh(record)

            return {
                "ok": True,
                "request_id": record.id,
                "provider": _serialize_listing(provider),
                "calculation": calc,
                "offered_cost_inr": offered_cost,
                "status": "pending",
            }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/charging/{request_id}/boost")
async def api_boost_offer(
    request_id: int,
    request: Request,
    session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
):
    body = await request.json()
    try:
        new_offer = round(float(body.get("new_offered_cost_inr", 0)), 2)
        with SessionLocal() as db:
            user = _require_user(db, session_token)
            record = (
                db.query(ChargingHistory)
                .filter(
                    ChargingHistory.id == request_id,
                    ChargingHistory.customer_id == user.id,
                )
                .first()
            )
            if not record:
                raise ValueError("Charging request not found.")
            if new_offer <= float(record.offered_cost_inr):
                raise ValueError(
                    f"New offer must be higher than ₹{record.offered_cost_inr:.2f}."
                )
            record.offered_cost_inr = new_offer
            record.status = "incentivized"
            db.commit()
            db.refresh(record)
            prov = record.provider
            return {
                "ok": True,
                "record": {
                    "id": record.id,
                    "offered_cost_inr": record.offered_cost_inr,
                    "status": record.status,
                    "host_name": prov.host_name if prov else "",
                    "phone_number": prov.phone_number if prov else "",
                    "property_type": prov.property_type if prov else "",
                    "address_label": prov.address_label if prov else "",
                },
            }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/history")
def api_get_history(
    session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
):
    with SessionLocal() as db:
        user = _require_user(db, session_token)
        cust_rows = (
            db.query(ChargingHistory)
            .filter(ChargingHistory.customer_id == user.id)
            .order_by(ChargingHistory.id.desc())
            .all()
        )
        as_customer = [
            {
                "id": r.id,
                "car_model": r.car_model,
                "units_needed_kwh": r.units_needed_kwh,
                "calculated_cost_inr": r.calculated_cost_inr,
                "offered_cost_inr": r.offered_cost_inr,
                "status": r.status,
                "host_name": r.provider.host_name if r.provider else "",
                "property_type": r.provider.property_type if r.provider else "",
                "address_label": r.provider.address_label if r.provider else "",
                "phone_number": r.provider.phone_number if r.provider else "",
            }
            for r in cust_rows
        ]

        as_provider = []
        if user.provider_listing:
            prov_rows = (
                db.query(ChargingHistory)
                .filter(ChargingHistory.provider_id == user.provider_listing.id)
                .order_by(ChargingHistory.id.desc())
                .all()
            )
            as_provider = [
                {
                    "id": r.id,
                    "customer_email": r.customer.email if r.customer else "",
                    "car_model": r.car_model,
                    "units_needed_kwh": r.units_needed_kwh,
                    "offered_cost_inr": r.offered_cost_inr,
                    "status": r.status,
                }
                for r in prov_rows
            ]

        return {"ok": True, "as_customer": as_customer, "as_provider": as_provider}


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
