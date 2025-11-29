import os
import json
import logging
from datetime import datetime
from functools import wraps

import jwt
from flask import Flask, request, jsonify, redirect, send_file
from flask_cors import CORS
from asgiref.wsgi import WsgiToAsgi

from libsql_client import create_client_sync, LibsqlError
#import vercel_blob
# ==================== Flask app ====================
app = Flask(__name__)

# На Vercel в продакшене куки с Secure и SameSite=None работают только по HTTPS
app.config.update(
    SESSION_COOKIE_SAMESITE='None',
    SESSION_COOKIE_SECURE=True,
    SECRET_KEY=os.urandom(32)  # лучше генерировать каждый раз в serverless
)

CORS(app, supports_credentials=True, origins=["*"])  # подстрой под свои домены в проде

# ==================== Конфиги ====================
JWT_ALGO = "HS256"

# Vercel Blob — автоматически доступен в serverless функциях
#blob_storage = vercel_blob


# Turso клиент (синхронный — идеально для Vercel Python)
JWT_SECRET = os.getenv("JWT_SECRET", "12345")
VERCEL_BLOB_READ_WRITE_TOKEN = os.getenv("VERCEL_BLOB_READ_WRITE_TOKEN")

# ==================== Turso — АНТИКРАШ РЕЖИМ (для Vercel) ====================
TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

client = None
db_ready = False

def execute_query(query, params=None):
    logging.warning(f"[FAKE DB] Запрос пропущен: {query[:60]}...")
    class Fake:
        rows = []
        last_insert_rowid = 999
        rows_affected = 0
    return Fake()

def init_db():
    logging.info("init_db() отключена — работаем в режиме без БД")
    # Ничего не делаем
    pass


# ==================== JWT хелперы ====================
def extract_token():
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None
    return auth.split(" ", 1)[1]


def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = extract_token()
        if not token:
            return jsonify({"error": "Токен отсутствует"}), 401
        try:
            data = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
            request.current_user = data
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Токен истёк"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Невалидный токен"}), 401
        return f(*args, **kwargs)
    return decorated


# ==================== Вызов инициализации (один раз при старте) ====================
# На Vercel функция стартует заново при каждом вызове, но init_db idempotent — можно вызывать всегда
#with app.app_context():
#    init_db()


# ==================== Пример роута (чтобы проверить) ====================
@app.route("/")
def home():
    return jsonify({
        "message": "Flask + Turso + Vercel Blob работает!",
        "turso_connected": True
    })


# ==================== Vercel handler ====================
asgi_app = WsgiToAsgi(app)

def handler(event, context=None):
    return asgi_app(event, context or {})


# ==================== Аутентификация и пользователи ====================

def get_current_user():
    """Возвращает декодированный payload из токена (или None)"""
    token = extract_token()
    if not token:
        return None
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def user_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return jsonify({"status": "error", "message": "Неверный или отсутствующий токен"}), 401
        request.current_user = user
        return f(*args, **kwargs)

    return decorated


def admin_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user or user.get("role") != "admin":
            return jsonify({"status": "error", "message": "Требуется роль admin"}), 403
        request.current_user = user
        return f(*args, **kwargs)

    return decorated


# Пока не используется Telegram ID, но если понадобится — вот адаптированная версия:
def find_user_by_telegram_username(username: str):
    if not username:
        return None
    result = execute_query(
        "SELECT id, telegram_username AS username, role, full_name FROM users WHERE telegram_username = ?",
        (username,)
    )
    row = result.rows[0] if result.rows else None
    return dict(row) if row else None


def get_user_associated_object_ids(user_id: int):
    result = execute_query(
        "SELECT object_id FROM appointment WHERE user_id = ?",
        (user_id,)
    )
    return [row[0] for row in result.rows]


# ==================== Пинг ====================
@app.route("/ping")
def ping():
    return jsonify({
        "status": "ALIVE",
        "message": "pong",
        "time": datetime.utcnow().isoformat() + "Z",
        "turso": "disabled for debug",
        "blob": "ready" if 'vercel_blob' in globals() else "missing"
    })


# ==================== Загрузка маркера + фото в Vercel Blob ====================
@app.route('/upload', methods=['POST'])
@user_auth
def upload():
    try:
        lat = request.form.get('lat')
        lon = request.form.get('lon')
        note = request.form.get('note', '').strip()
        breed = request.form.get('breed', '').strip()

        object_id_str = request.form.get('object_id')
        object_id = int(object_id_str) if object_id_str and object_id_str.isdigit() else None

        color = request.form.get('color', 'green').strip()
        if color not in ['green', 'red', 'blue', 'yellow', 'black']:
            color = 'green'

        photos = request.files.getlist('photos[]')

        if not lat or not lon:
            return jsonify({'status': 'error', 'message': 'Координаты не указаны'}), 400

        try:
            lat = float(lat)
            lon = float(lon)
        except ValueError:
            return jsonify({'status': 'error', 'message': 'Неверный формат координат'}), 400

        created_at = datetime.utcnow().isoformat() + "Z"

        # 1. Создаём маркер в Turso
        result = execute_query("""
            INSERT INTO markers (lat, lon, note, breed, object_id, color, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (lat, lon, note or None, breed or None, object_id, color, created_at))

        marker_id = result.last_insert_rowid

        saved_photos = []

        # 2. Загружаем каждое фото в Vercel Blob
        for photo in photos:
            if not photo or not photo.filename:
                continue

            # Генерируем уникальное имя
            timestamp = datetime.now().strftime('%Y%m%d%H%M%S%f')
            ext = os.path.splitext(photo.filename)[1].lower() or '.jpg'
            blob_key = f"photos/{marker_id}_{timestamp}{ext}"

            # Загружаем напрямую из потока
            #blob_result = blob_storage.put(
                blob_key,
                photo.stream,
                {
                    "content_type": photo.content_type or "application/octet-stream",
                    # Автоматически делаем публичным
                    "add_random_suffix": False,
                    "token": os.getenv("VERCEL_BLOB_TOKEN")  # необязательно, если дефолтный доступ есть
                }
            )

            # Получаем публичный URL
            public_url = blob_result.url

            # Сохраняем в БД
            execute_query("""
                INSERT INTO photos (marker_id, filename, blob_path)
                VALUES (?, ?, ?)
            """, (marker_id, photo.filename, blob_key))

            saved_photos.append({
                "original_name": photo.filename,
                "url": public_url
            })

        return jsonify({
            'status': 'success',
            'marker': {
                'id': marker_id,
                'lat': lat,
                'lon': lon,
                'note': note,
                'breed': breed,
                'object_id': object_id,
                'color': color,
                'created_at': created_at,
                'photos': saved_photos
            }
        })

    except Exception as e:
        logging.error(f"Ошибка при загрузке маркера: {e}")
        return jsonify({'status': 'error', 'message': 'Внутренняя ошибка сервера'}), 500




# ==================== Получение маркеров ====================

@app.route('/markers')
@user_auth
def get_markers():
    user_id = request.current_user.get('id')
    if not user_id:
        return jsonify({'status': 'error', 'message': 'User ID missing'}), 401

    allowed_object_ids = get_user_associated_object_ids(user_id)
    if not allowed_object_ids:
        return jsonify({'status': 'success', 'markers': []})

    placeholders = ', '.join(['?' for _ in allowed_object_ids])
    query = f"""
        SELECT m.id, m.lat, m.lon, m.color, m.object_id, o.name AS object_name
        FROM markers m
        LEFT JOIN objects o ON m.object_id = o.id
        WHERE m.object_id IN ({placeholders})
        ORDER BY m.created_at DESC
    """

    result = execute_query(query, allowed_object_ids)
    markers = [
        {
            'id': row['id'],
            'lat': row['lat'],
            'lon': row['lon'],
            'color': row['color'],
            'object_id': row['object_id'],
            'object_name': row['object_name']
        }
        for row in result.rows
    ]

    return jsonify({'status': 'success', 'markers': markers})


@app.route('/markers/by-object/<int:object_id>')
@admin_auth
def get_markers_by_object(object_id):
    result = execute_query("""
        SELECT id, lat, lon, note, breed, color, created_at, object_id
        FROM markers
        WHERE object_id = ?
        ORDER BY created_at DESC
    """, (object_id,))

    markers = [
        {
            'id': row['id'],
            'lat': row['lat'],
            'lon': row['lon'],
            'note': row['note'] or '',
            'breed': row['breed'] or '',
            'color': row['color'],
            'created_at': row['created_at'],
            'object_id': row['object_id']
        }
        for row in result.rows
    ]

    return jsonify({'status': 'success', 'markers': markers})


@app.route('/marker/<int:marker_id>')
@user_auth
def get_marker(marker_id):
    # Основные данные маркера
    result = execute_query("""
        SELECT m.*, o.name AS object_name
        FROM markers m
        LEFT JOIN objects o ON m.object_id = o.id
        WHERE m.id = ?
    """, (marker_id,))

    if not result.rows:
        return jsonify({'status': 'error', 'message': 'Marker not found'}), 404

    marker = dict(result.rows[0])

    # Фото с публичными URL
    photos_result = execute_query("""
        SELECT filename, blob_path FROM photos WHERE marker_id = ?
    """, (marker_id,))

    photos = []
    for row in photos_result.rows:
        blob_path = row['blob_path']
        try:
          #  blob_info = blob_storage.head(blob_path)
            url = blob_info.url if blob_info else None
        except:
            url = None
        photos.append({
            "original_name": row['filename'],
            "url": url
        })

    return jsonify({
        'status': 'success',
        'marker': {
            'id': marker['id'],
            'lat': marker['lat'],
            'lon': marker['lon'],
            'note': marker.get('note') or '',
            'breed': marker.get('breed') or '',
            'object_id': marker.get('object_id'),
            'object_name': marker.get('object_name'),
            'color': marker['color'],
            'created_at': marker['created_at'],
            'photos': photos
        }
    })


@app.route('/marker/<int:marker_id>', methods=['POST'])
@user_auth
def edit_marker(marker_id):
    try:
        # Проверяем, существует ли маркер
        check = execute_query("SELECT id FROM markers WHERE id = ?", (marker_id,))
        if not check.rows:
            return jsonify({'status': 'error', 'message': 'Marker not found'}), 404

        note = request.form.get('note', '').strip()
        breed = request.form.get('breed', '').strip()
        color = request.form.get('color', 'green').strip()
        if color not in ['green', 'red', 'blue', 'yellow', 'black']:
            color = 'green'

        object_id = None
        if request.form.get('object_id'):
            try:
                object_id = int(request.form.get('object_id'))
            except ValueError:
                pass

        # Обновляем маркер
        execute_query("""
            UPDATE markers 
            SET note = ?, breed = ?, color = ?, object_id = ?
            WHERE id = ?
        """, (note or None, breed or None, color, object_id, marker_id))

        # Удаляем старые фото, которые не пришли в existing_photos
        raw = request.form.get('existing_photos')
        keep_blob_paths = []
        if raw:
            try:
                keep_original_names = json.loads(raw)
                # Получаем текущие фото
                current = execute_query("SELECT blob_path, filename FROM photos WHERE marker_id = ?", (marker_id,))
                for row in current.rows:
                    if row['filename'] not in keep_original_names:
                        # Удаляем из Blob
                        try:
                         #   blob_storage.delete(row['blob_path'])
                        except:
                            pass  # если уже удалено — ок
                        execute_query("DELETE FROM photos WHERE blob_path = ?", (row['blob_path'],))
                    else:
                        keep_blob_paths.append(row['blob_path'])
            except:
                pass  # если JSON битый — ничего не удаляем

        # Добавляем новые фото
        new_photos = request.files.getlist('photos[]')
        for photo in new_photos:
            if not photo or not photo.filename:
                continue

            timestamp = datetime.now().strftime('%Y%m%d%H%M%S%f')
            ext = os.path.splitext(photo.filename)[1].lower() or '.jpg'
            blob_key = f"photos/{marker_id}_{timestamp}{ext}"

           # blob_storage.put( blob_key, photo.stream, {"content_type": photo.content_type or "image/jpeg"} )

            execute_query("""
                INSERT INTO photos (marker_id, filename, blob_path)
                VALUES (?, ?, ?)
            """, (marker_id, photo.filename, blob_key))

        return jsonify({'status': 'success', 'message': 'Marker updated'})

    except Exception as e:
        logging.error(f"Error editing marker {marker_id}: {e}")
        return jsonify({'status': 'error', 'message': 'Server error'}), 500


@app.route('/marker/<int:marker_id>', methods=['DELETE'])
@user_auth
def delete_marker(marker_id):
    try:
        # Получаем все blob_path фото
        photos = execute_query("SELECT blob_path FROM photos WHERE marker_id = ?", (marker_id,))
        blob_paths = [row['blob_path'] for row in photos.rows]

        # Удаляем из Vercel Blob (можно батчем)
        if blob_paths:
            try:
                #blob_storage.delete(blob_paths)  # поддерживает список!
                print("h")
            except Exception as e:
                logging.warning(f"Не удалось удалить некоторые файлы из Blob: {e}")

        # Удаляем из БД (каскадно удалит photos)
        execute_query("DELETE FROM markers WHERE id = ?", (marker_id,))

        return jsonify({'status': 'success', 'message': 'Marker deleted'})

    except Exception as e:
        logging.error(f"Error deleting marker {marker_id}: {e}")
        return jsonify({'status': 'error', 'message': 'Server error'}), 500



# ==================== Отдача фото из Vercel Blob ====================
@app.route('/photo/<path:blob_path>')
@user_auth
def get_photo(blob_path):
    """
    blob_path — это то, что мы сохраняем в таблице photos.blob_path
    Например: photos/123_20250101...jpg
    """
    try:
        #blob_info = blob_storage.head(blob_path)
        blob_info = 1 
        if not blob_info:
            return jsonify({"status": "error", "message": "Photo not found"}), 404

        # Самый быстрый и дешёвый способ — редирект на публичный URL Vercel Blob
        return redirect(blob_info.url)

        # Если хочешь проксировать через себя (не рекомендуется для больших файлов):
        # stream = blob_storage.get(blob_path)
        # return send_file(stream, mimetype=blob_info.content_type)

    except Exception as e:
        logging.error(f"Ошибка при отдаче фото {blob_path}: {e}")
        return jsonify({"status": "error", "message": "Internal error"}), 500


# ==================== Профиль текущего пользователя ====================
@app.route("/me", methods=["GET"])
@user_auth
def get_me():
    user = request.current_user
    return jsonify({
        "status": "success",
        "user": {
            "telegram_username": user.get("telegram_username"),
            "role": user.get("role"),
            "id": user.get("id")  # полезно для фронта
        }
    })


# ==================== Админ: пользователи ====================
@app.route("/admin/users", methods=["GET"])
@admin_auth
def get_all_users():
    result = execute_query("""
        SELECT id, telegram_username, full_name, phone, role, additional_info
        FROM users
        ORDER BY id
    """)

    users = []
    for row in result.rows:
        users.append({
            "id": row["id"],
            "telegram_username": row["telegram_username"] or "—",
            "full_name": row["full_name"] or "Не указано",
            "phone": row["phone"] or "—",
            "role": row["role"],
            "additional_info": row["additional_info"] or ""
        })

    return jsonify({"status": "success", "users": users})


@app.route("/admin/user", methods=["POST"])
@app.route("/admin/user/<int:user_id>", methods=["GET", "POST"])
@admin_auth
def manage_user(user_id=None):
    if request.method == "GET":
        if not user_id:
            return jsonify({"status": "error", "message": "user_id required"}), 400

        result = execute_query("""
            SELECT telegram_username, full_name, phone, role, additional_info
            FROM users WHERE id = ?
        """, (user_id,))

        if not result.rows:
            return jsonify({"status": "error", "message": "Пользователь не найден"}), 404

        row = result.rows[0]
        return jsonify({
            "status": "success",
            "user": {
                "telegram_username": row["telegram_username"],
                "full_name": row["full_name"],
                "phone": row["phone"],
                "role": row["role"],
                "additional_info": row["additional_info"]
            }
        })

    elif request.method == "POST":
        data = request.get_json(silent=True) or {}
        full_name = data.get("full_name")
        role = data.get("role")
        phone = data.get("phone")
        additional_info = data.get("additional_info")
        telegram_username = data.get("telegram_username")

        if not full_name or not role or role not in ["admin", "user"]:
            return jsonify({
                "status": "error",
                "message": "Обязательные поля (ФИО, Роль) не заполнены или некорректны"
            }), 400

        if user_id:
            # === Обновление пользователя ===
            if telegram_username:
                exists = execute_query(
                    "SELECT 1 FROM users WHERE telegram_username = ? AND id != ?",
                    (telegram_username, user_id)
                )
                if exists.rows:
                    return jsonify({
                        "status": "error",
                        "message": "Пользователь с таким Telegram Username уже существует"
                    }), 400

            execute_query("""
                UPDATE users
                SET telegram_username = ?, full_name = ?, phone = ?, role = ?, additional_info = ?
                WHERE id = ?
            """, (telegram_username, full_name, phone, role, additional_info, user_id))

            return jsonify({"status": "success", "message": "Пользователь обновлён"})

        else:
            # === Создание нового пользователя ===
            if not telegram_username:
                return jsonify({
                    "status": "error",
                    "message": "Для создания пользователя требуется Telegram Username"
                }), 400

            exists = execute_query(
                "SELECT 1 FROM users WHERE telegram_username = ?",
                (telegram_username,)
            )
            if exists.rows:
                return jsonify({
                    "status": "error",
                    "message": "Пользователь с таким Telegram Username уже существует"
                }), 400

            execute_query("""
                INSERT INTO users (telegram_username, role, full_name, phone, additional_info)
                VALUES (?, ?, ?, ?, ?)
            """, (telegram_username, role, full_name, phone, additional_info))

            return jsonify({"status": "success", "message": "Пользователь создан"})


@app.route("/admin/user/<int:user_id>", methods=["DELETE"])
@admin_auth  # Лучше только админу удалять пользователей
def delete_user(user_id):
    # Проверяем, что пользователь не удаляет сам себя (по желанию)
    current_user_id = request.current_user.get("id")
    if current_user_id == user_id:
        return jsonify({"status": "error", "message": "Нельзя удалить самого себя"}), 400

    result = execute_query("DELETE FROM users WHERE id = ?", (user_id,))
    if result.rows_affected == 0:
        return jsonify({"status": "error", "message": "Пользователь не найден"}), 404

    return jsonify({"status": "success", "message": "Пользователь удалён"})



# ==================== Объекты ====================

@app.route("/objects", methods=["GET"])
@user_auth
def get_objects():
    user_id = request.current_user.get("id")
    if not user_id:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    object_ids = get_user_associated_object_ids(user_id)
    if not object_ids:
        return jsonify({"status": "success", "objects": []})

    placeholders = ", ".join(["?" for _ in object_ids])
    query = f"SELECT id, name FROM objects WHERE id IN ({placeholders}) ORDER BY name"

    result = execute_query(query, object_ids)
    objects_list = [{"id": row["id"], "name": row["name"]} for row in result.rows]

    return jsonify({"status": "success", "objects": objects_list})


@app.route("/admin/objects", methods=["GET"])
@admin_auth
def get_all_objects_admin():
    result = execute_query("SELECT id, name FROM objects ORDER BY name")
    objects_list = [{"id": row["id"], "name": row["name"]} for row in result.rows]
    return jsonify({"status": "success", "objects": objects_list})


@app.route("/admin/object", methods=["POST"])
@app.route("/admin/object/<int:object_id>", methods=["GET", "POST", "DELETE"])
@admin_auth
def manage_object(object_id=None):
    current_user_id = request.current_user.get("id")

    if request.method == "GET":
        result = execute_query("SELECT name, additional_info FROM objects WHERE id = ?", (object_id,))
        if not result.rows:
            return jsonify({"status": "error", "message": "Объект не найден"}), 404
        row = result.rows[0]
        return jsonify({
            "status": "success",
            "object": {"name": row["name"], "additional_info": row["additional_info"] or ""}
        })

    elif request.method == "DELETE":
        result = execute_query("DELETE FROM objects WHERE id = ?", (object_id,))
        if result.rows_affected == 0:
            return jsonify({"status": "error", "message": "Объект не найден"}), 404
        return jsonify({"status": "success", "message": "Объект успешно удалён"})

    elif request.method == "POST":
        data = request.get_json(silent=True) or {}
        name = data.get("name", "").strip()
        additional_info = data.get("additional_info", "").strip()

        if not name:
            return jsonify({"status": "error", "message": "Название объекта обязательно"}), 400

        if object_id:
            # Обновление
            execute_query(
                "UPDATE objects SET name = ?, additional_info = ? WHERE id = ?",
                (name, additional_info, object_id)
            )
            message = "Объект обновлён"
        else:
            # Создание + автоматическое назначение создателю
            try:
                result = execute_query(
                    "INSERT INTO objects (name, additional_info) VALUES (?, ?)",
                    (name, additional_info)
                )
                new_object_id = result.last_insert_rowid
                execute_query(
                    "INSERT INTO appointment (user_id, object_id) VALUES (?, ?)",
                    (current_user_id, new_object_id)
                )
                message = "Объект создан и назначен вам"
            except Exception as e:
                if "UNIQUE constraint failed" in str(e):
                    return jsonify({"status": "error", "message": "Объект с таким названием уже существует"}), 400
                raise

        return jsonify({"status": "success", "message": message})


# ==================== Назначения (appointments) ====================

@app.route('/assignments', methods=['GET'])
@admin_auth
def get_assignments_data():
    users_res = execute_query("SELECT id, telegram_username, full_name FROM users ORDER BY id")
    objects_res = execute_query("SELECT id, name FROM objects ORDER BY name")
    appointments_res = execute_query("SELECT user_id, object_id FROM appointment")

    users = [
        {"id": r["id"], "telegram_username": r["telegram_username"] or "", "full_name": r["full_name"] or "—"}
        for r in users_res.rows
    ]
    objects = [{"id": r["id"], "name": r["name"]} for r in objects_res.rows]
    assigned_pairs = [f"{r['user_id']}_{r['object_id']}" for r in appointments_res.rows]

    return jsonify({
        "users": users,
        "objects": objects,
        "assignments": assigned_pairs
    })


@app.route('/assignments/toggle', methods=['POST'])
@admin_auth
def toggle_assignment():
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    object_id = data.get("object_id")

    if not user_id or not object_id:
        return jsonify({"status": "error", "message": "Missing IDs"}), 400

    try:
        user_id = int(user_id)
        object_id = int(object_id)
    except ValueError:
        return jsonify({"status": "error", "message": "Invalid IDs"}), 400

    exists_res = execute_query(
        "SELECT 1 FROM appointment WHERE user_id = ? AND object_id = ?",
        (user_id, object_id)
    )
    exists = len(exists_res.rows) > 0

    if exists:
        execute_query(
            "DELETE FROM appointment WHERE user_id = ? AND object_id = ?",
            (user_id, object_id)
        )
        action = "removed"
    else:
        execute_query(
            "INSERT INTO appointment (user_id, object_id) VALUES (?, ?)",
            (user_id, object_id)
        )
        action = "added"

    return jsonify({
        "status": "success",
        "action": action,
        "user_id": user_id,
        "object_id": object_id
    })
