from flask import Flask, request, jsonify
import time
import os
import psycopg
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)


database = os.getenv("PGDATABASE")
user = os.getenv("PGUSER")
password = os.getenv("PGPASSWORD")
host = os.getenv("PGHOST")
port = os.getenv("PGPORT")

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/payments", methods=["POST"])
def payment():
    start = time.perf_counter()

    data = request.get_json()

    sender = data.get("sender")
    receiver = data.get("receiver")
    amount = data.get("amount")

    if sender is None or receiver is None or amount is None:
        return jsonify({
            "status": "failed",
            "message": "check for valid details of amount and account"
        }), 400

    if not isinstance(amount, int):
        return jsonify({
            "status": "failed",
            "message": "amount must be an integer number of paise"
        }), 400

    if amount <= 0:
        return jsonify({
            "status": "failed",
            "message": "amount must be greater than zero"
        }), 400

    try:
        conn = psycopg.connect(
            dbname=database,
            user=user,
            password=password,
            host=host,
            port=port
        )

        cursor = conn.cursor()

        # Lock sender row because its balance determines
        # whether this payment is allowed.
        cursor.execute(
            """
            SELECT account_id, balance
            FROM accounts
            WHERE name = %s
            FOR UPDATE;
            """,
            (sender,)
        )

        sender_row = cursor.fetchone()

        if sender_row is None:
            conn.rollback()
            cursor.close()
            conn.close()

            return jsonify({
                "status": "failed",
                "message": "sender not found"
            }), 400

        sender_account_id = sender_row[0]
        sender_balance = sender_row[1]

        cursor.execute(
            """
            SELECT account_id, balance
            FROM accounts
            WHERE name = %s;
            """,
            (receiver,)
        )

        receiver_row = cursor.fetchone()

        if receiver_row is None:
            conn.rollback()
            cursor.close()
            conn.close()

            return jsonify({
                "status": "failed",
                "message": "receiver not found"
            }), 400

        receiver_account_id = receiver_row[0]

        # This balance was read while holding the sender lock.
        if sender_balance < amount:
            conn.rollback()
            cursor.close()
            conn.close()

            return jsonify({
                "status": "failed",
                "message": "insufficient balance"
            }), 400

        cursor.execute(
            """
            UPDATE accounts
            SET balance = balance - %s
            WHERE account_id = %s;
            """,
            (amount, sender_account_id)
        )

        cursor.execute(
            """
            UPDATE accounts
            SET balance = balance + %s
            WHERE account_id = %s;
            """,
            (amount, receiver_account_id)
        )

        conn.commit()

        cursor.close()
        conn.close()

    except Exception as error:
        try:
            conn.rollback()
            cursor.close()
            conn.close()
        except Exception:
            pass

        print("Payment error:", error)

        return jsonify({
            "status": "failed",
            "message": "payment failed"
        }), 500

    end = time.perf_counter()
    etime = (end - start) * 1000

    return jsonify({
        "status": "success",
        "message": "payment successful",
        "etime": etime
    }), 200


if __name__ == "__main__":
    app.run()