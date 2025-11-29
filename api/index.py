import os
import json
import logging
from datetime import datetime
from functools import wraps

import jwt
from flask import Flask, request, jsonify, redirect, send_file
from flask_cors import CORS
from asgiref.wsgi import WsgiToAsgi



# ==================== Flask app ====================
app = Flask(__name__)

# На Vercel в продакшене куки с Secure и SameSite=None работают только по HTTPS
app.config.update(
    SESSION_COOKIE_SAMESITE='None',
    SESSION_COOKIE_SECURE=True,
    SECRET_KEY=os.urandom(32)  # лучше генерировать каждый раз в serverless
)

CORS(app, supports_credentials=True, origins=["*"])  # подстрой под свои домены в проде

asgi_app = WsgiToAsgi(app)
def handler(event, context=None):
    return asgi_app(event, context or {})

@app.route("/ping")
def ping():
    return jsonify({
        "status": "ALIVE",
        "message": "pong",
        "time": datetime.utcnow().isoformat() + "Z",
        "turso": "disabled for debug"        
    })





















