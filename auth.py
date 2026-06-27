from fastapi import Depends, HTTPException, Header
from sqlalchemy.orm import Session
import base64
import json

from database import get_db
import models

def decode_token_payload(token: str):
    try:
        parts = token.split(".")
        if len(parts) >= 2:
            payload_b64 = parts[1]
            payload_b64 += "=" * (4 - len(payload_b64) % 4)
            payload_bytes = base64.urlsafe_b64decode(payload_b64)
            return json.loads(payload_bytes.decode("utf-8"))
    except Exception as e:
        print(f"Failed to decode token payload: {e}")
    return None

def verify_token(authorization: str = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid or missing token")
        
    token = authorization.split("Bearer ")[1]
    
    if token == "guest":
        # Guest mode - ensure guest user exists in DB to prevent foreign key violation
        uid = "guest"
        email = "guest@example.com"
        display_name = "Guest User"
        user = db.query(models.User).filter(models.User.id == uid).first()
        if not user:
            user = models.User(id=uid, email=email, display_name=display_name)
            db.add(user)
            db.commit()
            db.refresh(user)
        return {"uid": "guest", "email": "guest@example.com", "is_anonymous": True}
        
    # Try to decode JWT payload offline
    payload = decode_token_payload(token)
    github_id = None
    if payload and "sub" in payload:
        uid = payload["sub"]
        email = payload.get("email", f"{uid}@example.com")
        display_name = payload.get("name", "GitHub User")
        
        # Extract GitHub numeric ID from Firebase identities
        firebase_ident = payload.get("firebase", {})
        identities = firebase_ident.get("identities", {})
        github_ids = identities.get("github.com", [])
        if github_ids:
            github_id = str(github_ids[0])
    else:
        # Fallback to mock behavior
        uid = token[:30]
        email = f"{uid}@example.com"
        display_name = "GitHub User"
        if token == "admin-mock":
            github_id = "67980315"
        
    # Check if user exists in DB
    user = db.query(models.User).filter(models.User.id == uid).first()
    if not user:
        # Auto-register user
        user = models.User(id=uid, email=email, display_name=display_name)
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        # Sync user info if it has changed
        if user.email != email or user.display_name != display_name:
            user.email = email
            user.display_name = display_name
            db.commit()
            db.refresh(user)
        
    return {"uid": user.id, "email": user.email, "is_anonymous": False, "github_id": github_id}
