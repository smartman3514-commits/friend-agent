"""AI 친구 에이전트 서비스.

화면(/)과 API를 FastAPI 하나가 함께 제공한다.
- /health, /version: 서비스 상태와 배포 버전
- /api/config: 화면이 쓰는 설정(로그인 방식, 개발용 패널 표시 여부)
- /api/character, /api/settings: 캐릭터와 사용자 설정
- /api/chat: 메시지 한 턴. 결과를 이벤트(JSON 한 줄씩)로 스트리밍한다
- /api/greeting: 앱을 열 때 먼저 말 걸기
- /api/ocr: 사진 속 글자 읽기(OCR_ENABLED=true 일 때)
- /api/memories, /api/reminders, /api/data: 기억, 약속, 내 데이터 관리
- /api/credits, /api/products, /api/orders, /api/payments/*: 크레딧 원장과 토스페이먼츠 결제
"""
import html
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from langfuse import propagate_attributes
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app import agent, credits, ocr, payments, tracing, when
from app.auth import Auth, User
from app.config import ROOT, load_settings
from app.llm import make_llm
from app.logs import log
from app.persona import DEFAULT_CHARACTER, OPTIONS, build_persona, clean_character
from app.store.base import make_store
from app.tools import CITIES

class HidePaymentKey(logging.Filter):
    """토스가 successUrl 로 돌려보낸 주소(?paymentKey=...)가 uvicorn 접속 기록에 통째로 남지 않게 가린다.
    uvicorn.access 기록의 args 는 (주소, 메서드, 경로, HTTP 버전, 상태 코드) 순서다."""

    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            args = list(record.args)
            args[2] = PAYMENT_KEY.sub(r"\1***", str(args[2]))
            record.args = tuple(args)
        return True


PAYMENT_KEY = re.compile(r"(paymentKey=)[^&\s]+")
logging.getLogger("uvicorn.access").addFilter(HidePaymentKey())

settings = load_settings()  # 꼭 필요한 값이 없으면 여기서 종료한다
llm = make_llm(settings)
store = make_store(settings)
current_user = Auth(settings)

app = FastAPI(title="AI 친구 에이전트")

# 시작 지연 스위치(배포 실습용). 포트는 바로 열지만 정한 시간 동안은 준비 중이라 모든 요청에 503을 돌려준다.
# AI 서비스가 시작할 때 모델 설정이나 색인을 불러오느라 늦게 준비되는 상황을 흉내 낸다. 기본값 0(끔).
SLOW_START_SECONDS = float(os.environ.get("SLOW_START_SECONDS", "0") or 0)
READY_AT = time.time() + SLOW_START_SECONDS
if SLOW_START_SECONDS:
    log("WARNING", f"시작 지연 스위치가 켜져 있습니다. {SLOW_START_SECONDS:.0f}초 동안 준비 중으로 응답합니다.")


@app.middleware("http")
async def not_ready_yet(request, call_next):
    if time.time() < READY_AT:
        return JSONResponse({"status": "starting"}, status_code=503)
    return await call_next(request)


def client_ip(request: Request):
    """요청한 사람의 IP. Cloud Run 앞단은 X-Forwarded-For 맨 뒤에 실제 접속 IP를 붙이므로 맨 뒤 값을 쓴다
    (맨 앞 값은 사용자가 마음대로 넣을 수 있다). 로컬에서는 접속 주소."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded and settings.env != "local":
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def ip_key(request: Request):
    return credits.ip_hash(client_ip(request), settings.supabase_secret_key)


def get_llm():
    """테스트에서는 이 함수를 가짜 LLM을 돌려주는 함수로 바꿔 끼운다."""
    return llm


class ChatRequest(BaseModel):
    message: str


class SettingsRequest(BaseModel):
    location: str | None = None
    timezone: str | None = None


class ReportRequest(BaseModel):
    message_id: int
    reason: str = ""


class FeedbackRequest(BaseModel):
    message_id: int
    value: Literal["up", "down"]


class OrderRequest(BaseModel):
    product_id: str            # 화면은 상품 ID만 보낸다. 금액과 크레딧 수는 서버가 상품표에서 찾는다


class ConfirmRequest(BaseModel):
    paymentKey: str
    orderId: str
    amount: int


class FailRequest(BaseModel):
    orderId: str
    code: str = ""


@app.get("/", response_class=HTMLResponse)
def home():
    page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    banner = "" if settings.env in ("production", "local") else f'<div class="banner">테스트 환경 ({settings.env})</div>'
    if os.environ.get("BANNER"):  # 이미지를 새로 만들지 않고 환경변수만 바꿔 새 리비전을 만들 때 쓰는 화면 문구
        banner += f'<div class="banner">{html.escape(os.environ["BANNER"][:80])}</div>'
    return page.replace("<!--ENV_BANNER-->", banner)


@app.get("/terms", response_class=HTMLResponse)
def terms():
    """이용약관·환불 정책(초안). 숫자는 credits.py 의 실제 값으로 채워 코드와 약관이 어긋나지 않게 한다."""
    rows = "".join(f"<tr><td>{html.escape(p['name'])}</td><td>{p['amount']:,}원</td><td>{p['credits']:,}</td>"
                   f"<td>약 {p['unit_price']}원</td></tr>" for p in credits.product_list())
    page = (ROOT / "static" / "terms.html").read_text(encoding="utf-8")
    for key, value in {"COST_CHAT": credits.COST["chat"], "COST_OCR": credits.COST["ocr"], "FREE_CREDITS": credits.FREE_CREDITS,
                       "FREE_PER_IP": credits.FREE_PER_IP_PER_DAY, "REFUND_DAYS": credits.REFUND_DAYS,
                       "PRODUCT_ROWS": rows}.items():
        page = page.replace("{{" + key + "}}", str(value))
    return page


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/version")
def version():
    return {"commit": settings.commit, "revision": settings.revision, "env": settings.env,
            "llm": settings.provider, "model": settings.model, "storage": settings.storage, "auth": settings.auth,
            "banner": os.environ.get("BANNER", "")}


@app.get("/api/config")
def config():
    # publishable key 는 브라우저에 공개해도 되는 키다. secret key 는 절대 여기에 넣지 않는다.
    return {"env": settings.env, "auth": settings.auth, "storage": settings.storage,
            "supabase_url": settings.supabase_url if settings.auth == "supabase" else "",
            "supabase_publishable_key": settings.supabase_publishable_key if settings.auth == "supabase" else "",
            "google_login": settings.google_login, "show_turn_panel": settings.show_turn_panel,
            "ocr_enabled": settings.ocr_enabled,
            # 클라이언트 키는 공개해도 되는 값이다. 시크릿 키(toss_secret_key)는 여기에 넣지 않는다.
            "toss_client_key": settings.toss_client_key if settings.toss_secret_key else "",
            "options": OPTIONS, "cities": list(CITIES)}


@app.get("/api/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "anonymous": user.anonymous, "email": user.email,
            "has_character": store.get_character(user.id) is not None}


# ── 캐릭터와 설정 ──────────────────────────────────────────────────

@app.get("/api/character")
def get_character(user: User = Depends(current_user)):
    c = store.get_character(user.id)
    return {"character": c or DEFAULT_CHARACTER, "saved": c is not None}


@app.put("/api/character")
def put_character(data: dict, user: User = Depends(current_user)):
    c = clean_character(data)
    store.save_character(user.id, c)
    log("INFO", "캐릭터 저장")
    return {"character": c}


@app.post("/api/character/preview")
def preview_character(data: dict):
    return {"prompt": build_persona(clean_character(data))}


@app.get("/api/settings")
def get_settings(user: User = Depends(current_user)):
    return store.get_settings(user.id)


@app.put("/api/settings")
def put_settings(req: SettingsRequest, user: User = Depends(current_user)):
    changes = {}
    if req.location in CITIES:
        changes["location"] = req.location
    if req.timezone:
        changes["timezone"] = req.timezone if req.timezone == "Asia/Seoul" else "Asia/Seoul"  # 지금은 한국 시간만
    store.save_settings(user.id, changes)
    return store.get_settings(user.id)


# ── 대화 ───────────────────────────────────────────────────────────

@app.get("/api/messages")
def messages(user: User = Depends(current_user)):
    show = settings.show_turn_panel
    return [{"id": m["id"], "role": m["role"], "content": m["content"],
             "meta": m["meta"] if show else {}, "created_at": m["created_at"].isoformat()}
            for m in store.recent_messages(user.id, 50)]


@app.post("/api/chat")
def chat(req: ChatRequest, request: Request, user: User = Depends(current_user), model=Depends(get_llm)):
    message = req.message.strip()[:1000]
    if not message:
        raise HTTPException(400, "메시지가 비어 있습니다")
    # 크레딧은 받자마자 서버에서 뺀다. 화면이 확인하는 것은 믿지 않는다
    ok, balance, ref = credits.spend_for_chat(store, user.id, ip_key(request))
    if not ok:
        log("INFO", "크레딧 부족으로 거절", balance=balance)
        return JSONResponse({"error": "insufficient_credits", "message": credits.INSUFFICIENT_MESSAGE, "balance": balance},
                            status_code=402)

    def events():
        try:
            for event in agent.run_turn(message, user.id, model, store, settings.show_turn_panel):
                yield json.dumps(event, ensure_ascii=False, default=str) + "\n"
        except Exception as error:
            log("ERROR", "대화 처리 실패", error=type(error).__name__)
            credits.refund(store, user.id, ref)  # 답을 못 만들었으면 뺀 크레딧을 되돌리는 줄을 쌓는다
            yield json.dumps({"type": "error", "message": "답을 만들지 못했어요. 잠시 뒤 다시 보내 주세요."}, ensure_ascii=False) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


@app.post("/api/greeting")
def greeting(user: User = Depends(current_user), model=Depends(get_llm)):
    return {"greeting": agent.greet(user.id, model, store)}


@app.post("/api/ocr")
async def read_photo(photo: UploadFile, request: Request, user: User = Depends(current_user), model=Depends(get_llm)):
    """사진 속 글자를 읽는다(AI 가 사진을 보고 옮겨 적음). 크레딧 COST['ocr'] 개를 먼저 빼고, 실패하면 돌려준다."""
    if not settings.ocr_enabled:
        raise HTTPException(404, "사진 글자 읽기가 꺼져 있습니다")
    data = await photo.read()
    ok, balance, ref = credits.spend_for_ocr(store, user.id, ip_key(request))
    if not ok:
        return JSONResponse({"error": "insufficient_credits", "balance": balance,
                             "message": f"사진 읽기에는 크레딧 {credits.COST['ocr']}개가 필요해요. 충전하면 바로 읽어 드릴게요."},
                            status_code=402)
    now = when.now_in("Asia/Seoul")
    try:
        with propagate_attributes(**tracing.turn_attributes(user.id, now)), \
                tracing.lf().start_as_current_observation(name="ocr", as_type="generation", model=getattr(model, "model", None)):
            text = await run_in_threadpool(ocr.read_text, data, model)
    except ocr.OcrError as error:
        credits.refund(store, user.id, ref, credits.COST["ocr"])
        raise HTTPException(500, str(error)) from error
    return {"text": text, "credits_used": credits.COST["ocr"]}


def own_answer(user, message_id):
    """로그인한 사용자 자신의 답(assistant 메시지)만 반응을 받는다. 남의 메시지 ID면 404."""
    message = store.get_message(user.id, message_id)
    if not message or message["role"] != "assistant":
        raise HTTPException(404, "그 답을 찾지 못했어요.")
    return message


@app.post("/api/report")
def report(req: ReportRequest, user: User = Depends(current_user)):
    # 신고는 Langfuse 점수 report(1)로 보내고, 사유는 comment 로 붙인다. 서버 로그에는 사유 길이만 남긴다.
    message = own_answer(user, req.message_id)
    tracing.score(message["meta"].get("trace_id"), "report", 1, comment=req.reason.strip()[:500] or None)
    log("WARNING", "부적절한 답변 신고", message_id=req.message_id, reason_length=len(req.reason))
    return {"ok": True}


@app.post("/api/feedback")
def feedback(req: FeedbackRequest, user: User = Depends(current_user)):
    # 좋아요 1, 별로예요 0 을 user-feedback 점수로. 별로예요가 붙은 트레이스부터 읽을 수 있게 한다.
    message = own_answer(user, req.message_id)
    tracing.score(message["meta"].get("trace_id"), "user-feedback", 1 if req.value == "up" else 0)
    log("INFO", "답변 반응", message_id=req.message_id, value=req.value)
    return {"ok": True}


# ── 기억, 약속, 내 데이터 ───────────────────────────────────────────

@app.get("/api/memories")
def list_memories(user: User = Depends(current_user)):
    return [{"id": m["id"], "kind": m["kind"], "text": m["text"]} for m in store.list_memories(user.id)]


@app.delete("/api/memories/{memory_id}")
def delete_memory(memory_id: int, user: User = Depends(current_user)):
    store.delete_memory(user.id, memory_id)
    return {"ok": True}


@app.delete("/api/memories")
def clear_memories(user: User = Depends(current_user)):
    store.clear_memories(user.id)
    return {"ok": True}


@app.get("/api/reminders")
def list_reminders(user: User = Depends(current_user)):
    tz = when.now_in(store.get_settings(user.id)["timezone"]).tzinfo
    return [{"id": r["id"], "text": r["text"], "status": r["status"],
             "due_label": when.label(r["due_at"].astimezone(tz))} for r in store.list_reminders(user.id)]


@app.delete("/api/reminders/{reminder_id}")
def cancel_reminder(reminder_id: int, user: User = Depends(current_user)):
    store.cancel_reminder(user.id, reminder_id)
    return {"ok": True}


@app.delete("/api/data")
def delete_data(user: User = Depends(current_user)):
    store.delete_all(user.id)
    log("INFO", "사용자 데이터 전체 삭제")
    return {"ok": True}


# ── 크레딧과 결제 ──────────────────────────────────────────────────

@app.get("/api/credits")
def get_credits(request: Request, user: User = Depends(current_user)):
    credits.ensure_signup(store, user.id, ip_key(request))
    history = [{"delta": r["delta"], "reason": r["reason"], "created_at": r["created_at"].isoformat()}
               for r in store.credit_history(user.id, 10)]
    return {"balance": store.credit_balance(user.id), "cost": credits.COST, "low": credits.LOW_BALANCE,
            "history": history, "payments": bool(settings.toss_client_key and settings.toss_secret_key),
            "refund_days": credits.REFUND_DAYS}


@app.get("/api/products")
def products():
    return credits.product_list()


@app.post("/api/orders")
def create_order(req: OrderRequest, user: User = Depends(current_user)):
    product = credits.PRODUCTS.get(req.product_id)
    if not product:
        raise HTTPException(400, "없는 상품입니다")
    # 주문 번호는 추측할 수 없는 무작위 값(토스 규칙: 영문·숫자·-_=, 6~64자). 승인 때 멱등키로도 쓴다
    order = {"order_id": "order_" + secrets.token_urlsafe(18), "product_id": req.product_id,
             "amount": product["amount"], "credits": product["credits"]}
    store.create_order(user.id, order)
    log("INFO", "주문 생성", order_id=order["order_id"], product_id=req.product_id)
    return {"orderId": order["order_id"], "amount": order["amount"], "orderName": product["name"],
            "credits": order["credits"]}


@app.post("/api/payments/confirm")
def confirm_payment(req: ConfirmRequest, user: User = Depends(current_user)):
    """successUrl 로 돌아온 값으로 결제를 승인한다. 크레딧을 늘리는 곳은 여기 한 곳뿐이다."""
    if not settings.toss_secret_key:
        raise HTTPException(503, "결제가 설정되지 않았습니다")
    order = store.get_order(user.id, req.orderId)  # 다른 사용자의 주문이면 None
    if not order:
        raise HTTPException(404, "주문을 찾지 못했어요")
    if req.amount != order["amount"]:
        log("WARNING", "결제 금액 불일치로 승인 거절", order_id=order["order_id"])
        raise HTTPException(400, "결제 금액이 주문과 달라요")
    if order["status"] == "done":  # 새로고침이나 재시도: 승인 API를 다시 부르지 않고 첫 결과를 돌려준다
        return {"ok": True, "credits": order["credits"], "balance": store.credit_balance(user.id), "already": True}
    if order["status"] != "pending":
        raise HTTPException(409, "이미 끝난 주문입니다")

    status, body = payments.confirm(settings.toss_secret_key, req.paymentKey, order["order_id"], order["amount"],
                                    idempotency_key=order["order_id"])
    if status == 0 or status >= 500:
        # 승인됐는지 모른다. 주문은 대기로 두고, 같은 멱등키로 다시 승인하면 첫 결과를 받는다
        raise HTTPException(502, "결제 확인이 늦어지고 있어요. 잠시 뒤 새로고침해 주세요.")
    if status != 200:
        store.fail_order(user.id, order["order_id"], body.get("code", "UNKNOWN"))
        raise HTTPException(400, body.get("message") or "결제를 승인하지 못했어요")
    if body.get("orderId") != order["order_id"] or body.get("totalAmount") != order["amount"] or body.get("status") != "DONE":
        log("ERROR", "결제 승인 응답이 주문과 다름", order_id=order["order_id"], status=body.get("status"))
        raise HTTPException(502, "결제 확인 중 문제가 생겼어요. 문의해 주세요.")

    # 원장을 먼저 쌓는다(ref order:주문번호 가 유일해서 두 번 들어가지 않는다). 그다음 주문을 완료로
    store.add_credit(user.id, order["credits"], "purchase", f"order:{order['order_id']}")
    approved = body.get("approvedAt")
    store.complete_order(user.id, order["order_id"], req.paymentKey, datetime.fromisoformat(approved) if approved else None)
    log("INFO", "결제 완료, 크레딧 지급", order_id=order["order_id"], credits=order["credits"])
    return {"ok": True, "credits": order["credits"], "balance": store.credit_balance(user.id), "already": False}


@app.post("/api/payments/fail")
def payment_failed(req: FailRequest, user: User = Depends(current_user)):
    """failUrl 로 돌아온 주문을 실패로 바꾼다. 승인 API는 부르지 않는다."""
    changed = store.fail_order(user.id, req.orderId, req.code[:60] or "UNKNOWN")
    log("INFO", "결제 실패", order_id=req.orderId, code=req.code[:60], changed=changed)
    return {"ok": True}


@app.get("/api/orders")
def list_orders(user: User = Depends(current_user)):
    """결제 내역(완료·환불만). 환불할 수 있는지와 이유는 서버가 정해 준다."""
    now = datetime.now(timezone.utc)
    out = []
    for o in store.list_orders(user.id, 20):
        if o["status"] not in ("done", "refunding", "refunded"):
            continue
        block = credits.refund_block(store, user.id, o, now) if o["status"] == "done" else None
        out.append({"orderId": o["order_id"], "name": credits.PRODUCTS.get(o["product_id"], {}).get("name", o["product_id"]),
                    "amount": o["amount"], "credits": o["credits"], "status": o["status"],
                    "approved_at": o["approved_at"].isoformat() if o.get("approved_at") else None,
                    "refundable": o["status"] == "done" and block is None, "why_not": block})
    return out


@app.post("/api/orders/{order_id}/refund")
def refund_order(order_id: str, user: User = Depends(current_user)):
    """사용자가 직접 하는 전액 환불. 크레딧을 먼저 빼고(잠금), 토스 결제 취소를 멱등키와 함께 부른다.
    같은 주문을 두 번 눌러도 원장의 refund:주문번호 줄은 하나뿐이다."""
    if not settings.toss_secret_key:
        raise HTTPException(503, "결제가 설정되지 않았습니다")
    order = store.get_order(user.id, order_id)
    if not order:
        raise HTTPException(404, "주문을 찾지 못했어요")
    if order["status"] == "refunded":
        return {"ok": True, "already": True, "balance": store.credit_balance(user.id)}
    now = datetime.now(timezone.utc)
    if order["status"] == "done":
        block = credits.refund_block(store, user.id, order, now)
        if block:
            raise HTTPException(400, block)
        if not store.move_order(user.id, order_id, "done", "refunding"):  # 동시에 두 번 누르면 하나만 여기를 지난다
            raise HTTPException(409, "환불을 처리하고 있어요. 잠시 뒤 다시 확인해 주세요.")
        ok, _ = store.spend_credit(user.id, order["credits"], "refund", f"refund:{order_id}")
        if not ok:
            store.move_order(user.id, order_id, "refunding", "done")
            raise HTTPException(400, "이 결제로 받은 크레딧을 이미 써서 환불할 수 없어요.")
    elif order["status"] != "refunding":  # refunding: 지난번 취소 결과를 모른다. 크레딧은 이미 뺐으니 같은 키로 다시 묻는다
        raise HTTPException(409, "환불할 수 있는 결제가 아니에요.")

    status, body = payments.cancel(settings.toss_secret_key, order["payment_key"], order_id, "사용자 환불 요청",
                                   idempotency_key=f"cancel-{order_id}")
    if (status == 200 and body.get("status") == "CANCELED") or body.get("code") == "ALREADY_CANCELED_PAYMENT":
        store.move_order(user.id, order_id, "refunding", "refunded", refunded_at=now)
        log("INFO", "환불 완료", order_id=order_id, credits=order["credits"])
        return {"ok": True, "already": False, "balance": store.credit_balance(user.id)}
    if status == 0 or status >= 500:
        raise HTTPException(502, "환불 확인이 늦어지고 있어요. 잠시 뒤 다시 눌러 주세요.")
    # 토스가 취소를 거절했다: 뺀 크레딧을 되돌리고 주문을 완료 상태로 돌린다
    store.add_credit(user.id, order["credits"], "refund", f"refund-revert:{order_id}")
    store.move_order(user.id, order_id, "refunding", "done")
    log("WARNING", "환불 거절", order_id=order_id, code=body.get("code"))
    raise HTTPException(400, body.get("message") or "환불하지 못했어요. 문의해 주세요.")
