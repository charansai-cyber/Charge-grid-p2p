"""
backend/main.py
FastAPI + SQLAlchemy (SQLite) backend for the P2P Emergency EV Charging & Station Locator.

Key Architectural Highlights:
1. No Initial Login Wall:
   - `GET /api/providers/public` returns nearby P2P host pins (`House`, `Shop`, `Parking Area`),
     coordinates, local unit cost, and availability immediately WITHOUT requiring login,
     and NEVER exposes any provider's 10-digit phone number.
   - `POST /api/calculate` computes `(Units Needed × Local Area Unit Cost) + 200 Rupees`
     without requiring login.
2. Triggered Authentication (Gmail + OTP):
   - Triggered ONLY when the user clicks "Contact for Charge", "Save Address", or "History".
   - `POST /api/auth/request-otp` & `POST /api/auth/verify-otp`
3. Dual Roles & Top-Right Account Menu:
   - Any verified user can both save a provider listing (`POST /api/providers/save`)
     and contact hosts (`POST /api/charging/contact`).
   - `GET /api/history`, `POST /api/auth/logout`, and `DELETE /api/auth/account`
     (permanently deletes the user account and all linked data).
"""

import hashlib
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Optional

from .mailer import generate_otp_bundle, send_otp_email, verify_otp_hash
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
DEFAULT_DB_FILE = str(Path(__file__).resolve().parent.parent / "chargegrid.db")


def _hash_token(token: str) -> str:
    return hashlib.sha256(f"{SECRET_KEY}:{token}".encode("utf-8")).hexdigest()


class ChargeGridService:
    """
    Core database & domain service layer backing all FastAPI endpoints.
    Uses SQLite with foreign-key cascade deletion and identical table definitions
    to `backend/models.py` so automated tests and local servers run seamlessly.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.getenv("SQLITE_DB_PATH", DEFAULT_DB_FILE)
        self._init_schema_and_seed()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_schema_and_seed(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    session_token_hash TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS email_otps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL COLLATE NOCASE,
                    otp_hash TEXT NOT NULL,
                    otp_salt TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    expires_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS provider_listings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL UNIQUE,
                    host_name TEXT NOT NULL,
                    property_type TEXT NOT NULL CHECK (property_type IN ('House', 'Shop', 'Parking Area')),
                    phone_number TEXT NOT NULL CHECK (length(phone_number) = 10),
                    address_label TEXT NOT NULL,
                    lat REAL NOT NULL,
                    lng REAL NOT NULL,
                    unit_cost_inr REAL NOT NULL DEFAULT 8.0,
                    socket_type TEXT NOT NULL DEFAULT '15A Domestic (3.3 kW)',
                    is_available INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS charging_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    customer_id INTEGER NOT NULL,
                    provider_id INTEGER NOT NULL,
                    car_model TEXT NOT NULL,
                    battery_capacity_kwh REAL NOT NULL,
                    current_battery_pct REAL NOT NULL,
                    desired_battery_pct REAL NOT NULL,
                    units_needed_kwh REAL NOT NULL,
                    unit_cost_inr REAL NOT NULL,
                    calculated_cost_inr REAL NOT NULL,
                    offered_cost_inr REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (customer_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (provider_id) REFERENCES provider_listings(id) ON DELETE CASCADE
                );
                """
            )

            count = conn.execute("SELECT COUNT(*) FROM provider_listings").fetchone()[0]
            if count == 0:
                self._seed_initial_hosts(conn)

    def _seed_initial_hosts(self, conn: sqlite3.Connection) -> None:
        seeds = [
            (
                "kabir.homehost@gmail.com",
                "Kabir Gill — Sector 17 Residence",
                "House",
                "9814022334",
                "House 214, Sector 17-C, Chandigarh",
                30.7418,
                76.7825,
                8.0,
                "15A Domestic (3.3 kW)",
                1,
            ),
            (
                "verma.evshop@gmail.com",
                "Verma Auto & Electricals Shop",
                "Shop",
                "9872155667",
                "SCO 44, Ferozepur Road Market, Ludhiana, Punjab",
                30.8975,
                75.8412,
                9.0,
                "7.4 kW AC Wallbox",
                1,
            ),
            (
                "mohali.parking@gmail.com",
                "Phase 7 Guarded EV Parking Bay",
                "Parking Area",
                "9815688990",
                "Block B Community Parking Lot, Phase 7, Mohali",
                30.7124,
                76.7231,
                7.5,
                "15A Domestic (3.3 kW)",
                1,
            ),
            (
                "ranjit.avenue.ev@gmail.com",
                "Sandhu Courtyard Charger — Ranjit Ave",
                "House",
                "9815033445",
                "C-Block, Ranjit Avenue, Amritsar, Punjab",
                31.6528,
                74.8622,
                8.0,
                "7.4 kW AC Wallbox",
                1,
            ),
        ]
        for (
            email,
            host_name,
            prop_type,
            phone,
            addr,
            lat,
            lng,
            unit_cost,
            socket_type,
            avail,
        ) in seeds:
            cur = conn.execute("INSERT INTO users (email) VALUES (?)", (email,))
            uid = cur.lastrowid
            conn.execute(
                """
                INSERT INTO provider_listings (
                    user_id, host_name, property_type, phone_number,
                    address_label, lat, lng, unit_cost_inr, socket_type, is_available
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (uid, host_name, prop_type, phone, addr, lat, lng, unit_cost, socket_type, avail),
            )
        conn.commit()

    # -------------------------------------------------------------------------
    # Public Discovery (No Login Required — Phone Number NEVER Exposed)
    # -------------------------------------------------------------------------
    def list_public_providers(self, lat: float = 30.7418, lng: float = 76.7825) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, host_name, property_type, address_label,
                       lat, lng, unit_cost_inr, socket_type, is_available
                FROM provider_listings
                """
            ).fetchall()

        results = []
        for r in rows:
            dist = haversine_km(lat, lng, r["lat"], r["lng"])
            results.append(
                {
                    "id": r["id"],
                    "host_name": r["host_name"],
                    "property_type": r["property_type"],
                    "address_label": r["address_label"],
                    "lat": r["lat"],
                    "lng": r["lng"],
                    "unit_cost_inr": r["unit_cost_inr"],
                    "socket_type": r["socket_type"],
                    "is_available": bool(r["is_available"]),
                    "distance_km": dist,
                    # Privacy rule: phone_number is strictly omitted from public map listings
                }
            )
        results.sort(key=lambda x: (not x["is_available"], x["distance_km"]))
        return results

    # -------------------------------------------------------------------------
    # Nearby Public & Commercial EV Charging Stations (No Login Required)
    # -------------------------------------------------------------------------
    def list_nearby_ev_stations(self, lat: float = 30.7418, lng: float = 76.7825) -> list[dict]:
        stations = [
            {
                "id": "comm-1",
                "station_name": "Tata Power EZ Charge — Sector 17 Multi-Level Plaza",
                "operator": "Tata Power EZ Charge",
                "city": "Chandigarh",
                "address": "Sector 17-C Parking Plaza, Chandigarh",
                "lat": 30.7398,
                "lng": 76.7845,
                "connectors": "CCS2 (50 kW DC) · Type-2 AC (7.4 kW)",
                "power_kw": 50.0,
                "tariff_inr_per_kwh": 18.5,
                "hours": "24/7 Open",
                "is_operational": True,
            },
            {
                "id": "comm-2",
                "station_name": "Statiq Fast Charging Hub — Phase 7 Mohali",
                "operator": "Statiq Network",
                "city": "Mohali",
                "address": "SCF 62 Market Parking, Phase 7, Mohali, Punjab",
                "lat": 30.7135,
                "lng": 76.7254,
                "connectors": "CCS2 Dual-Gun (60 kW DC) · Bharat AC001",
                "power_kw": 60.0,
                "tariff_inr_per_kwh": 19.0,
                "hours": "24/7 Open",
                "is_operational": True,
            },
            {
                "id": "comm-3",
                "station_name": "Statiq Express Highway Charger — Ferozepur Road",
                "operator": "Statiq Network",
                "city": "Ludhiana",
                "address": "Grand Walk Mall Plaza, Ferozepur Road, Ludhiana, Punjab",
                "lat": 30.8895,
                "lng": 75.8312,
                "connectors": "CCS2 (60 kW DC) · CHAdeMO · Type-2 AC",
                "power_kw": 60.0,
                "tariff_inr_per_kwh": 19.5,
                "hours": "24/7 Open",
                "is_operational": True,
            },
            {
                "id": "ev-jal-1",
                "station_name": "Jio-bp pulse EV Station — GT Road Jalandhar",
                "operator": "Jio-bp pulse",
                "city": "Jalandhar",
                "address": "NH-44 Haveli Highway Complex, Jalandhar, Punjab",
                "lat": 31.2982,
                "lng": 75.6415,
                "connectors": "CCS2 (120 kW Ultra-Fast DC)",
                "power_kw": 120.0,
                "tariff_inr_per_kwh": 21.0,
                "hours": "24/7 Open",
                "is_operational": True,
            },
            {
                "id": "ev-asr-1",
                "station_name": "BPCL e-Drive Fast Charger — Ranjit Avenue Amritsar",
                "operator": "BPCL e-Drive",
                "city": "Amritsar",
                "address": "District Shopping Complex, Ranjit Avenue, Amritsar, Punjab",
                "lat": 31.6512,
                "lng": 74.8648,
                "connectors": "CCS2 (30 kW DC) · Type-2 AC (22 kW)",
                "power_kw": 30.0,
                "tariff_inr_per_kwh": 17.5,
                "hours": "24/7 Open",
                "is_operational": True,
            },
            {
                "id": "ev-pat-1",
                "station_name": "Ather Grid & Universal EV Point — Leela Bhawan Patiala",
                "operator": "Ather / Punjab State EV Grid",
                "city": "Patiala",
                "address": "Leela Bhawan Market Complex, Patiala, Punjab",
                "lat": 30.3398,
                "lng": 76.3869,
                "connectors": "CCS2 (30 kW DC) · 15A Universal AC",
                "power_kw": 30.0,
                "tariff_inr_per_kwh": 16.5,
                "hours": "06:00 AM – 11:00 PM",
                "is_operational": True,
            },
        ]

        results = []
        for s in stations:
            dist = haversine_km(lat, lng, s["lat"], s["lng"])
            results.append({**s, "distance_km": dist})

        results.sort(key=lambda x: x["distance_km"])
        return results

    # -------------------------------------------------------------------------
    # Triggered Authentication (Gmail + OTP)
    # -------------------------------------------------------------------------
    def request_otp(self, raw_email: str) -> dict:
        email = validate_email_address(raw_email)
        otp_code, salt, otp_hash = generate_otp_bundle(SECRET_KEY)
        expires_at = int(time.time()) + OTP_EXPIRY_SECONDS

        with self._connect() as conn:
            conn.execute("DELETE FROM email_otps WHERE email = ?", (email,))
            conn.execute(
                """
                INSERT INTO email_otps (email, otp_hash, otp_salt, attempts, expires_at)
                VALUES (?, ?, ?, 0, ?)
                """,
                (email, otp_hash, salt, expires_at),
            )
            conn.commit()

        delivery = send_otp_email(email, otp_code, expiry_minutes=OTP_EXPIRY_SECONDS // 60)
        return {
            "ok": True,
            "email": email,
            "mode": delivery["mode"],
            "dev_otp_preview": delivery["dev_otp_preview"],
            "message": delivery["message"],
        }

    def verify_otp(self, raw_email: str, raw_otp: str) -> tuple[dict, str]:
        email = validate_email_address(raw_email)
        otp = str(raw_otp or "").strip()
        if len(otp) != 6 or not otp.isdigit():
            raise ValueError("Please enter a valid 6-digit numeric OTP.")

        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM email_otps WHERE email = ? ORDER BY id DESC LIMIT 1",
                (email,),
            ).fetchone()

            if not row:
                raise ValueError("No active OTP found for this email. Please request a new code.")

            if int(time.time()) > row["expires_at"]:
                conn.execute("DELETE FROM email_otps WHERE id = ?", (row["id"],))
                conn.commit()
                raise ValueError("This OTP has expired. Please request a new code.")

            if row["attempts"] >= OTP_MAX_ATTEMPTS:
                conn.execute("DELETE FROM email_otps WHERE id = ?", (row["id"],))
                conn.commit()
                raise ValueError("Maximum verification attempts reached. Request a new OTP.")

            if not verify_otp_hash(otp, row["otp_salt"], row["otp_hash"], SECRET_KEY):
                conn.execute(
                    "UPDATE email_otps SET attempts = attempts + 1 WHERE id = ?",
                    (row["id"],),
                )
                conn.commit()
                raise PermissionError("Invalid OTP code. Please check and try again.")

            # Valid OTP -> consume it immediately
            conn.execute("DELETE FROM email_otps WHERE id = ?", (row["id"],))

            raw_token = secrets.token_hex(32)
            token_hash = _hash_token(raw_token)

            user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if not user:
                cur = conn.execute(
                    "INSERT INTO users (email, session_token_hash) VALUES (?, ?)",
                    (email, token_hash),
                )
                uid = cur.lastrowid
            else:
                uid = user["id"]
                conn.execute(
                    "UPDATE users SET session_token_hash = ? WHERE id = ?",
                    (token_hash, uid),
                )
            conn.commit()

        return {"id": uid, "email": email}, raw_token

    def get_user_by_token(self, raw_token: Optional[str]) -> Optional[dict]:
        if not raw_token:
            return None
        token_hash = _hash_token(raw_token)
        with self._connect() as conn:
            user = conn.execute(
                "SELECT id, email, created_at FROM users WHERE session_token_hash = ?",
                (token_hash,),
            ).fetchone()
            if not user:
                return None
            listing = conn.execute(
                "SELECT * FROM provider_listings WHERE user_id = ?",
                (user["id"],),
            ).fetchone()
            return {
                "id": user["id"],
                "email": user["email"],
                "provider_listing": dict(listing) if listing else None,
            }

    def logout_user(self, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET session_token_hash = NULL WHERE id = ?",
                (user_id,),
            )
            conn.commit()

    def delete_account_permanently(self, user_id: int) -> None:
        with self._connect() as conn:
            user = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
            if user:
                conn.execute("DELETE FROM email_otps WHERE email = ?", (user["email"],))
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            conn.commit()

    # -------------------------------------------------------------------------
    # Provider Flow ("Save Address" — Triggered Auth Required)
    # -------------------------------------------------------------------------
    def save_provider_address(self, user_id: int, payload: dict) -> dict:
        host_name = str(payload.get("host_name") or "").strip()
        if len(host_name) < 2 or len(host_name) > 80:
            raise ValueError("Host / Provider Name must be between 2 and 80 characters.")

        prop_type = validate_property_type(payload.get("property_type", ""))
        phone = validate_phone_10_digits(payload.get("phone_number", ""))
        address_label = str(payload.get("address_label") or "").strip()
        if len(address_label) < 4 or len(address_label) > 200:
            raise ValueError("Address must be between 4 and 200 characters.")

        lat = float(payload.get("lat"))
        lng = float(payload.get("lng"))
        if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
            raise ValueError("Please select a valid location pin on the map.")

        unit_cost = float(payload.get("unit_cost_inr", 8.0))
        if unit_cost < 0 or unit_cost > 100:
            raise ValueError("Local unit cost must be between ₹0 and ₹100.")

        socket_type = str(payload.get("socket_type") or "15A Domestic (3.3 kW)").strip()
        is_available = 1 if payload.get("is_available", True) else 0

        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM provider_listings WHERE user_id = ?", (user_id,)
            ).fetchone()
            if not existing:
                conn.execute(
                    """
                    INSERT INTO provider_listings (
                        user_id, host_name, property_type, phone_number,
                        address_label, lat, lng, unit_cost_inr, socket_type, is_available
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        host_name,
                        prop_type,
                        phone,
                        address_label,
                        lat,
                        lng,
                        unit_cost,
                        socket_type,
                        is_available,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE provider_listings
                    SET host_name = ?, property_type = ?, phone_number = ?,
                        address_label = ?, lat = ?, lng = ?, unit_cost_inr = ?,
                        socket_type = ?, is_available = ?, updated_at = datetime('now')
                    WHERE user_id = ?
                    """,
                    (
                        host_name,
                        prop_type,
                        phone,
                        address_label,
                        lat,
                        lng,
                        unit_cost,
                        socket_type,
                        is_available,
                        user_id,
                    ),
                )
            conn.commit()
            saved = conn.execute(
                "SELECT * FROM provider_listings WHERE user_id = ?", (user_id,)
            ).fetchone()

        return dict(saved)

    # -------------------------------------------------------------------------
    # Customer Flow ("Contact for Charge" & Price Boost Negotiation)
    # -------------------------------------------------------------------------
    def initiate_contact_for_charge(self, customer_user_id: int, payload: dict) -> dict:
        provider_id = int(payload.get("provider_id", 0))
        with self._connect() as conn:
            provider = conn.execute(
                "SELECT * FROM provider_listings WHERE id = ?", (provider_id,)
            ).fetchone()
            if not provider:
                raise ValueError("Selected electricity provider was not found.")

            unit_cost = (
                float(payload["unit_cost_inr"])
                if payload.get("unit_cost_inr") is not None
                else float(provider["unit_cost_inr"])
            )

            calc = calculate_ev_charge_pricing(
                car_model=payload.get("car_model", ""),
                battery_capacity_kwh=payload.get("battery_capacity_kwh", 0),
                current_battery_pct=payload.get("current_battery_pct", 0),
                desired_battery_pct=payload.get("desired_battery_pct", 0),
                unit_cost_inr=unit_cost,
            )

            offered_cost = (
                round(float(payload["offered_cost_inr"]), 2)
                if payload.get("offered_cost_inr") is not None
                else calc["total_cost_inr"]
            )
            if offered_cost < calc["total_cost_inr"]:
                offered_cost = calc["total_cost_inr"]

            cur = conn.execute(
                """
                INSERT INTO charging_history (
                    customer_id, provider_id, car_model, battery_capacity_kwh,
                    current_battery_pct, desired_battery_pct, units_needed_kwh,
                    unit_cost_inr, calculated_cost_inr, offered_cost_inr, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    customer_user_id,
                    provider["id"],
                    calc["car_model"],
                    calc["battery_capacity_kwh"],
                    calc["current_battery_pct"],
                    calc["desired_battery_pct"],
                    calc["units_needed_kwh"],
                    calc["unit_cost_inr"],
                    calc["total_cost_inr"],
                    offered_cost,
                ),
            )
            request_id = cur.lastrowid
            conn.commit()

        # Phone number is revealed ONLY here to an authenticated customer initiating a charge
        return {
            "request_id": request_id,
            "provider": {
                "id": provider["id"],
                "host_name": provider["host_name"],
                "property_type": provider["property_type"],
                "address_label": provider["address_label"],
                "phone_number": provider["phone_number"],
                "socket_type": provider["socket_type"],
            },
            "calculation": calc,
            "offered_cost_inr": offered_cost,
            "status": "pending",
        }

    def boost_offered_price(
        self, customer_user_id: int, request_id: int, new_offered_cost_inr: float
    ) -> dict:
        new_offer = round(float(new_offered_cost_inr), 2)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT h.*, p.host_name, p.phone_number, p.property_type, p.address_label
                FROM charging_history h
                JOIN provider_listings p ON p.id = h.provider_id
                WHERE h.id = ? AND h.customer_id = ?
                """,
                (request_id, customer_user_id),
            ).fetchone()
            if not row:
                raise ValueError("Charging request not found in your account.")

            if new_offer <= float(row["offered_cost_inr"]):
                raise ValueError(
                    f"New offer must be higher than the previous amount of ₹{row['offered_cost_inr']:.2f}."
                )

            conn.execute(
                """
                UPDATE charging_history
                SET offered_cost_inr = ?, status = 'incentivized'
                WHERE id = ?
                """,
                (new_offer, request_id),
            )
            conn.commit()

            updated = conn.execute(
                """
                SELECT h.*, p.host_name, p.phone_number, p.property_type, p.address_label
                FROM charging_history h
                JOIN provider_listings p ON p.id = h.provider_id
                WHERE h.id = ?
                """,
                (request_id,),
            ).fetchone()

        return dict(updated)

    def get_user_history(self, user_id: int) -> dict:
        with self._connect() as conn:
            as_customer = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT h.*, p.host_name, p.property_type, p.address_label, p.phone_number
                    FROM charging_history h
                    JOIN provider_listings p ON p.id = h.provider_id
                    WHERE h.customer_id = ?
                    ORDER BY h.id DESC
                    """,
                    (user_id,),
                ).fetchall()
            ]

            as_provider = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT h.*, u.email AS customer_email, p.host_name, p.property_type
                    FROM charging_history h
                    JOIN provider_listings p ON p.id = h.provider_id
                    JOIN users u ON u.id = h.customer_id
                    WHERE p.user_id = ?
                    ORDER BY h.id DESC
                    """,
                    (user_id,),
                ).fetchall()
            ]

        return {"as_customer": as_customer, "as_provider": as_provider}


# =============================================================================
# FastAPI App Wiring (When FastAPI is installed in the environment)
# =============================================================================
service = ChargeGridService()

try:
    from fastapi import Cookie, FastAPI, HTTPException, Request, Response
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles

    app = FastAPI(
        title="ChargeGrid P2P Emergency EV Charging & Station Locator",
        version="1.0.0",
    )

    FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

    def _require_user(session_token: Optional[str]) -> dict:
        user = service.get_user_by_token(session_token)
        if not user:
            raise HTTPException(
                status_code=401,
                detail="Authentication required. Please sign in with your Gmail OTP.",
            )
        return user

    @app.get("/api/providers/public")
    def api_public_providers(lat: float = 30.7418, lng: float = 76.7825):
        return {"ok": True, "providers": service.list_public_providers(lat, lng)}

    @app.get("/api/ev-stations/nearby")
    def api_nearby_ev_stations(lat: float = 30.7418, lng: float = 76.7825):
        return {"ok": True, "stations": service.list_nearby_ev_stations(lat, lng)}

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
            return service.request_otp(body.get("email", ""))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/auth/verify-otp")
    async def api_verify_otp(request: Request, response: Response):
        body = await request.json()
        try:
            user, raw_token = service.verify_otp(
                body.get("email", ""), body.get("otp", "")
            )
            response.set_cookie(
                key=SESSION_COOKIE_NAME,
                value=raw_token,
                httponly=True,
                samesite="lax",
                max_age=7 * 24 * 3600,
            )
            return {"ok": True, "user": service.get_user_by_token(raw_token)}
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/auth/session")
    def api_get_session(
        session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME)
    ):
        user = service.get_user_by_token(session_token)
        return {"ok": True, "authenticated": bool(user), "user": user}

    @app.post("/api/auth/logout")
    def api_logout(
        response: Response,
        session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    ):
        user = _require_user(session_token)
        service.logout_user(user["id"])
        response.delete_cookie(SESSION_COOKIE_NAME)
        return {"ok": True, "message": "Logged out."}

    @app.delete("/api/auth/account")
    def api_delete_account(
        response: Response,
        session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    ):
        user = _require_user(session_token)
        service.delete_account_permanently(user["id"])
        response.delete_cookie(SESSION_COOKIE_NAME)
        return {"ok": True, "message": "Account and all data permanently deleted."}

    @app.post("/api/providers/save")
    async def api_save_provider(
        request: Request,
        session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    ):
        user = _require_user(session_token)
        body = await request.json()
        try:
            saved = service.save_provider_address(user["id"], body)
            return {"ok": True, "listing": saved}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/charging/contact")
    async def api_contact_for_charge(
        request: Request,
        session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    ):
        user = _require_user(session_token)
        body = await request.json()
        try:
            result = service.initiate_contact_for_charge(user["id"], body)
            return {"ok": True, **result}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.patch("/api/charging/{request_id}/boost")
    async def api_boost_offer(
        request_id: int,
        request: Request,
        session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    ):
        user = _require_user(session_token)
        body = await request.json()
        try:
            updated = service.boost_offered_price(
                user["id"], request_id, body.get("new_offered_cost_inr", 0)
            )
            return {"ok": True, "record": updated}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/history")
    def api_get_history(
        session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    ):
        user = _require_user(session_token)
        return {"ok": True, **service.get_user_history(user["id"])}

    if FRONTEND_DIR.exists():
        app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

except ImportError:
    app = None
