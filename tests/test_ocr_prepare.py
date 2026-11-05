# 11-05 앱(Expo) 카메라 사진이 엉뚱한 글자로 읽혔다. 폰 사진은 EXIF '돌려서 보라' 표시만 붙고 옆으로 누운 채
# 저장되는 경우가 많아, 읽기 전에 방향을 바로잡고 흑백·크기·대비를 손질한다(app/ocr.py prepare).
import io
import shutil
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from app import ocr

WIN_TESSERACT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")


def schedule_photo(rotated_with_exif):
    """'NOV 20 3PM DENTIST' 가 적힌 사진. rotated_with_exif 면 옆으로 누운 채 저장하고 EXIF 로 '돌려 보라'고 적는다."""
    img = Image.new("RGB", (1400, 360), (235, 230, 220))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 110)
    except OSError:
        font = ImageFont.load_default()
    d.text((60, 110), "NOV 20 3PM DENTIST", fill=(40, 40, 40), font=font)
    exif = Image.Exif()
    if rotated_with_exif:
        img = img.rotate(90, expand=True)  # 폰이 저장한 그대로(옆으로 누움)
        exif[0x0112] = 6                    # 보는 쪽에서 시계 방향 90도 돌려야 바로 선다
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def test_prepare_applies_exif_rotation_and_grayscale():
    out = ocr.prepare(Image.open(io.BytesIO(schedule_photo(rotated_with_exif=True))))
    assert out.mode == "L"
    assert out.width > out.height          # 누워 있던 사진이 가로로 바로 섰다


def test_prepare_resizes_very_large_and_tiny_photos():
    big = ocr.prepare(Image.new("RGB", (6000, 4000), "white"))
    small = ocr.prepare(Image.new("RGB", (400, 300), "white"))
    assert max(big.size) == ocr.TARGET_LONG_SIDE and max(small.size) == ocr.TARGET_LONG_SIDE


@pytest.mark.skipif(not (shutil.which("tesseract") or WIN_TESSERACT.exists()), reason="tesseract 가 설치돼 있지 않음")
def test_rotated_phone_photo_is_read_after_prepare():
    import pytesseract
    if not shutil.which("tesseract"):
        pytesseract.pytesseract.tesseract_cmd = str(WIN_TESSERACT)
    text = ocr.read_text(schedule_photo(rotated_with_exif=True))
    assert "DENTIST" in text and "20" in text
