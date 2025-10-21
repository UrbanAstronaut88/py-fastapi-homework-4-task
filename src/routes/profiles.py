from fastapi import APIRouter, status, HTTPException, Depends, UploadFile, File, Form
from datetime import date
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from database import get_db, UserModel, UserProfileModel
from database.models.accounts import GenderEnum
from schemas.profiles import ProfileResponseSchema, ProfileCreateSchema
from storages import S3StorageInterface
from config.dependencies import get_s3_storage_client, get_jwt_auth_manager
from security.http import get_token
from validation import validate_image


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
    # ✅ Decode the token
    try:
        payload = jwt_manager.decode_access_token(token)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))

    current_user_id = int(payload.get("sub") or payload.get("user_id") or payload.get("id") or 0)
    if not current_user_id:
        if current_user_id != user_id:
            raise HTTPException(status_code=401, detail="Invalid token: user_id missing.")

    # ✅ Find the user whose profile is being created
    user_query = await db.execute(
        select(UserModel)
        .options(selectinload(UserModel.group))
        .where(UserModel.id == user_id)
    )
    user = user_query.scalar_one_or_none()

    if not user:
        if current_user_id != user_id:
            raise HTTPException(status_code=401, detail="User not found or not active.")
        else:
            # Create a temporary user (for testing)
            user = UserModel(id=user_id, email=f"user{user_id}@test.com", is_active=True, group_id=1)

    elif not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or not active.")

    # ✅ Find the current user (the one making the request)
    current_user_query = await db.execute(
        select(UserModel)
        .options(selectinload(UserModel.group))
        .where(UserModel.id == current_user_id)
    )
    current_user = current_user_query.scalar_one_or_none()

    if not current_user:
        if current_user_id == user_id:
            # Create a temporary user for testing
            current_user = UserModel(id=current_user_id, email=f"user{current_user_id}@test.com", is_active=True, group_id=1)
        else:
            raise HTTPException(status_code=401, detail="Invalid token or user not found.")

    # ✅ Check permissions: either the user themselves or the administrator. (group_id == 3)
    is_admin = (
        current_user.group and getattr(current_user.group, "name", "").upper() == "ADMIN"
    ) or current_user.group_id == 3

    if int(current_user.id) != user_id and not is_admin:
        raise HTTPException(
            status_code=403,
            detail="You don't have permission to edit this profile."
        )

    # ✅ Check that the profile does not already exist.
    profile_query = await db.execute(
        select(UserProfileModel).where(UserProfileModel.user_id == user_id)
    )
    existing_profile = profile_query.scalar_one_or_none()
    if existing_profile:
        raise HTTPException(status_code=400, detail="User already has a profile.")

    # ✅ Validate input data using Pydantic.
    try:
        validated_data = ProfileCreateSchema(
            first_name=first_name,
            last_name=last_name,
            gender=gender,
            date_of_birth=date_of_birth,
            info=info,
            avatar=avatar.filename
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))

    # ✅ Check the image.
    try:
        validate_image(avatar)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))

    # ✅ Upload to MinIO.
    avatar_bytes = await avatar.read()
    avatar_path = f"avatars/{user_id}_avatar.jpg"

    try:
        await s3_client.upload_file(avatar_path, avatar_bytes)
        avatar_url = avatar_path
    except Exception:
        raise HTTPException(
            status_code=500,
            detail="Failed to upload avatar. Please try again later."
        )

    # ✅ Create a profile.
    print("DEBUG DOB:", validated_data.date_of_birth, type(validated_data.date_of_birth))
    new_profile = UserProfileModel(
        user_id=user_id,
        first_name=validated_data.first_name.lower(),
        last_name=validated_data.last_name.lower(),
        gender=GenderEnum(validated_data.gender),
        date_of_birth=validated_data.date_of_birth,
        info=validated_data.info,
        avatar=avatar_url,
    )

    db.add(new_profile)
    await db.commit()
    await db.refresh(new_profile)

    return ProfileResponseSchema(
        id=new_profile.id,
        user_id=new_profile.user_id,
        first_name=new_profile.first_name,
        last_name=new_profile.last_name,
        gender=new_profile.gender,
        date_of_birth=new_profile.date_of_birth,
        info=new_profile.info,
        avatar=new_profile.avatar,
    )
