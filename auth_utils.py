import re
import uuid
import hashlib
import jwt
from datetime import datetime, timedelta
from functools import wraps
from flask import request, jsonify, g
from werkzeug.security import generate_password_hash, check_password_hash

from config import JWT_SECRET, JWT_EXPIRE_DAYS
from extensions import db
from models import Client, Admin, RevokedToken


def legacy_sha256_password(pwd):
    return hashlib.sha256(pwd.encode()).hexdigest()

def hash_password(pwd):
    return generate_password_hash(pwd, method="pbkdf2:sha256", salt_length=16)

def password_hash_is_legacy(password_hash):
    return bool(password_hash and re.fullmatch(r"[a-f0-9]{64}", password_hash))

def verify_and_upgrade_password(user, pwd):
    """校验密码；旧 SHA256 哈希登录成功后自动升级为 werkzeug PBKDF2。"""
    if not user or not user.password_hash:
        return False
    if password_hash_is_legacy(user.password_hash):
        if user.password_hash != legacy_sha256_password(pwd):
            return False
        user.password_hash = hash_password(pwd)
        db.session.commit()
        return True
    return check_password_hash(user.password_hash, pwd)

def generate_token(client_id: int, email: str, password_hash: str) -> str:
    payload = {
        "jti": str(uuid.uuid4()),
        "sub": str(client_id),
        "email": email,
        "phf": hashlib.sha256(password_hash.encode()).hexdigest()[:16],
        "exp": datetime.utcnow() + timedelta(days=JWT_EXPIRE_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

def generate_admin_token(admin_id: int, email: str, password_hash: str) -> str:
    payload = {
        "sub": str(admin_id),
        "email": email,
        "phf": hashlib.sha256(password_hash.encode()).hexdigest()[:16],
        "role": "admin",
        "exp": datetime.utcnow() + timedelta(days=JWT_EXPIRE_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

def verify_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except Exception:
        return None

def is_token_revoked(jti):
    if not jti:
        return True
    return RevokedToken.query.filter_by(jti=jti).first() is not None

def require_client(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get("bw_session")
        if not token:
            return jsonify({"error": "unauthorized"}), 401
        payload = verify_token(token)
        if not payload:
            return jsonify({"error": "unauthorized"}), 401
        if is_token_revoked(payload.get("jti")):
            return jsonify({"error": "unauthorized"}), 401
        try:
            client_id = int(payload.get("sub"))
        except Exception:
            return jsonify({"error": "unauthorized"}), 401
        email = payload.get("email")
        client = Client.query.get(client_id)
        if not client or client.email != email:
            return jsonify({"error": "unauthorized"}), 401
        if not client.password_hash:
            return jsonify({"error": "unauthorized"}), 401
        phf = hashlib.sha256(client.password_hash.encode()).hexdigest()[:16]
        if payload.get("phf") != phf:
            return jsonify({"error": "unauthorized", "error_code": "TOKEN_EXPIRED"}), 401
        g.client = client
        return f(*args, **kwargs)
    return decorated

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # 从 httpOnly cookie 读取，JS 完全无法访问
        token = request.cookies.get("admin_session")
        if not token:
            return jsonify({"error": "unauthorized"}), 401
        payload = verify_token(token)
        if not payload or payload.get("role") != "admin":
            return jsonify({"error": "unauthorized"}), 401
        try:
            admin_id = int(payload.get("sub"))
        except Exception:
            return jsonify({"error": "unauthorized"}), 401
        admin = Admin.query.get(admin_id)
        if not admin or admin.email != payload.get("email"):
            return jsonify({"error": "unauthorized"}), 401
        if not admin.password_hash:
            return jsonify({"error": "unauthorized"}), 401
        phf = hashlib.sha256(admin.password_hash.encode()).hexdigest()[:16]
        if payload.get("phf") != phf:
            return jsonify({"error": "unauthorized", "error_code": "TOKEN_EXPIRED"}), 401
        g.admin = admin
        return f(*args, **kwargs)
    return decorated
