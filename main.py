# main.py
from fastapi import FastAPI
from endpoints import router as api_router
from db import create_database_if_not_exists, initialize_schema
from logging_config import setup_logging

from db import get_db_connection

logger = setup_logging()
app = FastAPI()

@app.on_event("startup")
def initialize_database():
    logger.info("Uruchamianie serwera - inicjalizacja bazy danych")
    try:
        conn = get_db_connection()
        conn.close()
        logger.info("Baza danych jest dostępna.")
    except Exception:
        create_database_if_not_exists()
    initialize_schema()

app.include_router(api_router)