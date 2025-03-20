# checkSubscription.py
from fastapi import HTTPException
from datetime import date, datetime
import httpx
import jwt
import time
import logging
import base64
import json

from config import (
    APPLE_KEY_ID,
    APPLE_ISSUER_ID,
    APPLE_PRIVATE_KEY
)

logger = logging.getLogger("server_logger")

def create_apple_jwt() -> str:
    private_key = APPLE_PRIVATE_KEY
    headers = {"alg": "ES256", "kid": APPLE_KEY_ID}
    payload = {
        "iss": APPLE_ISSUER_ID,
        "iat": int(time.time()),
        "exp": int(time.time()) + 15777000,
        "aud": "appstoreconnect-v1"
    }
    return jwt.encode(payload, private_key, algorithm="ES256", headers=headers)

def verify_apple_subscribe_active(original_transaction_id: str) -> bool:
    token = create_apple_jwt()
    url = f"https://api.storekit.itunes.apple.com/inApps/v1/subscriptions/{original_transaction_id}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = httpx.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("status") in (0, 3, 4, 5)
    except httpx.HTTPStatusError as e:
        logger.error("Apple Server API error %s: %s", e.response.status_code, e.response.text)
        return False
    except Exception as e:
        logger.error("Network error verifying Apple subscription: %s", e)
        return False

def check_subscription_add_meal(cur, user_id: int, now: datetime, today: date, original_transaction_id: str):
    cur.execute("""
        SELECT ID, isActive, original_transaction_id
        FROM Subscription
        WHERE User_ID = %s
    """, (user_id,))
    row = cur.fetchone()
    subscription_id, is_active, stored_receipt = (row if row else (None, 'N', None))

    # Jeśli istnieje zapis subskrypcji i jest nieaktywny → weryfikuj po stored_receipt
    if stored_receipt and is_active == 'N':
        if verify_apple_subscribe_active(stored_receipt):
            cur.execute("UPDATE Subscription SET isActive = 'Y' WHERE ID = %s", (subscription_id,))
            is_active = 'Y'

    # Jeśli klient przesłał nowy receipt → weryfikuj i zapisuj
    if original_transaction_id and original_transaction_id != stored_receipt:
        if verify_apple_subscribe_active(original_transaction_id):
            if subscription_id:
                cur.execute("""
                    UPDATE Subscription
                    SET original_transaction_id = %s, isActive = 'Y'
                    WHERE ID = %s
                """, (original_transaction_id, subscription_id))
            else:
                cur.execute("""
                    INSERT INTO Subscription(User_ID, original_transaction_id, isActive)
                    VALUES(%s, %s, 'Y')
                """, (user_id, original_transaction_id))
                subscription_id = cur.lastrowid
            is_active = 'Y'

    daily_limit = 50 if is_active == 'Y' else 3
    cur.execute("SELECT COUNT(*) FROM OpenAI_request WHERE User_ID = %s AND date::date = %s", (user_id, today))
    if cur.fetchone()[0] >= daily_limit:
        return {"error": f"Limit {daily_limit} zapytań na dzień wyczerpany.", "status": 429}

    cur.execute("SELECT COUNT(*) FROM OpenAI_request WHERE User_ID = %s", (user_id,))
    total_requests = cur.fetchone()[0]

    if is_active == 'Y' and (total_requests + 1) % 10 == 0:
        receipt_to_check = stored_receipt or original_transaction_id
        if not verify_apple_subscribe_active(receipt_to_check):
            if subscription_id:
                cur.execute("UPDATE Subscription SET isActive = 'N' WHERE ID = %s", (subscription_id,))
            return {"error": "Subskrypcja wygasła wg Apple.", "status": 403}

    return None


def decode_apple_receipt(receipt_data: str) -> str:
    try:
        decoded_bytes = base64.b64decode(receipt_data)
        receipt_json = json.loads(decoded_bytes)
        original_transaction_id = receipt_json.get("original_transaction_id")
        if not original_transaction_id:
            raise HTTPException(status_code=400, detail="original_transaction_id not found in receipt")
        return original_transaction_id
    except (ValueError, json.JSONDecodeError) as e:
        logger.error("Error decoding Apple receipt: %s", e)
        raise HTTPException(status_code=400, detail="Invalid Apple receipt format")
    except Exception as e:
        logger.error("Unexpected error decoding receipt: %s", e)
        raise HTTPException(status_code=400, detail="Unable to parse Apple receipt")