import os
import requests

token = os.environ["TELEGRAM_BOT_TOKEN"]
chat_id = os.environ["TELEGRAM_CHAT_ID"]

url = f"https://api.telegram.org/bot{token}/sendMessage"

response = requests.post(
    url,
    json={
        "chat_id": chat_id,
        "text": "✅ 서65 배정 알림 테스트 성공!\n예선 배정 감시 시스템 연결 확인",
    },
    timeout=15,
)

response.raise_for_status()
print("텔레그램 테스트 메시지 전송 성공")
