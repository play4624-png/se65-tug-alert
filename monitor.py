import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

URL = 'http://ystug.co.kr/sub07/sub03_01.php'
STATE = Path('state.json')
KST = ZoneInfo('Asia/Seoul')
FIELDS = ['선박명', '총톤수', '흘수', '요청일시', '시작점', '종료점', 'Work', 'B', 'B/T', '배정예선']


def parse_rows(html):
    soup = BeautifulSoup(html, 'html.parser')
    table = soup.select_one('table.wover_table')
    if table is None:
        raise RuntimeError('예선 배정표를 찾지 못했습니다. 사이트 구조를 확인하세요.')
    headings = [x.get_text(' ', strip=True) for x in table.select('thead th')]
    if not all(x in headings for x in FIELDS):
        raise RuntimeError(f'표 항목이 변경되었습니다: {headings}')
    rows = {}
    for tr in table.select('tbody tr'):
        cells = [x.get_text(' ', strip=True) for x in tr.find_all('td', recursive=False)]
        if len(cells) != len(headings):
            continue
        data = dict(zip(headings, cells))
        key = '|'.join(data.get(x, '') for x in ['선박명', '요청일시', '시작점', '종료점'])
        rows[key] = {k: data.get(k, '') for k in FIELDS}
    if not rows:
        raise RuntimeError('배정표가 비어 있습니다. 빈 응답을 삭제로 처리하지 않습니다.')
    return rows


def assigned(row):
    # Exact tug name, not a substring such as '서650'.
    import re
    return bool(re.search(r'(?<![가-힣A-Za-z0-9])서\s*65(?![가-힣A-Za-z0-9])', row['배정예선']))


def eligible(row, now):
    try:
        req = datetime.strptime(row['요청일시'], '%Y/%m/%d %H:%M').replace(tzinfo=KST)
    except ValueError:
        return False
    return req.date() == now.date() and now < req + timedelta(hours=3)


def send(token, chat_id, kind, row):
    msg = (f"[서65 {kind}] {row['선박명']} | {row['총톤수']}톤 | 흘수 {row['흘수']} | "
           f"{row['요청일시']} | {row['시작점']} → {row['종료점']} | "
           f"Work {row['Work']} | B {row['B']} | B/T {row['B/T']} | 배정예선 {row['배정예선']}")
    response = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                             json={'chat_id': chat_id, 'text': msg}, timeout=20)
    response.raise_for_status()
    if not response.json().get('ok'):
        raise RuntimeError('텔레그램 전송 실패')


def main():
    now = datetime.now(KST)
    day = now.strftime('%Y%m%d')
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        raise RuntimeError('GitHub Secrets에 TELEGRAM_BOT_TOKEN과 TELEGRAM_CHAT_ID가 필요합니다.')
    response = requests.post(URL, data={'p_dt_req': day, 'p_txt_ship': '',
                                        'p_nm_bizarea': '', 'p_nm_tugboat': ''},
                             headers={'User-Agent': 'Mozilla/5.0', 'Referer': URL}, timeout=30)
    response.raise_for_status()
    response.encoding = response.apparent_encoding if response.apparent_encoding.lower() not in ('ascii',) else 'utf-8'
    current = parse_rows(response.text)
    previous = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
    if previous.get('day') != day:
        # Midnight rollover is NOT a deletion. First observation is baseline.
        previous_rows = None
    else:
        previous_rows = previous.get('rows', {})
    if previous_rows is not None:
        for key in set(previous_rows) | set(current):
            old = previous_rows.get(key)
            new = current.get(key)
            was = assigned(old) if old else False
            is_now = assigned(new) if new else False
            if was == is_now:
                continue
            row = new or old
            if eligible(row, now):
                send(token, chat_id, '신규 배정' if is_now else '배정 삭제', row)
    STATE.write_text(json.dumps({'day': day, 'rows': current}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'{day}: {len(current)}건 조회 완료')


if __name__ == '__main__':
    main()
