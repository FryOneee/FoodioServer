from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Depends, Request
from datetime import datetime, date, timedelta
from psycopg2.extras import RealDictCursor

import json
import re
import io
import logging
import requests  # jeśli nie został wcześniej zaimportowany

from PIL import Image
import openai
import boto3

from auth import get_current_user
from db import get_db_connection, get_or_create_user_by_sub
from config import S3_BUCKET_NAME, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION, OPENAI_API_KEY
from config import USER_POOL_ID


from checkSubscription import check_subscription_add_meal, verify_apple_subscribe_active, decode_apple_receipt
from OpenAI_requests import query_meal_nutrients, new_goal,meals_from_barcode_problems
from openfoodfacts_api import getInfoFromOpenFoodsApi





@router.post("/add_meal_from_barcode")
def add_meal_from_barcode(
        current_user: dict = Depends(get_current_user),
        latitude: float = Form(...),
        longitude: float = Form(...),
        apple_receipt: str = Form(...),   # <-- Nowy parametr z paragonem Apple
        barcode: str = File(...)
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
            conn.rollback()
            return subscription_response

        # Pobierz kontekst użytkownika (problemy, dieta)
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

        # Pobranie danych z OpenFoodsAPI
        name, kcal, proteins, carbs, fats, ingredients, image_front_url = getInfoFromOpenFoodsApi(barcode)

        # Analiza problemów w składnikach przy użyciu ChatGPT
        if len(ingredients) > 0:
            problems_result, _ = meals_from_barcode_problems(name, ingredients, user_context)
        else:
            problems_result={
                "healthy_index": -1,
                "problems": []
            }


        healthy_index_val = problems_result.get("healthy_index", -1)
        problems_val = problems_result.get("problems", [])

        image_response = requests.get(image_front_url)
        if image_response.status_code != 200:
            raise Exception("Błąd pobierania obrazka z URL-a.")
        original_file_contents = image_response.content

        file_name = f"{user_id}_{int(now.timestamp())}_{name.replace(' ', '_')}.png"
        s3.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=file_name,
            Body=original_file_contents
        )

        # Przygotowanie miniatury do wysłania (jeśli jest potrzebna w dalszych operacjach)
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

        # --- 3. Zapisujemy nowe problemy (jeśli wykryto) i zwracamy ich rekordy ---
        problems_with_id = []
        for problem in problems_val:
            cur.execute("INSERT INTO Problem (User_ID, description) VALUES (%s, %s) RETURNING ID", (user_id, problem))
            problem_id = cur.fetchone()[0]
            problems_with_id.append({"id": problem_id, "description": problem})
        conn.commit()

        # --- 4. Wstawiamy rekord do tabeli Meal korzystając z danych z OpenFoodsAPI ---
        cur.execute("""
            INSERT INTO Meal(
                User_ID,
                name,
                barcode,
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
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING ID
        """, (
            user_id, name, barcode, file_name, kcal,
            proteins, carbs, fats,
            now, healthy_index_val, latitude, longitude, False
        ))
        meal_id = cur.fetchone()[0]
        conn.commit()

        # --- Zbudowanie danych posiłku na podstawie danych wejściowych, bez ponownego pobierania z serwera ---
        meal_data = {
            "id": meal_id,
            "name": name,
            "img_link": file_name,
            "kcal": kcal,
            "proteins": proteins,
            "carbs": carbs,
            "fats": fats,
            "healthy_index": healthy_index_val,
            "latitude": str(latitude),
            "longitude": str(longitude),
            "date": now.isoformat(),
            "added": False,
            "warnings": []  # Zakładamy, że warningi nie zostały pobrane z serwera
        }

        # --- 5. Zapisz rekord do OpenAI_request (liczenie zapytań) ---
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
            "warnings": meal_data["warnings"],
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