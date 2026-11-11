"""
backend/mailer.py
Sends OTP via Brevo HTTPS API on Render, with an automatic on-screen
fallback so login always works even if email is blocked.
"""

import hashlib
import hmac
import json
import os
import secrets
import urllib.error
import urllib.request


def generate_otp_bundle(secret_key: str) -> tuple[str, str, str]:
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


def send_otp_email(to_email: str, otp_code: str, expiry_minutes: int = 10) -> dict:
    sender_email = os.getenv("SENDER_EMAIL", "chargegeid@gmail.com").strip()
    brevo_api_key = os.getenv("BREVO_API_KEY", "").strip()

    if brevo_api_key:
        try:
            payload = json.dumps({
                "sender": {"name": "ChargeGrid P2P", "email": sender_email},
                "to": [{"email": to_email}],
                "subject": "Your ChargeGrid P2P Verification Code",
                "textContent": (
                    f"ChargeGrid P2P Emergency EV Charging\n\n"
                    f"Your single-use 6-digit verification code is: {otp_code}\n"
                    f"This code expires in {expiry_minutes} minutes."
                ),
            }).encode("utf-8")

            req = urllib.request.Request(
                "https://api.brevo.com/v3/smtp/email",
                data=payload,
                headers={
                    "accept": "application/json",
                    "content-type": "application/json",
                    "api-key": brevo_api_key,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status in (200, 201, 202):
                    return {
                        "delivered": True,
                        "mode": "brevo_https",
                        "dev_otp_preview": None,
                        "message": f"6-digit OTP sent to {to_email}!",
                    }
        except Exception as e:
            print(f"[BREVO ERROR]: {e}")

    return {
        "delivered": False,
        "mode": "dev_preview",
        "dev_otp_preview": otp_code,
        "message": f"Verification code for {to_email}: {otp_code}",
    }
