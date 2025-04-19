import os
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
_cached_apple_jwt = None
_cached_apple_jwt_exp = 0

# Użyj sandboxu, jeśli zmienna środowiskowa APPLE_USE_SANDBOX jest ustawiona na "true"
USE_SANDBOX = os.getenv("APPLE_USE_SANDBOX", "false").lower() == "true"


def create_apple_jwt() -> str:
    global _cached_apple_jwt, _cached_apple_jwt_exp
    current_time = int(time.time())
    # Reuse the token if it's still valid (with a 60-second buffer before expiration)
    if _cached_apple_jwt and current_time < _cached_apple_jwt_exp - 60:
        return _cached_apple_jwt

    private_key = APPLE_PRIVATE_KEY
    headers = {"alg": "ES256", "kid": APPLE_KEY_ID}
    payload = {
        "iss": APPLE_ISSUER_ID,
        "iat": current_time,
        "exp": current_time + 15777000,
        "aud": "appstoreconnect-v1"
    }
    token = jwt.encode(payload, private_key, algorithm="ES256", headers=headers)
    _cached_apple_jwt = token
    _cached_apple_jwt_exp = current_time + 15777000
    return token


def verify_apple_subscribe_active(receipt_data: str) -> bool:
    # Decode receipt and get transaction ID
    tx_id = decode_apple_receipt(receipt_data)
    token = create_apple_jwt()

    # Production endpoint (zakomentowane)
    # prod_url = f"https://api.storekit.itunes.apple.com/inApps/v1/subscriptions/{tx_id}"

    # Sandbox endpoint
    sandbox_url = f"https://api.storekit-sandbox.itunes.apple.com/inApps/v1/subscriptions/{tx_id}"

    url = sandbox_url if USE_SANDBOX else sandbox_url  # używamy sandbox w testach
    # Aby wrócić do produkcji, ustaw USE_SANDBOX=False lub usuń komentarz powyżej

    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = httpx.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("status") in (0, 3, 4, 5)
    except httpx.HTTPStatusError as e:
        logger.error("Apple API error %s: %s", e.response.status_code, e.response.text)
        return False
    except Exception as e:
        logger.error("Network error: %s", e)
        return False


def check_subscription_add_meal(cur, user_id: int, now: datetime, today: date, original_transaction_id: str) -> bool:
    # If the provided original_transaction_id is "No", treat it as no subscription
    if original_transaction_id == "No":
        return False

    # Retrieve the subscription record for the user
    cur.execute("""
        SELECT ID, isActive, original_transaction_id
        FROM Subscription
        WHERE User_ID = %s
    """, (user_id,))
    row = cur.fetchone()
    subscription_id, is_active, stored_receipt = (row if row else (None, 'N', None))

    # Verify the stored receipt with Apple's API if available
    if stored_receipt:
        if verify_apple_subscribe_active(stored_receipt):
            cur.execute("UPDATE Subscription SET isActive = 'Y' WHERE ID = %s", (subscription_id,))
            is_active = 'Y'
        else:
            cur.execute("UPDATE Subscription SET isActive = 'N' WHERE ID = %s", (subscription_id,))
            is_active = 'N'

    # If a new receipt is provided and differs from the stored one, verify and update or insert a record
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
            is_active = 'Y'
        else:
            is_active = 'N'

    return True if is_active == 'Y' else False


def is_subscription_active(cur, user_id: int) -> bool:
    cur.execute("""
        SELECT isActive
        FROM Subscription
        WHERE User_ID = %s
        ORDER BY ID DESC
        LIMIT 1
    """, (user_id,))
    row = cur.fetchone()
    return True if row and row[0] == 'Y' else False


def decode_apple_receipt(receipt_data: str) -> str:
    try:
        logger.info(f"Długość paragonu: {len(receipt_data)}")
        decoded = base64.b64decode(receipt_data)
        receipt_json = json.loads(decoded)
        tx_id = receipt_json.get("original_transaction_id")
        if not tx_id:
            raise HTTPException(status_code=400, detail="original_transaction_id not found")
        return tx_id
    except (ValueError, json.JSONDecodeError) as e:
        logger.error("Error decoding receipt: %s", e)
        raise HTTPException(status_code=400, detail="Invalid Apple receipt format")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Unexpected decode error: %s", e)
        raise HTTPException(status_code=400, detail="Unable to parse receipt")
