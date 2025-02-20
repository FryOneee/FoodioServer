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
        # For a list of exceptions thrown, see
        # https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_GetSecretValue.html
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
        else:
            raise HTTPException(status_code=500, detail="Nie można pobrać JWKS z Cognito.")
    return jwks_data

def verify_jwt_token(token: str):
    """
    Weryfikuje podpis JWT, sprawdza 'aud' i 'iss', zwraca payload (claims).
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
        raise HTTPException(status_code=401, detail="Nieprawidłowy token (kid).")

    # Dekodowanie i weryfikacja
    try:
        decoded_token = jwt.decode(
            token,
            public_key,
            audience=COGNITO_APP_CLIENT_ID,  # weryfikacja audience
            issuer=f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{USER_POOL_ID}",
            options={"verify_aud": True}  # weryfikacja aud
        )
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Token niepoprawny: {str(e)}")

    return decoded_token

async def get_current_user(request: Request, creds: HTTPAuthorizationCredentials = Depends(oauth2_scheme)):
    """
    FastAPI dependency – weryfikuje token JWT i zwraca claims (sub, email, itp.).
    """
    token = creds.credentials
    decoded = verify_jwt_token(token)
    return decoded

def get_or_create_user_by_sub(sub: str, email: str) -> int:
    """
    Zwraca ID użytkownika w tabeli "User" na podstawie sub z Cognito.
    Jeśli nie istnieje, tworzy nowego usera i zwraca ID.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Szukamy usera po polu cognito_sub
        cur.execute('SELECT "ID" FROM "User" WHERE cognito_sub = %s', (sub,))
        row = cur.fetchone()
        if row:
            user_id = row[0]
        else:
            # Jeśli nie ma, tworzymy
            cur.execute(
                'INSERT INTO "User"(email, cognito_sub) VALUES (%s, %s) RETURNING "ID"',
                (email, sub)
            )
            user_id = cur.fetchone()[0]
            conn.commit()
        return user_id
    finally:
        cur.close()
        conn.close()

# ------------------------------------------------------------
# 3. Stare endpointy (opcjonalne) - rejestracja, subskrypcje, cele
# ------------------------------------------------------------
# Te endpointy możesz zostawić lub usunąć w zależności od potrzeb.

@app.post("/register")
def register_user(email: str = Form(...), password: str = Form(...)):
    """
    Rejestruje nowego użytkownika w tabeli User (klasycznie, z hasłem).
    Można to pominąć, jeśli używamy wyłącznie Cognito + SIWA.
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Sprawdź czy email już istnieje
        cur.execute('SELECT "ID" FROM "User" WHERE email=%s', (email,))
        existing_user = cur.fetchone()
        if existing_user:
            raise HTTPException(status_code=400, detail="Użytkownik o podanym email już istnieje.")

        # Wstaw nowego użytkownika
        cur.execute("""
            INSERT INTO "User"(email, password)
            VALUES (%s, %s)
            RETURNING "ID"
        """, (email, password))
        new_id = cur.fetchone()[0]
        conn.commit()
        return {"message": "Użytkownik zarejestrowany.", "user_id": new_id}
    except Exception as e:
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

        # Wstaw subskrypcję
        cur.execute("""
            INSERT INTO Subscription(User_ID, subscription_type, start_date, end_date)
            VALUES (%s, %s, %s, %s)
            RETURNING ID
        """, (user_id, subscription_type, start_date, end_date))
        sub_id = cur.fetchone()[0]
        conn.commit()
        return {"message": "Subskrypcja kupiona.", "subscription_id": sub_id}
    except Exception as e:
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

        # Sprawdź czy użytkownik ma już cel
        cur.execute("SELECT ID FROM Goal WHERE User_ID=%s", (user_id,))
        existing_goal = cur.fetchone()

        if existing_goal:
            # Aktualizacja
            goal_id = existing_goal[0]
            cur.execute("""
                UPDATE Goal
                SET kcal=%s, type=%s
                WHERE ID=%s
            """, (kcal, goal_type, goal_id))
            conn.commit()
            return {"message": "Zaktualizowano istniejący cel.", "goal_id": goal_id}
        else:
            # Wstaw nowy cel
            cur.execute("""
                INSERT INTO Goal(User_ID, kcal, type)
                VALUES (%s, %s, %s)
                RETURNING ID
            """, (user_id, kcal, goal_type))
            goal_id = cur.fetchone()[0]
            conn.commit()
            return {"message": "Utworzono nowy cel.", "goal_id": goal_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

# ------------------------------------------------------------
# 4. Endpoint add_meal (TYLKO Cognito) - makra = -1, potem update
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
    Dodawanie posiłku przez zalogowanego użytkownika (Cognito) z wykorzystaniem obrazu:
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
        # 1. Pobieramy user_id z Cognito
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()

        now = datetime.now()
        today = date.today()

        # Sprawdzenie aktywnej subskrypcji
        cur.execute("""
            SELECT COUNT(*) 
            FROM Subscription 
            WHERE User_ID = %s AND start_date <= %s AND end_date >= %s
        """, (user_id, today, today))
        subscription_count = cur.fetchone()[0]
        is_subscribed = subscription_count > 0

        if is_subscribed:
            # Limit: 5 zapytań na godzinę
            time_limit = now - timedelta(hours=1)
            cur.execute("""
                SELECT COUNT(*)
                FROM OpenAI_request
                WHERE User_ID = %s AND date >= %s
            """, (user_id, time_limit))
            count_last_hour = cur.fetchone()[0]
            if count_last_hour >= 5:
                return {"message": "Przekroczono limit zapytań: 5 zapytań na godzinę.", "allowed": False}
        else:
            # Limit: 3 zapytania dziennie
            cur.execute("""
                SELECT COUNT(*)
                FROM OpenAI_request
                WHERE User_ID = %s AND date::date = %s
            """, (user_id, today))
            count_requests = cur.fetchone()[0]
            if count_requests >= 3:
                return {"message": "Przekroczono dzienny limit zapytań do OpenAI.", "allowed": False}

        # 2. Odczyt oryginalnego obrazu
        original_file_contents = image.file.read()

        # Zapis oryginalnego obrazu do S3
        file_name = f"{user_id}_{int(now.timestamp())}_{image.filename}"
        s3.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=file_name,
            Body=original_file_contents
        )

        # 3. Przygotowanie obrazu do wysłania do OpenAI:
        #    - Przeskalowanie obrazu do maksymalnych rozmiarów 512x1024 (dla obrazu pionowego)
        from PIL import Image
        import io

        image_stream = io.BytesIO(original_file_contents)
        img = Image.open(image_stream)

        max_size = (512, 1024)  # maksymalna szerokość 512, maksymalna wysokość 1024
        img_for_openai = img.copy()
        if img_for_openai.width > max_size[0] or img_for_openai.height > max_size[1]:
            img_for_openai.thumbnail(max_size, Image.ANTIALIAS)

        buf = io.BytesIO()
        img_format = img.format if img.format else "PNG"
        img_for_openai.save(buf, format=img_format)
        resized_image_bytes = buf.getvalue()

        # --- Nowe: Zapis przeskalowanego obrazu do S3 oraz generowanie 5-minutowego linku ---
        resized_file_name = f"resized_{file_name}"
        s3.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=resized_file_name,
            Body=resized_image_bytes
        )
        presigned_url = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': S3_BUCKET_NAME, 'Key': resized_file_name},
            ExpiresIn=300  # 5 minut = 300 sekund
        )
        # -----------------------------------------------------------------------------------

        # 4. Wstawienie rekordu posiłku do bazy – zapisujemy klucz oryginalnego obrazu, a nie tymczasowy URL
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

        # 5. Wywołanie zapytania do OpenAI z wykorzystaniem przeskalowanego obrazu
        #    Teraz obraz przekazujemy jako 5-minutowy link do S3
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

        # 6. Parsowanie odpowiedzi z OpenAI – oczekujemy formatu JSON
        try:
            parsed = json.loads(openai_result_text)
            kcal_val = parsed.get("kcal", -1)
            proteins_val = parsed.get("proteins", -1)
            carbs_val = parsed.get("carbs", -1)
            fats_val = parsed.get("fats", -1)
            healthy_index_val = parsed.get("healthy_index", healthy_index)
        except Exception as e:
            kcal_val = -1
            proteins_val = -1
            carbs_val = -1
            fats_val = -1
            healthy_index_val = healthy_index

        # 7. Aktualizacja rekordu posiłku w bazie danymi z OpenAI
        cur.execute("""
            UPDATE Meal
            SET kcal = %s, proteins = %s, carbs = %s, fats = %s, healthy_index = %s
            WHERE ID = %s
        """, (kcal_val, proteins_val, carbs_val, fats_val, healthy_index_val, meal_id))
        conn.commit()

        # 8. Zapis loga zapytania do OpenAI_request
        cur.execute("""
            INSERT INTO OpenAI_request(User_ID, Meal_ID, img_link, date)
            VALUES (%s, %s, %s, %s)
            RETURNING ID
        """, (user_id, meal_id, file_name, now))
        openai_req_id = cur.fetchone()[0]
        conn.commit()

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
# 5. Pobieranie posiłków (Cognito) - secure_meals_by_day
# ------------------------------------------------------------

@app.get("/secure_meals_by_day")
def secure_meals_by_day(current_user: dict = Depends(get_current_user)):
    """
    Pobiera posiłki z podziałem na dni dla zalogowanego użytkownika (Cognito).
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
            # Generujemy presigned URL na podstawie klucza (row[2])
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

        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

# ------------------------------------------------------------
# 6. Edycja posiłku (Cognito) - zmiana pola 'added'
# ------------------------------------------------------------

@app.put("/secure_edit_meal/{meal_id}")
def secure_edit_meal(meal_id: int, current_user: dict = Depends(get_current_user)):
    """
    Edycja posiłku – zmiana pola 'added' na true.
    Endpoint chroniony przez Cognito; edycja możliwa tylko, gdy posiłek
    należy do zalogowanego użytkownika.
    """
    try:
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()

        # Sprawdzamy, czy posiłek istnieje i należy do użytkownika
        cur.execute("SELECT User_ID FROM Meal WHERE ID = %s", (meal_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Posiłek nie został znaleziony.")
        if row[0] != user_id:
            raise HTTPException(status_code=403, detail="Brak uprawnień do edycji tego posiłku.")

        # Aktualizujemy pole 'added' na true
        cur.execute("UPDATE Meal SET added = true WHERE ID = %s", (meal_id,))
        conn.commit()

        return {"message": "Posiłek został zaktualizowany, pole 'added' ustawione na true."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()