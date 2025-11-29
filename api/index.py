import os
import json
import logging
from datetime import datetime
from functools import wraps

import jwt
from flask import Flask, request, jsonify
from flask_cors import CORS
from asgiref.wsgi import WsgiToAsgi

import requests
from libsql_client import create_client_sync, LibsqlError

app = Flask(__name__)
app.config.update(
    SESSION_COOKIE_SAMESITE='None',
    SESSION_COOKIE_SECURE=True,
    SECRET_KEY=os.urandom(32)
)

CORS(app, supports_credentials=True, origins=["*"])

# ==================== TURSO — БЕЗОПАСНЫЙ РЕЖИМ ====================


client = None
db_ready = False

try:
    TURSO_URL = os.getenv("TURSO_DATABASE_URL")
    TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

    if TURSO_URL and TURSO_TOKEN and TURSO_URL.startswith("libsql://"):
        client = create_client_sync(url=TURSO_URL, auth_token=TURSO_TOKEN)
        client.execute("SELECT 1")
        db_ready = True
        logging.info("Turso подключён!")
except Exception as e:
    logging.warning(f"Turso недоступен: {e} — работаем без БД")
    client = None

def execute_query(query, params=None):
    if not client:
        class Fake: rows=[]; last_insert_rowid=1; rows_affected=0
        return Fake()
    try:
        return client.execute(query, params or ())
    except Exception as e:
        logging.error(f"Turso error: {e}")
        class Fake: rows=[]; last_insert_rowid=0; rows_affected=0
        return Fake()



def upload_to_blob(file_stream, filename, content_type="image/jpeg"):
    token = os.getenv("VERCEL_BLOB_READ_WRITE_TOKEN")
    if not token:
        return None

    url = "https://blob.vercel-storage.com"
    headers = {"authorization": f"Bearer {token}"}

    # Генерируем имя файла
    name = f"photos/{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{filename}"

    response = requests.put(f"{url}/{name}", data=file_stream, headers={
        **headers,
        "x-upload-mode": "raw",
        "content-type": content_type
    })

    if response.status_code == 200:
        return f"https://blob.vercel-storage.com/{name}"
    else:
        logging.error(f"Blob upload failed: {response.text}")
        return None


@app.route("/ping")
def ping():
    return jsonify({
        "status": "ALIVE",
        "message": "pong",
        "time": datetime.utcnow().isoformat() + "Z",
        "debug": "100% clean"
    })

asgi_app = WsgiToAsgi(app)
