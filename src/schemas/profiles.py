from datetime import date
from fastapi import UploadFile
from pydantic import BaseModel, field_validator, ConfigDict
from validation import validate_name, validate_gender, validate_birth_date, validate_image


class ProfileCreateSchema(BaseModel):
    first_name: str
    last_name: str
    gender: str
    date_of_birth: date
    info: str
    avatar: UploadFile

    @field_validator("first_name", "last_name")
    def validate_name_fields(cls, v):
        return validate_name(v)

    @field_validator("gender")
    def validate_gender_field(cls, v):
        return validate_gender(v)

    @field_validator("date_of_birth")
    def validate_dob(cls, v):
        validate_birth_date(v)
        return v

    @field_validator("info")
    def validate_info(cls, v):
        if not v or not v.strip():
            raise ValueError("Info field cannot be empty or contain only spaces.")
        return v

    @field_validator("avatar")
    def validate_avatar(cls, v):
        validate_image(v)
        return v


class ProfileResponseSchema(BaseModel):
    id: int
    user_id: int
    first_name: str
    last_name: str
    gender: str
    date_of_birth: date
    info: str
    avatar: str

    model_config = ConfigDict(from_attributes=True)
