from typing import Dict, Literal

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr


LicenseType = Literal["basic", "premium", "plus"]


class User(BaseModel):
    email: EmailStr
    password: str
    license: LicenseType = "basic"


class CreateUserRequest(BaseModel):
    email: EmailStr
    password: str
    license: LicenseType


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    new_password: str


class AssignLicenseRequest(BaseModel):
    email: EmailStr
    license: LicenseType


app = FastAPI(title="Admin Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory store keyed by email
users: Dict[str, User] = {
    "worker1@example.com": User(email="worker1@example.com", password="pass123", license="basic"),
    "worker2@example.com": User(email="worker2@example.com", password="pass123", license="premium"),
    "worker3@example.com": User(email="worker3@example.com", password="pass123", license="plus"),
    "worker4@example.com": User(email="worker4@example.com", password="pass123", license="basic"),
    "worker5@example.com": User(email="worker5@example.com", password="pass123", license="premium"),
}


@app.get("/users")
def search_users(email: str = Query("", description="Filter users by email")):
    filtered_users = [user for user in users.values() if email.lower() in user.email.lower()]
    return {"users": filtered_users}


@app.get("/users/{email}")
def get_user(email: EmailStr):
    user = users.get(str(email).lower())
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@app.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(payload: CreateUserRequest):
    user_email = str(payload.email).lower()
    if user_email in users:
        raise HTTPException(status_code=409, detail="User already exists")

    users[user_email] = User(email=user_email, password=payload.password, license=payload.license)
    return RedirectResponse(url=f"/users/{user_email}", status_code=status.HTTP_303_SEE_OTHER)


@app.delete("/users/{email}")
def delete_user(email: EmailStr):
    user_email = str(email).lower()
    if user_email not in users:
        raise HTTPException(status_code=404, detail="User not found")

    del users[user_email]
    return {"message": "User deleted successfully", "email": user_email}


@app.post("/users/reset-password")
def reset_password(payload: ResetPasswordRequest):
    user_email = str(payload.email).lower()
    user = users.get(user_email)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.password = payload.new_password
    users[user_email] = user
    return {"message": "Password reset successful", "email": user_email}


@app.post("/users/assign-license")
def assign_license(payload: AssignLicenseRequest):
    user_email = str(payload.email).lower()
    user = users.get(user_email)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.license = payload.license
    users[user_email] = user
    return {"message": "License assigned successfully", "email": user_email, "license": user.license}
