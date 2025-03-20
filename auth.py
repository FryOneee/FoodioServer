# auth.py
import requests
from fastapi import HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import logging
from jose import jwt
from config import JWKS_URL, COGNITO_APP_CLIENT_ID, COGNITO_REGION, USER_POOL_ID, APPLE_JWKS_URL, APPLE_CLIENT_ID

logger = logging.getLogger("server_logger")
oauth2_scheme = HTTPBearer()
jwks_data = None
apple_jwks_data = None

def get_jwks():
    global jwks_data
    if not jwks_data:
        resp = requests.get(JWKS_URL)
        if resp.status_code == 200:
            jwks_data = resp.json()
            logger.info("Pobrano klucze JWKS z Cognito.")
        else:
            logger.error("Nie można pobrać JWKS z Cognito, status: %s", resp.status_code)
            raise HTTPException(status_code=500, detail="Nie można pobrać JWKS z Cognito.")
    return jwks_data

def verify_jwt_token(token: str):
    jwks = get_jwks()
    unverified_headers = jwt.get_unverified_header(token)
    kid = unverified_headers.get("kid")

    public_key = None
    for key in jwks["keys"]:
        if key["kid"] == kid:
            public_key = key
            break
    if not public_key:
        logger.error("Nieprawidłowy token (brak odpowiedniego klucza kid)")
        raise HTTPException(status_code=401, detail="Nieprawidłowy token (kid).")
    try:
        decoded_token = jwt.decode(
            token,
            public_key,
            audience=COGNITO_APP_CLIENT_ID,
            issuer=f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{USER_POOL_ID}",
            options={"verify_aud": True}
        )
        logger.info("Token JWT z Cognito został pomyślnie zweryfikowany.")
    except Exception as e:
        logger.error("Token Cognito niepoprawny: %s", e)
        raise HTTPException(status_code=401, detail=f"Token Cognito niepoprawny: {str(e)}")
    return decoded_token

def get_apple_jwks():
    global apple_jwks_data
    if not apple_jwks_data:
        resp = requests.get(APPLE_JWKS_URL)
        if resp.status_code == 200:
            apple_jwks_data = resp.json()
            logger.info("Pobrano klucze JWKS z Apple.")
        else:
            logger.error("Nie można pobrać JWKS z Apple, status: %s", resp.status_code)
            raise HTTPException(status_code=500, detail="Nie można pobrać JWKS z Apple.")
    return apple_jwks_data

def verify_apple_jwt_token(token: str):
    jwks = get_apple_jwks()
    unverified_headers = jwt.get_unverified_header(token)
    kid = unverified_headers.get("kid")
    public_key = None
    for key in jwks["keys"]:
        if key["kid"] == kid:
            public_key = key
            break
    if not public_key:
        logger.error("Nieprawidłowy token (brak odpowiedniego klucza kid) z Apple.")
        raise HTTPException(status_code=401, detail="Nieprawidłowy token (kid) z Apple.")
    try:
        decoded_token = jwt.decode(
            token,
            public_key,
            audience=APPLE_CLIENT_ID,
            issuer="https://appleid.apple.com",
            options={"verify_aud": True}
        )
        logger.info("Token JWT z Apple został pomyślnie zweryfikowany.")
    except Exception as e:
        logger.error("Token z Apple niepoprawny: %s", e)
        raise HTTPException(status_code=401, detail=f"Token z Apple niepoprawny: {str(e)}")
    return decoded_token

async def get_current_user(request: Request, creds: HTTPAuthorizationCredentials = Depends(oauth2_scheme)):
    token = creds.credentials
    try:
        decoded = verify_jwt_token(token)
        return decoded
    except HTTPException as e_cognito:
        logger.info("Weryfikacja tokena jako Cognito nie powiodła się, próba jako Apple...")
        try:
            decoded = verify_apple_jwt_token(token)
            return decoded
        except HTTPException as e_apple:
            logger.error("Token niepoprawny dla Cognito ani Apple: Cognito: %s, Apple: %s", e_cognito.detail,
                         e_apple.detail)
            raise HTTPException(status_code=401, detail="Niepoprawny token.")