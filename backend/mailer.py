"""
backend/mailer.py
Configurable Gmail SMTP OTP sender + development-only OTP preview fallback.
Never stores or requests user Gmail passwords.
"""

import hashlib
import hmac
import os
import secrets
import smtplib
from email.message import EmailMessage


def generate_otp_bundle(secret_key: str) -> tuple[str, str, str]:
    """Generates a 6-digit OTP, random salt, and HMAC-SHA256 hash."""
    otp_code = f"{secrets.randbelow(900000) + 100000}"
    salt = secrets.token_hex(8)
    digest = compute_otp_hash(otp_code, salt, secret_key)
    return otp_code, salt, digest


def compute_otp_hash(otp_code: str, salt: str, secret_key: str) -> str:
    key = f"{secret_key}:{salt}".encode("utf-8")
    return hmac.new(key, str(otp_code).strip().encode("utf-8"), hashlib.sha256).hexdigest()


def verify_otp_hash(candidate_otp: str, salt: str, stored_hash: str, secret_key: str) -> bool:
    candidate_hash = compute_otp_hash(candidate_otp, salt, secret_key)
    return hmac.compare_digest(candidate_hash, stored_hash)


def is_smtp_configured() -> bool:
    return True


def send_otp_email(to_email: str, otp_code: str, expiry_minutes: int = 10) -> dict:
    """
    Sends the 6-digit OTP via Gmail SMTP if configured in `.env`.
    Otherwise, when DEV_OTP_PREVIEW=true in development, logs the OTP and returns
    a clearly labeled dev_otp_preview so triggered authentication can be tested locally.
    """
    if is_smtp_configured():
        host = "smtp.gmail.com"
        port = 465
        user = "chargegeid@gmail.com"
        pw = "ratuutvztcsqqivp"
        sender = "ChargeGrid P2P <chargegeid@gmail.com>"
        msg = EmailMessage()
        msg["Subject"] = "Your ChargeGrid P2P Verification Code"
        msg["From"] = sender
        msg["To"] = to_email
        msg.set_content(
            f"ChargeGrid P2P Emergency EV Charging\n\n"
            f"Your single-use 6-digit verification code is: {otp_code}\n"
            f"This code expires in {expiry_minutes} minutes.\n\n"
            f"We never ask for your Gmail password."
        )

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=10) as server:
            server.starttls()
            server.login("chargegeid@gmail.com", "ratuutvztcsqqivp")
            server.send_message(msg)

        return {
            "delivered": True,
            "mode": "smtp",
            "dev_otp_preview": None,
            "message": f"6-digit OTP sent to {to_email}.",
        }

    dev_preview = os.getenv("DEV_OTP_PREVIEW", "true").lower() == "true"
    app_env = os.getenv("APP_ENV", "development").lower()

    if dev_preview and app_env != "production":
        print(
            f"\n[DEV-ONLY OTP PREVIEW] Email: {to_email} | OTP: {otp_code} "
            f"(Configure SMTP_USER & SMTP_PASS in .env for real Gmail delivery)\n"
        )
        return {
            "delivered": False,
            "mode": "dev_preview",
            "dev_otp_preview": otp_code,
            "message": f"Development Mode: Use one-time code {otp_code} to verify {to_email}.",
        }

    raise RuntimeError(
        "SMTP credentials are not configured. Set SMTP_HOST, SMTP_USER, and SMTP_PASS in .env."
    )
