"""Opaque Redis administrator sessions; browser writes require a CSRF token."""
import hashlib
import hmac
import json
import os
import secrets

from flask import Blueprint, g, jsonify, redirect, request
from redis.exceptions import RedisError

api = Blueprint("admin_auth", __name__)
COOKIE = "modeltrace_admin"
SESSION_SECONDS = 8 * 3600


def session_key(value):
    return "modeltrace:admin:session:" + hashlib.sha256(value.encode()).hexdigest()


def bearer_admin():
    token = os.getenv("MODELTRACE_API_TOKEN", "")
    return bool(token and hmac.compare_digest(request.headers.get("Authorization", "").encode(), f"Bearer {token}".encode()))


def same_origin():
    origin = request.headers.get("Origin")
    return not origin or origin == request.host_url.rstrip("/")


@api.before_app_request
def authorize_admin():
    protected = request.path.startswith("/status/api/") or request.path.startswith("/admin/api/") or request.path == "/admin"
    if not protected or request.path == "/admin/api/login":
        return None
    from detection_api import store
    try:
        g.admin_bearer = bearer_admin()
        raw = store().redis.get(session_key(request.cookies.get(COOKIE, ""))) if request.cookies.get(COOKIE) else None
        g.admin_session = json.loads(raw) if raw else None
        token_version = hashlib.sha256(os.getenv("MODELTRACE_API_TOKEN", "").encode()).hexdigest()
        if g.admin_session and not hmac.compare_digest(g.admin_session.get("token_version", ""), token_version):
            g.admin_session = None
        if not g.admin_bearer and not g.admin_session:
            if request.path == "/admin":
                return redirect("/admin/login")
            return jsonify(error="请先登录管理员后台"), 401
        if not g.admin_bearer and request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not same_origin() or not hmac.compare_digest(request.headers.get("X-CSRF-Token", "").encode(), g.admin_session["csrf"].encode()):
                return jsonify(error="请求校验失败，请刷新后台重试"), 403
        from monitor_store import MonitorStore
        MonitorStore(store()).migrate_visibility()
    except (RedisError, ValueError):
        return jsonify(error="管理服务暂不可用"), 503


@api.post("/admin/api/login")
def login():
    from detection_api import store
    if not same_origin():
        return jsonify(error="请求来源无效"), 403
    payload = request.get_json(silent=True)
    token = os.getenv("MODELTRACE_API_TOKEN", "")
    if not token:
        return jsonify(error="服务未配置管理令牌"), 503
    try:
        redis = store().redis
        limiter = "modeltrace:admin:login:" + hashlib.sha256((request.remote_addr or "unknown").encode()).hexdigest()
        with redis.pipeline() as pipe:
            pipe.incr(limiter)
            pipe.expire(limiter, 900, nx=True)
            attempts = pipe.execute()[0]
        if attempts > 5:
            return jsonify(error="登录尝试过多，请稍后再试"), 429, {"Retry-After": str(max(1, redis.ttl(limiter)))}
        supplied = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(supplied, str) or not hmac.compare_digest(supplied.encode(), token.encode()):
            return jsonify(error="管理令牌不正确"), 401
        redis.delete(limiter)
        old = request.cookies.get(COOKIE)
        if old:
            redis.delete(session_key(old))
        sid = secrets.token_urlsafe(32)
        session = {"csrf": secrets.token_urlsafe(32), "token_version": hashlib.sha256(token.encode()).hexdigest()}
        redis.set(session_key(sid), json.dumps(session), ex=SESSION_SECONDS)
        response = jsonify(csrf=session["csrf"])
        response.set_cookie(COOKIE, sid, max_age=SESSION_SECONDS, httponly=True, secure=request.is_secure, samesite="Lax", path="/")
        response.headers["Cache-Control"] = "no-store"
        return response
    except (RedisError, ValueError):
        return jsonify(error="管理服务暂不可用"), 503


@api.get("/admin/api/session")
def session():
    return jsonify(csrf=g.admin_session["csrf"] if g.admin_session else None)


@api.post("/admin/api/logout")
def logout():
    from detection_api import store
    sid = request.cookies.get(COOKIE)
    if sid:
        store().redis.delete(session_key(sid))
    response = jsonify(ok=True)
    response.delete_cookie(COOKIE, path="/")
    return response


@api.errorhandler(RedisError)
def unavailable(error):
    return jsonify(error="管理服务暂不可用"), 503


@api.after_app_request
def admin_headers(response):
    if request.path.startswith(("/admin", "/status/api/", "/public/api/")):
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    return response
