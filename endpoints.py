# endpoints.py
from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Depends, Request
from datetime import datetime, date, timedelta
from psycopg2.extras import RealDictCursor

import json
import re
import io
import logging
from PIL import Image
import openai
import boto3

from auth import get_current_user
from db import get_db_connection, get_or_create_user_by_sub
from config import S3_BUCKET_NAME, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION, OPENAI_API_KEY
from config import USER_POOL_ID


from checkSubscription import check_subscription_add_meal, verify_apple_subscribe_active, decode_apple_receipt
from OpenAI_requests import query_meal_nutrients, new_goal

router = APIRouter()
logger = logging.getLogger("server_logger")

# Inicjalizacja klienta S3 oraz ustawienie klucza OpenAI
s3 = boto3.client(
    's3',
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    region_name=AWS_REGION
)
openai.api_key = OPENAI_API_KEY

@router.post("/register")
def register_user(email: str = Form(...), password: str = Form(...)):
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute('SELECT ID FROM "User" WHERE email=%s', (email,))
        existing_user = cur.fetchone()
        if existing_user:
            logger.warning("Próba rejestracji użytkownika z istniejącym emailem: %s", email)
            raise HTTPException(status_code=400, detail="Użytkownik o podanym email już istnieje.")

        cur.execute("""
            INSERT INTO "User"(email, password,dateOfJoin)
            VALUES (%s, %s,%s)
            RETURNING ID
        """, (email, password,date.today()))
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

@router.post("/delete_account")
def delete_account(current_user: dict = Depends(get_current_user)):
    try:
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            'UPDATE "User" SET email = %s, password = NULL WHERE ID = %s',
            ("foodio.example@gmail.com", user_id)
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="User not found or unauthorized")
        conn.commit()
        # Usuń użytkownika z AWS Cognito User Pool
        cognito_client = boto3.client(
            'cognito-idp',
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            region_name=AWS_REGION
        )
        # Zakładamy, że 'sub' jest używany jako identyfikator użytkownika w Cognito
        cognito_client.admin_delete_user(
            UserPoolId=USER_POOL_ID,
            Username=sub
        )
        return {"message": "Konto zostało usunięte (anonymized)."}
    except Exception as e:
        logger.error("Błąd przy usuwaniu konta: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


@router.post("/buy_subscription")
def buy_subscription(
        current_user: dict = Depends(get_current_user),
        subscription_type: int = Form(...),
        receipt: str = Form(...)
):
    try:
        original_transaction_id = decode_apple_receipt(receipt)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Dodajemy sprawdzenie aktywności subskrypcji
    if not verify_apple_subscribe_active(original_transaction_id):
        raise HTTPException(status_code=403, detail="Subskrypcja nieaktywna wg Apple")

    sub = current_user["sub"]
    email = current_user.get("email", "")
    user_id = get_or_create_user_by_sub(sub, email)

    conn = get_db_connection()

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Upsert subscription
            cur.execute("""
                INSERT INTO Subscription (User_ID, subscription_type, original_transaction_id, isActive)
                VALUES (%s, %s, %s, 'Y')
                ON CONFLICT (User_ID) DO UPDATE
                  SET subscription_type = EXCLUDED.subscription_type,
                      original_transaction_id = EXCLUDED.original_transaction_id,
                      isActive = 'Y';
            """, (user_id, subscription_type, original_transaction_id))
            conn.commit()
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail="DB error: " + str(e))
    finally:
        conn.close()

    return {"success": True, "original_transaction_id": original_transaction_id}


@router.post("/add_meal")
def add_meal(
        current_user: dict = Depends(get_current_user),
        latitude: float = Form(...),
        longitude: float = Form(...),
        apple_receipt: str = Form(...),   # <-- Nowy parametr z paragonem Apple
        image: UploadFile = File(...)
):
    try:
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()

        now = datetime.now()
        today = date.today()

        original_transaction_id = decode_apple_receipt(apple_receipt)

        # --- 1. Sprawdź subskrypcję i ewentualne limity ---
        subscription_response = check_subscription_add_meal(cur, user_id, now, today, original_transaction_id)
        if subscription_response is not None:
            # Jeśli funkcja zwróciła błąd, odsyłamy go do klienta
            conn.rollback()
            return subscription_response

        # --- 2. Zapisujemy obraz do S3, tworzymy miniaturę, pobieramy URL itd. ---
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

        # --- 3. Pobieramy kontekst użytkownika z bazy (problemy, dietę) ---
        cur.execute("SELECT description FROM Problem WHERE User_ID = %s LIMIT 7", (user_id,))
        problems_rows = cur.fetchall()
        user_problems = [row[0] for row in problems_rows] if problems_rows else []

        cur.execute("SELECT diet FROM Goal WHERE User_ID = %s ORDER BY startDate DESC LIMIT 1", (user_id,))
        diet_row = cur.fetchone()
        user_diet = diet_row[0] if diet_row else ""

        user_context = {
            "problems": user_problems,
            "diet": user_diet
        }

        # --- 4. Wywołujemy ChatGPT (query_meal_nutrients) ---
        nutrients_dict, openai_result_text = query_meal_nutrients(presigned_url, user_context)
        name_val = nutrients_dict.get("name", "dish")
        kcal_val = nutrients_dict.get("kcal", -1)
        proteins_val = nutrients_dict.get("proteins", -1)
        carbs_val = nutrients_dict.get("carbs", -1)
        fats_val = nutrients_dict.get("fats", -1)
        healthy_index_val = nutrients_dict.get("healthy_index", -1)
        problems_val = nutrients_dict.get("problems", [])

        # --- 5. Dodaj nowe problemy (jeśli ChatGPT wykrył) i zwróć id ---
        problems_with_id = []
        for problem in problems_val:
            cur.execute("INSERT INTO Problem (User_ID, description) VALUES (%s, %s) RETURNING ID", (user_id, problem))
            problem_id = cur.fetchone()[0]
            problems_with_id.append({"id": problem_id, "description": problem})
        conn.commit()

        # --- 6. Wstawiamy rekord do tabeli Meal ---
        cur.execute("""
            INSERT INTO Meal(
                User_ID,
                bar_code,
                img_link,
                kcal,
                proteins,
                carbs,
                fats,
                date,
                healthy_index,
                latitude,
                longitude,
                added
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING ID
        """, (
            user_id, name_val, file_name, kcal_val,
            proteins_val, carbs_val, fats_val,
            now, healthy_index_val, latitude, longitude, False
        ))
        meal_id = cur.fetchone()[0]
        conn.commit()

        # Odczyt nowego posiłku
        cur.execute("""
            SELECT ID, bar_code, img_link, kcal, proteins, carbs, fats, healthy_index, latitude, longitude, date, added
            FROM Meal WHERE ID = %s
        """, (meal_id,))
        updated_meal = cur.fetchone()
        meal_data = {
            "id": updated_meal[0],
            "bar_code": updated_meal[1],
            "img_link": updated_meal[2],
            "kcal": updated_meal[3],
            "proteins": updated_meal[4],
            "carbs": updated_meal[5],
            "fats": updated_meal[6],
            "healthy_index": updated_meal[7],
            "latitude": str(updated_meal[8]),
            "longitude": str(updated_meal[9]),
            "date": updated_meal[10].isoformat() if isinstance(updated_meal[10], datetime) else updated_meal[10],
            "added": updated_meal[11]
        }
        # Pobierz warningi dla nowego posiłku
        cur.execute("SELECT warning FROM Warning WHERE Meal_ID = %s", (meal_id,))
        warning_rows = cur.fetchall()
        warnings = [row[0] for row in warning_rows] if warning_rows else []
        meal_data["warnings"] = warnings

        # --- 7. Na koniec wstawiamy rekord do OpenAI_request (zliczanie zapytań) ---
        cur.execute("""
            INSERT INTO OpenAI_request(User_ID, type, img_link, date)
            VALUES (%s, %s, %s, %s)
            RETURNING ID
        """, (user_id, 'M', file_name, now))
        openai_req_id = cur.fetchone()[0]
        conn.commit()

        logger.info("Dodano posiłek (meal_id: %s) dla user_id: %s", meal_id, user_id)
        return {
            "message": "Dodano posiłek i zaktualizowano dane makroskładników przez OpenAI.",
            "meal": meal_data,
            "warnings": meal_data["warnings"],
            "openai_request_id": openai_req_id,
            "openai_result": openai_result_text,
        "extracted_problems": problems_with_id
        }

    except Exception as e:
        logger.error("Błąd przy dodawaniu posiłku: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


@router.get("/secure_meals_by_day")
def secure_meals_by_day(current_user: dict = Depends(get_current_user)):
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

@router.get("/secure_meals_by_day/detailed")
def secure_meals_detailed(current_user: dict = Depends(get_current_user)):
    try:
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()

        # Pobranie posiłków dla użytkownika
        cur.execute("SELECT * FROM Meal WHERE User_ID = %s ORDER BY date DESC", (user_id,))
        rows = cur.fetchall()

        # Pobranie identyfikatorów posiłków
        meal_ids = [row[0] for row in rows]

        # Pobranie ostrzeżeń dla posiłków
        warnings_dict = {}
        if meal_ids:
            cur.execute("SELECT Meal_ID, warning FROM Warning WHERE Meal_ID = ANY(%s)", (meal_ids,))
            warning_rows = cur.fetchall()
            for meal_id, warning in warning_rows:
                if meal_id not in warnings_dict:
                    warnings_dict[meal_id] = []
                warnings_dict[meal_id].append(warning)

        meals_by_day = {}
        for row in rows:
            meal_date = row[8]  # kolumna date
            day_str = meal_date.date().isoformat() if isinstance(meal_date, datetime) else str(meal_date)
            presigned_url = s3.generate_presigned_url(
                'get_object',
                Params={'Bucket': S3_BUCKET_NAME, 'Key': row[3]},  # kolumna img_link
                ExpiresIn=3600
            )
            meal_id = row[0]
            meal_data = {
                "id": meal_id,
                "user_id": row[1],
                "bar_code": row[2],  # zamiast meal_name
                "img_link": presigned_url,
                "kcal": row[4],
                "proteins": row[5],
                "carbs": row[6],
                "fats": row[7],
                "date": meal_date.isoformat() if isinstance(meal_date, datetime) else meal_date,
                "healthy_index": row[9],
                "latitude": str(row[10]),
                "longitude": str(row[11]),
                "added": row[12],
                "warnings": warnings_dict.get(meal_id, [])
            }
            if day_str not in meals_by_day:
                meals_by_day[day_str] = []
            meals_by_day[day_str].append(meal_data)

        result = []
        for day, meals in meals_by_day.items():
            result.append({
                "day": day,
                "meals": meals
            })
        logger.info("Pobrano posiłki (detailed) dla user_id: %s", user_id)
        return result
    except Exception as e:
        logger.error("Błąd przy pobieraniu posiłków (detailed): %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()
        # def add_meal(
        #         current_user: dict = Depends(get_current_user),
        #         latitude: float = Form(...),
        #         longitude: float = Form(...),
        #         image: UploadFile = File(...)
        # ):

# @router.get("/example")
# def example(
#         current_user: dict = Depends(get_current_user)
# ):
#     try:
#     except Exception as e:

@router.get("/edit_isAdded_true")
def edit_isAdded_true(
        current_user: dict = Depends(get_current_user),
        meal_idx: int = Form(...)
):
    try:
        # Retrieve the current user information
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        # Connect to the database
        conn = get_db_connection()
        cur = conn.cursor()

        # Update the 'added' field to True for the specified meal, ensuring it belongs to the current user
        cur.execute(
            "UPDATE Meal SET added = TRUE WHERE ID = %s AND User_ID = %s",
            (meal_idx, user_id)
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Meal not found or unauthorized")
        conn.commit()

        # Retrieve the updated meal record
        cur.execute("SELECT ID, added FROM Meal WHERE ID = %s", (meal_idx,))
        updated = cur.fetchone()

        return {
            "message": "Pole 'added' zostało zaktualizowane.",
            "meal": {"id": updated[0], "added": updated[1]}
        }
    except Exception as e:
        logger.error("Błąd przy aktualizacji pola added: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass



@router.get("/edit_isAdded_false")
def edit_isAdded_true(
        current_user: dict = Depends(get_current_user),
        meal_idx: int = Form(...)
):
    try:
        # Retrieve the current user information
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        # Connect to the database
        conn = get_db_connection()
        cur = conn.cursor()

        # Update the 'added' field to True for the specified meal, ensuring it belongs to the current user
        cur.execute(
            "UPDATE Meal SET added = FALSE WHERE ID = %s AND User_ID = %s",
            (meal_idx, user_id)
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Meal not found or unauthorized")
        conn.commit()

        # Retrieve the updated meal record
        cur.execute("SELECT ID, added FROM Meal WHERE ID = %s", (meal_idx,))
        updated = cur.fetchone()

        return {
            "message": "Pole 'added' zostało zaktualizowane.",
            "meal": {"id": updated[0], "added": updated[1]}
        }
    except Exception as e:
        logger.error("Błąd przy aktualizacji pola added: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass



@router.get("/get_user_info")
def get_user_info(
        current_user: dict = Depends(get_current_user)
):
    try:
        # Retrieve the current user information from the token
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        # Connect to the database and query the "User" table for the required fields
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            'SELECT email, sex, birthDate, height, dateOfJoin FROM "User" WHERE ID = %s',
            (user_id,)
        )
        user_info = cur.fetchone()
        if user_info is None:
            raise HTTPException(status_code=404, detail="User not found")

        # Prepare the response, converting dates to ISO format if they are not None
        response_data = {
            "email": user_info[0],
            "sex": user_info[1],
            "birthDate": user_info[2].isoformat() if user_info[2] is not None else None,
            "height": user_info[3],
            "dateOfJoin": user_info[4].isoformat() if user_info[4] is not None else None
        }
        return response_data
    except Exception as e:
        logger.error("Błąd przy pobieraniu informacji o użytkowniku: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


@router.get("/get_goal")
def get_goal(
        current_user: dict = Depends(get_current_user),
        meal_idx: int = Form(...)
):
    try:
        # Retrieve the current user information from the token
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        # Connect to the database and query the Goal table for the specified goal belonging to the user
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT kcal, protein, fats, carbs, desiredWeight, lifestyle, diet, startDate, endDate FROM Goal WHERE ID = %s AND User_ID = %s",
            (meal_idx, user_id)
        )
        goal_record = cur.fetchone()
        if goal_record is None:
            raise HTTPException(status_code=404, detail="Goal not found or unauthorized")

        response_data = {
            "kcal": goal_record[0],
            "protein": goal_record[1],
            "fats": goal_record[2],
            "carbs": goal_record[3],
            "desiredWeight": goal_record[4],
            "lifestyle": goal_record[5],
            "diet": goal_record[6],
            "startDate": goal_record[7].isoformat() if goal_record[7] is not None else None,
            "endDate": goal_record[8].isoformat() if goal_record[8] is not None else None,
        }
        return response_data
    except Exception as e:
        logger.error("Błąd przy pobieraniu goal: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


@router.post("/update_sex")
def update_sex(
        current_user: dict = Depends(get_current_user),
        sex: str = Form(...)
):
    try:
        # Pobranie identyfikatora użytkownika
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)
        if sex not in ("W", "M", "X"):
            raise HTTPException(status_code=400, detail="Nieprawidłowa wartość dla pola sex. Dozwolone wartości to: W, M, X.")
        conn = get_db_connection()
        cur = conn.cursor()

        # Aktualizacja pola sex
        cur.execute('UPDATE "User" SET sex = %s WHERE ID = %s', (sex, user_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Użytkownik nie został znaleziony lub aktualizacja nie powiodła się")
        conn.commit()
        logger.info("Zaktualizowano pole sex dla user_id: %s", user_id)
        return {"message": "Pole sex zostało zaktualizowane.", "user_id": user_id, "sex": sex}
    except Exception as e:
        logger.error("Błąd przy aktualizacji pola sex: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


@router.post("/update_birthDate")
def update_birthDate(
        current_user: dict = Depends(get_current_user),
        birth_date: date = Form(...)
):
    try:
        # Pobranie identyfikatora użytkownika
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        # Walidacja: data urodzenia nie może być wcześniejsza niż 1 stycznia 1900
        if birth_date < date(1900, 1, 1):
            raise HTTPException(
                status_code=400,
                detail="Nieprawidłowa data urodzenia. Osoba nie mogła urodzić się przed 1 stycznia 1900."
            )

        conn = get_db_connection()
        cur = conn.cursor()

        # Aktualizacja pola birthDate
        cur.execute('UPDATE "User" SET birthDate = %s WHERE ID = %s', (birth_date, user_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Użytkownik nie został znaleziony lub aktualizacja nie powiodła się")
        conn.commit()
        logger.info("Zaktualizowano pole birthDate dla user_id: %s", user_id)
        return {"message": "Pole birthDate zostało zaktualizowane.", "user_id": user_id, "birth_date": birth_date.isoformat()}
    except Exception as e:
        logger.error("Błąd przy aktualizacji pola birthDate: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


@router.post("/update_height")
def update_height(
        current_user: dict = Depends(get_current_user),
        height: int = Form(...)
):
    try:
        # Pobranie identyfikatora użytkownika
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        # Walidacja: height musi być w zakresie 50 - 250 cm
        if height < 50 or height > 250:
            raise HTTPException(
                status_code=400,
                detail="Nieprawidłowa wartość dla pola height. Dozwolony zakres: 50-250 cm."
            )

        conn = get_db_connection()
        cur = conn.cursor()

        # Aktualizacja pola height
        cur.execute('UPDATE "User" SET height = %s WHERE ID = %s', (height, user_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404,
                                detail="Użytkownik nie został znaleziony lub aktualizacja nie powiodła się")
        conn.commit()
        logger.info("Zaktualizowano pole height dla user_id: %s", user_id)
        return {"message": "Pole height zostało zaktualizowane.", "user_id": user_id, "height": height}
    except Exception as e:
        logger.error("Błąd przy aktualizacji pola height: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


@router.post("/create_problem")
def create_problem(
        current_user: dict = Depends(get_current_user),
        description: str = Form(...)
):
    try:
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()

        # Sprawdzenie, ile problemów posiada użytkownik
        cur.execute('SELECT COUNT(*) FROM Problem WHERE User_ID = %s', (user_id,))
        count = cur.fetchone()[0]
        if count >= 7:
            raise HTTPException(status_code=400, detail="Nie można dodać więcej niż 7 problemów.")

        cur.execute('INSERT INTO Problem (User_ID, description) VALUES (%s, %s) RETURNING ID', (user_id, description))
        problem_id = cur.fetchone()[0]
        conn.commit()
        return {"message": "Problem został utworzony.", "problem_id": problem_id}
    except Exception as e:
        logger.error("Błąd przy tworzeniu problemu: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


@router.post("/update_problem/{problem_id}")
def update_problem(
        problem_id: int,
        current_user: dict = Depends(get_current_user),
        description: str = Form(...)
):
    try:
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()

        # Sprawdzenie, czy problem istnieje i należy do użytkownika
        cur.execute('SELECT ID FROM Problem WHERE ID = %s AND User_ID = %s', (problem_id, user_id))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Problem nie został znaleziony lub nie należy do użytkownika.")

        cur.execute('UPDATE Problem SET description = %s WHERE ID = %s', (description, problem_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Aktualizacja nie powiodła się.")
        conn.commit()
        return {"message": "Problem został zaktualizowany.", "problem_id": problem_id, "description": description}
    except Exception as e:
        logger.error("Błąd przy aktualizacji problemu: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


@router.delete("/delete_problem/{problem_id}")
def delete_problem(
        problem_id: int,
        current_user: dict = Depends(get_current_user)
):
    try:
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()

        # Sprawdzenie, czy problem istnieje i należy do użytkownika
        cur.execute('SELECT ID FROM Problem WHERE ID = %s AND User_ID = %s', (problem_id, user_id))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Problem nie został znaleziony lub nie należy do użytkownika.")

        cur.execute('DELETE FROM Problem WHERE ID = %s', (problem_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Usunięcie problemu nie powiodło się.")
        conn.commit()
        return {"message": "Problem został usunięty.", "problem_id": problem_id}
    except Exception as e:
        logger.error("Błąd przy usuwaniu problemu: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass




@router.post("/add_current_weight")
def add_current_weight(
    current_user: dict = Depends(get_current_user),
    weight: float = Form(...)
):
    try:
        # Pobranie identyfikatora użytkownika
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()

        # Dodanie wpisu w tabeli Weight z dzisiejszą datą
        today = date.today()
        cur.execute(
            'INSERT INTO Weight (User_ID, weight, date) VALUES (%s, %s, %s) RETURNING ID',
            (user_id, weight, today)
        )
        weight_id = cur.fetchone()[0]
        conn.commit()
        logger.info("Dodano aktualną wagę dla user_id: %s, waga: %s", user_id, weight)
        return {
            "message": "Aktualna waga została dodana.",
            "weight_id": weight_id,
            "user_id": user_id,
            "weight": weight,
            "date": today.isoformat()
        }
    except Exception as e:
        logger.error("Błąd przy dodawaniu aktualnej wagi: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass



@router.post("/update_goal_kcal")
def update_goal_kcal(
        current_user: dict = Depends(get_current_user),
        kcal: int = Form(...)
):
    try:
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()

        # Pobranie ostatniego celu dla użytkownika
        cur.execute("SELECT ID FROM Goal WHERE User_ID = %s ORDER BY ID DESC LIMIT 1", (user_id,))
        result = cur.fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="Nie znaleziono celu dla użytkownika.")
        goal_id = result[0]

        # Aktualizacja pola kcal
        cur.execute("UPDATE Goal SET kcal = %s WHERE ID = %s", (kcal, goal_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Aktualizacja nie powiodła się.")
        conn.commit()
        return {"message": "Pole kcal zostało zaktualizowane.", "goal_id": goal_id, "kcal": kcal}
    except Exception as e:
        logger.error("Błąd przy aktualizacji pola kcal w Goal: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


@router.post("/update_goal_protein")
def update_goal_protein(
        current_user: dict = Depends(get_current_user),
        protein: int = Form(...)
):
    try:
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()

        # Pobranie ostatniego celu dla użytkownika
        cur.execute("SELECT ID FROM Goal WHERE User_ID = %s ORDER BY ID DESC LIMIT 1", (user_id,))
        result = cur.fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="Nie znaleziono celu dla użytkownika.")
        goal_id = result[0]

        # Aktualizacja pola protein
        cur.execute("UPDATE Goal SET protein = %s WHERE ID = %s", (protein, goal_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Aktualizacja nie powiodła się.")
        conn.commit()
        return {"message": "Pole protein zostało zaktualizowane.", "goal_id": goal_id, "protein": protein}
    except Exception as e:
        logger.error("Błąd przy aktualizacji pola protein w Goal: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


@router.post("/update_goal_fats")
def update_goal_fats(
        current_user: dict = Depends(get_current_user),
        fats: int = Form(...)
):
    try:
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()

        # Pobranie ostatniego celu dla użytkownika
        cur.execute("SELECT ID FROM Goal WHERE User_ID = %s ORDER BY ID DESC LIMIT 1", (user_id,))
        result = cur.fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="Nie znaleziono celu dla użytkownika.")
        goal_id = result[0]

        # Aktualizacja pola fats
        cur.execute("UPDATE Goal SET fats = %s WHERE ID = %s", (fats, goal_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Aktualizacja nie powiodła się.")
        conn.commit()
        return {"message": "Pole fats zostało zaktualizowane.", "goal_id": goal_id, "fats": fats}
    except Exception as e:
        logger.error("Błąd przy aktualizacji pola fats w Goal: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


@router.post("/update_goal_carbs")
def update_goal_carbs(
        current_user: dict = Depends(get_current_user),
        carbs: int = Form(...)
):
    try:
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()

        # Pobranie ostatniego celu dla użytkownika
        cur.execute("SELECT ID FROM Goal WHERE User_ID = %s ORDER BY ID DESC LIMIT 1", (user_id,))
        result = cur.fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="Nie znaleziono celu dla użytkownika.")
        goal_id = result[0]

        # Aktualizacja pola carbs
        cur.execute("UPDATE Goal SET carbs = %s WHERE ID = %s", (carbs, goal_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Aktualizacja nie powiodła się.")
        conn.commit()
        return {"message": "Pole carbs zostało zaktualizowane.", "goal_id": goal_id, "carbs": carbs}
    except Exception as e:
        logger.error("Błąd przy aktualizacji pola carbs w Goal: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


@router.post("/create_goal")
def create_goal(
        current_user: dict = Depends(get_current_user),
        desiredWeight: float = Form(...),
        lifestyle: str = Form(...),
        diet: str = Form(...),
        startDate: date = Form(...),
        endDate: date = Form(...)
):
    try:
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        # Pobranie danych użytkownika niezbędnych do zapytania do ChatGPT
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT sex, birthDate, height FROM "User" WHERE ID = %s', (user_id,))
        user_data = cur.fetchone()

        now = datetime.now()
        if not user_data:
            raise HTTPException(status_code=404, detail="Nie znaleziono danych użytkownika.")
        sex, birthDate, height = user_data

        # Wywołanie funkcji new_goal, która zwróci rekomendacje kcal, proteins, carbs i fats
        nutrients, raw_response = new_goal(sex, birthDate, height, lifestyle, diet, str(startDate), str(endDate))

        cur.execute("""
                    INSERT INTO OpenAI_request(User_ID, type, img_link, date)
                    VALUES (%s, %s, %s, %s)
                    RETURNING ID
                """, (user_id, 'G', now))
        openai_req_id = cur.fetchone()[0]
        conn.commit()

        # Mapowanie klucza 'proteins' do pola 'protein' w bazie
        kcal = nutrients.get("kcal", -1)
        protein = nutrients.get("proteins", -1)
        fats = nutrients.get("fats", -1)
        carbs = nutrients.get("carbs", -1)

        insert_query = """
            INSERT INTO Goal (
                User_ID, kcal, protein, fats, carbs, desiredWeight, lifestyle, diet, startDate, endDate
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING ID;
        """
        cur.execute(insert_query, (
            user_id, kcal, protein, fats, carbs, desiredWeight, lifestyle, diet, startDate, endDate
        ))
        goal_id = cur.fetchone()[0]
        conn.commit()
        return {
            "message": "Cel został dodany.",
            "goal_id": goal_id,
            "kcal": kcal,
            "protein": protein,
            "fats": fats,
            "carbs": carbs
        }
    except Exception as e:
        logger.error("Błąd przy dodawaniu celu: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

@router.post("/get_meals")
def get_meals(
        current_user: dict = Depends(get_current_user),
):
    try:
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()

        # Pobierz wszystkie posiłki użytkownika
        cur.execute("""
            SELECT ID, img_link, kcal, proteins, carbs, fats, date, healthy_index
            FROM Meal
            WHERE User_ID = %s
            ORDER BY date DESC
        """, (user_id,))
        meals = cur.fetchall()

        # Pobierz warningi dla tych posiłków
        meal_ids = [meal[0] for meal in meals]
        warnings_dict = {}
        if meal_ids:
            cur.execute("SELECT Meal_ID, warning FROM Warning WHERE Meal_ID = ANY(%s)", (meal_ids,))
            for meal_id, warning in cur.fetchall():
                warnings_dict.setdefault(meal_id, []).append(warning)

        # Zbuduj wynik
        result = []
        for meal in meals:
            meal_id, img_key, kcal, proteins, carbs, fats, date_val, healthy_index = meal
            presigned_url = s3.generate_presigned_url(
                'get_object',
                Params={'Bucket': S3_BUCKET_NAME, 'Key': img_key},
                ExpiresIn=3600
            )
            result.append({
                "id": meal_id,
                "img_link": presigned_url,
                "kcal": kcal,
                "proteins": proteins,
                "carbs": carbs,
                "fats": fats,
                "date": date_val.isoformat(),
                "healthy_index": healthy_index,
                "warnings": warnings_dict.get(meal_id, [])
            })

        return {"meals": result}
    except Exception as e:
        logger.error("Błąd przy pobieraniu posiłków: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()





