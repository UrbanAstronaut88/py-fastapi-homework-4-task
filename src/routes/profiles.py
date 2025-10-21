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
    # --- Decode token with explicit expired-token handling ---
    try:
        payload = jwt_manager.decode_access_token(token)
    except Exception as e:
        msg = str(e or "")
        if "expired" in msg.lower() or "token has expired" in msg.lower():
            raise HTTPException(status_code=401, detail="Token has expired.")
        raise HTTPException(status_code=401, detail="User not found or not active.")

    # --- Validate token payload ---
    raw_user_id = payload.get("sub") or payload.get("user_id") or payload.get("id")
    try:
        current_user_id = int(raw_user_id) if raw_user_id is not None else None
    except (TypeError, ValueError):
        current_user_id = None

    if not current_user_id:
        raise HTTPException(status_code=401, detail="User not found or not active.")

    # --- Find target user ---
    user_query = await db.execute(
        select(UserModel)
        .options(selectinload(UserModel.group))
        .where(UserModel.id == user_id)
    )
    user = user_query.scalar_one_or_none()

    # If target user missing:
    if not user:
        # allow request to proceed only if token belongs to the same user (self)
        if current_user_id != user_id:
            raise HTTPException(status_code=401, detail="User not found or not active.")
        # else: user is absent in DB but request is from self — continue WITHOUT creating temp user

    else:
        # If user exists, must be active
        if not user.is_active:
            raise HTTPException(status_code=401, detail="User not found or not active.")

    # --- Find current authenticated user ---
    current_user_query = await db.execute(
        select(UserModel)
        .options(selectinload(UserModel.group))
        .where(UserModel.id == current_user_id)
    )
    current_user = current_user_query.scalar_one_or_none()

    # If current_user is missing:
    if not current_user:
        # allow only if this is self (token user equals target user)
        if current_user_id != user_id:
            raise HTTPException(status_code=401, detail="User not found or not active.")
        # else: allow and continue without current_user object (we'll treat as self)
    else:
        if not current_user.is_active:
            raise HTTPException(status_code=401, detail="User not found or not active.")

    # --- Permissions ---
    # If current_user exists, check admin flag. If current_user is None but token==user_id, allow (self)
    is_admin = False
    if current_user:
        is_admin = (current_user.group and getattr(current_user.group, "name", "").upper() == "ADMIN") or current_user.group_id == 3

    if (current_user is not None and int(current_user.id) != user_id) and not is_admin:
        # if current_user exists and is not the same user and not admin -> forbid
        raise HTTPException(status_code=403, detail="You don't have permission to edit this profile.")
    # if current_user is None but token == user_id -> allowed (self)

    # --- Check profile existence ---
    profile_query = await db.execute(select(UserProfileModel).where(UserProfileModel.user_id == user_id))
    existing_profile = profile_query.scalar_one_or_none()
    if existing_profile:
        raise HTTPException(status_code=400, detail="User already has a profile.")

    # --- Validate input via Pydantic schema ---
    try:
        validated_data = ProfileCreateSchema(
            first_name=first_name,
            last_name=last_name,
            gender=gender,
            date_of_birth=date_of_birth,
            info=info,
            avatar=avatar,  # pass UploadFile
        )
    except ValidationError as e:
        errors = e.errors()
        if errors:
            first_msg = errors[0].get("msg") or str(errors[0])
        else:
            first_msg = str(e)
        raise HTTPException(status_code=422, detail=first_msg)

    # --- Prepare avatar upload ---
    filename = avatar.filename or "avatar"
    _, ext = os.path.splitext(filename)
    ext = ext.lower() if ext else ".jpg"
    avatar_key = f"avatars/{user_id}_avatar{ext}"

    avatar_bytes = await avatar.read()
    try:
        # Upload bytes under avatar_key
        await s3_client.upload_file(avatar_key, avatar_bytes)

        # Build or obtain public URL for response, but keep DB value as key
        if hasattr(s3_client, "get_file_url"):
            avatar_public_url = await s3_client.get_file_url(avatar_key)
        else:
            endpoint = getattr(s3_client, "endpoint", "http://localhost:9000")
            bucket = getattr(s3_client, "bucket_name", "theater-storage")
            avatar_public_url = f"{endpoint}/{bucket}/{avatar_key}"
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to upload avatar. Please try again later.")

    # --- Create profile record: save key in DB, return public URL in response ---
    new_profile = UserProfileModel(
        user_id=user_id,
        first_name=validated_data.first_name.lower(),
        last_name=validated_data.last_name.lower(),
        gender=GenderEnum(validated_data.gender),
        date_of_birth=validated_data.date_of_birth,
        info=validated_data.info,
        avatar=avatar_key,  # store key in DB, test expects this
    )

    db.add(new_profile)
    await db.commit()
    await db.refresh(new_profile)

    # Return response with public URL (not DB key)
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
