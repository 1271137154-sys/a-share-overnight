import hashlib
import hmac
from fastapi import HTTPException, Request, status
from .config import settings

def is_admin(request: Request) -> bool:
    return bool(request.session.get("admin"))

def authenticate(username: str, password: str) -> bool:
    candidate = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return bool(settings.admin_password_hash) and hmac.compare_digest(username, settings.admin_username) and hmac.compare_digest(candidate, settings.admin_password_hash)

def require_admin(request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="需要管理员登录")
