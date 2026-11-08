"""
tests/test_app.py
Automated unit & domain tests for ChargeGrid P2P (FastAPI + SQLite):
1. Strict 10-digit Indian mobile number validation
2. Smart Calculator formula: Total Cost = (Units Needed * Local Area Unit Cost) + 200 Rupees
3. Public map listing privacy (NEVER exposes provider phone numbers)
4. Triggered Gmail OTP authentication, "Save Address", "Contact for Charge",
   negotiation price boost, "History", and permanent "Delete Account"
"""

import os
import tempfile
import unittest

os.environ["DEV_OTP_PREVIEW"] = "true"
os.environ["APP_ENV"] = "development"
os.environ["SQLITE_DB_PATH"] = os.path.join(tempfile.gettempdir(), "chargegrid_default_test.db")

from backend.main import ChargeGridService
from backend.schemas import calculate_ev_charge_pricing, validate_phone_10_digits


class TestChargeGridP2P(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_chargegrid.db")
        self.service = ChargeGridService(db_path=self.db_path)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_strict_10_digit_phone_validation(self):
        self.assertEqual(validate_phone_10_digits("9876543210"), "9876543210")
        self.assertEqual(validate_phone_10_digits("98765 43210"), "9876543210")

        with self.assertRaises(ValueError):
            validate_phone_10_digits("+919876543210")
        with self.assertRaises(ValueError):
            validate_phone_10_digits("987654321")
        with self.assertRaises(ValueError):
            validate_phone_10_digits("1234567890")

    def test_smart_calculator_formula(self):
        # 40 kWh battery, 20% -> 70% = 50% delta = 20.0 kWh units needed
        # Total Cost = (20.0 * 8.0) + 200 = 160 + 200 = 360.0 Rupees
        calc = calculate_ev_charge_pricing(
            car_model="Tata Nexon EV",
            battery_capacity_kwh=40,
            current_battery_pct=20,
            desired_battery_pct=70,
            unit_cost_inr=8.0,
        )
        self.assertEqual(calc["units_needed_kwh"], 20.0)
        self.assertEqual(calc["energy_charge_inr"], 160.0)
        self.assertEqual(calc["service_fee_inr"], 200.0)
        self.assertEqual(calc["total_cost_inr"], 360.0)

        with self.assertRaises(ValueError):
            calculate_ev_charge_pricing("Tata Nexon EV", 40, 60, 50, 8.0)

    def test_public_map_never_exposes_phone_numbers(self):
        public_pins = self.service.list_public_providers(30.7418, 76.7825)
        self.assertTrue(len(public_pins) >= 4)
        for pin in public_pins:
            self.assertNotIn("phone_number", pin)

    def test_triggered_auth_save_address_contact_negotiation_and_delete(self):
        # 1. Request & verify Gmail OTP
        otp_res = self.service.request_otp("host_and_driver@gmail.com")
        otp_code = otp_res["dev_otp_preview"]
        user, token = self.service.verify_otp("host_and_driver@gmail.com", otp_code)
        self.assertIsNotNone(token)

        # 2. Save Provider Address with valid 10-digit mobile number
        saved_listing = self.service.save_provider_address(
            user["id"],
            {
                "host_name": "Singh Driveway Charger",
                "property_type": "House",
                "phone_number": "9876543210",
                "address_label": "Sector 22, Chandigarh",
                "lat": 30.7350,
                "lng": 76.7750,
                "unit_cost_inr": 8.5,
                "socket_type": "15A Domestic (3.3 kW)",
                "is_available": True,
            },
        )
        self.assertEqual(saved_listing["phone_number"], "9876543210")

        # 3. Contact for Charge reveals phone number and records history
        contact_res = self.service.initiate_contact_for_charge(
            user["id"],
            {
                "provider_id": saved_listing["id"],
                "car_model": "MG ZS EV",
                "battery_capacity_kwh": 50,
                "current_battery_pct": 10,
                "desired_battery_pct": 50,
            },
        )
        self.assertEqual(contact_res["provider"]["phone_number"], "9876543210")
        # 50 kWh * 40% = 20 kWh * 8.5 = 170 + 200 = 370.0
        self.assertEqual(contact_res["calculation"]["total_cost_inr"], 370.0)

        # 4. Provider refuses -> Customer increases price to incentivize provider
        boosted = self.service.boost_offered_price(
            user["id"], contact_res["request_id"], 470.0
        )
        self.assertEqual(boosted["offered_cost_inr"], 470.0)
        self.assertEqual(boosted["status"], "incentivized")

        # 5. Check History
        history = self.service.get_user_history(user["id"])
        self.assertEqual(len(history["as_customer"]), 1)

        # 6. Delete Account permanently erases user, listing, and history
        self.service.delete_account_permanently(user["id"])
        self.assertIsNone(self.service.get_user_by_token(token))



    def test_nearby_ev_stations_sorted_by_distance(self):
        stations = self.service.list_nearby_ev_stations(30.7418, 76.7825)
        self.assertTrue(len(stations) >= 5)
        distances = [s["distance_km"] for s in stations]
        self.assertEqual(distances, sorted(distances))
        self.assertIn("connectors", stations[0])
        self.assertIn("power_kw", stations[0])


if __name__ == "__main__":
    unittest.main()
