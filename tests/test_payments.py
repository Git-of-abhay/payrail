import os
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest


# IMPORTANT:
# Tests must never touch the real PayRail database.
os.environ["PGDATABASE"] = "payrail_test_db"

from server import app


def db_connection():
    return psycopg.connect(
        dbname="payrail_test_db",
        user=os.getenv("PGUSER"),
        password=os.getenv("PGPASSWORD"),
        host=os.getenv("PGHOST"),
        port=os.getenv("PGPORT"),
    )


def get_balances():
    conn = db_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT name, balance FROM accounts ORDER BY name;"
    )

    rows = cursor.fetchall()

    cursor.close()
    conn.close()

    return {name: balance for name, balance in rows}


@pytest.fixture(autouse=True)
def reset_database():
    """
    Every test starts from exactly the same state.
    """

    conn = db_connection()
    cursor = conn.cursor()

    cursor.execute(
        "TRUNCATE accounts RESTART IDENTITY;"
    )

    cursor.execute(
        """
        INSERT INTO accounts (name, balance)
        VALUES
            ('abhay', 100000),
            ('rahul', 100000);
        """
    )

    conn.commit()

    cursor.close()
    conn.close()


def test_home_page_loads():
    client = app.test_client()

    response = client.get("/")

    assert response.status_code == 200
    assert b"PayRail" in response.data


def test_zero_amount_is_rejected():
    client = app.test_client()

    response = client.post(
        "/payments",
        json={
            "sender": "abhay",
            "receiver": "rahul",
            "amount": 0,
        },
    )

    assert response.status_code == 400
    assert response.get_json()["status"] == "failed"


def test_negative_amount_is_rejected():
    client = app.test_client()

    response = client.post(
        "/payments",
        json={
            "sender": "abhay",
            "receiver": "rahul",
            "amount": -100,
        },
    )

    assert response.status_code == 400

    assert get_balances() == {
        "abhay": 100000,
        "rahul": 100000,
    }


def test_float_amount_is_rejected():
    client = app.test_client()

    response = client.post(
        "/payments",
        json={
            "sender": "abhay",
            "receiver": "rahul",
            "amount": 1.5,
        },
    )

    assert response.status_code == 400


def test_missing_amount_is_rejected():
    client = app.test_client()

    response = client.post(
        "/payments",
        json={
            "sender": "abhay",
            "receiver": "rahul",
        },
    )

    assert response.status_code == 400


def test_missing_receiver_is_rejected():
    client = app.test_client()

    response = client.post(
        "/payments",
        json={
            "sender": "abhay",
            "amount": 500,
        },
    )

    assert response.status_code == 400


def test_unknown_sender_is_rejected():
    client = app.test_client()

    response = client.post(
        "/payments",
        json={
            "sender": "ghost",
            "receiver": "rahul",
            "amount": 500,
        },
    )

    assert response.status_code == 400

    assert get_balances() == {
        "abhay": 100000,
        "rahul": 100000,
    }


def test_unknown_receiver_is_rejected():
    client = app.test_client()

    response = client.post(
        "/payments",
        json={
            "sender": "abhay",
            "receiver": "ghost",
            "amount": 500,
        },
    )

    assert response.status_code == 400

    assert get_balances() == {
        "abhay": 100000,
        "rahul": 100000,
    }


def test_insufficient_balance_is_rejected():
    client = app.test_client()

    response = client.post(
        "/payments",
        json={
            "sender": "abhay",
            "receiver": "rahul",
            "amount": 100001,
        },
    )

    assert response.status_code == 400

    assert get_balances() == {
        "abhay": 100000,
        "rahul": 100000,
    }


def test_valid_payment_moves_exact_amount():
    client = app.test_client()

    response = client.post(
        "/payments",
        json={
            "sender": "abhay",
            "receiver": "rahul",
            "amount": 25000,
        },
    )

    assert response.status_code == 200

    balances = get_balances()

    assert balances["abhay"] == 75000
    assert balances["rahul"] == 125000

    # Money must never appear or disappear.
    assert sum(balances.values()) == 200000


def test_payment_is_visible_from_new_db_connection():
    client = app.test_client()

    response = client.post(
        "/payments",
        json={
            "sender": "abhay",
            "receiver": "rahul",
            "amount": 5000,
        },
    )

    assert response.status_code == 200

    # get_balances() creates a completely fresh DB connection.
    balances = get_balances()

    assert balances["abhay"] == 95000
    assert balances["rahul"] == 105000


def test_receiver_failure_rolls_back_sender_debit():
    """
    Force receiver UPDATE to fail using BIGINT overflow.

    Sender debit executes first.
    Receiver credit crashes.
    Entire transaction must roll back.
    """

    max_bigint = 9223372036854775807

    conn = db_connection()
    cursor = conn.cursor()

    cursor.execute(
        "UPDATE accounts SET balance = %s WHERE name = 'rahul';",
        (max_bigint,),
    )

    conn.commit()
    cursor.close()
    conn.close()

    client = app.test_client()

    response = client.post(
        "/payments",
        json={
            "sender": "abhay",
            "receiver": "rahul",
            "amount": 1,
        },
    )

    assert response.status_code == 500

    balances = get_balances()

    # Sender debit must have disappeared because of rollback.
    assert balances["abhay"] == 100000
    assert balances["rahul"] == max_bigint


def make_concurrent_payment():
    # Each thread gets its own Flask test client.
    with app.test_client() as client:
        response = client.post(
            "/payments",
            json={
                "sender": "abhay",
                "receiver": "rahul",
                "amount": 70000,
            },
        )

        return response.status_code


def test_concurrent_spending_allows_only_one_payment():
    """
    Abhay has 100000.

    Two requests each try to spend 70000 simultaneously.

    Together they need 140000, so only one may succeed.
    """

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(make_concurrent_payment),
            executor.submit(make_concurrent_payment),
        ]

        statuses = [future.result() for future in futures]

    assert sorted(statuses) == [200, 400]

    balances = get_balances()

    assert balances["abhay"] == 30000
    assert balances["rahul"] == 170000

    # Core invariant.
    assert sum(balances.values()) == 200000