from typing import List

from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Depends, Request, Body
from datetime import datetime, date, timedelta
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel


import json
import re
import io
import logging
from PIL import Image
from openai import OpenAI
import boto3
import requests

from auth import get_current_user
from db import get_db_connection, get_or_create_user_by_sub
from config import S3_BUCKET_NAME, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION, OPENAI_API_KEY
from config import USER_POOL_ID

from checkSubscription import check_subscription_add_meal, verify_apple_subscribe_active, decode_apple_receipt
from OpenAI_requests import query_meal_nutrients, new_goal, meals_from_barcode_problems
from openfoodfacts_api import getInfoFromOpenFoodsApi

router = APIRouter()
logger = logging.getLogger("server_logger")

# Inicjalizacja klienta S3 oraz ustawienie klucza OpenAI
s3 = boto3.client(
    's3',
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    region_name=AWS_REGION
)


class ProblemsUpdateRequest(BaseModel):
    problems: List[str]

@router.post("/test")
def test(
    current_user: dict = Depends(get_current_user),
    test: str = Form(...)
):
    try:
        logger.info("Test przeprowadzono pomyślnie")
        # Przykładowa operacja – można tutaj umieścić dowolną logikę
        wynik = {
            "status": "success",
            "user": current_user,
            "test_message": test
        }
        return wynik
    except Exception as e:
        logger.error("Błąd podczas przeprowadzania testu: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Wystąpił błąd podczas testu")


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
            INSERT INTO "User"(email, password, dateOfJoin,language)
            VALUES (%s, %s, %s,%s)
            RETURNING ID
        """, (email, password, date.today(), "English"))
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
        original_transaction_id: str = Form(...)
):
    # try:
    #     original_transaction_id = decode_apple_receipt(receipt)
    # except Exception as e:
    #     raise HTTPException(status_code=400, detail=str(e))


    if not verify_apple_subscribe_active(original_transaction_id):
        raise HTTPException(status_code=403, detail="Subskrypcja nieaktywna wg Apple")

    sub = current_user["sub"]
    email = current_user.get("email", "")
    user_id = get_or_create_user_by_sub(sub, email)

    logger.info(f"{email} original_transaction_id is {original_transaction_id}")


    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
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


@router.post("/add_meal_from_barcode")
def add_meal_from_barcode(
        current_user: dict = Depends(get_current_user),
        latitude: float = Form(...),
        longitude: float = Form(...),
        original_transaction_id: str = Form(...),
        barcode: int = Form(...),
        image: UploadFile = File(...)
):
    try:
        logger.info(f"apple recipe ma forme (pierwsze 50 znakow): {original_transaction_id[:50]}")
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()

        now = datetime.now()
        today = date.today()

        # original_transaction_id = decode_apple_receipt(original_transaction_id)
        logger.info(f"apple recipe ma forme (pierwsze 50 znakow): {original_transaction_id[:50]}")
        original_file_contents = image.file.read()
        file_name = f"{user_id}_{int(now.timestamp())}_barcode_{image.filename}"
        s3.put_object(Bucket=S3_BUCKET_NAME, Key=file_name, Body=original_file_contents)

        # Pobranie kontekstu użytkownika (problemy, dieta)
        cur.execute("SELECT description FROM Problem WHERE User_ID = %s LIMIT 7", (user_id,))
        problems_rows = cur.fetchall()
        user_problems = [row[0] for row in problems_rows] if problems_rows else []

        cur.execute("SELECT diet, language FROM \"User\" WHERE ID = %s", (user_id,))
        diet_row = cur.fetchone()
        user_diet = diet_row[0] if diet_row and diet_row[0] is not None else ""
        user_language = diet_row[1] if diet_row and diet_row[1] is not None else ""

        user_context = {
            "problems": user_problems,
            "diet": user_diet,
            "language": user_language
        }

        # Pobranie danych z OpenFoodsAPI
        name, kcal, proteins, carbs, fats, ingredients, image_front_url = getInfoFromOpenFoodsApi(barcode)

        if len(ingredients) > 0:
            problems_result, _ = meals_from_barcode_problems(name, ingredients, user_context)
        else:
            problems_result = {"healthy_index": -1, "problems": []}

        healthy_index_val = problems_result.get("healthy_index", -1)
        problems_val = problems_result.get("problems", [])

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
        s3.put_object(Bucket=S3_BUCKET_NAME, Key=resized_file_name, Body=resized_image_bytes)
        presigned_url = s3.generate_presigned_url(
            'get_object', Params={'Bucket': S3_BUCKET_NAME, 'Key': resized_file_name}, ExpiresIn=300
        )

        # Wstawienie rekordu do Meal, aby uzyskać meal_id
        cur.execute("""
            INSERT INTO Meal(
                User_ID, name, bar_code, img_link, kcal, proteins, carbs, fats, date, healthy_index, latitude, longitude, added
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING ID
        """, (
            user_id, name, barcode, file_name, kcal, proteins, carbs, fats, now, healthy_index_val, latitude, longitude,
            False))
        meal_id = cur.fetchone()[0]
        conn.commit()

        # Wstawienie problemów do tabeli Warning z wykorzystaniem meal_id
        problems_with_id = []
        for problem in problems_val:
            cur.execute("INSERT INTO Warning (Meal_ID, warning) VALUES (%s, %s) RETURNING ID", (meal_id, problem))
            problem_id = cur.fetchone()[0]
            problems_with_id.append({"id": problem_id, "description": problem})
        conn.commit()

        meal_data = {
            "id": meal_id,
            "name": name,
            "img_link": presigned_url,
            "kcal": kcal,
            "proteins": proteins,
            "carbs": carbs,
            "fats": fats,
            "healthy_index": healthy_index_val,
            "latitude": str(latitude),
            "longitude": str(longitude),
            "date": now.isoformat(),
            "added": False,
            "warnings": problems_with_id
        }

        cur.execute("""
            INSERT INTO OpenAI_request(User_ID, type, img_link, date)
            VALUES (%s, %s, %s, %s)
            RETURNING ID
        """, (user_id, 'B', file_name, now))
        openai_req_id = cur.fetchone()[0]
        conn.commit()

        logger.info("Dodano posiłek (meal_id: %s) dla user_id: %s", meal_id, user_id)
        return {
            "message": "Dodano posiłek i zaktualizowano dane makroskładników.",
            "meal": meal_data,
            "warnings": problems_with_id,
            "openai_request_id": openai_req_id,
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

@router.post("/add_meal_from_photo")
def add_meal_from_photo(
        current_user: dict = Depends(get_current_user),
        latitude: float = Form(...),
        longitude: float = Form(...),
        original_transaction_id: str = Form(...),
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

        original_file_contents = image.file.read()
        file_name = f"{user_id}_{int(now.timestamp())}_{image.filename}"
        s3.put_object(Bucket=S3_BUCKET_NAME, Key=file_name, Body=original_file_contents)

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
        s3.put_object(Bucket=S3_BUCKET_NAME, Key=resized_file_name, Body=resized_image_bytes)
        presigned_url = s3.generate_presigned_url(
            'get_object', Params={'Bucket': S3_BUCKET_NAME, 'Key': resized_file_name}, ExpiresIn=300
        )

        cur.execute("SELECT description FROM Problem WHERE User_ID = %s LIMIT 7", (user_id,))
        problems_rows = cur.fetchall()
        user_problems = [row[0] for row in problems_rows] if problems_rows else []

        cur.execute("SELECT diet, language FROM \"User\" WHERE ID = %s", (user_id,))
        diet_row = cur.fetchone()
        user_diet = diet_row[0] if diet_row and diet_row[0] is not None else ""
        user_language = diet_row[1] if diet_row and diet_row[1] is not None else ""

        user_context = {
            "problems": user_problems,
            "diet": user_diet,
            "language": user_language
        }

        nutrients_dict, openai_result_text = query_meal_nutrients(presigned_url, user_context)
        name_val = nutrients_dict.get("name", "dish")
        kcal_val = nutrients_dict.get("kcal", -1)
        proteins_val = nutrients_dict.get("proteins", -1)
        carbs_val = nutrients_dict.get("carbs", -1)
        fats_val = nutrients_dict.get("fats", -1)
        healthy_index_val = nutrients_dict.get("healthy_index", -1)
        problems_val = nutrients_dict.get("problems", [])

        # Wstawienie rekordu do Meal, aby uzyskać meal_id
        cur.execute("""
            INSERT INTO Meal(
                User_ID, name, img_link, kcal, proteins, carbs, fats, date, healthy_index, latitude, longitude, added
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING ID
        """, (
            user_id, name_val, file_name, kcal_val, proteins_val, carbs_val, fats_val, now, healthy_index_val, latitude,
            longitude, False))
        meal_id = cur.fetchone()[0]
        conn.commit()

        # Wstawienie problemów do tabeli Warning z wykorzystaniem meal_id
        problems_with_id = []
        for problem in problems_val:
            cur.execute("INSERT INTO Warning (Meal_ID, warning) VALUES (%s, %s) RETURNING ID", (meal_id, problem))
            problem_id = cur.fetchone()[0]
            problems_with_id.append({"id": problem_id, "description": problem})
        conn.commit()

        cur.execute("""
            SELECT ID, name, img_link, kcal, proteins, carbs, fats, healthy_index, latitude, longitude, date, added
            FROM Meal WHERE ID = %s
        """, (meal_id,))
        updated_meal = cur.fetchone()
        meal_data = {
            "id": updated_meal[0],
            "name": updated_meal[1],
            "img_link": presigned_url,
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
        meal_data["warnings"] = problems_with_id

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
            "warnings": problems_with_id,
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

        cur.execute("SELECT * FROM Meal WHERE User_ID = %s ORDER BY date DESC", (user_id,))
        rows = cur.fetchall()

        meal_ids = [row[0] for row in rows]

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
            meal_date = row[8]
            day_str = meal_date.date().isoformat() if isinstance(meal_date, datetime) else str(meal_date)
            presigned_url = s3.generate_presigned_url(
                'get_object',
                Params={'Bucket': S3_BUCKET_NAME, 'Key': row[3]},
                ExpiresIn=3600
            )
            meal_id = row[0]
            meal_data = {
                "id": meal_id,
                "user_id": row[1],
                "bar_code": row[2],
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


@router.post("/edit_isAdded_true")
def edit_isAdded_true(
        current_user: dict = Depends(get_current_user),
        meal_idx: int = Form(...)
):
    try:
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("UPDATE Meal SET added = TRUE WHERE ID = %s AND User_ID = %s", (meal_idx, user_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Meal not found or unauthorized")
        conn.commit()

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


@router.post("/edit_isAdded_false")
def edit_isAdded_false(
        current_user: dict = Depends(get_current_user),
        meal_idx: int = Form(...)
):
    try:
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("UPDATE Meal SET added = FALSE WHERE ID = %s AND User_ID = %s", (meal_idx, user_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Meal not found or unauthorized")
        conn.commit()

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
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT email, sex, birthDate, height, dateOfJoin FROM "User" WHERE ID = %s', (user_id,))
        user_info = cur.fetchone()
        if user_info is None:
            raise HTTPException(status_code=404, detail="User not found")

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
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

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
        logger.info(f"plec otrzymana od uzytkownika to: {sex}")

        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        gender=""
        if sex=="male":
            gender="M"
        elif sex=="female":
            gender="W"
        else:
            gender="X"

        # if sex not in ("W", "M", "X"):
        #     raise HTTPException(status_code=400,
        #
        #                         detail="Nieprawidłowa wartość dla pola sex. Dozwolone wartości to: W, M, X.")
        conn = get_db_connection()
        cur = conn.cursor()

        logger.info(f"plec otrzymana od uzytkownika to: {sex}")

        cur.execute('UPDATE "User" SET sex = %s WHERE email=%s', (gender[0], email))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404,
                                detail="Użytkownik nie został znaleziony lub aktualizacja nie powiodła się")
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



@router.post("/update_language")
def update_language(
        current_user: dict = Depends(get_current_user),
        language: str = Form(...)
):
    try:
        logger.info(f"jezyk otrzymany od uzytkownika to: {language}")


        # Lista dozwolonych języków (angielskie nazwy)
        allowed_languages = [
            "English", "Chinese", "Spanish", "Hindi", "Arabic","Chinese Simplified",
            "French", "Korean", "Russian", "Polish", "Portuguese", "Japanese"
        ]
        if language not in allowed_languages:
            raise HTTPException(
                status_code=400,
                detail=f"Nieprawidłowa wartość dla pola language. Dozwolone wartości to: {', '.join(allowed_languages)}."
            )

        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()



        cur.execute('UPDATE "User" SET language = %s WHERE email=%s', (language, email))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404,
                                detail="Użytkownik nie został znaleziony lub aktualizacja nie powiodła się")
        conn.commit()
        logger.info("Zaktualizowano pole language dla user_id: %s", user_id)
        return {"message": "Pole language zostało zaktualizowane.", "user_id": user_id, "language": language}
    except Exception as e:
        logger.error("Błąd przy aktualizacji pola language: %s", e)
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
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        if birth_date < date(1900, 1, 1):
            raise HTTPException(
                status_code=400,
                detail="Nieprawidłowa data urodzenia. Osoba nie mogła urodzić się przed 1 stycznia 1900."
            )

        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute('UPDATE "User" SET birthDate = %s WHERE ID = %s', (birth_date, user_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404,
                                detail="Użytkownik nie został znaleziony lub aktualizacja nie powiodła się")
        conn.commit()
        logger.info("Zaktualizowano pole birthDate dla user_id: %s", user_id)
        return {"message": "Pole birthDate zostało zaktualizowane.", "user_id": user_id,
                "birth_date": birth_date.isoformat()}
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
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        if height < 50 or height > 250:
            raise HTTPException(
                status_code=400,
                detail="Nieprawidłowa wartość dla pola height. Dozwolony zakres: 50-250 cm."
            )

        conn = get_db_connection()
        cur = conn.cursor()

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



@router.post("/update_diet")
def update_diet(
        current_user: dict = Depends(get_current_user),
        diet: str = Form(...)
):
    try:
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute('UPDATE "User" SET diet = %s WHERE ID = %s', (diet, user_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Użytkownik nie został znaleziony lub aktualizacja nie powiodła się")
        conn.commit()
        logger.info("Zaktualizowano pole diet dla user_id: %s", user_id)
        return {"message": "Pole diet zostało zaktualizowane.", "user_id": user_id, "diet": diet}
    except Exception as e:
        logger.error("Błąd przy aktualizacji pola diet: %s", e)
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


@router.post("/update_problems")
def update_problems(
    payload: ProblemsUpdateRequest,
    current_user: dict = Depends(get_current_user)
):
    try:
        problems = payload.problems
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()

        logger.info(f"Updating problems for user {user_id}")

        cur.execute("SELECT ID, description FROM Problem WHERE User_ID = %s", (user_id,))
        existing_rows = cur.fetchall()

        existing_dict = {row[1]: row[0] for row in existing_rows}
        existing_descriptions = set(existing_dict.keys())
        provided_descriptions = set(problems)

        for desc in existing_descriptions - provided_descriptions:
            problem_id = existing_dict[desc]
            cur.execute("DELETE FROM Problem WHERE ID = %s", (problem_id,))

        for desc in provided_descriptions - existing_descriptions:
            cur.execute("INSERT INTO Problem (User_ID, description) VALUES (%s, %s) RETURNING ID", (user_id, desc))
            new_id = cur.fetchone()[0]

        conn.commit()
        return {"message": "Problemy zostały zaktualizowane.", "problems": list(provided_descriptions)}
    except Exception as e:
        conn.rollback()
        logger.error("Błąd przy aktualizacji problemów: %s", e)
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
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()

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

        cur.execute("SELECT ID FROM Goal WHERE User_ID = %s ORDER BY ID DESC LIMIT 1", (user_id,))
        result = cur.fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="Nie znaleziono celu dla użytkownika.")
        goal_id = result[0]

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

        cur.execute("SELECT ID FROM Goal WHERE User_ID = %s ORDER BY ID DESC LIMIT 1", (user_id,))
        result = cur.fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="Nie znaleziono celu dla użytkownika.")
        goal_id = result[0]

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

        cur.execute("SELECT ID FROM Goal WHERE User_ID = %s ORDER BY ID DESC LIMIT 1", (user_id,))
        result = cur.fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="Nie znaleziono celu dla użytkownika.")
        goal_id = result[0]

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

        cur.execute("SELECT ID FROM Goal WHERE User_ID = %s ORDER BY ID DESC LIMIT 1", (user_id,))
        result = cur.fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="Nie znaleziono celu dla użytkownika.")
        goal_id = result[0]

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


# @router.post("/create_goal")
# def create_goal(
#         current_user: dict = Depends(get_current_user),
#         desiredWeight: float = Form(...),
#         lifestyle: str = Form(...),
#         diet: str = Form(...),
#         startDate: date = Form(...),
#         endDate: date = Form(...)
# ):
#     logger.info("Początek tworzenia celu dla użytkownika.")
#     try:
#         sub = current_user["sub"]
#         email = current_user.get("email", "")
#         user_id = get_or_create_user_by_sub(sub, email)
#         logger.info(f"Znaleziono użytkownika o ID: {user_id}")
#
#         conn = get_db_connection()
#         cur = conn.cursor()
#
#         cur.execute('SELECT sex, birthDate, height FROM "User" WHERE ID = %s', (user_id,))
#         user_data = cur.fetchone()
#         logger.info(f"Dane użytkownika pobrane: {user_data}")
#
#         now = datetime.now()
#         if not user_data:
#             logger.error("Nie znaleziono danych użytkownika.")
#             raise HTTPException(status_code=404, detail="Nie znaleziono danych użytkownika.")
#         sex, birthDate, height = user_data
#
#         cur.execute('UPDATE "User" SET diet = %s WHERE ID = %s', (diet, user_id))
#         conn.commit()
#         logger.info("Zaktualizowano pole diet w tabeli User.")
#
#         cur.execute("SELECT date FROM OpenAI_request WHERE User_ID = %s AND type = 'G' ORDER BY date DESC LIMIT 1", (user_id,))
#         last_request = cur.fetchone()
#         logger.info(f"Ostatnie zapytanie OpenAI: {last_request}")
#
#         if last_request:
#             last_date = last_request[0]
#             if now - last_date < timedelta(weeks=1):
#                 next_allowed = last_date + timedelta(weeks=1)
#                 time_remaining = next_allowed - now
#                 logger.warning(f"Cel został już utworzony niedawno; kolejna operacja za {time_remaining}.")
#                 raise HTTPException(status_code=400,
#                                     detail=f"Cel będzie można utworzyć ponownie za {time_remaining}.")
#
#         # Wywołanie funkcji new_goal
#         nutrients, raw_response = new_goal(sex, birthDate, height, lifestyle, diet, str(startDate), str(endDate))
#         logger.info(f"Wynik new_goal: {nutrients}")
#
#         cur.execute("""
#             INSERT INTO OpenAI_request(User_ID, type, img_link, date)
#             VALUES (%s, %s, %s, %s)
#             RETURNING ID
#         """, (user_id, 'G', None, now))
#         openai_req_id = cur.fetchone()[0]
#         conn.commit()
#         logger.info(f"Zapisano nowy rekord w OpenAI_request: ID {openai_req_id}")
#
#         kcal = nutrients.get("kcal", -1)
#         protein = nutrients.get("proteins", -1)
#         fats = nutrients.get("fats", -1)
#         carbs = nutrients.get("carbs", -1)
#
#         insert_query = """
#             INSERT INTO Goal (
#                 User_ID, kcal, protein, fats, carbs, desiredWeight, lifestyle, startDate, endDate
#             )
#             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
#             RETURNING ID;
#         """
#         cur.execute(insert_query, (user_id, kcal, protein, fats, carbs, desiredWeight, lifestyle, startDate, endDate))
#         goal_id = cur.fetchone()[0]
#         conn.commit()
#         logger.info(f"Cel utworzony o ID: {goal_id}")
#
#         # Zwracamy wynik z widokiem
#         return {
#             "message": "Cel został dodany.",
#             "goal_id": goal_id,
#             "kcal": kcal,
#             "protein": protein,
#             "fats": fats,
#             "carbs": carbs
#         }
#     except Exception as e:
#         logger.exception("Wystąpił błąd podczas tworzenia celu:")
#         raise HTTPException(status_code=500, detail=str(e))
#     finally:
#         try:
#             cur.close()
#             logger.info("Zamknięto kursor.")
#         except Exception as ex:
#             logger.warning(f"Błąd przy zamykaniu kursora: {ex}")
#         try:
#             conn.close()
#             logger.info("Zamknięto połączenie z bazą.")
#         except Exception as ex:
#             logger.warning(f"Błąd przy zamykaniu połączenia: {ex}")

@router.post("/create_goal")
def create_goal(
        current_user: dict = Depends(get_current_user),
        desiredWeight: float = Form(...),
        lifestyle: str = Form(...),
        diet: str = Form(...),
        startDate: date = Form(...),
        endDate: date = Form(...)
):
    logger.info("Początek tworzenia celu dla użytkownika.")
    try:
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)
        logger.info(f"Znaleziono użytkownika o ID: {user_id}")

        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute('SELECT sex, birthDate, height FROM "User" WHERE ID = %s', (user_id,))
        user_data = cur.fetchone()
        logger.info(f"Dane użytkownika pobrane: {user_data}")

        now = datetime.now()
        if not user_data:
            logger.error("Nie znaleziono danych użytkownika.")
            raise HTTPException(status_code=404, detail="Nie znaleziono danych użytkownika.")
        sex, birthDate, height = user_data

        cur.execute('UPDATE "User" SET diet = %s WHERE ID = %s', (diet, user_id))
        conn.commit()
        logger.info("Zaktualizowano pole diet w tabeli User.")

        # Dla celów testowych pomijamy weryfikację daty ostatniego celu
        cur.execute("SELECT date FROM OpenAI_request WHERE User_ID = %s AND type = 'G' ORDER BY date DESC LIMIT 1", (user_id,))
        last_request = cur.fetchone()
        logger.info(f"Ostatnie zapytanie OpenAI (pomijamy weryfikację): {last_request}")

        # Wywołanie funkcji new_goal
        nutrients, raw_response,text_from_openai = new_goal(sex, birthDate, height, lifestyle, diet, str(startDate), str(endDate))
        logger.info(f"Wynik new_goal: {nutrients}")
        logger.info(f"Wynik new_goal: {text_from_openai}")

        cur.execute("""
            INSERT INTO OpenAI_request(User_ID, type, img_link, date)
            VALUES (%s, %s, %s, %s)
            RETURNING ID
        """, (user_id, 'G', None, now))
        openai_req_id = cur.fetchone()[0]
        conn.commit()
        logger.info(f"Zapisano nowy rekord w OpenAI_request: ID {openai_req_id}")

        kcal = nutrients.get("kcal", -1)
        protein = nutrients.get("proteins", -1)
        fats = nutrients.get("fats", -1)
        carbs = nutrients.get("carbs", -1)

        logger.info(f"takie wartosci maja nowe goal: kcal: {kcal}, protein: {protein}, fats: {fats}, carbs: {carbs}")

        insert_query = """
            INSERT INTO Goal (
                User_ID, kcal, protein, fats, carbs, desiredWeight, lifestyle, startDate, endDate
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING ID;
        """
        cur.execute(insert_query, (user_id, kcal, protein, fats, carbs, desiredWeight, lifestyle, startDate, endDate))
        goal_id = cur.fetchone()[0]
        conn.commit()
        logger.info(f"Cel utworzony o ID: {goal_id}")

        # Zwracamy wynik z widokiem
        return {
            "message": "Cel został dodany.",
            "goal_id": goal_id,
            "kcal": kcal,
            "protein": protein,
            "fats": fats,
            "carbs": carbs
        }
    except Exception as e:
        logger.exception("Wystąpił błąd podczas tworzenia celu:")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            cur.close()
            logger.info("Zamknięto kursor.")
        except Exception as ex:
            logger.warning(f"Błąd przy zamykaniu kursora: {ex}")
        try:
            conn.close()
            logger.info("Zamknięto połączenie z bazą.")
        except Exception as ex:
            logger.warning(f"Błąd przy zamykaniu połączenia: {ex}")




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

        cur.execute("""
            SELECT ID, img_link, name, kcal, proteins, carbs, fats, date, healthy_index
            FROM Meal
            WHERE User_ID = %s and added = true
            ORDER BY date DESC
        """, (user_id,))
        meals = cur.fetchall()

        meal_ids = [meal[0] for meal in meals]
        warnings_dict = {}
        if meal_ids:
            cur.execute("SELECT Meal_ID, warning FROM Warning WHERE Meal_ID = ANY(%s)", (meal_ids,))
            for meal_id, warning in cur.fetchall():
                warnings_dict.setdefault(meal_id, []).append(warning)

        result = []
        for meal in meals:
            meal_id, img_key, name, kcal, proteins, carbs, fats, date_val, healthy_index = meal
            presigned_url = s3.generate_presigned_url(
                'get_object',
                Params={'Bucket': S3_BUCKET_NAME, 'Key': img_key},
                ExpiresIn=3600
            )
            result.append({
                "id": meal_id,
                "img_link": presigned_url,
                "name": name,
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


@router.post("/meal_update_protein")
def meal_update_protein(
        current_user: dict = Depends(get_current_user),
        meal_id: int = Form(...),
        new_value: int = Form(...),
):
    try:
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("UPDATE Meal SET proteins = %s WHERE ID = %s AND User_ID = %s", (new_value, meal_id, user_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Nie znaleziono posiłku lub brak uprawnień")
        conn.commit()

        return {"message": "Protein value updated.", "meal": {"id": meal_id, "proteins": new_value}}
    except Exception as e:
        logger.error("Błąd przy aktualizacji białka w posiłku: %s", e)
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


@router.post("/meal_update_fats")
def meal_update_fats(
        current_user: dict = Depends(get_current_user),
        meal_id: int = Form(...),
        new_value: int = Form(...),
):
    try:
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("UPDATE Meal SET fats = %s WHERE ID = %s AND User_ID = %s", (new_value, meal_id, user_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Nie znaleziono posiłku lub brak uprawnień")
        conn.commit()

        return {"message": "fats value updated.", "meal": {"id": meal_id, "proteins": new_value}}
    except Exception as e:
        logger.error("Błąd przy aktualizacji białka w posiłku: %s", e)
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


@router.post("/meal_update_carbs")
def meal_update_carbs(
        current_user: dict = Depends(get_current_user),
        meal_id: int = Form(...),
        new_value: int = Form(...),
):
    try:
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("UPDATE Meal SET carbs = %s WHERE ID = %s AND User_ID = %s", (new_value, meal_id, user_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Nie znaleziono posiłku lub brak uprawnień")
        conn.commit()

        return {"message": "fats value updated.", "meal": {"id": meal_id, "proteins": new_value}}
    except Exception as e:
        logger.error("Błąd przy aktualizacji białka w posiłku: %s", e)
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


@router.post("/meal_update_healthy_index")
def meal_update_healthy_index(
        current_user: dict = Depends(get_current_user),
        meal_id: int = Form(...),
        new_value: int = Form(...),
):
    try:
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("UPDATE Meal SET healthy_index = %s WHERE ID = %s AND User_ID = %s",
                    (new_value % 11, meal_id, user_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Nie znaleziono posiłku lub brak uprawnień")
        conn.commit()

        return {"message": "healthy_index value updated.", "meal": {"id": meal_id, "proteins": new_value}}
    except Exception as e:
        logger.error("Błąd przy aktualizacji białka w posiłku: %s", e)
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


@router.post("/meal_update_kcal")
def meal_update_kcal(
        current_user: dict = Depends(get_current_user),
        meal_id: int = Form(...),
        new_value: int = Form(...),
):
    try:
        sub = current_user["sub"]
        email = current_user.get("email", "")
        user_id = get_or_create_user_by_sub(sub, email)

        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("UPDATE Meal SET kcal = %s WHERE ID = %s AND User_ID = %s", (new_value, meal_id, user_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Nie znaleziono posiłku lub brak uprawnień")
        conn.commit()

        return {"message": "kcal value updated.", "meal": {"id": meal_id, "proteins": new_value}}
    except Exception as e:
        logger.error("Błąd przy aktualizacji białka w posiłku: %s", e)
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


class AppleNotification(BaseModel):
    user_id: int
    subscription_type: int
    original_transaction_id: str
    notification_type: str  # Możliwe wartości: "BUY", "RENEW", "CANCEL"


@router.post("/apple_notification")
def handle_apple_notification(notification: AppleNotification):
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        if notification.notification_type == "BUY":
            # Przy zakupie subskrypcji – tworzymy nowy rekord lub aktualizujemy istniejący:
            cur.execute("""
                INSERT INTO Subscription (User_ID, subscription_type, original_transaction_id, isActive)
                VALUES (%s, %s, %s, 'Y')
                ON CONFLICT (User_ID) DO UPDATE
                  SET subscription_type = EXCLUDED.subscription_type,
                      original_transaction_id = EXCLUDED.original_transaction_id,
                      isActive = 'Y';
            """, (notification.user_id, notification.subscription_type, notification.original_transaction_id))
            conn.commit()
            return {"message": "Subskrypcja kupiona"}

        elif notification.notification_type == "RENEW":
            # Przy odnowieniu – ustawiamy status subskrypcji na aktywny ('Y')
            cur.execute("""
                UPDATE Subscription
                SET isActive = 'Y'
                WHERE original_transaction_id = %s;
            """, (notification.original_transaction_id,))
            conn.commit()
            return {"message": "Subskrypcja odnowiona - aktywna"}

        elif notification.notification_type == "CANCEL":
            # Przy anulowaniu – ustawiamy status subskrypcji jako nieaktywny ('N')
            cur.execute("""
                UPDATE Subscription
                SET isActive = 'N'
                WHERE original_transaction_id = %s;
            """, (notification.original_transaction_id,))
            conn.commit()
            return {"message": "Subskrypcja anulowana"}

        else:
            raise HTTPException(status_code=400, detail="Nieprawidłowy typ powiadomienia")

    except Exception as e:
        logger.error("Błąd przy obsłudze powiadomienia Apple: %s", e)
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