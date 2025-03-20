# config.py
import json
import boto3
from botocore.exceptions import ClientError

def get_secret():
    secret_name = "foodio-secrets"
    region_name = "eu-north-1"

    session = boto3.session.Session()
    client = session.client(
        service_name='secretsmanager',
        region_name=region_name
    )

    try:
        get_secret_value_response = client.get_secret_value(SecretId=secret_name)
    except ClientError as e:
        raise e

    secret = get_secret_value_response['SecretString']
    return secret

secrets_data = json.loads(get_secret())

# OpenAI
OPENAI_API_KEY = secrets_data.get("OPENAI_API_KEY")

# Konfiguracja bazy danych
DB_HOST = secrets_data.get("DB_HOST")
DB_PORT = secrets_data.get("DB_PORT")
DB_NAME = secrets_data.get("DB_NAME")
DB_USER = secrets_data.get("DB_USER")
DB_PASS = secrets_data.get("DB_PASS")

# Konfiguracja S3
AWS_ACCESS_KEY_ID = secrets_data.get("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = secrets_data.get("AWS_SECRET_ACCESS_KEY")
AWS_REGION = secrets_data.get("AWS_REGION")
S3_BUCKET_NAME = secrets_data.get("S3_BUCKET_NAME")

# Konfiguracja Cognito
COGNITO_REGION = secrets_data.get("COGNITO_REGION")
USER_POOL_ID = secrets_data.get("USER_POOL_ID")
COGNITO_APP_CLIENT_ID = secrets_data.get("COGNITO_APP_CLIENT_ID")
JWKS_URL = f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{USER_POOL_ID}/.well-known/jwks.json"

# Konfiguracja Apple
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
APPLE_CLIENT_ID = secrets_data.get("APPLE_CLIENT_ID")


APPLE_KEY_ID = secrets_data.get("APPLE_KEY_ID")
APPLE_ISSUER_ID = secrets_data.get("APPLE_ISSUER_ID")
APPLE_PRIVATE_KEY = secrets_data.get("APPLE_PRIVATE_KEY")