import json
import hashlib
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

URL = 'http://ystug.co.kr/sub07/sub03_01.php'
STATE = Path('state.json')
KST = ZoneInfo('Asia/Seoul')
FIELDS = ['선박명', '총톤수', '흘수', '요청일시', '시작점', '종료점', 'Work', 'B', 'B/T', '배정예선']
CHANGE_FIELDS = ['요청일시', '시작점', '종료점', 'Work', 'B', 'B/T', '배정예선']


def parse_rows(html, requested_day):
    soup = BeautifulSoup(html, 'html.parser')
    date_input = soup.select_one('input[name="p_dt_req"]')
    returned_day = (date_input.get('value') or '').strip() if date_input else ''
    if returned_day != requested_day:
        raise RuntimeError(f'날짜 불일치: 요청 {requested_day}, 응답 {returned_day or "없음"}. 상태 저장 및 알림 중단')

    table = soup.select_one('table.wover_table')
    if table is None:
        raise RuntimeError('예선 배정표를 찾지 못했습니다.')
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
        # Keep every row, including jobs sharing the same ship/time/ports.
        # A content fingerprint makes duplicate keys stable across row reordering.
        record = {k: data.get(k, '') for k in FIELDS}
        if key in rows:
            fingerprint = hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
            candidate = f'{key}|duplicate:{fingerprint}'
            n = 2
            while candidate in rows:
                candidate = f'{key}|duplicate:{fingerprint}:{n}'
                n += 1
            key = candidate
        rows[key] = record
    if not rows:
        raise RuntimeError(f'{requested_day}: 배정표가 비어 있어 기존 상태를 보존합니다.')
    return rows


def assigned(row):
    return bool(row and re.search(r'(?<![가-힣A-Za-z0-9])서\s*65(?![가-힣A-Za-z0-9])', row.get('배정예선', '')))


def eligible(row, now):
    try:
        req = datetime.strptime(row['요청일시'], '%Y/%m/%d %H:%M').replace(tzinfo=KST)
    except (ValueError, KeyError):
        return False
    return now.date() <= req.date() <= (now + timedelta(days=1)).date() and now < req + timedelta(hours=3)


def normalized_name(row):
    return ' '.join(row.get('선박명', '').upper().split())


def match_rows(old_rows, current):
    pairs = []
    old_remaining, new_remaining = dict(old_rows), dict(current)
    # Match identical full records first even when duplicate-key suffixes shift.
    for old_key, old_row in list(old_remaining.items()):
        new_key = next((k for k, r in new_remaining.items() if r == old_row), None)
        if new_key is not None:
            pairs.append((old_remaining.pop(old_key), new_remaining.pop(new_key)))
    for key in set(old_remaining) & set(new_remaining):
        pairs.append((old_remaining.pop(key), new_remaining.pop(key)))
    names = set(map(normalized_name, old_remaining.values())) & set(map(normalized_name, new_remaining.values()))
    for name in names:
        old_keys = [k for k, r in old_remaining.items() if normalized_name(r) == name]
        new_keys = [k for k, r in new_remaining.items() if normalized_name(r) == name]
        if len(old_keys) == 1 and len(new_keys) == 1:
            pairs.append((old_remaining.pop(old_keys[0]), new_remaining.pop(new_keys[0])))
    pairs.extend((r, None) for r in old_remaining.values())
    pairs.extend((None, r) for r in new_remaining.values())
    return pairs


def describe(row):
    return (f"{row['선박명']} | {row['총톤수']}톤 | 흘수 {row['흘수']}\n"
            f"요청일시 {row['요청일시']} | {row['시작점']} → {row['종료점']}\n"
            f"Work {row['Work']} | B {row['B']} | B/T {row['B/T']}\n"
            f"배정예선 {row['배정예선']}")


def send(token, chat_id, kind, row, changes=None):
    lines = [f'[{kind}] 서65', describe(row)]
    if changes:
        lines.append('변경: ' + ' / '.join(f'{k}: {before or "-"} → {after or "-"}' for k, before, after in changes))
    response = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                             json={'chat_id': chat_id, 'text': '\n'.join(lines)}, timeout=20)
    response.raise_for_status()
    if not response.json().get('ok'):
        raise RuntimeError('텔레그램 전송 실패')


def fetch_rows(day, session):
    response = session.post(URL,
                            data={'p_dt_req': day, 'p_txt_ship': '', 'p_nm_bizarea': '', 'p_nm_tugboat': ''},
                            headers={'User-Agent': 'Mozilla/5.0', 'Referer': URL}, timeout=30)
    response.raise_for_status()
    response.encoding = response.apparent_encoding if response.apparent_encoding.lower() != 'ascii' else 'utf-8'
    return parse_rows(response.text, day)


def main():
    now = datetime.now(KST)
    days = [now.strftime('%Y%m%d'), (now + timedelta(days=1)).strftime('%Y%m%d')]
    token, chat_id = os.environ.get('TELEGRAM_BOT_TOKEN'), os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        raise RuntimeError('GitHub Secrets를 확인하세요.')
    with requests.Session() as session:
        current_by_day = {day: fetch_rows(day, session) for day in days}
    if current_by_day[days[0]] == current_by_day[days[1]]:
        raise RuntimeError('오늘과 내일 배정표가 완전히 동일합니다. 날짜 필터 미적용 가능성으로 알림과 상태 저장을 중단합니다.')

    previous = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
    if 'days' in previous:
        previous_by_day = previous['days']
    elif previous.get('day') and isinstance(previous.get('rows'), dict):
        previous_by_day = {previous['day']: previous['rows']}
    else:
        previous_by_day = {}

    for day in days:
        current = current_by_day[day]
        old_rows = previous_by_day.get(day)
        if old_rows is None:
            print(f'{day}: {len(current)}건 조회 완료 / 첫 조회 기준 데이터 저장 (알림 없음)')
            continue
        counts = {'신규배정': 0, '작업변경': 0, '작업취소': 0}
        for old, new in match_rows(old_rows, current):
            was, is_now = assigned(old), assigned(new)
            kind, changes = None, None
            if not was and is_now:
                kind = '신규배정'
            elif was and not is_now:
                kind = '작업취소'
            elif was and is_now:
                changes = [(field, old.get(field, ''), new.get(field, ''))
                           for field in CHANGE_FIELDS if old.get(field, '') != new.get(field, '')]
                if changes:
                    kind = '작업변경'
            if kind and eligible(new or old, now):
                send(token, chat_id, kind, new or old, changes)
                counts[kind] += 1
        print(f'{day}: {len(current)}건 조회 완료 / 알림 {counts}')
    STATE.write_text(json.dumps({'days': current_by_day}, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
