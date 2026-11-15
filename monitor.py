import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

URL = 'http://ystug.co.kr/sub07/sub03_01.php'
STATE = Path('state.json')
KST = ZoneInfo('Asia/Seoul')
FIELDS = ['선박명', '총톤수', '흘수', '요청일시', '시작점', '종료점', 'Work', 'B', 'B/T', '배정예선']
CHANGE_FIELDS = ['요청일시', '시작점', '종료점', 'Work', 'B', 'B/T']
TUG = re.compile(r'(?<![가-힣A-Za-z0-9])서\s*65(?![가-힣A-Za-z0-9])')


def parse_rows(html, day):
    soup = BeautifulSoup(html, 'html.parser')
    table = soup.select_one('table.wover_table')
    if table is None:
        raise RuntimeError('배정표를 찾지 못했습니다.')
    headings = [x.get_text(' ', strip=True) for x in table.select('thead th')]
    if not all(x in headings for x in FIELDS):
        raise RuntimeError(f'배정표 열 구성이 변경되었습니다: {headings}')
    grouped = defaultdict(list)
    for tr in table.select('tbody tr'):
        cells = [x.get_text(' ', strip=True) for x in tr.find_all('td', recursive=False)]
        if len(cells) != len(headings):
            continue
        row = dict(zip(headings, cells))
        try:
            req = datetime.strptime(row['요청일시'], '%Y/%m/%d %H:%M')
        except ValueError:
            continue
        if req.strftime('%Y%m%d') != day:
            continue
        row = {k: row.get(k, '') for k in FIELDS}
        key = '|'.join(row[k] for k in ['선박명', '요청일시', '시작점', '종료점'])
        grouped[key].append(row)
    rows = {}
    for key, entries in grouped.items():
        for i, row in enumerate(sorted(entries, key=lambda r: json.dumps(r, ensure_ascii=False, sort_keys=True))):
            rows[f'{key}|#{i+1}'] = row
    if not rows:
        raise RuntimeError(f'{day}: 해당 날짜의 행이 없습니다. 오탐 방지를 위해 중단합니다.')
    return rows


def fetch_rows(day):
    response = requests.post(URL, data={
        'p_dt_req': day, 'p_txt_ship': '', 'p_nm_bizarea': '', 'p_nm_tugboat': ''
    }, headers={'User-Agent': 'Mozilla/5.0', 'Referer': URL}, timeout=30)
    response.raise_for_status()
    response.encoding = response.apparent_encoding if response.apparent_encoding.lower() != 'ascii' else 'utf-8'
    return parse_rows(response.text, day)


def tug_names(row):
    if not row:
        return []
    return [re.sub(r'\s+', '', x) for x in re.split(r'[,，、/;\n]+', row.get('배정예선', '')) if x.strip()]


def assigned(row):
    return bool(row and TUG.search(row.get('배정예선', '')))


def eligible(row, now):
    try:
        req = datetime.strptime(row['요청일시'], '%Y/%m/%d %H:%M').replace(tzinfo=KST)
    except (KeyError, ValueError):
        return False
    return now.date() <= req.date() <= (now + timedelta(days=1)).date() and now < req + timedelta(hours=3)


def ship(row):
    return ' '.join(row['선박명'].upper().split())


def match_rows(old_rows, new_rows):
    old_remaining, new_remaining = dict(old_rows), dict(new_rows)
    pairs = []

    def identity(row):
        return tuple(row.get(k, '') for k in ('선박명', '요청일시', '시작점', '종료점'))

    old_groups, new_groups = defaultdict(list), defaultdict(list)
    for k, row in old_remaining.items():
        old_groups[identity(row)].append(k)
    for k, row in new_remaining.items():
        new_groups[identity(row)].append(k)
    for ident in old_groups.keys() & new_groups.keys():
        oks, nks = old_groups[ident], new_groups[ident]
        if len(oks) == len(nks) == 1:
            ok, nk = oks[0], nks[0]
            pairs.append((old_remaining.pop(ok), new_remaining.pop(nk)))
    old_names, new_names = defaultdict(list), defaultdict(list)
    for k, row in old_remaining.items():
        old_names[ship(row)].append(k)
    for k, row in new_remaining.items():
        new_names[ship(row)].append(k)
    for name in old_names.keys() | new_names.keys():
        oks, nks = old_names.get(name, []), new_names.get(name, [])
        if len(oks) == len(nks) == 1:
            pairs.append((old_remaining.pop(oks[0]), new_remaining.pop(nks[0])))
        elif not oks:
            pairs.extend((None, new_remaining.pop(k)) for k in nks)
        elif not nks:
            pairs.extend((old_remaining.pop(k), None) for k in oks)
        else:
            print(f'주의: {name} 동일 선박 복수 작업 매칭 불명확: 기존 {len(oks)} / 현재 {len(nks)}. 알림 보류')
    return pairs


def tug_changes(old, new):
    before, after = Counter(tug_names(old)), Counter(tug_names(new))
    removed = list((before - after).elements())
    added = list((after - before).elements())
    if not removed and not added:
        return None
    if len(removed) == len(added) == 1:
        return f'🔄 예선 변경: {removed[0]} → {added[0]}'
    parts = []
    if removed:
        parts.append('제외 ' + ', '.join(removed))
    if added:
        parts.append('추가 ' + ', '.join(added))
    return '🔄 예선 변경: ' + ' / '.join(parts)


def describe(row):
    return (f"{row['선박명']} | {row['총톤수']}톤 | 흘수 {row['흘수']}\n"
            f"요청일시 {row['요청일시']}\n"
            f"작업구간 {row['시작점']} → {row['종료점']}\n"
            f"Work {row['Work']} | B {row['B']} | B/T {row['B/T']}\n"
            f"배정예선 {row['배정예선']}")


def send(token, chat_id, kind, row, old=None, new=None):
    icon = {'신규배정': '🟢', '작업변경': '🔵', '배정취소': '🟠', '작업취소': '🔴'}[kind]
    lines = [f'{icon} [{kind}] 서65', describe(row)]
    if kind == '작업취소':
        lines.append('❌ 작업 자체가 배정표에서 삭제됨')
    if old and new:
        for field in CHANGE_FIELDS:
            if old.get(field, '') != new.get(field, ''):
                label = '🕒 시간 변경' if field == '요청일시' else f'✏️ {field} 변경'
                lines.append(f"{label}: {old.get(field) or '-'} → {new.get(field) or '-'}")
        changes = tug_changes(old, new)
        if changes:
            lines.append(changes)
    response = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                             json={'chat_id': chat_id, 'text': '\n'.join(lines)}, timeout=20)
    response.raise_for_status()
    if not response.json().get('ok'):
        raise RuntimeError('텔레그램 전송 실패')


def normalize_day_rows(data):
    """Accept keyed rows or a list of row dictionaries from older state formats."""
    if isinstance(data, dict):
        return data if all(isinstance(v, dict) for v in data.values()) else None
    if isinstance(data, list) and all(isinstance(row, dict) for row in data):
        return {f'legacy-#{i}': row for i, row in enumerate(data, 1)}
    return None


def load_previous():
    if not STATE.exists():
        return {}
    previous = json.loads(STATE.read_text(encoding='utf-8'))
    if not isinstance(previous, dict):
        raise RuntimeError('state.json 최상위 형식이 올바르지 않습니다. 파일을 확인해 주세요.')
    raw_days = previous.get('days')
    if isinstance(raw_days, dict):
        result = {}
        for day, data in raw_days.items():
            rows = normalize_day_rows(data)
            if rows is not None:
                result[day] = rows
            else:
                print(f'주의: {day}의 기존 데이터 형식이 불명확하여 최초 기준 저장으로 처리합니다.')
        return result
    if isinstance(raw_days, list):
        print('주의: state.json의 days가 리스트입니다. 기존 기록을 추측해 비교하지 않고 기준을 다시 저장합니다.')
        return {}
    if isinstance(previous.get('day'), str):
        rows = normalize_day_rows(previous.get('rows'))
        if rows is not None:
            return {previous['day']: rows}
    return {}


def main():
    now = datetime.now(KST)
    days = [now.strftime('%Y%m%d'), (now + timedelta(days=1)).strftime('%Y%m%d')]
    token, chat_id = os.getenv('TELEGRAM_BOT_TOKEN'), os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        raise RuntimeError('GitHub Secrets에 TELEGRAM_BOT_TOKEN과 TELEGRAM_CHAT_ID가 필요합니다.')
    current_by_day = {day: fetch_rows(day) for day in days}
    previous_by_day = load_previous()
    for day in days:
        current = current_by_day[day]
        old_rows = previous_by_day.get(day)
        if old_rows is None:
            print(f'{day}: 기준 데이터 최초 저장 (알림 없음)')
            continue
        counts = {'신규배정': 0, '작업변경': 0, '배정취소': 0, '작업취소': 0}
        for old, new in match_rows(old_rows, current):
            was, is_now = assigned(old), assigned(new)
            kind = None
            if not was and is_now:
                kind = '신규배정'
            elif was and new is None:
                kind = '작업취소'
            elif was and not is_now:
                kind = '배정취소'
            elif was and is_now and (any(old.get(k) != new.get(k) for k in CHANGE_FIELDS) or tug_changes(old, new)):
                kind = '작업변경'
            if kind and eligible(new or old, now):
                send(token, chat_id, kind, new or old, old, new)
                counts[kind] += 1
        print(f'{day}: 요청일시 기준 {len(current)}건 / 알림 {counts}')
    STATE.write_text(json.dumps({'days': current_by_day}, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
