import os
import json
import logging
from datetime import datetime
from functools import wraps

import jwt
from flask import Flask, request, jsonify
from flask_cors import CORS
from asgiref.wsgi import WsgiToAsgi

app = Flask(__name__)
app.config.update(
    SESSION_COOKIE_SAMESITE='None',
    SESSION_COOKIE_SECURE=True,
    SECRET_KEY=os.urandom(32)
)

CORS(app, supports_credentials=True, origins=["*"])

@app.route("/ping")
def ping():
    return jsonify({
        "status": "ALIVE",
        "message": "pong",
        "time": datetime.utcnow().isoformat() + "Z",
        "debug": "100% clean"
    })

asgi_app = WsgiToAsgi(app)
