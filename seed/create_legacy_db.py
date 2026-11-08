"""Seed a messy legacy NBFC lending database in Postgres (PG_* settings from .env).

Drops and recreates branches, customers, loans, payments and loan_events, then
fills them with deterministic Faker (en_IN) data containing deliberate
migration traps: NUMERIC(14,4) money with fractional paise, naive timestamps,
NULL-vs-'' text, Indic/apostrophe/accented names, JSONB, TEXT[] and huge values.

Run from the project root:  python seed/create_legacy_db.py
"""
import json
import os
import random
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from faker import Faker
from psycopg2.extras import Json, execute_values

SEED = 42
AS_OF = datetime(2026, 10, 31, 23, 59, 59)
HISTORY_START = datetime(2021, 1, 1)
FORBIDDEN_PORT = 5432  # unrelated Postgres on this machine; never touch it
COUNTS = {"customers": 2000, "loans": 5000, "payments": 20000, "loan_events": 10000}
PAISE = Decimal("0.0001")
BIG_LOAN_PRINCIPALS = [Decimal("99999999.9999")] * 3 + [Decimal("9999999999.9999")] * 2
BIG_PAYMENT_AMOUNT = Decimal("99999999.9999")
BIG_INCOME = Decimal("99999999.9999")

BRANCHES = [
    ("Bengaluru - Jayanagar", "Bengaluru", "Karnataka"),
    ("Bengaluru - Whitefield", "Bengaluru", "Karnataka"),
    ("Mysuru", "Mysuru", "Karnataka"),
    ("Mangaluru", "Mangaluru", "Karnataka"),
    ("Hubballi", "Hubballi", "Karnataka"),
    ("Belagavi", "Belagavi", "Karnataka"),
    ("Davanagere", "Davanagere", "Karnataka"),
    ("Shivamogga", "Shivamogga", "Karnataka"),
    ("Mumbai - Andheri", "Mumbai", "Maharashtra"),
    ("Pune - Hadapsar", "Pune", "Maharashtra"),
    ("Nagpur", "Nagpur", "Maharashtra"),
    ("Lucknow", "Lucknow", "Uttar Pradesh"),
    ("Kanpur", "Kanpur", "Uttar Pradesh"),
    ("Varanasi", "Varanasi", "Uttar Pradesh"),
    ("Jaipur", "Jaipur", "Rajasthan"),
    ("Indore", "Indore", "Madhya Pradesh"),
    ("Bhopal", "Bhopal", "Madhya Pradesh"),
    ("Patna", "Patna", "Bihar"),
    ("Chennai - T Nagar", "Chennai", "Tamil Nadu"),
    ("Hyderabad - Ameerpet", "Hyderabad", "Telangana"),
]

KANNADA_NAMES = ["ಅನಿಲ್ ಕುಮಾರ್", "ಲಕ್ಷ್ಮಿ ದೇವಿ", "ಮಂಜುನಾಥ ಗೌಡ", "ಶ್ರೀನಿವಾಸ ರಾವ್", "ಕಾವ್ಯ ಹೆಗ್ಡೆ", "ರಮೇಶ್ ಶೆಟ್ಟಿ"]
HINDI_NAMES = ["राहुल शर्मा", "प्रिया सिंह", "अमित कुमार वर्मा", "सुनीता देवी", "विकास यादव", "अंजलि गुप्ता"]
APOSTROPHE_NAMES = ["Joseph D'Souza", "Maria D'Costa", "Neil D'Mello", "Sean O'Brien", "Clara D'Sa", "Anand D'Silva"]
ACCENTED_NAMES = ["José Fernandes", "Renée Pereira", "André Gonçalves", "Zoë Rodrigues", "Inês D'Souza", "François Pinto"]

# loan_type -> (min principal, max principal, min rate, max rate)
LOAN_TYPES = {
    "GOLD": (10000, 500000, 9.5, 18.0),
    "PERSONAL": (25000, 1500000, 12.0, 26.0),
    "TWO_WHEELER": (40000, 200000, 10.5, 19.0),
    "MSME": (200000, 5000000, 14.0, 24.0),
    "HOME": (800000, 7500000, 8.5, 12.5),
    "MICROFINANCE": (10000, 80000, 20.0, 28.0),
}
LOAN_STATUSES = (["ACTIVE", "CLOSED", "OVERDUE", "WRITTEN_OFF"], [55, 25, 15, 5])
TENURES = [6, 12, 18, 24, 36, 48, 60]
TAGS = ["top-up", "priority", "co-applicant", "women's scheme", "rural",
        "gold, 22k", "NPA-watch", "pre-approved", "ಗ್ರಾಮೀಣ"]
REMARKS = ["Customer requested EMI date change", "Settled under OTS scheme",
           "Restructured – COVID moratorium", "Field visit done", "Legal notice sent"]
PAYMENT_MODES = ["UPI", "NACH", "CASH", "NEFT", "Cheque", "upi"]
PAYMENT_STATUSES = (["SUCCESS", "FAILED", "BOUNCED"], [85, 8, 7])
OCCUPATIONS = ["Salaried", "Self-employed", "Farmer", "Shop owner", "Driver", "Teacher"]
EVENT_TYPES = ["EMI_BOUNCE", "RESTRUCTURE", "STATUS_CHANGE", "KYC_UPDATE"]

DDL = """
DROP TABLE IF EXISTS loan_events, payments, loans, customers, branches CASCADE;

CREATE TABLE branches (
    branch_id     INTEGER PRIMARY KEY,
    branch_code   VARCHAR(10) NOT NULL UNIQUE,
    branch_name   TEXT NOT NULL,
    city          TEXT NOT NULL,
    state         TEXT NOT NULL,
    ifsc_code     TEXT,
    manager_name  TEXT,
    opened_on     DATE NOT NULL,
    created_at    TIMESTAMP WITHOUT TIME ZONE NOT NULL
);

CREATE TABLE customers (
    customer_id     INTEGER PRIMARY KEY,
    branch_id       INTEGER NOT NULL REFERENCES branches(branch_id),
    full_name       TEXT NOT NULL,
    pan_number      CHAR(10),
    phone           TEXT NOT NULL,
    alt_phone       TEXT,
    email           TEXT,
    address         TEXT,
    occupation      TEXT,
    monthly_income  NUMERIC(14,4),
    date_of_birth   DATE,
    created_at      TIMESTAMP WITHOUT TIME ZONE NOT NULL
);

CREATE TABLE loans (
    loan_id           INTEGER PRIMARY KEY,
    customer_id       INTEGER NOT NULL REFERENCES customers(customer_id),
    branch_id         INTEGER NOT NULL REFERENCES branches(branch_id),
    loan_type         TEXT NOT NULL,
    principal_amount  NUMERIC(14,4) NOT NULL,
    interest_rate     NUMERIC(6,3) NOT NULL,
    tenure_months     INTEGER NOT NULL,
    emi_amount        NUMERIC(14,4) NOT NULL,
    status            TEXT NOT NULL,
    disbursed_at      TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    next_due_date     DATE,
    tags              TEXT[],
    remarks           TEXT,
    created_at        TIMESTAMP WITHOUT TIME ZONE NOT NULL
);

CREATE TABLE payments (
    payment_id    BIGINT PRIMARY KEY,
    loan_id       INTEGER NOT NULL REFERENCES loans(loan_id),
    amount        NUMERIC(14,4) NOT NULL,
    payment_time  TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    payment_mode  TEXT NOT NULL,
    status        TEXT NOT NULL,
    reference_no  TEXT,
    created_at    TIMESTAMP WITHOUT TIME ZONE NOT NULL
);

CREATE TABLE loan_events (
    event_id    BIGINT PRIMARY KEY,
    loan_id     INTEGER NOT NULL REFERENCES loans(loan_id),
    event_type  TEXT NOT NULL,
    event_data  JSONB,
    created_at  TIMESTAMP WITHOUT TIME ZONE NOT NULL
);

CREATE INDEX idx_customers_branch ON customers(branch_id);
CREATE INDEX idx_loans_customer   ON loans(customer_id);
CREATE INDEX idx_loans_branch     ON loans(branch_id);
CREATE INDEX idx_payments_loan    ON payments(loan_id);
CREATE INDEX idx_events_loan      ON loan_events(loan_id);
"""

COLUMNS = {
    "branches": ["branch_id", "branch_code", "branch_name", "city", "state",
                 "ifsc_code", "manager_name", "opened_on", "created_at"],
    "customers": ["customer_id", "branch_id", "full_name", "pan_number", "phone",
                  "alt_phone", "email", "address", "occupation", "monthly_income",
                  "date_of_birth", "created_at"],
    "loans": ["loan_id", "customer_id", "branch_id", "loan_type", "principal_amount",
              "interest_rate", "tenure_months", "emi_amount", "status", "disbursed_at",
              "next_due_date", "tags", "remarks", "created_at"],
    "payments": ["payment_id", "loan_id", "amount", "payment_time", "payment_mode",
                 "status", "reference_no", "created_at"],
    "loan_events": ["event_id", "loan_id", "event_type", "event_data", "created_at"],
}


# ---------------------------------------------------------------- settings / db

def read_pg_settings() -> dict:
    """Read PG_* settings from .env and refuse to target the forbidden port."""
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    keys = ["PG_HOST", "PG_PORT", "PG_USER", "PG_PASSWORD", "PG_DB"]
    missing = [k for k in keys if not os.getenv(k)]
    if missing:
        sys.exit(f"Missing settings in .env: {', '.join(missing)}")
    port = int(os.environ["PG_PORT"])
    if port == FORBIDDEN_PORT:
        sys.exit(f"PG_PORT={port} is the unrelated Postgres. Refusing to connect.")
    return {"host": os.environ["PG_HOST"], "port": port, "user": os.environ["PG_USER"],
            "password": os.environ["PG_PASSWORD"], "dbname": os.environ["PG_DB"]}


def insert_rows(cur, table: str, rows: list[dict]) -> None:
    """Bulk insert dict rows into a table using the column order in COLUMNS."""
    cols = COLUMNS[table]
    values = [tuple(r[c] for c in cols) for r in rows]
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES %s"
    execute_values(cur, sql, values, page_size=1000)


# ---------------------------------------------------------------- value helpers

def money(rng: random.Random, low: float, high: float) -> Decimal:
    """Random amount with 4 decimal places (fractional paise)."""
    raw = Decimal(rng.randint(int(low * 10000), int(high * 10000))) / 10000
    return raw.quantize(PAISE)


def messy_text(rng: random.Random, value, p_null: float = 0.15, p_empty: float = 0.10):
    """Return NULL, '' or the value, so NULL and empty string are mixed."""
    roll = rng.random()
    if roll < p_null:
        return None
    if roll < p_null + p_empty:
        return ""
    return value


def random_ts(rng: random.Random, start: datetime, end: datetime) -> datetime:
    """Random naive timestamp (with microseconds) between start and end."""
    span = int((end - start).total_seconds() * 1_000_000)
    return start + timedelta(microseconds=rng.randint(0, max(span, 0)))


def customer_name(rng: random.Random, fake: Faker) -> str:
    """Mostly Faker names, with some Kannada, Hindi, apostrophe and accented names."""
    roll = rng.random()
    for threshold, pool in ((0.06, KANNADA_NAMES), (0.10, HINDI_NAMES),
                            (0.14, APOSTROPHE_NAMES), (0.17, ACCENTED_NAMES)):
        if roll < threshold:
            return rng.choice(pool)
    return fake.name()


def pan_number(rng: random.Random) -> str:
    """Fake PAN in the AAAPA9999A shape (4th char P = individual)."""
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    head = "".join(rng.choice(letters) for _ in range(3)) + "P" + rng.choice(letters)
    return head + f"{rng.randint(0, 9999):04d}" + rng.choice(letters)


def emi(principal: Decimal, annual_rate: Decimal, months: int) -> Decimal:
    """Standard reducing-balance EMI, rounded to 4 decimals."""
    r = float(annual_rate) / 12 / 100
    factor = (1 + r) ** months
    value = float(principal) * r * factor / (factor - 1)
    return Decimal(str(round(value, 4))).quantize(PAISE)


def as_json(obj):
    """Wrap a Python object for JSONB, keeping Unicode unescaped."""
    return Json(obj, dumps=lambda o: json.dumps(o, ensure_ascii=False))


# ---------------------------------------------------------------- generators

def make_branches(rng: random.Random, fake: Faker) -> list[dict]:
    """20 branches across Karnataka and other states."""
    rows = []
    for i, (name, city, state) in enumerate(BRANCHES, start=1):
        opened = random_ts(rng, datetime(2010, 1, 1), datetime(2020, 12, 31))
        rows.append({
            "branch_id": i,
            "branch_code": f"BR{i:03d}",
            "branch_name": name,
            "city": city,
            "state": state,
            "ifsc_code": messy_text(rng, f"NBFC0{i:06d}", 0.2, 0.15),
            "manager_name": messy_text(rng, fake.name(), 0.1, 0.1),
            "opened_on": opened.date(),
            "created_at": opened,
        })
    return rows


def make_customers(rng: random.Random, fake: Faker, branches: list[dict]) -> list[dict]:
    """Customers with messy contact fields and a few huge incomes."""
    rows = []
    big_income_ids = set(rng.sample(range(1, COUNTS["customers"] + 1), 3))
    for cid in range(1, COUNTS["customers"] + 1):
        income = BIG_INCOME if cid in big_income_ids else money(rng, 12000, 350000)
        rows.append({
            "customer_id": cid,
            "branch_id": rng.choice(branches)["branch_id"],
            "full_name": customer_name(rng, fake),
            "pan_number": messy_text(rng, pan_number(rng), 0.05, 0.0),
            "phone": fake.phone_number(),
            "alt_phone": messy_text(rng, fake.phone_number(), 0.4, 0.25),
            "email": messy_text(rng, fake.email()),
            "address": messy_text(rng, fake.address().replace("\n", ", "), 0.05, 0.05),
            "occupation": messy_text(rng, rng.choice(OCCUPATIONS), 0.1, 0.1),
            "monthly_income": None if rng.random() < 0.08 else income,
            "date_of_birth": fake.date_of_birth(minimum_age=21, maximum_age=70),
            "created_at": random_ts(rng, HISTORY_START, AS_OF - timedelta(days=60)),
        })
    return rows


def loan_tags(rng: random.Random):
    """NULL, empty array, or 1-3 tags (some with commas/quotes/Kannada)."""
    roll = rng.random()
    if roll < 0.20:
        return None
    if roll < 0.30:
        return []
    return rng.sample(TAGS, rng.randint(1, 3))


def next_due(rng: random.Random, status: str):
    """Next EMI due date relative to AS_OF; past for OVERDUE, none for closed."""
    today = AS_OF.date()
    if status == "ACTIVE":
        return today + timedelta(days=rng.randint(1, 30))
    if status == "OVERDUE":
        return today - timedelta(days=rng.randint(1, 180))
    return None


def make_loans(rng: random.Random, customers: list[dict]) -> list[dict]:
    """Loans with NUMERIC money, TEXT[] tags and a few huge principals."""
    rows = []
    big = dict(zip(rng.sample(range(1, COUNTS["loans"] + 1), len(BIG_LOAN_PRINCIPALS)),
                   BIG_LOAN_PRINCIPALS))
    for lid in range(1, COUNTS["loans"] + 1):
        cust = rng.choice(customers)
        loan_type = rng.choice(list(LOAN_TYPES))
        lo, hi, rlo, rhi = LOAN_TYPES[loan_type]
        principal = big.get(lid) or money(rng, lo, hi)
        rate = Decimal(str(round(rng.uniform(rlo, rhi), 3)))
        tenure = rng.choice(TENURES)
        status = rng.choices(*LOAN_STATUSES)[0]
        disbursed = random_ts(rng, max(cust["created_at"], HISTORY_START),
                              AS_OF - timedelta(days=30))
        rows.append({
            "loan_id": lid,
            "customer_id": cust["customer_id"],
            "branch_id": cust["branch_id"],
            "loan_type": loan_type,
            "principal_amount": principal,
            "interest_rate": rate,
            "tenure_months": tenure,
            "emi_amount": emi(principal, rate, tenure),
            "status": status,
            "disbursed_at": disbursed,
            "next_due_date": next_due(rng, status),
            "tags": loan_tags(rng),
            "remarks": messy_text(rng, rng.choice(REMARKS), 0.4, 0.2),
            "created_at": disbursed - timedelta(days=rng.randint(1, 10),
                                                seconds=rng.randint(0, 86399)),
        })
    return rows


def payment_amount(rng: random.Random, emi_amount: Decimal) -> Decimal:
    """Usually the exact EMI, sometimes a partial or excess payment."""
    if rng.random() < 0.6:
        return emi_amount
    factor = Decimal(str(round(rng.uniform(0.3, 1.2), 4)))
    return max((emi_amount * factor).quantize(PAISE), Decimal("1.0000"))


def payment_reference(rng: random.Random, mode: str):
    """UTR/cheque references, mostly missing (NULL or '') for cash."""
    if mode == "CASH":
        return messy_text(rng, None, 0.5, 0.5)
    return messy_text(rng, f"UTR{rng.randint(10**11, 10**12 - 1)}", 0.05, 0.05)


def make_payments(rng: random.Random, loans: list[dict]) -> list[dict]:
    """Payments after disbursal, ids assigned in payment_time order."""
    rows = []
    big_idx = set(rng.sample(range(COUNTS["payments"]), 3))
    for i in range(COUNTS["payments"]):
        loan = rng.choice(loans)
        mode = rng.choice(PAYMENT_MODES)
        paid_at = random_ts(rng, loan["disbursed_at"] + timedelta(days=1), AS_OF)
        rows.append({
            "loan_id": loan["loan_id"],
            "amount": BIG_PAYMENT_AMOUNT if i in big_idx
            else payment_amount(rng, loan["emi_amount"]),
            "payment_time": paid_at,
            "payment_mode": mode,
            "status": rng.choices(*PAYMENT_STATUSES)[0],
            "reference_no": payment_reference(rng, mode),
            "created_at": paid_at + timedelta(seconds=rng.randint(0, 120)),
        })
    rows.sort(key=lambda r: r["payment_time"])
    for pid, row in enumerate(rows, start=1):
        row["payment_id"] = pid
    return rows


def event_payload(rng: random.Random, fake: Faker, event_type: str, loan: dict):
    """Varied JSONB payloads: nested objects, arrays, nulls, numbers-as-strings."""
    if event_type == "DISBURSED":
        return {"amount": str(loan["principal_amount"]), "channel": rng.choice(["branch", "app"]),
                "disbursed_by": fake.name(),
                "bank": {"name": rng.choice(["SBI", "Canara Bank", "HDFC"]),
                         "ifsc": f"SBIN0{rng.randint(10000, 99999)}"}}
    if event_type == "EMI_BOUNCE":
        return {"amount": float(loan["emi_amount"]), "attempt": rng.randint(1, 3),
                "reason": rng.choice(["INSUFFICIENT_FUNDS", "ACCOUNT_CLOSED", None]),
                "charges": 590.0}
    if event_type == "RESTRUCTURE":
        return {"old_tenure": loan["tenure_months"], "new_tenure": loan["tenure_months"] + 12,
                "reason": "COVID moratorium", "approved_by": [fake.name(), fake.name()]}
    if event_type == "STATUS_CHANGE":
        return {"from": "ACTIVE", "to": loan["status"], "note": rng.choice(["", None, "auto"])}
    return {"fields": rng.sample(["address", "phone", "email", "pan"], 2),
            "verified": rng.random() < 0.8,
            "remarks": rng.choice(["ಪರಿಶೀಲಿಸಲಾಗಿದೆ", "सत्यापित", "Verified – OK"])}


def make_event(rng: random.Random, fake: Faker, loan: dict, event_type: str) -> dict:
    """One loan event; ~3% NULL payloads and ~2% empty-object payloads."""
    roll = rng.random()
    data = None if roll < 0.03 else {} if roll < 0.05 else event_payload(rng, fake, event_type, loan)
    when = (loan["disbursed_at"] if event_type == "DISBURSED"
            else random_ts(rng, loan["disbursed_at"], AS_OF))
    return {"loan_id": loan["loan_id"], "event_type": event_type,
            "event_data": None if data is None else as_json(data), "created_at": when}


def make_loan_events(rng: random.Random, fake: Faker, loans: list[dict]) -> list[dict]:
    """A DISBURSED event per loan, then random other events up to the target count."""
    rows = [make_event(rng, fake, loan, "DISBURSED") for loan in loans]
    while len(rows) < COUNTS["loan_events"]:
        rows.append(make_event(rng, fake, rng.choice(loans), rng.choice(EVENT_TYPES)))
    rows.sort(key=lambda r: r["created_at"])
    for eid, row in enumerate(rows, start=1):
        row["event_id"] = eid
    return rows


# ---------------------------------------------------------------- main

def build_dataset() -> dict[str, list[dict]]:
    """Generate all tables deterministically (same SEED -> same data)."""
    rng = random.Random(SEED)
    Faker.seed(SEED)
    fake = Faker("en_IN")
    branches = make_branches(rng, fake)
    customers = make_customers(rng, fake, branches)
    loans = make_loans(rng, customers)
    return {
        "branches": branches,
        "customers": customers,
        "loans": loans,
        "payments": make_payments(rng, loans),
        "loan_events": make_loan_events(rng, fake, loans),
    }


def main() -> None:
    settings = read_pg_settings()
    print(f"Connecting to {settings['host']}:{settings['port']}/{settings['dbname']} ...")
    data = build_dataset()
    conn = psycopg2.connect(**settings)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(DDL)
            for table, rows in data.items():  # dict order respects FK dependencies
                insert_rows(cur, table, rows)
                print(f"  {table:<12} {len(rows):>6} rows")
    finally:
        conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
