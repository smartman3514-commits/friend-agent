"""사진 속 글자 읽기(OCR). 일정표나 공지 사진을 보내면 글자를 읽어 대화 입력창에 넣어 준다.

Python 라이브러리(pytesseract)는 requirements.txt 로 설치되지만, 실제로 글자를 읽는 tesseract 는
운영체제에 따로 설치하는 프로그램이다. 노트북에는 직접 설치하고, 서버에서는 실행 환경(이미지)에 들어 있어야 한다.
"""
import io

from app.logs import log

MAX_BYTES = 5 * 1024 * 1024  # 사진 한 장 최대 5MB
TARGET_LONG_SIDE = 2000      # 손질할 때 사진의 긴 변을 이 정도로 맞춘다(너무 크면 느리고, 너무 작으면 글자가 뭉개진다)


def prepare(image):
    """11-05 앱(Expo) 카메라 사진이 엉뚱한 글자로 읽혀 추가한 손질.
    1) 폰 사진은 '세워서 보라'는 EXIF 표시만 붙이고 옆으로 누운 채 저장되는 경우가 많다 → 표시대로 돌린다.
    2) 흑백으로 바꾸고 3) 긴 변을 TARGET_LONG_SIDE 근처로 맞추고 4) 대비를 높인다(그림자·흐린 사진)."""
    from PIL import Image, ImageOps

    image = ImageOps.exif_transpose(image)
    image = image.convert("L")
    w, h = image.size
    long_side = max(w, h)
    if long_side > TARGET_LONG_SIDE * 1.2 or long_side < TARGET_LONG_SIDE / 2:
        scale = TARGET_LONG_SIDE / long_side
        image = image.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    return ImageOps.autocontrast(image, cutoff=1)


class OcrError(Exception):
    pass


# AI 에게 보낼 사진 크기. gpt-4o-mini 는 사진을 512px 조각 수로 요금을 매긴다(짧은 변 768 이하로 맞춘 뒤 512 조각).
# 11-05 실측: 긴 변 1600px → 조각 4개, 입력 25,609 토큰, 약 5원. 짧은 변을 512 이하로 두면 조각 2개로 거의 절반.
VISION_SHORT_SIDE = 512
VISION_LONG_SIDE = 1024
VISION_INSTRUCTION = (
    "이 사진 속 글자를 한국어 그대로 옮겨 적어 줘. 손글씨도 읽는다.\n"
    "- 일정표·안내문이면 일정 하나를 한 줄로 쓴다(날짜, 시각, 할 일 순서).\n"
    "- 사진에 없는 내용, 추측, 설명, 인사는 쓰지 않는다. 읽기 어려운 글자는 [?] 로 둔다.\n"
    "- 글자가 없으면 아무것도 쓰지 않는다. 옮긴 글만 답한다."
)


def read_text(data: bytes, llm=None) -> str:
    """사진 속 글자를 읽는다. llm 이 사진을 읽을 수 있으면(read_image) AI 로, 아니면 tesseract 로.
    11-05: 앱 카메라로 찍은 손글씨 일정이 tesseract 로는 조각('우 3시 A 과')만 읽혀 AI 읽기를 기본으로 했다.
    가짜 LLM(테스트·로컬)은 사진을 못 읽으므로 tesseract 를 쓴다."""
    from PIL import Image, UnidentifiedImageError

    if len(data) > MAX_BYTES:
        raise OcrError("사진이 5MB보다 큽니다.")
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError) as error:
        raise OcrError("사진 파일을 읽을 수 없습니다.") from error
    if llm is not None and hasattr(llm, "read_image"):
        return read_with_ai(image, llm)
    return read_with_tesseract(image)


def read_with_ai(image, llm) -> str:
    from PIL import Image, ImageOps

    image = ImageOps.exif_transpose(image).convert("RGB")
    w, h = image.size
    scale = min(1.0, VISION_SHORT_SIDE / min(w, h), VISION_LONG_SIDE / max(w, h))
    if scale < 1.0:
        image = image.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=85)
    try:
        text = llm.read_image(VISION_INSTRUCTION, buf.getvalue())
    except Exception as error:
        log("ERROR", "AI 사진 글자 읽기 실패", error=type(error).__name__)
        raise OcrError("사진 글자를 읽지 못했어요. 잠시 뒤 다시 해 주세요.") from error
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    log("INFO", "사진 글자 읽기", engine="ai", chars=len(text))
    return text


def read_with_tesseract(image) -> str:
    import pytesseract

    try:
        langs = pytesseract.get_languages(config="")
        lang = "kor+eng" if "kor" in langs else "eng"
        if lang == "eng":
            log("WARNING", "tesseract 한국어 데이터가 없어 영어로만 읽습니다.")
        # --psm 6: 사진 전체를 글자 덩어리 하나로 본다. 기본값(자동)은 여백이 많은 공지 사진을 빈 페이지로 읽는 경우가 있다.
        text = pytesseract.image_to_string(prepare(image), lang=lang, config="--psm 6")
    except pytesseract.TesseractNotFoundError as error:
        log("ERROR", "tesseract 프로그램을 찾을 수 없습니다. 실행 환경에 tesseract 가 설치되어 있지 않습니다.")
        raise OcrError("사진에서 글자를 읽는 프로그램이 서버에 없습니다.") from error
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    log("INFO", "사진 글자 읽기", engine="tesseract", lang=lang, chars=len(text))
    return text
