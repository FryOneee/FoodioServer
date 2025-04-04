import psycopg2
from datetime import datetime, date, timedelta
import logging
from config import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASS

logger = logging.getLogger("server_logger")


def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASS
    )


def create_database_if_not_exists():
    try:
        logger.info("Sprawdzanie istnienia bazy danych: %s", DB_NAME)
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


def initialize_schema():
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        logger.info("Inicjalizacja schematu bazy danych zgodnie z nową specyfikacją")

        # Tabela Goal z autoinkrementacją
        cur.execute("""
            CREATE TABLE IF NOT EXISTS Goal (
                ID int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                User_ID int NOT NULL,
                kcal int NOT NULL,
                protein int NOT NULL,
                fats int NOT NULL,
                carbs int NOT NULL,
                desiredWeight numeric(3,1) NOT NULL,
                lifestyle varchar(50) NOT NULL,
                startDate date NOT NULL,
                endDate date NOT NULL
            );
        """)

        # Tabela Warning z autoinkrementacją
        cur.execute("""
            CREATE TABLE IF NOT EXISTS Warning (
                ID int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                Meal_ID int NOT NULL,
                warning text NOT NULL
            );
        """)

        # Tabela Meal z autoinkrementacją
        cur.execute("""
            CREATE TABLE IF NOT EXISTS Meal (
                ID int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                User_ID int NOT NULL,
                name varchar(255) NOT NULL,
                bar_code varchar(100) NULL,
                img_link varchar(255) NOT NULL,
                kcal int NOT NULL,
                proteins int NOT NULL,
                carbs int NOT NULL,
                fats int NOT NULL,
                date timestamp NOT NULL,
                healthy_index int NOT NULL,
                latitude decimal(9,6) NULL,
                longitude decimal(9,6) NULL,
                added boolean NULL
            );
        """)

        # Tabela OpenAI_request z autoinkrementacją
        cur.execute("""
            CREATE TABLE IF NOT EXISTS OpenAI_request (
                ID int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                User_ID int NOT NULL,
                type char(1) NOT NULL,
                img_link varchar(255) NULL,
                date timestamp NOT NULL
            );
        """)

        # Tabela Problem z autoinkrementacją
        cur.execute("""
            CREATE TABLE IF NOT EXISTS Problem (
                ID int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                User_ID int NOT NULL,
                description varchar(100) NOT NULL
            );
        """)

        # Tabela Subscription z autoinkrementacją
        cur.execute("""
            CREATE TABLE IF NOT EXISTS Subscription (
                ID int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                User_ID int NOT NULL,
                subscription_type int NOT NULL,
                original_transaction_id text NOT NULL,
                isActive char(1) NOT NULL
            );
        """)

        # Tabela User z autoinkrementacją
        cur.execute("""
            CREATE TABLE IF NOT EXISTS "User" (
                ID int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                email varchar(255) NOT NULL,
                password varchar(255) NULL,
                sex char(1) NULL,
                birthDate date NULL,
                height int NULL,
                diet varchar(70) NULL,
                dateOfJoin date NOT NULL
            );
        """)

        # Tabela Weight z autoinkrementacją
        cur.execute("""
            CREATE TABLE IF NOT EXISTS Weight (
                ID int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                User_ID int NOT NULL,
                weight numeric(3,1) NOT NULL,
                date date NOT NULL
            );
        """)

        # Dodawanie ograniczeń kluczy obcych
        alter_commands = [
            """ALTER TABLE Goal ADD CONSTRAINT Goal_User
                FOREIGN KEY (User_ID)
                REFERENCES "User" (ID)
                NOT DEFERRABLE
                INITIALLY IMMEDIATE;""",
            """ALTER TABLE OpenAI_request ADD CONSTRAINT OpenAI_request_User
                FOREIGN KEY (User_ID)
                REFERENCES "User" (ID)
                NOT DEFERRABLE
                INITIALLY IMMEDIATE;""",
            """ALTER TABLE Problem ADD CONSTRAINT Problem_User
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
                INITIALLY IMMEDIATE;""",
            """ALTER TABLE Weight ADD CONSTRAINT Weight_User
                FOREIGN KEY (User_ID)
                REFERENCES "User" (ID)
                NOT DEFERRABLE
                INITIALLY IMMEDIATE;""",
            """ALTER TABLE Warning ADD CONSTRAINT Warning_Meal
                FOREIGN KEY (Meal_ID)
                REFERENCES Meal (ID)
                NOT DEFERRABLE
                INITIALLY IMMEDIATE;"""
        ]
        for cmd in alter_commands:
            try:
                cur.execute(cmd)
            except Exception as e:
                logger.warning("Błąd przy dodawaniu ograniczenia: %s", e)

        # Dodanie indeksów
        cur.execute("CREATE INDEX IF NOT EXISTS idx_goal_user ON Goal (User_ID);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_meal_user ON Meal (User_ID);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_meal_date ON Meal (date);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_openai_user ON OpenAI_request (User_ID);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_openai_date ON OpenAI_request (date);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_problem_user ON Problem (User_ID);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_subscription_user ON Subscription (User_ID);")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_user_email ON \"User\" (email);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_weight_user ON Weight (User_ID);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_weight_date ON Weight (date);")

        conn.commit()
        logger.info("Schemat bazy danych został pomyślnie zainicjowany i jest gotowy do użytku.")
    except Exception as e:
        conn.rollback()
        logger.error("Błąd przy inicjalizacji schematu: %s", e)
    finally:
        cur.close()
        conn.close()


def get_or_create_user_by_sub(sub: str, email: str) -> int:
    """
    W nowym schemacie kolumna 'cognito_sub' nie występuje, dlatego identyfikacja użytkownika odbywa się wyłącznie na podstawie adresu email.
    Jeśli użytkownik nie istnieje, wstawiamy rekord z automatycznie generowanym ID.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute('SELECT ID FROM "User" WHERE email = %s', (email,))
        row = cur.fetchone()
        if row:
            user_id = row[0]
            logger.info("Znaleziono istniejącego użytkownika o email: %s", email)
        else:
            cur.execute(
                'INSERT INTO "User"(email) VALUES (%s) RETURNING ID',
                (email,)
            )
            user_id = cur.fetchone()[0]
            conn.commit()
            logger.info("Utworzono nowego użytkownika o email: %s, przy ID: %s", email, user_id)
        return user_id
    finally:
        cur.close()
        conn.close()