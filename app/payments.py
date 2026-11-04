"""토스페이먼츠 결제 승인과 취소. 서버만 부른다(시크릿 키가 필요하다).

근거(토스페이먼츠 문서, 2026-11-04 확인):
- 결제 승인: POST https://api.tosspayments.com/v1/payments/confirm, 본문 paymentKey·orderId·amount
  https://docs.tosspayments.com/guides/v2/payment-widget/integration
- 인증: Authorization: Basic base64("시크릿키:") — 콜론을 빠뜨리지 않는다
  https://docs.tosspayments.com/guides/v2/get-started/llms-quick-reference (8. Common Mistakes)
- 멱등키: Idempotency-Key 헤더, 300자 이하, 15일 유효. 처리 중에 같은 키로 다시 보내면
  409 IDEMPOTENT_REQUEST_PROCESSING → 같은 키로 다시 물어 결과를 받는다. 키를 바꿔 다시 보내지 않는다.
  https://docs.tosspayments.com/reference/using-api/idempotency-key

시크릿 키와 paymentKey 전체는 로그에 남기지 않는다.
"""
import base64
import time

import httpx

from app.logs import log

CONFIRM_URL = "https://api.tosspayments.com/v1/payments/confirm"
CANCEL_URL = "https://api.tosspayments.com/v1/payments/{payment_key}/cancel"
RETRY_DELAY = 1.0


def _post(what, url, body, secret_key, order_id, idempotency_key):
    """(HTTP 상태, 응답 JSON). 연결이 끊기면 (0, {}) — 처리됐는지 모르므로 주문 상태를 실패로 바꾸지 않는다."""
    auth = base64.b64encode(f"{secret_key}:".encode()).decode()
    headers = {"Authorization": f"Basic {auth}", "Content-Type": "application/json", "Idempotency-Key": idempotency_key}
    for attempt in range(3):
        log("INFO", f"결제 {what} 요청", order_id=order_id, idempotency_key=idempotency_key, attempt=attempt + 1)
        try:
            r = httpx.post(url, json=body, headers=headers, timeout=30)
        except httpx.HTTPError as error:
            log("ERROR", f"결제 {what} 연결 실패", order_id=order_id, error=type(error).__name__)
            return 0, {}
        data = r.json() if r.content else {}
        if r.status_code == 409 and data.get("code") == "IDEMPOTENT_REQUEST_PROCESSING":
            time.sleep(RETRY_DELAY)  # 같은 키로 다시 물어 첫 결과를 받는다. 키는 바꾸지 않는다
            continue
        log("INFO", f"결제 {what} 응답", order_id=order_id, status=r.status_code, code=data.get("code"))
        return r.status_code, data
    return r.status_code, data


def confirm(secret_key, payment_key, order_id, amount, idempotency_key):
    """결제 승인. 멱등키는 주문 번호."""
    return _post("승인", CONFIRM_URL, {"paymentKey": payment_key, "orderId": order_id, "amount": amount},
                 secret_key, order_id, idempotency_key)


def cancel(secret_key, payment_key, order_id, reason, idempotency_key):
    """결제 전액 취소: POST /v1/payments/{paymentKey}/cancel, 본문 cancelReason(cancelAmount 없으면 전액).
    https://docs.tosspayments.com/guides/v2/cancel-payment"""
    return _post("취소", CANCEL_URL.format(payment_key=payment_key), {"cancelReason": reason},
                 secret_key, order_id, idempotency_key)
