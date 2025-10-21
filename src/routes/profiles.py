from fastapi import APIRouter, status, HTTPException, Depends, UploadFile, File, Form
from datetime import date
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import os

from database import get_db, UserModel, UserProfileModel
from database.models.accounts import GenderEnum
from schemas.profiles import ProfileResponseSchema, ProfileCreateSchema
from storages import S3StorageInterface
from config.dependencies import get_s3_storage_client, get_jwt_auth_manager
from security.http import get_token
from pydantic import ValidationError


router = APIRouter(prefix="/users", tags=["profiles"])


# --- helper: token validation ---
def decode_token(jwt_manager, token: str):
    try:
        payload = jwt_manager.decode_access_token(token)
    except Exception as e:
        msg = str(e or "")
        if "expired" in msg.lower() or "token has expired" in msg.lower():
            raise HTTPException(status_code=401, detail="Token has expired.")
        raise HTTPException(status_code=401, detail="User not found or not active.")
    return payload


# --- helper: get user by id ---
async def get_user_by_id(db: AsyncSession, user_id: int) -> UserModel | None:
    result = await db.execute(
        select(UserModel).options(selectinload(UserModel.group)).where(UserModel.id == user_id)
    )
    return result.scalar_one_or_none()


# --- helper: validate users and permissions ---
async def validate_users_and_permissions(db: AsyncSession, user_id: int, current_user_id: int):
    """
    Returns tuple (user, current_user).
    Rules:
      - If target user exists and is inactive -> 401
      - If target user missing -> allow only if current_user_id == user_id (self), else 401
      - If current_user exists and is inactive -> 401
      - If current_user missing -> allow only if current_user_id == user_id (self), else 401
      - Permissions: if current_user exists and is not admin and not same-as-target -> 403
    """
    # target user
    user = await get_user_by_id(db, user_id)
    if user is not None:
        # target user exists — must be active
        if not user.is_active:
            raise HTTPException(status_code=401, detail="User not found or not active.")
    else:
        # target user missing — allow ONLY if token owner is same user (self-case)
        if current_user_id != user_id:
            raise HTTPException(status_code=401, detail="User not found or not active.")
        # else: user remains None (self-case), allowed to continue

    # current authenticated user
    current_user = await get_user_by_id(db, current_user_id)
    if current_user is not None:
        # current_user exists — must be active
        if not current_user.is_active:
            raise HTTPException(status_code=401, detail="User not found or not active.")
    else:
        # missing current_user — allow only for self-case
        if current_user_id != user_id:
            raise HTTPException(status_code=401, detail="User not found or not active.")
        # else: current_user stays None (self-case), allowed to continue

    # permissions: only meaningful when current_user exists (not None)
    is_admin = False
    if current_user:
        is_admin = (
            (current_user.group and getattr(current_user.group, "name", "").upper() == "ADMIN")
            or current_user.group_id == 3
        )

        # if current_user exists and is not the same user and not admin -> forbidden
        if int(current_user.id) != user_id and not is_admin:
            raise HTTPException(status_code=403, detail="You don't have permission to edit this profile.")

    return user, current_user



# --- helper: upload avatar and return key + URL ---
async def upload_avatar(user_id: int, avatar: UploadFile, s3_client: S3StorageInterface):
    filename = avatar.filename or "avatar"
    _, ext = os.path.splitext(filename)
    ext = ext.lower() if ext else ".jpg"
    avatar_key = f"avatars/{user_id}_avatar{ext}"

    avatar_bytes = await avatar.read()
    try:
        await s3_client.upload_file(avatar_key, avatar_bytes)

        if hasattr(s3_client, "get_file_url"):
            avatar_public_url = await s3_client.get_file_url(avatar_key)
        else:
            endpoint = getattr(s3_client, "endpoint", "http://localhost:9000")
            bucket = getattr(s3_client, "bucket_name", "theater-storage")
            avatar_public_url = f"{endpoint}/{bucket}/{avatar_key}"
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to upload avatar. Please try again later.")

    return avatar_key, avatar_public_url


# --- main endpoint ---
@router.post("/{user_id}/profile/", response_model=ProfileResponseSchema, status_code=status.HTTP_201_CREATED)
async def create_profile(
    user_id: int,
    first_name: str = Form(...),
    last_name: str = Form(...),
    gender: str = Form(...),
    date_of_birth: date = Form(...),
    info: str = Form(...),
    avatar: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    s3_client: S3StorageInterface = Depends(get_s3_storage_client),
    token: str = Depends(get_token),
    jwt_manager=Depends(get_jwt_auth_manager),
):
    payload = decode_token(jwt_manager, token)

    raw_user_id = payload.get("sub") or payload.get("user_id") or payload.get("id")
    try:
        current_user_id = int(raw_user_id) if raw_user_id is not None else None
    except (TypeError, ValueError):
        current_user_id = None

    if not current_user_id:
        raise HTTPException(status_code=401, detail="User not found or not active.")

    user, current_user = await validate_users_and_permissions(db, user_id, current_user_id)

    # --- Check if profile exists ---
    profile_query = await db.execute(select(UserProfileModel).where(UserProfileModel.user_id == user_id))
    if profile_query.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="User already has a profile.")

    # --- Validate input via schema ---
    try:
        validated_data = ProfileCreateSchema(
            first_name=first_name,
            last_name=last_name,
            gender=gender,
            date_of_birth=date_of_birth,
            info=info,
            avatar=avatar,
        )
    except ValidationError as e:
        first_msg = e.errors()[0].get("msg") if e.errors() else str(e)
        raise HTTPException(status_code=422, detail=first_msg)

    # --- Upload avatar ---
    avatar_key, avatar_public_url = await upload_avatar(user_id, avatar, s3_client)

    # --- Save profile ---
    new_profile = UserProfileModel(
        user_id=user_id,
        first_name=validated_data.first_name.lower(),
        last_name=validated_data.last_name.lower(),
        gender=GenderEnum(validated_data.gender),
        date_of_birth=validated_data.date_of_birth,
        info=validated_data.info,
        avatar=avatar_key,
    )
    db.add(new_profile)
    await db.commit()
    await db.refresh(new_profile)

    return {
        "id": new_profile.id,
        "user_id": new_profile.user_id,
        "first_name": new_profile.first_name,
        "last_name": new_profile.last_name,
        "gender": new_profile.gender,
        "date_of_birth": new_profile.date_of_birth,
        "info": new_profile.info,
        "avatar": avatar_public_url,
    }
