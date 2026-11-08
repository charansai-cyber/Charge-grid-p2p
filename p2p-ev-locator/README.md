# ChargeGrid P2P — Emergency EV Charging & Station Locator

A Peer-to-Peer (P2P) Emergency EV Charging & Commercial Station Locator built with **Python (FastAPI)**, **SQLite + SQLAlchemy ORM**, **Vanilla HTML5/CSS3/JS**, and **Leaflet.js (OpenStreetMap)**.

---

## 1. Architecture & Project Structure

```text
p2p-ev-locator/
├── .env.example
├── requirements.txt
├── README.md
├── backend/
│   ├── __init__.py
│   ├── database.py
│   ├── models.py
│   ├── schemas.py
│   ├── mailer.py
│   └── main.py
├── frontend/
│   ├── index.html
│   ├── styles.css
│   └── app.js
└── tests/
    └── test_app.py
```

---

## 2. Core Flows Implemented

1. **No Initial Login Wall**: Visitors immediately view the interactive Leaflet map, locate commercial EV stations and nearby P2P host pins (`House`, `Shop`, `Parking Area`), and run the Smart Calculator without logging in.
2. **Triggered Authentication (Gmail + OTP)**: A modal prompts for Gmail + 6-digit OTP verification **only** when a user clicks **"Contact for Charge"**, **"Save Address"**, or **"History"**, and automatically completes their action once verified.
3. **Strict Provider Privacy & 10-Digit Mobile Validation**: Provider phone numbers are validated as strictly 10 digits (`[6-9]\d{9}`) and are **never** returned on public map endpoints (`/api/providers/public`). They unlock only when an authenticated customer clicks **"Contact for Charge"**.
4. **Smart Calculator & Price Boost Negotiation**:
   - `Units Needed (kWh) = Battery Capacity × (Desired % − Current %) ÷ 100`
   - `Total Cost (₹) = (Units Needed × Local Area Unit Cost) + 200 Rupees`
   - Includes one-tap incentive boost chips (`+₹50`, `+₹100`, `+₹200`) and a custom rupee input if the provider refuses the initial calculated amount.
5. **Top-Right Account Menu**: Includes **History**, **Logout**, and **Delete Account** (which displays the strict warning *"This account and all data will be permanently deleted."* and erases all user data on clicking OK).

---

## 3. Running Locally

```bash
# 1. Create a virtual environment and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure environment variables
cp .env.example .env

# 3. Run the automated test suite
python3 -m unittest discover -s tests -p "test_*.py" -v

# 4. Start the FastAPI server
uvicorn backend.main:app --reload --port 8000
```
Open **`http://localhost:8000`** in your browser.
