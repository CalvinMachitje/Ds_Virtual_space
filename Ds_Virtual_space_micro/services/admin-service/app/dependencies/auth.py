# Authentication dependencies for the Admin Service, including JWT token validation.
# services/admin-service/app/dependencies/auth.py
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from app.core.config import settings

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

async def get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=["HS256"])
        user_id: str = payload.get("sub")
        if user_id is None or payload.get("role") != "admin":
            raise HTTPException(status_code=401, detail="Invalid admin token")
        return user_id
    except JWTError:
        raise HTTPException(status_code=401, detail="Could not validate credentials")