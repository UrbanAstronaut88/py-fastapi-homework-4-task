import re
from datetime import date
from io import BytesIO
from PIL import Image
from fastapi import UploadFile
from database.models.accounts import GenderEnum


def validate_name(name: str) -> str:
    """
    Validate that name contains only English letters and spaces.
    Must not be empty, and must be ASCII-only.
    """
    if not name or not name.strip():
        raise ValueError("Name cannot be empty.")

    name = name.strip()

    if not re.fullmatch(r"[A-Za-z ]+", name):
        raise ValueError(f"{name} contains non-english letters")

    return name.lower()


def validate_image(avatar: UploadFile) -> None:
    """
    Validate uploaded image for size and format.
    Raises ValueError for invalid cases (used by Pydantic validators).
    """
    supported_formats = ["JPG", "JPEG", "PNG"]
    max_file_size = 1 * 1024 * 1024  # 1 MB

    contents = avatar.file.read()
    if len(contents) > max_file_size:
        raise ValueError("Image size exceeds 1 MB")

    try:
        image = Image.open(BytesIO(contents))
        image_format = image.format.upper()
        if image_format not in supported_formats:
            raise ValueError(f"Unsupported image format: {image_format}. Use one of next: {supported_formats}")
    except Exception:
        raise ValueError("Invalid image format")
    finally:
        avatar.file.seek(0)


def validate_gender(gender: str) -> str:
    valid_values = [g.value for g in GenderEnum]
    if gender not in valid_values:
        raise ValueError(f"Gender must be one of: {', '.join(valid_values)}")
    return gender


def validate_birth_date(birth_date: date) -> date:
    if birth_date.year < 1900:
        raise ValueError("Invalid birth date - year must be greater than 1900.")
    age = (date.today() - birth_date).days // 365
    if age < 18:
        raise ValueError("You must be at least 18 years old to register.")
    return birth_date
