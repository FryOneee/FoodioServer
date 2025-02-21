import logging
from logging.handlers import RotatingFileHandler
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional, List
from datetime import datetime, date, timedelta
import psycopg2
import openai
import boto3
import os
import requests
import json
from jose import jwt
from botocore.exceptions import ClientError
from PIL import Image
import io

logger = logging.getLogger("server_logger")
logger.setLevel(logging.INFO)

# Handler do pliku
file_handler = RotatingFileHandler("server.log", maxBytes=10 * 1024 * 1024, backupCount=5)
file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(file_formatter)
logger.addHandler(file_handler)

# Handler do konsoli
console_handler = logging.StreamHandler()
console_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
console_handler.setFormatter(console_formatter)
logger.addHandler(console_handler)


def get_secret():
    secret_name = "foodio-secrets"
    region_name = "eu-north-1"

    # Create a Secrets Manager client
    session = boto3.session.Session()
    client = session.client(
        service_name='secretsmanager',
        region_name=region_name
    )

    try:
        get_secret_value_response = client.get_secret_value(
            SecretId=secret_name
        )
    except ClientError as e:
        logger.error("Błąd przy pobieraniu sekretów: %s", e)
        raise e

    secret = get_secret_value_response['SecretString']
    return secret


# Wczytanie konfiguracji z AWS Secrets Manager
secrets_data = json.loads(get_secret())

app = FastAPI()

# ------------------------------------------------------------
# 1. Konfiguracja środowiska (OpenAI, Baza, S3, Cognito)
# ------------------------------------------------------------

# -- Ustaw klucz do OpenAI --
openai.api_key = secrets_data.get("OPENAI_API_KEY")

# -- Połączenie z bazą danych PostgreSQL --
DB_HOST = secrets_data.get("DB_HOST")
DB_PORT = secrets_data.get("DB_PORT")
DB_NAME = secrets_data.get("DB_NAME")
DB_USER = secrets_data.get("DB_USER")
DB_PASS = secrets_data.get("DB_PASS")


def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASS
    )


# Funkcja tworząca bazę danych, jeśli nie istnieje
def create_database_if_not_exists():
    try:
        logger.info("Sprawdzanie istnienia bazy danych: %s", DB_NAME)
        # Łączymy się z domyślną bazą 'postgres'
        default_conn = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            database="postgres",
            user=DB_USER,
            password=DB_PASS
        )
        default_conn.autocommit = True
        cur = default_conn.cursor()
        cur.execute(f"SELECT 1 FROM pg_database WHERE datname = '{DB_NAME}';")
        exists = cur.fetchone()
        if not exists:
            logger.info("Baza danych %s nie istnieje. Tworzenie...", DB_NAME)
            cur.execute(f"CREATE DATABASE {DB_NAME};")
            logger.info("Baza danych %s utworzona pomyślnie.", DB_NAME)
        else:
            logger.info("Baza danych %s już istnieje.", DB_NAME)
        cur.close()
        default_conn.close()
    except Exception as e:
        logger.error("Błąd przy tworzeniu bazy danych: %s", e)
        raise


# Funkcja inicjująca schemat bazy danych (tabele, klucze obce, itp.)
def initialize_schema():
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        logger.info("Inicjalizacja schematu bazy danych")
        # Tworzenie tabel z użyciem IF NOT EXISTS
        cur.execute("""
            CREATE TABLE IF NOT EXISTS Goal (
                ID int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                User_ID int NOT NULL,
                kcal int NOT NULL,
                type int NOT NULL
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS Meal (
                ID int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                User_ID int NOT NULL,
                img_link varchar(255) NOT NULL,
                kcal int NOT NULL,
                proteins int NOT NULL,
                carbs int NOT NULL,
                fats int NOT NULL,
                date timestamp NOT NULL,
                healthy_index int NOT NULL,
                latitude decimal(9,6) NOT NULL,
                longitude decimal(9,6) NOT NULL,
                added bool NOT NULL DEFAULT false
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS OpenAI_request (
                ID int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                User_ID int NOT NULL,
                Meal_ID int NOT NULL,
                img_link varchar(255) NOT NULL,
                date timestamp NOT NULL
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS Subscription (
                ID int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                User_ID int NOT NULL,
                subscription_type int NOT NULL,
                start_date date NOT NULL,
                end_date date NOT NULL
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS "User" (
                ID int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                email varchar(255) NOT NULL,
                password varchar(255) NULL
            );
        """)
        # Dodanie kolumny cognito_sub (jeśli nie istnieje)
        try:
            cur.execute('ALTER TABLE "User" ADD COLUMN IF NOT EXISTS cognito_sub varchar(255);')
            logger.info("Kolumna 'cognito_sub' została dodana lub już istnieje w tabeli User.")
        except Exception as e:
            logger.warning("Nie udało się dodać kolumny cognito_sub: %s", e)

        # Dodawanie kluczy obcych – opakowujemy w blok try/except, aby pominąć błędy przy już istniejących ograniczeniach
        alter_commands = [
            """ALTER TABLE Goal ADD CONSTRAINT Goal_User
                FOREIGN KEY (User_ID)
                REFERENCES "User" (ID)
                NOT DEFERRABLE
                INITIALLY IMMEDIATE;""",
            """ALTER TABLE OpenAI_request ADD CONSTRAINT OpenAI_request_Meal
                FOREIGN KEY (Meal_ID)
                REFERENCES Meal (ID)
                NOT DEFERRABLE
                INITIALLY IMMEDIATE;""",
            """ALTER TABLE OpenAI_request ADD CONSTRAINT OpenAI_request_User
                FOREIGN KEY (User_ID)
                REFERENCES "User" (ID)
                NOT DEFERRABLE
                INITIALLY IMMEDIATE;""",
            """ALTER TABLE Subscription ADD CONSTRAINT Subscription_User
                FOREIGN KEY (User_ID)
                REFERENCES "User" (ID)
                NOT DEFERRABLE
                INITIALLY IMMEDIATE;""",
            """ALTER TABLE Meal ADD CONSTRAINT Table_2_User
                FOREIGN KEY (User_ID)
                REFERENCES "User" (ID)
                NOT DEFERRABLE
                INITIALLY IMMEDIATE;"""
        ]
        for cmd in alter_commands:
            try:
                cur.execute(cmd)
            except Exception as e:
                logger.warning("Błąd przy dodawaniu ograniczenia: %s", e)

        conn.commit()
        logger.info("Schemat bazy danych został pomyślnie zainicjowany i jest gotowy do użytku.")
    except Exception as e:
        conn.rollback()
        logger.error("Błąd przy inicjalizacji schematu: %s", e)
    finally:
        cur.close()
        conn.close()


# Rejestracja funkcji inicjujących bazę danych przy starcie aplikacji
@app.on_event("startup")
def initialize_database():
    logger.info("Uruchamianie serwera - inicjalizacja bazy danych")
    try:
        # Próba połączenia – jeśli baza nie istnieje, wystąpi błąd
        conn = get_db_connection()
        conn.close()
        logger.info("Baza danych %s już istnieje.", DB_NAME)
    except psycopg2.OperationalError:
        # Jeśli wystąpił błąd, utwórz bazę danych
        create_database_if_not_exists()
    # Następnie inicjujemy schemat (tabele, klucze)
    initialize_schema()


# -- Połączenie z S3 --
s3 = boto3.client(
    's3',
    aws_access_key_id=secrets_data.get("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=secrets_data.get("AWS_SECRET_ACCESS_KEY"),
    region_name=secrets_data.get("AWS_REGION")
)

S3_BUCKET_NAME = secrets_data.get("S3_BUCKET_NAME")

# ------------------------------------------------------------
# 2. Konfiguracja Cognito (SIWA) – weryfikacja tokena JWT
# ------------------------------------------------------------

COGNITO_REGION = secrets_data.get("COGNITO_REGION")
USER_POOL_ID = secrets_data.get("USER_POOL_ID")
COGNITO_APP_CLIENT_ID = secrets_data.get("COGNITO_APP_CLIENT_ID")
JWKS_URL = f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{USER_POOL_ID}/.well-known/jwks.json"

oauth2_scheme = HTTPBearer()
jwks_data = None


def get_jwks():
    """
    Pobiera klucze publiczne z Cognito (JWKS), trzyma w pamięci (jwks_data),
    by nie pobierać przy każdym wywołaniu.
    """
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
    """
    Weryfikuje podpis JWT, sprawdza 'aud' i 'iss', zwraca payload (claims) dla tokenów Cognito.
    """
    jwks = get_jwks()
    unverified_headers = jwt.get_unverified_header(token)
    kid = unverified_headers.get("kid")

    # Znajdź właściwy klucz w JWKS
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


# ------------------------------------------------------------
# 2a. Konfiguracja Apple (SIWA) – weryfikacja tokena JWT przy użyciu JWK od Apple
# ------------------------------------------------------------

APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
APPLE_CLIENT_ID = secrets_data.get(
    "APPLE_CLIENT_ID")  # Upewnij się, że ten klucz jest zdefiniowany w AWS Secrets Manager

apple_jwks_data = None


def get_apple_jwks():
    """
    Pobiera klucze publiczne z Apple (JWKS) i zapisuje je w pamięci,
    by nie pobierać przy każdym wywołaniu.
    """
    global apple_jwks_data
    if not apple_jwks_data:
        resp = requests.get(APPLE_JWKS_URL)
        if resp.status_code == 200:
            apple_jwks_data = resp.json()
            # logger.info(f"apple jwks: {apple_jwks_data}")
            logger.info("Pobrano klucze JWKS z Apple.")
        else:
            logger.error("Nie można pobrać JWKS z Apple, status: %s", resp.status_code)
            raise HTTPException(status_code=500, detail="Nie można pobrać JWKS z Apple.")
    return apple_jwks_data


def verify_apple_jwt_token(token: str):
    """
    Weryfikuje token JWT otrzymany od Apple poprzez:
    1. Pobranie kluczy JWKS z Apple.
    2. Wybór właściwego klucza na podstawie nagłówka tokena.
    3. Dekodowanie tokena i weryfikację 'aud' oraz 'iss'.
    """
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


# ------------------------------------------------------------
# 2b. Unified dependency – obsługa tokenów Cognito i Apple
# ------------------------------------------------------------
async def get_current_user(request: Request, creds: HTTPAuthorizationCredentials = Depends(oauth2_scheme)):
    """
    FastAPI dependency – próbuje zweryfikować token JWT najpierw jako token Cognito,
    a w przypadku niepowodzenia – jako token Apple.
    """
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


def get_or_create_user_by_sub(sub: str, email: str) -> int:
    """
    Zwraca ID użytkownika w tabeli "User" na podstawie sub (Cognito lub Apple).
    Jeśli nie istnieje, tworzy nowego użytkownika i zwraca ID.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute('SELECT "ID" FROM "User" WHERE cognito_sub = %s', (sub,))
        row = cur.fetchone()
        if row:
            user_id = row[0]
            logger.info("Znaleziono istniejącego użytkownika o sub: %s", sub)
        else:
            cur.execute(
                'INSERT INTO "User"(email, cognito_sub) VALUES (%s, %s) RETURNING "ID"',
                (email, sub)
            )
            user_id = cur.fetchone()[0]
            conn.commit()
            logger.info("Utworzono nowego użytkownika o sub: %s", sub)
        return user_id
    finally:
        cur.close()
        conn.close()


# ------------------------------------------------------------
# 3. Stare endpointy (opcjonalne) - rejestracja, subskrypcje, cele
# ------------------------------------------------------------
@app.post("/register")
def register_user(email: str = Form(...), password: str = Form(...)):
    """
    Rejestruje nowego użytkownika w tabeli User (klasycznie, z hasłem).
    Można to pominąć, jeśli używamy wyłącznie Cognito + SIWA.
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute('SELECT ID FROM "User" WHERE email=%s', (email,))
        existing_user = cur.fetchone()
        if existing_user:
            logger.warning("Próba rejestracji użytkownika z istniejącym emailem: %s", email)
            raise HTTPException(status_code=400, detail="Użytkownik o podanym email już istnieje.")

        cur.execute("""
            INSERT INTO "User"(email, password)
            VALUES (%s, %s)
            RETURNING ID
        """, (email, password))
        new_id = cur.fetchone()[0]
        conn.commit()
        logger.info("Zarejestrowano użytkownika: %s", email)
        return {"message": "Użytkownik zarejestrowany.", "user_id": new_id}
    except Exception as e:
        logger.error("Błąd przy rejestracji użytkownika: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


@app.post("/buy_subscription")
def buy_subscription(user_id: int = Form(...), subscription_type: int = Form(...),
                     start_date: date = Form(...), end_date: date = Form(...)):
    """
    Kupno subskrypcji (klasyczne podejście z user_id).
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO Subscription(User_ID, subscription_type, start_date, end_date)
            VALUES (%s, %s, %s, %s)
            RETURNING ID
        """, (user_id, subscription_type, start_date, end_date))
        sub_id = cur.fetchone()[0]
        conn.commit()
        logger.info("Zakupiono subskrypcję dla user_id: %s", user_id)
        return {"message": "Subskrypcja kupiona.", "subscription_id": sub_id}
    except Exception as e:
        logger.error("Błąd przy zakupie subskrypcji: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


@app.post("/set_goal")
def set_goal(user_id: int = Form(...), kcal: int = Form(...), goal_type: int = Form(...)):
    """
    Ustawienie (lub aktualizacja) celu użytkownika (klasyczne podejście z user_id).
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("SELECT ID FROM Goal WHERE User_ID=%s", (user_id,))
        existing_goal = cur.fetchone()

        if existing_goal:
            goal_id = existing_goal[0]
            cur.execute("""
                UPDATE Goal
                SET kcal=%s, type=%s
                WHERE ID=%s
            """, (kcal, goal_type, goal_id))
            conn.commit()
            logger.info("Zaktualizowano cel dla user_id: %s", user_id)
            return {"message": "Zaktualizowano istniejący cel.", "goal_id": goal_id}
        else:
            cur.execute("""
                INSERT INTO Goal(User_ID, kcal, type)
                VALUES (%s, %s, %s)
                RETURNING ID
            """, (user_id, kcal, goal_type))
            goal_id = cur.fetchone()[0]
            conn.commit()
            logger.info("Utworzono nowy cel dla user_id: %s", user_id)
            return {"message": "Utworzono nowy cel.", "goal_id": goal_id}
    except Exception as e:
        logger.error("Błąd przy ustawianiu celu: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


# ------------------------------------------------------------
# 4. Endpoint add_meal – działający dla tokenów Cognito oraz Apple
# ------------------------------------------------------------
@app.post("/add_meal")
def add_meal(
        current_user: dict = Depends(get_current_user),
        healthy_index: int = Form(...),
        latitude: float = Form(...),
        longitude: float = Form(...),
        image: UploadFile = File(...)
):
    """
    Dodawanie posiłku przez zalogowanego użytkownika (Cognito lub Apple) z wykorzystaniem obrazu:
    1) Zapis oryginalnego obrazu w S3 – klucz obrazu zapisywany jest w bazie,
    2) Utworzenie rekordu w Meal z placeholderem makroskładników (-1),
    3) Wywołanie OpenAI z wykorzystaniem obrazu – obraz przeskalowany do maksymalnych rozmiarów 512x1024,
       dodatkowo wykorzystujemy 5-minutowy link do obrazu z S3.
       Zapytanie sformułowane jest tak, aby otrzymać dane:
       kcal, proteins, carbs, fats oraz healthy_index w formacie JSON,
    4) Aktualizacja rekordu w bazie na podstawie odpowiedzi z OpenAI,
    5) Zapis loga zapytania do OpenAI_request.

    Limity zapytań:
      - dla subskrybentów: 5 zapytań na godzinę,
      - dla użytkowników bez subskrypcji: 3 zapytania dziennie.
    """
    try:
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()

        now = datetime.now()
        today = date.today()

        cur.execute("""
            SELECT COUNT(*) 
            FROM Subscription 
            WHERE User_ID = %s AND start_date <= %s AND end_date >= %s
        """, (user_id, today, today))
        subscription_count = cur.fetchone()[0]
        is_subscribed = subscription_count > 0

        if is_subscribed:
            time_limit = now - timedelta(hours=1)
            cur.execute("""
                SELECT COUNT(*)
                FROM OpenAI_request
                WHERE User_ID = %s AND date >= %s
            """, (user_id, time_limit))
            count_last_hour = cur.fetchone()[0]
            if count_last_hour >= 5:
                logger.info("Limit zapytań na godzinę przekroczony dla user_id: %s", user_id)
                return {"message": "Przekroczono limit zapytań: 5 zapytań na godzinę.", "allowed": False}
        else:
            cur.execute("""
                SELECT COUNT(*)
                FROM OpenAI_request
                WHERE User_ID = %s AND date::date = %s
            """, (user_id, today))
            count_requests = cur.fetchone()[0]
            if count_requests >= 3:
                logger.info("Dzienny limit zapytań przekroczony dla user_id: %s", user_id)
                return {"message": "Przekroczono dzienny limit zapytań do OpenAI.", "allowed": False}

        original_file_contents = image.file.read()
        file_name = f"{user_id}_{int(now.timestamp())}_{image.filename}"
        s3.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=file_name,
            Body=original_file_contents
        )

        image_stream = io.BytesIO(original_file_contents)
        img = Image.open(image_stream)

        max_size = (512, 1024)
        img_for_openai = img.copy()
        if img_for_openai.width > max_size[0] or img_for_openai.height > max_size[1]:
            img_for_openai.thumbnail(max_size, Image.ANTIALIAS)

        buf = io.BytesIO()
        img_format = img.format if img.format else "PNG"
        img_for_openai.save(buf, format=img_format)
        resized_image_bytes = buf.getvalue()

        resized_file_name = f"resized_{file_name}"
        s3.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=resized_file_name,
            Body=resized_image_bytes
        )
        presigned_url = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': S3_BUCKET_NAME, 'Key': resized_file_name},
            ExpiresIn=300
        )

        cur.execute("""
            INSERT INTO Meal(
                User_ID,
                img_link,
                kcal,
                proteins,
                carbs,
                fats,
                date,
                healthy_index,
                latitude,
                longitude
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING ID
        """, (user_id, file_name, -1, -1, -1, -1, now, healthy_index, latitude, longitude))
        meal_id = cur.fetchone()[0]
        conn.commit()

        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": (
                            "Proszę oszacować wartości makroskładników na podstawie obrazu. "
                            "Podaj wynik w formacie JSON, zawierający dokładnie klucze: "
                            "'kcal', 'proteins', 'carbs', 'fats', 'healthy_index'. "
                            "Nie dodawaj żadnego dodatkowego tekstu."
                        )},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": presigned_url,
                            },
                        },
                    ],
                }
            ],
            max_tokens=300,
        )
        openai_result_text = response.choices[0].message["content"]

        try:
            parsed = json.loads(openai_result_text)
            kcal_val = parsed.get("kcal", -1)
            proteins_val = parsed.get("proteins", -1)
            carbs_val = parsed.get("carbs", -1)
            fats_val = parsed.get("fats", -1)
            healthy_index_val = parsed.get("healthy_index", healthy_index)
        except Exception as e:
            logger.error("Błąd przy parsowaniu odpowiedzi z OpenAI: %s", e)
            kcal_val = -1
            proteins_val = -1
            carbs_val = -1
            fats_val = -1
            healthy_index_val = healthy_index

        cur.execute("""
            UPDATE Meal
            SET kcal = %s, proteins = %s, carbs = %s, fats = %s, healthy_index = %s
            WHERE ID = %s
        """, (kcal_val, proteins_val, carbs_val, fats_val, healthy_index_val, meal_id))
        conn.commit()

        cur.execute("""
            INSERT INTO OpenAI_request(User_ID, Meal_ID, img_link, date)
            VALUES (%s, %s, %s, %s)
            RETURNING ID
        """, (user_id, meal_id, file_name, now))
        openai_req_id = cur.fetchone()[0]
        conn.commit()

        logger.info("Dodano posiłek (meal_id: %s) dla user_id: %s", meal_id, user_id)
        return {
            "message": "Dodano posiłek i zaktualizowano dane makroskładników przez OpenAI.",
            "meal_id": meal_id,
            "openai_request_id": openai_req_id,
            "openai_result": openai_result_text,
            "updated_kcal": kcal_val,
            "updated_proteins": proteins_val,
            "updated_carbs": carbs_val,
            "updated_fats": fats_val,
            "updated_healthy_index": healthy_index_val
        }

    except Exception as e:
        logger.error("Błąd przy dodawaniu posiłku: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            cur.close()
        except:
            pass
        try:
            conn.close()
        except:
            pass


# ------------------------------------------------------------
# 5. Pobieranie posiłków (Cognito lub Apple) - secure_meals_by_day
# ------------------------------------------------------------
@app.get("/secure_meals_by_day")
def secure_meals_by_day(current_user: dict = Depends(get_current_user)):
    """
    Pobiera posiłki z podziałem na dni dla zalogowanego użytkownika (Cognito lub Apple).
    Dla każdego posiłku generowany jest tymczasowy (presigned) URL obrazu, ważny przez 1 godzinę.
    """
    try:
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            SELECT date::date, ID, img_link, kcal, proteins, carbs, fats, healthy_index, latitude, longitude
            FROM Meal
            WHERE User_ID = %s
            ORDER BY date DESC
        """, (user_id,))
        rows = cur.fetchall()

        meals_by_day = {}
        for row in rows:
            day = row[0].isoformat()
            presigned_url = s3.generate_presigned_url(
                'get_object',
                Params={'Bucket': S3_BUCKET_NAME, 'Key': row[2]},
                ExpiresIn=3600
            )
            meal_data = {
                "meal_id": row[1],
                "img_link": presigned_url,
                "kcal": row[3],
                "proteins": row[4],
                "carbs": row[5],
                "fats": row[6],
                "healthy_index": row[7],
                "latitude": str(row[8]),
                "longitude": str(row[9])
            }
            if day not in meals_by_day:
                meals_by_day[day] = []
            meals_by_day[day].append(meal_data)

        result = []
        for day, meals in meals_by_day.items():
            result.append({
                "day": day,
                "meals": meals
            })
        logger.info("Pobrano posiłki dla user_id: %s", user_id)
        return result
    except Exception as e:
        logger.error("Błąd przy pobieraniu posiłków: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


# ------------------------------------------------------------
# 6. Edycja posiłku (Cognito lub Apple) - zmiana pola 'added'
# ------------------------------------------------------------
@app.put("/secure_edit_meal/{meal_id}")
def secure_edit_meal(meal_id: int, current_user: dict = Depends(get_current_user)):
    """
    Edycja posiłku – zmiana pola 'added' na true.
    Endpoint chroniony – edycja możliwa tylko, gdy posiłek należy do zalogowanego użytkownika.
    """
    try:
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("SELECT User_ID FROM Meal WHERE ID = %s", (meal_id,))
        row = cur.fetchone()
        if not row:
            logger.warning("Posiłek o ID %s nie został znaleziony.", meal_id)
            raise HTTPException(status_code=404, detail="Posiłek nie został znaleziony.")
        if row[0] != user_id:
            logger.warning("Użytkownik %s próbuje edytować posiłek, który nie należy do niego.", user_id)
            raise HTTPException(status_code=403, detail="Brak uprawnień do edycji tego posiłku.")

        cur.execute("UPDATE Meal SET added = true WHERE ID = %s", (meal_id,))
        conn.commit()
        logger.info("Zaktualizowano posiłek (meal_id: %s) dla user_id: %s", meal_id, user_id)
        return {"message": "Posiłek został zaktualizowany, pole 'added' ustawione na true."}
    except Exception as e:
        logger.error("Błąd przy edycji posiłku: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

# ------------------------------------------------------------
# Schemat bazy danych
# ------------------------------------------------------------
# -- Created by Vertabelo (http://vertabelo.com)
# -- Last modification date: 2025-02-16 03:24:09.208
#
# Powyżej znajdują się polecenia SQL tworzące tabele:
#   Table: Goal
#   Table: Meal
#   Table: OpenAI_request
#   Table: Subscription
#   Table: User
#
# Dodatkowo, dodane zostało ograniczenie kluczy obcych między tabelami.