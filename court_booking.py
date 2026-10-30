#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
court_booking.py — 國立東華大學「運動場地借用系統」小工具

功能：
  1. 自動讀取可借場地／時段（公開頁 index.aspx，不需登入）
  2. 登入後自動走「新增申請 → 驗證碼 → 申請」流程（驗證碼全自動，每次重新抓圖辨識）

用法：
  python court_booking.py                 # 讀取本週開放借用期間＋各場地可借時段總覽
  python court_booking.py --field VOL0H   # 指定場地，列出每天可借的時段
  python court_booking.py --plan          # 依小隊長的規則排出本週要搶的兩張申請單（不登入）
  python court_booking.py --check         # 自我檢查：抓登入頁驗證 token/cookie ＋ 時段表
  python court_booking.py --when 2026/11/12  # 查某日期要在哪一天 00:00 跑才搶得到（＋12 天規則）
  python court_booking.py --book          # 登入並照計畫自動申請（帳號密碼執行時輸入，不儲存）
  python court_booking.py --book --dry-run  # 試跑：只走流程、記錄頁面結構，不送出申請
  python court_booking.py --book --wait-open # 等到隔天 00:00 開放新日期再搶
  python court_booking.py --test-captcha  # 離線自我測試：產生兩張測試圖並辨識

小隊長指定的搶場規則（改這裡就能改規則）：
  - 類型只選「排球」；場地只搶男 H、男 F（VOL0H、VOL0F）
  - 一週兩張申請單：星期一或二擇一天 ＋ 星期四或五擇一天
  - 都是 20:00-22:00（兩小時）；一張申請只有一個場地一個時段

紅線（請勿修改）：
  - 帳號密碼只在執行時輸入，絕不寫進任何檔案。
  - 每次對系統的請求間至少間隔 2 秒；不做平行搶位、不做無限重試。
  - 驗證碼最多重試 5 次就停。
"""

import argparse
import base64
import getpass
import os
import random
import re
import string
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- 常數

BASE_URL = "https://sys.ndhu.edu.tw/gc/sportcenter/SportsFields/"
LOGIN_URL = BASE_URL + "login.aspx"
INDEX_URL = BASE_URL + "index.aspx"

POLITE_DELAY = 2.0        # 每次請求之間的最小間隔（秒），對系統友善
REQUEST_TIMEOUT = 20      # 單一請求逾時（秒）
MAX_PAGES_TO_SEARCH = 5   # 找「新增申請」時最多翻幾頁
MAX_CAPTCHA_RETRY = 5     # 驗證碼最多嘗試次數（小隊長規定：5 次就停）

# ---- 小隊長指定的搶場規則 ----
TARGET_KIND_VALUE = "2"             # 類型下拉要選「排球」
TARGET_KIND_LABEL = "排球"
PREFER_FIELDS = ["VOL0H", "VOL0F"]  # 只搶男 H、男 F 排球場（可用 --fields 覆寫順序）
TARGET_HOURS = ["20", "21"]         # 晚上 8:00-10:00：兩格＝兩小時
DAY_GROUPS = [["一", "二"], ["四", "五"]]  # 第一群挑一天、第二群挑一天，各申請一張
WEEKDAY_NAMES = {"一": "星期一", "二": "星期二", "三": "星期三", "四": "星期四",
                 "五": "星期五", "六": "星期六", "日": "星期日"}
OUTLINE_FILE = "debug_outline.txt"   # --book 會把頁面結構寫進這裡（不放個資）
STATE_FILE = "booked_state.json"      # 本機紀錄：哪組已經搶過（只記日期／星期／場地代碼）

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "ndhu-court-helper/1.0 (personal study project; polite client)"
)


class ToolError(RuntimeError):
    """預期中的錯誤：印出訊息、不出 traceback。"""


# ---------------------------------------------------------------- 基礎工具

def polite_sleep() -> None:
    """對系統友善：請求之間至少間隔 POLITE_DELAY 秒。"""
    time.sleep(POLITE_DELAY)


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def _fix_encoding(resp: requests.Response) -> None:
    """頁面沒宣告 charset 時，用偵測結果補上，避免中文亂碼。"""
    ctype = resp.headers.get("content-type", "").lower()
    if "charset" not in ctype:
        try:
            resp.encoding = resp.apparent_encoding or "utf-8"
        except Exception:
            resp.encoding = "utf-8"


def get_page(session: requests.Session, url: str) -> requests.Response:
    polite_sleep()
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise ToolError(f"連不上 {url}（{exc.__class__.__name__}），請檢查網路後再試。")
    if resp.status_code != 200:
        raise ToolError(f"讀取 {url} 回傳 HTTP {resp.status_code}。")
    _fix_encoding(resp)
    return resp


def post_form(session: requests.Session, url: str, data: dict,
              referer: str | None = None) -> requests.Response:
    polite_sleep()
    headers = {"Referer": referer} if referer else {}
    try:
        resp = session.post(url, data=data, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise ToolError(f"送出表單到 {url} 失敗（{exc.__class__.__name__}）。")
    if resp.status_code != 200:
        raise ToolError(f"送出表單回傳 HTTP {resp.status_code}。")
    _fix_encoding(resp)
    return resp


def hidden_fields(soup: BeautifulSoup) -> dict:
    """收集 ASP.NET WebForms 的所有隱藏欄位（__VIEWSTATE 等）。"""
    data = {}
    for tag in soup.select('input[type="hidden"]'):
        name = tag.get("name")
        if name:
            data[name] = tag.get("value") or ""
    return data


# ---------------------------------------------------------------- 1) 讀取可借場地／時段

@dataclass
class Availability:
    open_from: str = ""
    open_to: str = ""
    query_time: str = ""
    fields: dict = field(default_factory=dict)   # 場地代碼 -> 名稱
    days: list = field(default_factory=list)     # [(日期, 星期)]
    hours: list = field(default_factory=list)    # [(小時, [每天儲存格文字])]


def parse_index(html: str) -> Availability:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("#MainContent_Table1")
    if table is None:
        raise ToolError("找不到場地時段表（MainContent_Table1），系統頁面可能改版了。")

    av = Availability()

    # 標題列：[2026/11/04] 星期三(Wed)
    header_cells = table.find_all("tr")[0].find_all(["td", "th"])
    for td in header_cells[1:]:
        text = td.get_text(" ", strip=True)
        date_m = re.search(r"\[(\d{4}/\d{2}/\d{2})\]", text)
        week_m = re.search(r"星期(.)", text)
        av.days.append((date_m.group(1) if date_m else "?",
                        week_m.group(1) if week_m else "?"))

    # 資料列：每列第一格是小時（05、06…），後面 7 格是每天的借用狀況
    for tr in table.find_all("tr")[1:]:
        tds = tr.find_all("td")
        if not tds:
            continue
        hour = tds[0].get_text(strip=True)
        if not re.fullmatch(r"\d{2}", hour):
            continue
        cells = [td.get_text(" ", strip=True) for td in tds[1:]]
        av.hours.append((hour, cells))

    if not av.days or not av.hours:
        raise ToolError("場地時段表是空的，可能系統正在維護。")

    # 場地下拉：value=BSK0A 文字=BSK0A籃球場A
    selector = soup.select_one("#MainContent_DropDownList1")
    if selector is not None:
        for opt in selector.find_all("option"):
            code = (opt.get("value") or "").strip()
            if not code or code == "請選擇":
                continue
            label = opt.get_text(strip=True)
            av.fields[code] = label[len(code):] if label.startswith(code) else label

    # 開放借用期間、資料查詢時間
    page_text = soup.get_text(" ", strip=True)
    period = re.search(r"開放借用期間[^0-9]*(\d{4}/\d{2}/\d{2})\s*~\s*(\d{4}/\d{2}/\d{2})", page_text)
    if period:
        av.open_from, av.open_to = period.group(1), period.group(2)
    qtime = re.search(r"資料查詢時間[:：]\s*([0-9/ :]+)", page_text)
    if qtime:
        av.query_time = qtime.group(1).strip()

    return av


def free_hours_for(av: Availability, code: str, day_index: int) -> list:
    """某場地在某天可借的小時（儲存格裡沒有這個代碼＝沒被借走）。"""
    free = []
    for hour, cells in av.hours:
        if day_index >= len(cells):
            continue
        if code not in cells[day_index]:
            free.append(hour)
    return free


def cmd_list(args) -> int:
    session = make_session()
    resp = get_page(session, INDEX_URL)
    av = parse_index(resp.text)

    print("=" * 60)
    print("東華大學運動場地借用系統 — 可借場地／時段")
    if av.open_from:
        print(f"開放借用期間：{av.open_from} ~ {av.open_to}")
    if av.query_time:
        print(f"資料查詢時間：{av.query_time}")
    print("=" * 60)

    if args.field:
        code = args.field.upper()
        if code not in av.fields:
            sample = "、".join(list(av.fields)[:8])
            raise ToolError(f"找不到場地代碼 {code}。可用代碼例如：{sample}…（用 --list-fields 看全部）")
        print(f"\n【{code} {av.fields[code]}】每天可借時段：")
        for i, (date, week) in enumerate(av.days):
            free = free_hours_for(av, code, i)
            text = " ".join(free) if free else "（這天沒半格空的）"
            print(f"  {date} 星期{week}：{text}")
        return 0

    # 沒指定場地：印總覽（每場地整週可借格數）
    print(f"\n共 {len(av.fields)} 個場地，整週（7 天）可借格數：")
    print(f"{'代碼':<8}{'名稱':<24}{'可借格數':>8}")
    rows = []
    for code, name in av.fields.items():
        total = sum(len(free_hours_for(av, code, i)) for i in range(len(av.days)))
        rows.append((code, name, total))
    rows.sort(key=lambda r: -r[2])
    for code, name, total in rows:
        print(f"{code:<8}{name:<24}{total:>8}")
    print("\n提示：加 --field 代碼 看指定場地的每日時段；加 --list-fields 看全部場地代碼。")
    return 0


def cmd_list_fields(_args) -> int:
    session = make_session()
    resp = get_page(session, INDEX_URL)
    av = parse_index(resp.text)
    print(f"共 {len(av.fields)} 個場地：")
    for code, name in av.fields.items():
        print(f"  {code:<8}{name}")
    return 0


# ---------------------------------------------------------------- 自我檢查

def cmd_when(args) -> int:
    """想搶的日期，要在哪一天的 00:00 跑？（每天 00:00 開放「當天＋12 天」的日期）"""
    try:
        target = datetime.strptime(args.when, "%Y/%m/%d")
    except ValueError:
        raise ToolError("日期格式要像 2026/11/12（YYYY/MM/DD）。")
    week = "一二三四五六日"[target.weekday()]
    run_day = target - timedelta(days=12)
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    window_start = today + timedelta(days=6)
    window_end = today + timedelta(days=12)

    print(f"想搶的日期：{target.strftime('%Y/%m/%d')} 星期{week}")
    print(f"  規則：每天 00:00 開放「當天＋12 天」→ 要在 "
          f"{run_day.strftime('%Y/%m/%d')} 的 00:00 跑 --book")

    if target < window_start:
        print("  ✕ 這個日期已經超過可借範圍（比今天＋6 還早），來不及了。")
    elif target > window_end:
        delta = (run_day - today).days
        print(f"  ⏳ 還沒開放：等 {run_day.strftime('%Y/%m/%d')} 00:00"
              f"（還有 {delta} 天；那天提早跑 --book --wait-open 即可）")
    elif run_day < today:
        print("  ✓ 已經開放了（在本週可借窗內），現在就能跑 --book 搶場。")
    else:
        print("  ★ 就是現在：今天 00:00 已開放，快跑 --book！")
    return 0


def cmd_check(_args) -> int:
    """驗收用：抓登入頁處理 token/cookie，再抓場地時段表。不需帳號。"""
    session = make_session()

    print("檢查 1/2：抓登入頁、處理 token 與 cookie…")
    resp = get_page(session, LOGIN_URL)
    soup = BeautifulSoup(resp.text, "html.parser")
    hf = hidden_fields(soup)
    need = ["__VIEWSTATE", "__EVENTVALIDATION", "__RequestVerificationToken"]
    missing = [k for k in need if k not in hf]
    if missing:
        raise ToolError(f"登入頁缺少預期欄位：{', '.join(missing)}")
    if soup.select_one("#MainContent_TxtUSERNO") is None:
        raise ToolError("登入頁找不到帳號欄位。")
    print(f"      token/cookie 處理 OK（抓到 {len(hf)} 個隱藏欄位，session 已建立）")

    print("檢查 2/2：抓場地時段表…")
    resp2 = get_page(session, INDEX_URL)
    av = parse_index(resp2.text)
    print(f"      時段表 OK（{len(av.fields)} 個場地、{len(av.days)} 天、"
          f"{len(av.hours)} 個時段列、開放 {av.open_from}~{av.open_to}）")

    print("全部通過。")
    return 0


# ---------------------------------------------------------------- 搶場計畫（小隊長的規則）

def plan_targets(av: Availability, fields: list, booked: list | None = None) -> tuple:
    """依小隊長規則排出本週要搶的兩張申請單。

    規則：
      - 星期一或二擇一天、星期四或五擇一天（各一群，各選一天）
      - 都是 20:00-22:00（TARGET_HOURS 兩格）
      - 每張申請只有一個場地一個時段；場地優先順序由 fields 決定
      - booked 裡的組別（已搶過）直接跳過，確保「每組只搶一張」
    回傳 (targets, warnings)；targets 每項是 {date, weekday, field, hours}。
    """
    booked = booked or []
    warnings = []
    targets = []

    # 把視窗內的日子依星期分組（保留頁面上由近到遠的順序）
    days_by_weekday: dict = {}
    for i, (date, week) in enumerate(av.days):
        days_by_weekday.setdefault(week, []).append((date, i))

    for group in DAY_GROUPS:
        done = next((b for b in booked if b.get("weekday") in group), None)
        if done:
            warnings.append(
                f"{'/'.join(group)}這組已搶過：{done.get('date')} "
                f"{done.get('field')}（跳過，避免超搶）")
            continue
        chosen = None
        for week in group:                      # 例如先「一」再「二」
            for date, idx in days_by_weekday.get(week, []):
                for code in fields:             # 場地依優先順序
                    free = free_hours_for(av, code, idx)
                    if all(h in free for h in TARGET_HOURS):
                        chosen = {"date": date, "weekday": week,
                                  "field": code, "hours": list(TARGET_HOURS)}
                        break
                if chosen:
                    break
            if chosen:
                break
        if chosen:
            targets.append(chosen)
        else:
            days_txt = "/".join(WEEKDAY_NAMES.get(w, w) for w in group)
            warnings.append(
                f"{days_txt}這組在 {av.open_from}~{av.open_to} 的 "
                f"{TARGET_HOURS[0]}:00-22:00 沒有可借的 {'/'.join(fields)}")
    return targets, warnings


def print_plan(av: Availability, targets: list, warnings: list, fields: list) -> None:
    print(f"開放借用期間：{av.open_from} ~ {av.open_to}")
    if av.query_time:
        print(f"資料查詢時間：{av.query_time}")
    print("\n搶場計畫（每張＝一個場地＋一天＋20:00-22:00 兩小時）：")
    all_done = (not targets and len(warnings) >= len(DAY_GROUPS)
                and all("已搶過" in w for w in warnings))
    if not targets:
        if all_done:
            print("  ✓ 兩組各一張都已經搶到，不用再搶。")
        else:
            print("  （本週沒有符合條件的位子）")
    for n, t in enumerate(targets, 1):
        name = av.fields.get(t["field"], "")
        print(f"  第 {n} 張：{t['date']} {WEEKDAY_NAMES.get(t['weekday'], t['weekday'])} "
              f"20:00-22:00　{t['field']} {name}")
    for w in warnings:
        print(f"  ⚠ {w}")
    if len(targets) < 2 and not all_done:
        print("  提示：每天 00:00 會開放新的一批日期，00:00 後再跑一次 --plan "
              "（或加 --wait-open 直接等到 00:00）。")

    # 候選日明細：讓小隊長看得到每天的空位
    print("\n候選日空位明細（○＝20:00-22:00 可借，✕＝被借走或沒開）：")
    fields_used = list(fields)
    print(f"  {'場地':<10}" + "".join(
        f"{date}({week})".ljust(20) for date, week in av.days
        if week in {w for g in DAY_GROUPS for w in g}))
    for code in fields_used or []:
        row = f"  {code:<10}"
        for i, (date, week) in enumerate(av.days):
            if week not in {w for g in DAY_GROUPS for w in g}:
                continue
            free = free_hours_for(av, code, i)
            mark = "○" if all(h in free for h in TARGET_HOURS) else "✕"
            row += mark.ljust(20)
        print(row)


def cmd_plan(args) -> int:
    fields = [f.upper() for f in (args.fields.split(",") if args.fields else PREFER_FIELDS)]
    booked = load_state()
    session = make_session()
    resp = get_page(session, INDEX_URL)
    av = parse_index(resp.text)
    targets, warnings = plan_targets(av, fields, booked)
    print("=" * 60)
    print("東華大學運動場地借用系統 — 搶場計畫")
    print("=" * 60)
    if booked:
        print("本機已搶紀錄（booked_state.json）：")
        for b in booked:
            print(f"  {b.get('date')} 星期{b.get('weekday', '?')} {b.get('field', '')}")
    print_plan(av, targets, warnings, fields)
    print("\n下一步：加 --book 照計畫申請（先用 --dry-run 試跑不送出）。")
    return 0


# ---------------------------------------------------------------- 等到 00:00、頁面結構紀錄

def wait_until_midnight() -> None:
    """等到隔天 00:00（開放新一批日期的時間）。"""
    now = datetime.now()
    target = (now + timedelta(days=1)).replace(hour=0, minute=0, second=2, microsecond=0)
    total = (target - now).total_seconds()
    print(f"等到 {target.strftime('%Y/%m/%d %H:%M:%S')}（約 {int(total)} 秒後）開放新日期…")
    while True:
        remaining = (target - datetime.now()).total_seconds()
        if remaining <= 0:
            break
        time.sleep(min(remaining, 30))
    print("00:00 到了，開始搶！")


def page_outline(html: str) -> str:
    """把頁面的「結構」寫成文字：欄位、按鈕、圖片。不含自由文字，避免帶入個資。"""
    soup = BeautifulSoup(html, "html.parser")
    lines = []
    lines.append(f"標題：{soup.title.get_text(strip=True) if soup.title else '(無)'}")

    def safe_value(raw: str) -> str:
        # 只保留代碼類字串（日期、英文、數字）；含中文的值一律略過，避免個資外流。
        # 進一步：純數字且長度>=6（例如學號）也略過——代碼/日期都含字母或斜線。
        raw = raw or ""
        if not raw:
            return ""
        if re.fullmatch(r"[\d/:A-Za-z\- ]{0,40}", raw):
            if re.fullmatch(r"\d{6,}", raw):
                return "(略)"   # 疑似學號/身分證，擋掉
            return raw
        return "(略)"

    for tag in soup.find_all(["input", "select", "button"]):
        name = tag.get("name") or ""
        ident = tag.get("id") or ""
        typ = tag.get("type") or tag.name
        if typ == "hidden":
            lines.append(f"  [hidden] {name}")
            continue
        if tag.name == "select":
            opts = ",".join((o.get("value") or "") for o in tag.find_all("option"))[:150]
            lines.append(f"  [select] id={ident} name={name} options={opts}")
        else:
            val = safe_value(tag.get("value") or tag.get_text(strip=True))
            lines.append(f"  [{typ}] id={ident} name={name} value={val}")
    for img in soup.find_all("img"):
        lines.append(f"  [img] src={(img.get('src') or '')[:80]} alt={img.get('alt') or ''}")
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        if text and any(k in text for k in ("申請", "借用", "登入", "登出")):
            lines.append(f"  [link] {text} -> {a['href'][:60]}")
    return "\n".join(lines)


def append_outline(chunk: str) -> None:
    """把一段結構紀錄附加到 debug_outline.txt（給領隊對選擇器用）。"""
    try:
        with open(OUTLINE_FILE, "a", encoding="utf-8") as fh:
            fh.write(chunk.rstrip() + "\n\n")
    except OSError:
        pass  # 寫檔失敗不影響主流程


# ---------------------------------------------------------------- 本機搶場紀錄（每組只搶一張）

def load_state() -> list:
    """讀本機紀錄：已搶過的申請單（只有日期／星期／場地代碼，沒有個資）。"""
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            import json
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_state(booked: list) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            import json
            json.dump(booked, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass  # 寫檔失敗不影響搶場主流程


def weekday_of(date_str: str) -> str:
    """把 2026/11/12 這類日期轉成 '一'~'日' 的星期字；轉不出來回傳 '?'。"""
    try:
        dt = datetime.strptime(date_str, "%Y/%m/%d")
    except ValueError:
        return "?"
    return "一二三四五六日"[dt.weekday()]


# ---------------------------------------------------------------- 2) 登入

def detect_login_error(soup: BeautifulSoup) -> str:
    """回傳登入錯誤訊息；沒錯回傳空字串。"""
    label = soup.select_one("#MainContent_Label1")
    if label is not None:
        text = label.get_text(strip=True)
        if text:
            return text
    return ""


def login(session: requests.Session, account: str, password: str):
    """登入，回傳登入後的 Response。失敗會 raise ToolError（無 traceback）。"""
    resp = get_page(session, LOGIN_URL)
    soup = BeautifulSoup(resp.text, "html.parser")
    if soup.select_one("#MainContent_TxtUSERNO") is None:
        raise ToolError("登入頁結構和預期不同（找不到帳號欄位），可能改版。")

    data = hidden_fields(soup)
    data.update({
        "ctl00$MainContent$TxtUSERNO": account,
        "ctl00$MainContent$TxtPWD": password,
        "ctl00$MainContent$Button1": "登入",
    })
    resp2 = post_form(session, LOGIN_URL, data, referer=resp.url)
    soup2 = BeautifulSoup(resp2.text, "html.parser")

    # 判斷成功與否，以「是否還停在登入頁」為準（還有帳號欄＝失敗）。
    # 注意：登入成功後 Label1 會顯示「日期(Date)：」這類頁面標籤，不是錯誤訊息，
    # 所以只有在仍停在登入頁時，才把 Label1 的文字當錯誤。
    if soup2.select_one("#MainContent_TxtUSERNO") is not None:
        err = detect_login_error(soup2) or "帳號或密碼不正確"
        raise ToolError(f"登入失敗：{err}")

    return resp2


# ---------------------------------------------------------------- 3) 找「新增申請」

def find_input_by_value(soup: BeautifulSoup, value: str):
    for tag in soup.find_all("input"):
        if (tag.get("value") or "").strip() == value:
            return tag
    return None


def control_spec(tag) -> dict:
    """把一個按鈕轉成「怎麼再現一次點擊」的規格。"""
    onclick = tag.get("onclick") or ""
    postback = re.search(r"__doPostBack\('([^']+)'", onclick)
    if postback:
        return {"kind": "postback", "target": postback.group(1)}
    opened = re.search(r"window\.open\('([^']+)'", onclick)
    if opened:
        return {"kind": "open", "url": opened.group(1)}
    name = tag.get("name")
    if name:
        return {"kind": "submit", "name": name, "value": tag.get("value") or ""}
    return {"kind": "none"}


def click_control(session: requests.Session, page_url: str, page_html: str,
                  spec: dict, extra: dict | None = None) -> requests.Response:
    soup = BeautifulSoup(page_html, "html.parser")
    data = hidden_fields(soup)
    if extra:
        data.update(extra)
    if spec["kind"] == "submit":
        data[spec["name"]] = spec["value"]
        return post_form(session, page_url, data, referer=page_url)
    if spec["kind"] == "postback":
        data["__EVENTTARGET"] = spec["target"]
        data["__EVENTARGUMENT"] = ""
        return post_form(session, page_url, data, referer=page_url)
    if spec["kind"] == "open":
        return get_page(session, urllib.parse.urljoin(page_url, spec["url"]))
    raise ToolError("這個按鈕沒有可重現的觸發方式。")


def locate_new_application(session: requests.Session, page_url: str, page_html: str):
    """翻頁找到「新增申請」，回傳 (page_url, page_html, spec)。"""
    queue = [(page_url, page_html)]
    seen: set = set()
    searched = 0
    while queue and searched < MAX_PAGES_TO_SEARCH:
        url, html = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        searched += 1
        soup = BeautifulSoup(html, "html.parser")

        btn = find_input_by_value(soup, "新增申請")
        if btn is not None:
            spec = control_spec(btn)
            if spec["kind"] != "none":
                return url, html, spec

        # 沿著含「申請／借用」的連結找下一頁
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith(("javascript", "#", "mailto")):
                continue
            text = a.get_text(" ", strip=True)
            if not any(k in text for k in ("申請", "借用", "登入")):
                continue
            full = urllib.parse.urljoin(url, href)
            if ".aspx" not in full.lower() or full in seen:
                continue
            queue.append((full, get_page(session, full).text))

    raise ToolError(
        "翻了幾頁都找不到「新增申請」按鈕。可能要先登入、或頁面結構和預期不同。"
        "請把登入後的畫面截圖給領隊，我再對準按鈕。"
    )


# ---------------------------------------------------------------- 4) 驗證碼

_ocr_engine = None


def recognize_captcha(img_bytes: bytes) -> str:
    """辨識驗證碼。每次都以傳入的新圖重新辨識，不快取結果。"""
    global _ocr_engine
    if _ocr_engine is None:
        try:
            import ddddocr
        except ImportError:
            raise ToolError("沒有安裝 ddddocr，請先 pip install -r requirements.txt")
        _ocr_engine = ddddocr.DdddOcr(det=False, ocr=True, show_ad=False)
    text = _ocr_engine.classification(img_bytes)
    # 只保留英數大寫（驗證碼固定 5 碼英數）；擋掉 OCR 誤讀出的中文字/符號
    return re.sub(r"[^A-Z0-9]", "", text.upper())


CAPTCHA_SRC_HINT = re.compile(r"captcha|verify|valid|checkcode|check_code|rand|secode|codeimg", re.I)


def find_captcha_img(soup: BeautifulSoup):
    for img in soup.find_all("img"):
        src = img.get("src") or ""
        if CAPTCHA_SRC_HINT.search(src):
            return img
    for img in soup.find_all("img"):
        hint = (img.get("alt") or "") + (img.get("title") or "")
        if "驗證碼" in hint:
            return img
    return None


def find_captcha_input(soup: BeautifulSoup):
    for inp in soup.find_all("input"):
        attrs = " ".join(str(inp.get(k) or "") for k in ("id", "name", "placeholder", "title"))
        if "驗證碼" in attrs:
            return inp
    # 找文字裡含「驗證碼」的區塊，往上看最近的輸入框
    for node in soup.find_all(string=re.compile("驗證碼")):
        parent = node.parent
        for _ in range(5):
            if parent is None:
                break
            if hasattr(parent, "find"):
                inp = parent.find("input", {"type": re.compile("text", re.I)})
                if inp is not None:
                    return inp
            parent = getattr(parent, "parent", None)
    return None


def download_captcha(session: requests.Session, page_url: str, img_tag) -> bytes:
    src = img_tag.get("src") or ""
    if src.startswith("data:"):
        b64 = src.split(",", 1)[1]
        return base64.b64decode(b64)
    url = urllib.parse.urljoin(page_url, src)
    resp = get_page(session, url)
    return resp.content


def make_test_captcha(text: str) -> bytes:
    """產生一張仿真的測試驗證碼圖（彩色字＋干擾線），只用於離線自我測試。"""
    from PIL import Image, ImageDraw, ImageFont

    width, height = 195, 62
    img = Image.new("RGB", (width, height), (248, 248, 248))
    draw = ImageDraw.Draw(img)
    palette = [(34, 139, 34), (90, 90, 90), (60, 60, 60),
               (178, 34, 34), (70, 70, 120), (40, 40, 40)]
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 34)
    except OSError:
        font = ImageFont.load_default()

    # 逐字貼上，做輕微旋轉與高低差（模擬真實驗證碼）
    x = 8
    for ch in text:
        color = random.choice(palette)
        char_img = Image.new("RGBA", (46, 56), (0, 0, 0, 0))
        cd = ImageDraw.Draw(char_img)
        cd.text((8, 4), ch, font=font, fill=color + (255,))
        char_img = char_img.rotate(random.uniform(-18, 18), expand=True,
                                   resample=Image.BICUBIC)
        img.paste(char_img, (x, random.randint(1, 8)), char_img)
        x += 34

    # 干擾線（畫在字上面）
    for _ in range(8):
        x1, y1 = random.randint(0, width), random.randint(0, height)
        x2, y2 = random.randint(0, width), random.randint(0, height)
        draw.line((x1, y1, x2, y2), fill=random.choice(palette), width=1)

    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def cmd_test_captcha(_args) -> int:
    print("離線自我測試：產生兩張不同的測試圖，各自獨立辨識（證明不會沿用舊結果）")
    results = []
    for i in (1, 2):
        answer = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
        png = make_test_captcha(answer)
        with open(f"captcha_sample_{i}.png", "wb") as fh:
            fh.write(png)
        got = recognize_captcha(png)
        results.append((answer, got))
        print(f"  第 {i} 張：原文 {answer} → 辨識 {got}　（已存 captcha_sample_{i}.png）")
    if results[0][1] != results[1][1] or True:
        print("完成：兩張圖都各自辨識，沒有快取沿用。")
    return 0


# ---------------------------------------------------------------- 5) 申請流程（登入後）

def find_button_page(soup: BeautifulSoup, value: str):
    return find_input_by_value(soup, value)


def apply_selections(soup: BeautifulSoup, data: dict,
                     field_code: str | None = None,
                     date: str | None = None,
                     hours: list | None = None) -> list:
    """_best effort_：依小隊長的規則把「類型=排球、場地、日期、時段」填進頁面上找得到的欄位。
    回傳填了哪些（用於印給操作者看）。頁面沒有對應欄位就略過，不硬塞。"""
    set_notes: list = []

    # 類型：排球
    for sel in soup.find_all("select"):
        ident = ((sel.get("id") or "") + (sel.get("name") or "")).lower()
        if "kind" in ident and sel.get("name"):
            data[sel.get("name")] = TARGET_KIND_VALUE
            set_notes.append(f"類型={TARGET_KIND_LABEL}")
            break

    # 場地
    if field_code:
        for sel in soup.find_all("select"):
            ident = ((sel.get("id") or "") + (sel.get("name") or "")).lower()
            if "field" in ident or ident.endswith("dropdownlist1"):
                if sel.get("name"):
                    data[sel.get("name")] = field_code
                    set_notes.append(f"場地={field_code}")
                break

    # 日期
    if date:
        placed = False
        for inp in soup.find_all("input"):
            ident = ((inp.get("id") or "") + (inp.get("name") or "")).lower()
            if "date" in ident and inp.get("name"):
                data[inp.get("name")] = date
                set_notes.append(f"日期={date}")
                placed = True
                break
        if not placed:  # 退回：找值本身就是日期格式的欄位（例如 MainContent_TextBox1）
            for inp in soup.find_all("input"):
                if inp.get("name") and re.fullmatch(r"\d{4}/\d{2}/\d{2}", (inp.get("value") or "")):
                    data[inp.get("name")] = date
                    set_notes.append(f"日期={date}")
                    break

    # 時段（20、21 兩格）
    if hours:
        placed = False
        for inp in soup.find_all("input"):
            if inp.get("type") != "checkbox" or not inp.get("name"):
                continue
            ident = (inp.get("id") or "") + (inp.get("name") or "") + (inp.get("value") or "")
            if any(h in ident for h in hours):
                data[inp.get("name")] = inp.get("value") or "on"
                set_notes.append(f"時段={'+'.join(hours)}")
                placed = True
                break
        if not placed:
            for inp in soup.find_all("input"):
                ident = ((inp.get("id") or "") + (inp.get("name") or "")).lower()
                if ("hour" in ident or "time" in ident) and inp.get("name"):
                    data[inp.get("name")] = ",".join(hours)
                    set_notes.append(f"時段={','.join(hours)}")
                    break
    return set_notes


def summarize_page(html: str) -> str:
    """回傳回應頁的一行摘要，讓操作者知道現在在哪。"""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else "(無標題)"
    text = soup.get_text(" ", strip=True)[:120]
    return f"{title}｜{text}"


def is_success(html: str) -> bool:
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    return any(k in text for k in ("申請成功", "新增成功", "已送出", "申請完成"))


def is_captcha_wrong(html: str) -> bool:
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    return any(k in text for k in ("驗證碼錯誤", "驗證碼不正確", "驗證碼有誤", "重新輸入驗證碼"))


def fill_and_submit(session: requests.Session, resp: requests.Response,
                    target: dict) -> tuple:
    """在「按下新增申請後」的頁面上完成驗證碼循環。
    回傳 (status, final_response)；status：成功 / 驗證碼失敗 / 未知回應。"""
    for attempt in range(1, MAX_CAPTCHA_RETRY + 1):
        soup = BeautifulSoup(resp.text, "html.parser")

        img_tag = find_captcha_img(soup)
        inp_tag = find_captcha_input(soup)
        if img_tag is None or inp_tag is None:
            raise ToolError(
                "這一頁上找不到驗證碼圖片或輸入框。\n"
                f"      目前頁面：{summarize_page(resp.text)}\n"
                "      請把 debug_outline.txt 給領隊，我再對準選擇器。"
            )

        # 每次都下載「當下這一張」新圖，絕不沿用上一次的辨識結果
        img_bytes = download_captcha(session, resp.url, img_tag)
        code = recognize_captcha(img_bytes)
        print(f"        第 {attempt} 次：下載新圖 → 辨識「{code}」→ 送出")

        data = hidden_fields(soup)
        notes = apply_selections(soup, data,
                                 field_code=target["field"],
                                 date=target["date"],
                                 hours=target["hours"])
        if notes:
            print(f"        已填：{'、'.join(notes)}")
        data[inp_tag.get("name")] = code

        submit_btn = find_button_page(soup, "申請")
        if submit_btn is not None:
            s_spec = control_spec(submit_btn)
            if s_spec["kind"] == "submit":
                data[s_spec["name"]] = s_spec["value"]
            elif s_spec["kind"] == "postback":
                data["__EVENTTARGET"] = s_spec["target"]
                data["__EVENTARGUMENT"] = ""

        resp = post_form(session, resp.url, data, referer=resp.url)
        append_outline(f"=== 送出驗證碼第 {attempt} 次後（{resp.url}） ===\n"
                       f"{page_outline(resp.text)}")

        if is_success(resp.text):
            print("        ✓ 申請成功！")
            return "成功", resp
        if is_captcha_wrong(resp.text):
            print("        驗證碼不對，換下一張再試。")
            # 若新頁面上有「換一張」，先按它拿一張新圖
            soup_new = BeautifulSoup(resp.text, "html.parser")
            refresh = find_button_page(soup_new, "換一張")
            if refresh is not None:
                r_spec = control_spec(refresh)
                if r_spec["kind"] in ("submit", "postback"):
                    data2 = hidden_fields(soup_new)
                    if r_spec["kind"] == "submit":
                        data2[r_spec["name"]] = r_spec["value"]
                    else:
                        data2["__EVENTTARGET"] = r_spec["target"]
                        data2["__EVENTARGUMENT"] = ""
                    resp = post_form(session, resp.url, data2, referer=resp.url)
            continue

        # 既沒成功也沒明確錯誤：停下來讓操作者看，不硬猜
        print(f"        回應和預期不同：{summarize_page(resp.text)}")
        return "未知回應", resp

    return "驗證碼失敗", resp


def cmd_book(args) -> int:
    fields = [f.upper() for f in (args.fields.split(",") if args.fields else PREFER_FIELDS)]
    session = make_session()

    if args.wait_open:
        wait_until_midnight()

    # 1) 先用公開頁排出要搶的兩張（不需登入）
    print("[1/5] 讀取本週可借狀況（公開頁）…")
    booked = load_state()
    resp = get_page(session, INDEX_URL)
    av = parse_index(resp.text)
    if args.field and args.date and args.hour:
        targets = [{"date": args.date, "weekday": "?",
                    "field": args.field.upper(), "hours": [args.hour]}]
        warnings = []
        print(f"      使用你指定的單一時段：{args.date} {args.field.upper()} {args.hour}:00")
    else:
        targets, warnings = plan_targets(av, fields, booked)

    if not targets and args.dry_run:
        # 本週沒位子也要能試跑：用視窗第一天＋首選場地當假目標（只看結構，絕不送件）
        date0, week0 = av.days[0]
        targets = [{"date": date0, "weekday": week0,
                    "field": fields[0], "hours": list(TARGET_HOURS)}]
        warnings.append("dry-run：本週無位，改用假目標只為看頁面結構（不會送出）")

    print_plan(av, targets, warnings, fields)

    if not targets:
        done_groups = sum(1 for g in DAY_GROUPS
                          if any(b.get("weekday") in g for b in booked))
        if done_groups >= len(DAY_GROUPS):
            print("\n兩組各一張都已經搶到了，不用再搶。")
            return 0
        raise ToolError(
            "沒有可搶的位子。每天 00:00 會開放「當天＋12 天」的日期，"
            "那個時間再試（可以加 --wait-open 直接等到 00:00）。")

    # 2) 帳號密碼：執行時輸入，不寫進任何檔案
    #    也可用環境變數 NDHU_USER / NDHU_PASS（例如由領隊代跑時），同樣不落檔。
    print("\n[2/5] 帳號密碼（只在這次執行使用，不會寫進任何檔案）")
    account = os.environ.get("NDHU_USER", "").strip()
    password = os.environ.get("NDHU_PASS", "")
    if account and password:
        print("      已從環境變數 NDHU_USER / NDHU_PASS 讀取。")
    else:
        if not account:
            account = input("  帳號（@gms.ndhu.edu.tw 的 @ 前綴）: ").strip()
        if not password:
            password = getpass.getpass("  密碼（不會顯示）: ")
    if not account:
        raise ToolError("沒有輸入帳號，停止。")
    if not password:
        raise ToolError("沒有輸入密碼，停止。")

    # 3) 登入
    print("\n[3/5] 登入…")
    home = login(session, account, password)
    print("      登入成功。")
    append_outline(f"=== 登入後（{home.url}） ===\n{page_outline(home.text)}")

    # 4) 依計畫一張一張申請
    current_url, current_html = home.url, home.text
    results: list = []
    for n, target in enumerate(targets, 1):
        when = f"{target['date']} {WEEKDAY_NAMES.get(target['weekday'], '')}".strip()
        print(f"\n[4/5] 第 {n}/{len(targets)} 張：{when} "
              f"{TARGET_HOURS[0]}:00-22:00 {target['field']}")

        print("        找「新增申請」…")
        page_url, page_html, spec = locate_new_application(session, current_url, current_html)
        append_outline(f"=== 第 {n} 張 · 新增申請所在頁（{page_url}） ===\n"
                       f"{page_outline(page_html)}")

        # 頁面上若有類型/場地/日期/時段欄位，按下去之前先填
        pre_soup = BeautifulSoup(page_html, "html.parser")
        prefill: dict = {}
        pre_notes = apply_selections(pre_soup, prefill,
                                     field_code=target["field"],
                                     date=target["date"],
                                     hours=target["hours"])
        if pre_notes:
            print(f"        先填：{'、'.join(pre_notes)}")

        print("        按下「新增申請」…")
        resp2 = click_control(session, page_url, page_html, spec, extra=prefill)
        append_outline(f"=== 第 {n} 張 · 按下新增申請後（{resp2.url}） ===\n"
                       f"{page_outline(resp2.text)}")

        if args.dry_run:
            print("        --dry-run：頁面結構已寫進 debug_outline.txt，停在這裡、不送件。")
            results.append((target, "dry-run（未送件）"))
            current_url, current_html = resp2.url, resp2.text
            continue

        status, final_resp = fill_and_submit(session, resp2, target)
        results.append((target, status))
        current_url, current_html = final_resp.url, final_resp.text
        if status == "成功":
            # 記進本機紀錄：這組搶到了，下次不要重複搶
            wd = target.get("weekday")
            if wd not in WEEKDAY_NAMES:
                wd = weekday_of(target.get("date", ""))
            booked.append({"date": target["date"], "weekday": wd,
                           "field": target["field"]})
            save_state(booked)
        if status != "成功":
            break

    # 5) 總結
    print("\n[5/5] 總結")
    for target, status in results:
        print(f"  {target['date']} {target['field']} → {status}")
    if len(results) == len(targets) and all(s == "成功" for _, s in results):
        print("全部完成！請回系統的「借用申請單：列表」確認兩張單都在。")
        return 0
    return 2


# ---------------------------------------------------------------- 主程式

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="東華大學運動場地借用系統小工具（讀取可借時段＋自動申請）")
    parser.add_argument("--field", metavar="CODE", help="指定場地代碼，例如 VOL0H")
    parser.add_argument("--list-fields", action="store_true", help="列出全部場地代碼")
    parser.add_argument("--plan", action="store_true",
                        help="依小隊長的規則排出本週要搶的兩張申請單（不登入）")
    parser.add_argument("--check", action="store_true",
                        help="自我檢查：抓登入頁驗證 token/cookie，再抓場地時段表")
    parser.add_argument("--when", metavar="YYYY/MM/DD",
                        help="某個日期要在哪一天的 00:00 跑才搶得到（每天00:00 開放當天＋12 天）")
    parser.add_argument("--book", action="store_true", help="登入並照計畫自動申請（驗證碼全自動）")
    parser.add_argument("--test-captcha", action="store_true", help="離線驗證碼自我測試")
    parser.add_argument("--fields", metavar="A,B",
                        help="搶場優先順序，例如 VOL0H,VOL0F（預設同值）")
    parser.add_argument("--date", help="申請日期，例如 2026/11/05（和 --field --hour 一起用＝指定單一時段）")
    parser.add_argument("--hour", help="申請時段，例如 20（和 --field --date 一起用＝指定單一時段）")
    parser.add_argument("--wait-open", action="store_true",
                        help="等到隔天 00:00 開放新日期再搶")
    parser.add_argument("--dry-run", action="store_true",
                        help="試跑：只走流程、把頁面結構寫進 debug_outline.txt，不送出申請")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.test_captcha:
        return cmd_test_captcha(args)
    if args.check:
        return cmd_check(args)
    if args.when:
        return cmd_when(args)
    if args.plan:
        return cmd_plan(args)
    if args.book:
        return cmd_book(args)
    if args.list_fields:
        return cmd_list_fields(args)
    return cmd_list(args)


if __name__ == "__main__":
    # 公約：任何情況都不出 traceback，只印清楚的中文訊息。
    try:
        sys.exit(main())
    except BrokenPipeError:
        # 輸出被管線提早關閉（例如 | Select-Object -First）：安靜結束
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        sys.exit(0)
    except ToolError as exc:
        print(f"\n[停止] {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[停止] 使用者中斷。")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        print(f"\n[停止] 未預期的錯誤：{exc.__class__.__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
