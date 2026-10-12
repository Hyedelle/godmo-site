#!/usr/bin/env python3
"""갓모 식구들 파일 생성기 — TelemetryDeck 조회 → family/v1.json · family/v2.json.

하루 한 번 GitHub Actions(.github/workflows/family-feed.yml)가 돌린다. 설계·운영 절차는 갓모 저장소
docs/backend/FAMILY.md 7절. 표준 라이브러리만 쓴다.

규칙(FAMILY.md 1·6절 그대로)
- 사용자 기기만: 테스트 표시(isTestMode) 기기와 제외 목록(FAMILY_EXCLUDE, clientUser 앞 8자 이상)을 뺀다
- 기기 5대 미만에서 나온 항목은 넣지 않는다. 숫자·번호만 — 글자는 없다
- 기간: 어제까지 최근 30일(한국 시간)
- 실패하면 파일을 건드리지 않고 비정상 종료한다 → 어제 파일이 그대로 남는다
- 로그에는 합계만 찍는다. 기기별 줄을 찍지 않는다(공개 저장소의 Actions 로그는 공개)
"""
import argparse
import collections
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request

API_URL = "https://api.telemetrydeckapi.com/api/v4/query/tql"
NAMESPACE = "com.dreamhub"
APP_ID = "4331F1F8-F74E-49B6-8755-802E50CDE9DB"
MIN_DEVICES = 5
WINDOW_DAYS = 30
SANITY_MIN_NOTES = 10  # 30일 동안 노트가 이보다 적게 잡히면 조회가 비정상이다(기기 수십 대 규모에서)
RAIL = ["θ", "χρ", "πν", "ε", "✝", "★", "→", "↑", "↓", "※", "=", "≠", "∴", "∵"]
WEEKDAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
KST = dt.timezone(dt.timedelta(hours=9))
TYPE_CODE = re.compile(r"^[EI][SN][TF][JP]$")


class SanityError(RuntimeError):
    pass


# ---------- 조회 ----------

def _selector(dimension, value):
    return {"type": "selector", "dimension": dimension, "value": value}


def query_body(interval, event_type, dims, granularity="all"):
    return {
        "queryType": "groupBy",
        "dataSource": NAMESPACE,
        "intervals": [interval],
        "granularity": granularity,
        "filter": {"type": "and", "fields": [_selector("appID", APP_ID), _selector("isTestMode", "false"),
                                              _selector("type", event_type)]},
        "dimensions": [{"type": "default", "dimension": d, "outputName": o, "outputType": "STRING"} for d, o in dims],
        "aggregations": [{"type": "longSum", "name": "n", "fieldName": "count"}],
    }


def window_bounds(today):
    """today(한국 시간 날짜)의 0시를 끝(미포함)으로 30일."""
    end = dt.datetime.combine(today, dt.time(0), KST)
    return end - dt.timedelta(days=WINDOW_DAYS), end


def feed_queries(today):
    start, end = window_bounds(today)
    utc = dt.timezone.utc
    interval = f"{start.astimezone(utc):%Y-%m-%dT%H:%M:%SZ}/{end.astimezone(utc):%Y-%m-%dT%H:%M:%SZ}"
    hourly = {"type": "period", "period": "PT1H", "timeZone": "Asia/Seoul"}
    daily = {"type": "period", "period": "P1D", "timeZone": "Asia/Seoul"}
    return {
        "symbols": query_body(interval, "Editor.symbolInserted", [("clientUser", "u"), ("glyph", "glyph")]),
        "notes": query_body(interval, "Note.created", [("clientUser", "u"), ("kind", "kind")], hourly),
        "verses": query_body(interval, "Family.verse", [("clientUser", "u"), ("book", "book"), ("chapter", "chapter")]),
        "songs": query_body(interval, "Family.song", [("clientUser", "u"), ("song", "song")]),
        "types": query_body(interval, "Family.type", [("clientUser", "u"), ("type", "type")], daily),
    }


def run_query(token, body):
    request = urllib.request.Request(
        API_URL, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=150) as response:  # 문서: 복잡한 조회는 최대 2분
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"조회 실패 HTTP {error.code}: {error.read().decode('utf-8', 'replace')[:300]}") from None


def events(response):
    """API 응답 → [(timestamp, 이벤트 dict)]. rows 모양(REST)과 목록 모양(MCP) 모두 읽는다."""
    result = response.get("result") if isinstance(response, dict) else None
    rows = result.get("rows") if isinstance(result, dict) else result
    out = []
    for row in rows or []:
        ts = row.get("timestamp")
        if isinstance(row.get("event"), dict):
            out.append((ts, row["event"]))
        elif isinstance(row.get("result"), list):
            out.extend((ts, item) for item in row["result"] if isinstance(item, dict))
        elif isinstance(row.get("result"), dict):
            out.append((ts, row["result"]))
    return out


_TS = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")


def to_kst(ts):
    match = _TS.match(ts or "")
    if not match:
        raise ValueError(f"시각 모양을 모른다: {ts!r}")
    y, mo, d, h, mi, s = (int(x) for x in match.groups()[:6])
    zone = match.group(7)
    if zone in (None, "Z"):
        minutes = 0
    else:
        sign = -1 if zone[0] == "-" else 1
        minutes = sign * (int(zone[1:3]) * 60 + int(zone[-2:]))
    return dt.datetime(y, mo, d, h, mi, s, tzinfo=dt.timezone(dt.timedelta(minutes=minutes))).astimezone(KST)


# ---------- 집계(순수 함수) ----------

def plain(glyph):
    return glyph.replace("︎", "").replace("️", "")


def keep_user_devices(evs, exclude):
    out = []
    for ts, ev in evs:
        user = ev.get("u") or ""
        if user and not any(user.startswith(p) for p in exclude):
            out.append((ts, ev))
    return out


def _count(ev):
    return int(ev.get("n") or 0)


def symbols_top(evs):
    devices, counts = collections.defaultdict(set), collections.Counter()
    for _, ev in evs:
        glyph = ev.get("glyph")
        if glyph:
            devices[glyph].add(ev["u"])
            counts[glyph] += _count(ev)
    rail = lambda g: RAIL.index(plain(g)) if plain(g) in RAIL else len(RAIL)
    ranked = sorted((g for g in devices if len(devices[g]) >= MIN_DEVICES),
                    key=lambda g: (-len(devices[g]), -counts[g], rail(g)))
    return ranked[:3]


def ratios_sum_to_one(counts):
    """합 1.00이 되도록 소수 둘째 자리로 나눈다(가장 큰 나머지부터 올림)."""
    total = sum(counts)
    raw = [c * 100 / total for c in counts]
    floors = [int(r) for r in raw]
    for i in sorted(range(len(raw)), key=lambda i: (-(raw[i] - floors[i]), i))[: 100 - sum(floors)]:
        floors[i] += 1
    return [f / 100 for f in floors]


def note_hours(evs):
    """한국 시간 0~23시 24칸 비율(종류별, 기기 5대 이상일 때만)과 설교 최다 요일."""
    per = {"sermon": [0] * 24, "qt": [0] * 24}
    devices = {"sermon": set(), "qt": set()}
    weekdays = collections.Counter()
    for ts, ev in evs:
        kind = ev.get("kind")
        if kind not in per:
            continue
        t = to_kst(ts)
        per[kind][t.hour] += _count(ev)
        devices[kind].add(ev["u"])
        if kind == "sermon":
            weekdays[WEEKDAYS[(t.weekday() + 1) % 7]] += _count(ev)
    hours = {k: ratios_sum_to_one(per[k]) for k in per if len(devices[k]) >= MIN_DEVICES and sum(per[k]) > 0}
    top_day = max(WEEKDAYS, key=lambda d: weekdays[d]) if "sermon" in hours and weekdays else None
    return hours, top_day


def verses_top(evs):
    devices, counts = collections.defaultdict(set), collections.Counter()
    for _, ev in evs:
        if str(ev.get("book")).isdigit() and str(ev.get("chapter")).isdigit():
            key = (int(ev["book"]), int(ev["chapter"]))
            devices[key].add(ev["u"])
            counts[key] += _count(ev)
    ranked = sorted((k for k in devices if len(devices[k]) >= MIN_DEVICES), key=lambda k: (-len(devices[k]), -counts[k], k))
    return [{"book": b, "chapter": c} for b, c in ranked[:5]]


def songs_top(evs):
    devices, counts = collections.defaultdict(set), collections.Counter()
    for _, ev in evs:
        if str(ev.get("song")).isdigit() and int(ev["song"]) > 0:
            devices[int(ev["song"])].add(ev["u"])
            counts[int(ev["song"])] += _count(ev)
    ranked = sorted((s for s in devices if len(devices[s]) >= MIN_DEVICES), key=lambda s: (-len(devices[s]), -counts[s], s))
    return ranked[:5]


def type_distribution(evs):
    """기기마다 윈도 안 마지막 유형. 기기 5대 미만이면 None."""
    latest = {}
    for ts, ev in evs:
        code = ev.get("type")
        if code and TYPE_CODE.match(code):
            when = to_kst(ts)
            if ev["u"] not in latest or when > latest[ev["u"]][0]:
                latest[ev["u"]] = (when, code)
    codes = [c for _, c in latest.values()]
    if len(codes) < MIN_DEVICES:
        return None
    axes = {letter: round(sum(1 for c in codes if c[i] == letter) / len(codes), 2)
            for i, letter in enumerate("ESTJ")}
    tally = collections.Counter(codes)
    top = [c for c, n in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0])) if n >= MIN_DEVICES][:16]
    return {"devices": len(codes), "axes": axes, "top": top}


def build(feeds, today, exclude, now):
    """feeds: 이름 → [(timestamp, 이벤트)]. 반환 (v1, v2)."""
    feeds = {name: keep_user_devices(evs, exclude) for name, evs in feeds.items()}
    notes = sum(_count(ev) for _, ev in feeds["notes"])
    if notes < SANITY_MIN_NOTES:
        raise SanityError(f"최근 {WINDOW_DAYS}일 노트가 {notes}건뿐이다 — 조회가 비정상이라 파일을 만들지 않는다")
    hours, top_day = note_hours(feeds["notes"])
    symbols = symbols_top(feeds["symbols"])
    start, end = window_bounds(today)
    last_day = (end - dt.timedelta(days=1)).date()
    head = {"month": f"{last_day:%Y-%m}",
            "generatedAt": now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "basis": "user-devices", "minDevices": MIN_DEVICES,
            "window": {"from": start.date().isoformat(), "to": last_day.isoformat(), "days": WINDOW_DAYS}}
    v1 = {"schema": 1, **head, "symbolsTop": symbols, "hours": hours}
    v2 = {"schema": 2, **head, "versesTop": verses_top(feeds["verses"]), "songsTop": songs_top(feeds["songs"]),
          "symbolsTop": symbols, "hours": hours}
    if top_day:
        v1["sermonTopWeekday"] = v2["sermonTopWeekday"] = top_day
    types = type_distribution(feeds["types"])
    if types:
        v2["types"] = types
    return v1, v2


# ---------- 검사(앱 FamilyStats.decode의 거울) ----------

def validate(doc):
    def fail(message):
        raise ValueError(f"파일 모양 오류: {message}")

    if doc.get("schema") not in (1, 2):
        fail("schema")
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", str(doc.get("month"))):
        fail("month")
    symbols = doc.get("symbolsTop", [])
    if len(symbols) > 3 or any(not str(s).strip() for s in symbols):
        fail("symbolsTop")
    for key, row in doc.get("hours", {}).items():
        if key not in ("sermon", "qt") or len(row) != 24 or any(not 0 <= x <= 1 for x in row) \
                or not 0.95 <= sum(row) <= 1.05:
            fail(f"hours.{key}")
    if "sermonTopWeekday" in doc and doc["sermonTopWeekday"] not in WEEKDAYS:
        fail("sermonTopWeekday")
    verses = doc.get("versesTop", [])
    if len(verses) > 5 or any(not (1 <= v["book"] <= 66 and 1 <= v["chapter"] <= 150) for v in verses):
        fail("versesTop")
    songs = doc.get("songsTop", [])
    if len(songs) > 5 or any(not isinstance(s, int) or s <= 0 for s in songs):
        fail("songsTop")
    types = doc.get("types")
    if types is not None:
        if types["devices"] < MIN_DEVICES or set(types["axes"]) != set("ESTJ") \
                or any(not 0 <= x <= 1 for x in types["axes"].values()) \
                or any(not TYPE_CODE.match(c) for c in types["top"]):
            fail("types")
    if doc["schema"] == 1 and any(k in doc for k in ("versesTop", "songsTop", "types")):
        fail("schema 1에 새 칸이 있다")


# ---------- 실행 ----------

def write_atomic(files):
    tmps = []
    for path, doc in files.items():
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
        tmps.append((tmp, path))
    for tmp, path in tmps:
        os.replace(tmp, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", default="family")
    parser.add_argument("--dry-run", action="store_true", help="쓰지 않고 결과만 보여 준다")
    parser.add_argument("--fixture", help="조회 대신 읽을 응답 모음 JSON(이름 → API 응답)")
    parser.add_argument("--today", help="YYYY-MM-DD(한국 시간). 기본은 오늘")
    args = parser.parse_args(argv)
    now = dt.datetime.now(dt.timezone.utc)
    today = dt.date.fromisoformat(args.today) if args.today else now.astimezone(KST).date()
    exclude = [p.strip() for p in os.environ.get("FAMILY_EXCLUDE", "").replace("\n", ",").split(",") if p.strip()]
    try:
        if args.fixture:
            with open(args.fixture, encoding="utf-8") as handle:
                responses = json.load(handle)
        else:
            token = os.environ.get("TELEMETRYDECK_TOKEN", "").strip()
            if not token:
                print("실패: TELEMETRYDECK_TOKEN 이 비어 있다", file=sys.stderr)
                return 2
            responses = {name: run_query(token, body) for name, body in feed_queries(today).items()}
        v1, v2 = build({name: events(r) for name, r in responses.items()}, today, exclude, now)
        validate(v1)
        validate(v2)
    except (RuntimeError, ValueError, KeyError, OSError, urllib.error.URLError) as error:
        print(f"실패: {error}", file=sys.stderr)
        return 1
    print(f"창 {v2['window']['from']}~{v2['window']['to']} · 기호 {len(v2['symbolsTop'])}개 · 말씀 {len(v2['versesTop'])}개 · "
          f"찬양 {len(v2['songsTop'])}개 · 유형 {'있음' if 'types' in v2 else '기준 미달'} · "
          f"시간대 {','.join(sorted(v2['hours'])) or '기준 미달'} · 제외 접두 {len(exclude)}개")
    if args.dry_run:
        print(json.dumps(v2, ensure_ascii=False, indent=2))
        print("(dry-run: 파일을 쓰지 않았다)")
        return 0
    write_atomic({os.path.join(args.out_dir, "v1.json"): v1, os.path.join(args.out_dir, "v2.json"): v2})
    return 0


if __name__ == "__main__":
    sys.exit(main())
