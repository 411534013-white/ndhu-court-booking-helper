#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
東華運動場地借用系統 — 正式搶場腳本（Playwright 真實瀏覽器版）

用法（帳號密碼用環境變數 NDHU_USER / NDHU_PASS 注入，不落檔）：
  # 試跑：走到「驗證碼辨識＋填入」就停，不送出
  python booker_pw.py --date 2026/11/12 --field VOL0H --dry-run

  # 正式搶：搶指定日期 20:00-22:00（--hour 預設 20）
  python booker_pw.py --date 2026/11/12 --field VOL0H

  # 等到隔天 00:00（開放「當天+12天」）再搶
  python booker_pw.py --date 2026/11/12 --field VOL0H --wait-open

流程：登入 → 選排球→選場地→查詢（[申請]鈕出現）→ 點目標日期/時段的 [申請]
      → 讀驗證碼 base64 → ddddocr 立即辨識 → 填入 → 申請(doApp) → 確定鎖單

規則（小隊長指定）：只搶排球 VOL0H/VOL0F；20:00-22:00；一張一個場地兩小時。
紅線：帳密只在記憶體、不寫檔；每步間隔≥2秒；驗證碼最多重試5次。
"""
import argparse
import base64
import os
import sys
import time
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright

# 复用 court_booking 的驗證碼辨識（ddddocr）
from court_booking import recognize_captcha, ToolError

LOGIN_URL = "https://sys.ndhu.edu.tw/gc/sportcenter/SportsFields/login.aspx"
TARGET_HOUR = 20          # 20:00 開始（= 20~22，兩小時）
MAX_CAPTCHA_RETRY = 5     # 小隊長規定：驗證碼最多 5 次
STEP_DELAY = 2            # 每個動作間至少間隔 2 秒（不對系統洪水式請求）


def log(msg: str) -> None:
    print(msg, flush=True)


class Booker:
    def __init__(self, headless: bool = True):
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=headless)
        self.page = self.browser.new_page(viewport={"width": 1500, "height": 1000})
        # 攔截 alert / confirm，自動關閉
        self.page.on("dialog", lambda d: d.dismiss())

    def close(self):
        try:
            self.browser.close()
        finally:
            self._pw.stop()

    # -------------------------------------------------------- 登入
    def login(self) -> None:
        account = os.environ.get("NDHU_USER", "").strip()
        password = os.environ.get("NDHU_PASS", "")
        if not account or not password:
            raise ToolError("缺少環境變數 NDHU_USER / NDHU_PASS（帳密不落檔，執行時注入）。")

        log("[1] 打開登入頁…")
        self.page.goto(LOGIN_URL, wait_until="domcontentloaded")
        time.sleep(STEP_DELAY)
        self.page.fill("#MainContent_TxtUSERNO", account)
        self.page.fill("#MainContent_TxtPWD", password)
        self.page.click("#MainContent_Button1")
        self.page.wait_for_load_state("networkidle")
        if "Default.aspx" not in self.page.url:
            raise ToolError(f"登入失敗（仍在 {self.page.url}）。")
        log("      登入成功。")

    # -------------------------------------------------------- 進入可申請模式
    def prepare_grid(self, field: str) -> int:
        """選 排球 → 選場地 → 查詢，讓 [申請] 按鈕出現。回傳 [申請] 鈕數。"""
        log(f"[2] 選類型=排球、場地={field}，查詢…")
        self.page.select_option("#MainContent_drpkind", "2")   # 排球
        self.page.wait_for_load_state("networkidle")
        time.sleep(STEP_DELAY)
        self.page.select_option("#MainContent_DropDownList1", field)
        self.page.wait_for_load_state("networkidle")
        time.sleep(STEP_DELAY)
        self.page.click("#MainContent_Button1")                # 查詢
        self.page.wait_for_load_state("networkidle")
        time.sleep(STEP_DELAY)
        n = self.page.locator("button:has-text('[申請]')").count()
        log(f"      查詢完成，[申請] 按鈕 {n} 個。")
        # 必須先建立申請單，否則點 [申請] 會被擋（「請新增一張申請單…」）
        self.page.click("#MainContent_Button2")                # 新增申請
        self.page.wait_for_load_state("networkidle")
        time.sleep(STEP_DELAY)
        has_form = self.page.evaluate(
            "() => !!document.getElementById('MainContent_AppPlaceTB')")
        log(f"      新增申請單：{'成功' if has_form else '失敗'}。")
        return n

    # -------------------------------------------------------- 找目標 [申請] 按鈕
    def find_app_button(self, date: str, hour: int):
        """回傳目標格的 button element handle；找不到回傳 None。
        date 形如 2026/11/12；hour 為起始時段（20 = 20:00）。"""
        return self.page.evaluate(
            """([date, hour]) => {
              const t = document.getElementById('MainContent_Table1');
              if (!t) return null;
              const rows = [...t.querySelectorAll('tr')];
              const header = rows[0] ? [...rows[0].querySelectorAll('td,th')] : [];
              // 找目標日期所在欄 index
              let col = -1;
              header.forEach((h, i) => {
                if ((h.innerText||'').includes(date)) col = i;
              });
              if (col < 0) return null;
              const hourStr = String(hour).padStart(2, '0');
              // 找時段列（第0格 = 起始時段）在該欄的按鈕
              for (let ri = 1; ri < rows.length; ri++) {
                const cells = [...rows[ri].querySelectorAll('td,th')];
                if (!cells[0]) continue;
                const h = (cells[0].innerText||'').trim();
                if (h === hourStr) {
                  const td = cells[col];
                  if (!td) return null;
                  const b = td.querySelector('button');
                  if (b && (b.textContent||'').includes('[申請]')) {
                    b.setAttribute('data-pw-target', '1');
                    return true;
                  }
                  return null;  // 該時段格不是 [申請]（已滿/沒開）
                }
              }
              return null;
            }""",
            [date, hour],
        )

    # -------------------------------------------------------- 驗證碼循環 + 送出
    def book_slot(self, dry_run: bool) -> str:
        """點 [申請] → 驗證碼 → 辨識填入 → 送出。回傳狀態字串。"""
        btn = self.page.locator("button[data-pw-target='1']")
        log("[3] 點目標 [申請] 鈕，等驗證碼視窗…")
        btn.first.click()
        self.page.wait_for_selector("#imgCaptcha", timeout=15000)
        time.sleep(1)

        for attempt in range(1, MAX_CAPTCHA_RETRY + 1):
            # 讀當下這張圖的 base64 → 立即辨識（不下載、不沿用舊結果）
            src = self.page.evaluate(
                "() => (document.getElementById('imgCaptcha')||{}).src || ''")
            if not src:
                raise ToolError("驗證碼視窗找不到圖。")
            b64 = src.split(",", 1)[1] if src.startswith("data:") else src
            img = base64.b64decode(b64)
            code = recognize_captcha(img)
            log(f"      第 {attempt} 次：讀圖→辨識「{code}」→ 填入")

            self.page.fill("#txtCaptchaValue", code)
            time.sleep(0.5)

            if dry_run:
                log("      --dry-run：已辨識並填入，停在送出前（不送出）。")
                return "dry-run（未送出）"

            # 點驗證碼視窗裡的「申請」鈕（onclick 含 doApp）
            self.page.evaluate(
                """() => {
                  const b = [...document.querySelectorAll('button')]
                    .find(x => (x.getAttribute('onclick')||'').includes('doApp'));
                  if (b) b.click();
                }""")
            self.page.wait_for_load_state("networkidle")
            time.sleep(STEP_DELAY)

            # 判斷：申請單第1列是否已填入場地代碼（=這張時段加進去了）
            filled = self.page.evaluate(
                """() => {
                  const tb = document.getElementById('MainContent_AppPlaceTB');
                  if (!tb) return false;
                  const r = tb.querySelectorAll('tr')[1];
                  if (!r) return false;
                  return /[A-Z]{3,4}\\d/.test(r.innerText||'');  // 有場地代碼
                }""")
            if filled:
                log("      ✓ 時段已加入申請單！")
                return "已加入"

            # 沒成功 → 看是不是驗證碼錯（視窗還在/有錯誤訊息）
            err = self.page.evaluate(
                "() => (document.getElementById('hfCaptchaErrMsg')||{}).value || ''")
            still_open = self.page.evaluate(
                "() => !!document.getElementById('imgCaptcha')")
            if still_open:
                log(f"      驗證碼沒過（{err or '換一張再試'}），換下一張…")
                # 換一張
                self.page.evaluate(
                    """() => {
                      const b = [...document.querySelectorAll('button')]
                        .find(x => (x.textContent||'').includes('換一張'));
                      if (b) b.click();
                    }""")
                time.sleep(1)
                continue
            return f"未知回應（{err or '視窗已關但未確認加入'}）"

        return "驗證碼失敗（達5次上限）"

    # -------------------------------------------------------- 確定鎖單
    def confirm_lock(self) -> bool:
        log("[4] 按「確定」鎖單…")
        self.page.click("#MainContent_Button4")
        self.page.wait_for_load_state("networkidle")
        time.sleep(STEP_DELAY)
        # 鎖單後通常跳出確認或成功訊息；用申請單是否仍在＋無錯誤判斷
        ok = self.page.evaluate(
            "() => !!document.getElementById('MainContent_AppPlaceTB')")
        log("      已送出確定。" if ok else "      確定後狀態需人工確認。")
        return ok


def wait_until_midnight() -> None:
    now = datetime.now()
    target = (now + timedelta(days=1)).replace(hour=0, minute=0, second=3, microsecond=0)
    total = (target - now).total_seconds()
    log(f"[等待] 到 {target.strftime('%Y/%m/%d %H:%M:%S')}（約 {int(total)} 秒）開放新日期…")
    while datetime.now() < target:
        time.sleep(min((target - datetime.now()).total_seconds(), 30))
    log("      00:00 到了，開始搶！")


def main() -> int:
    ap = argparse.ArgumentParser(description="東華運動場地借用系統 — Playwright 正式搶場")
    ap.add_argument("--date", required=True, help="目標日期，例如 2026/11/12")
    ap.add_argument("--field", default="VOL0H", help="場地代碼（VOL0H 或 VOL0F）")
    ap.add_argument("--hour", type=int, default=TARGET_HOUR, help="起始時段，預設 20（=20:00-22:00）")
    ap.add_argument("--dry-run", action="store_true", help="走到辨識+填入就停，不送出")
    ap.add_argument("--wait-open", action="store_true", help="等到隔天 00:00 再搶")
    ap.add_argument("--headed", action="store_true", help="顯示瀏覽器畫面（除錯用）")
    args = ap.parse_args()

    try:
        if args.wait_open:
            wait_until_midnight()
            # 00:00 後新日期才進窗口，重新登入確保乾淨狀態

        b = Booker(headless=not args.headed)
        try:
            b.login()
            n = b.prepare_grid(args.field)
            if n == 0:
                raise ToolError(
                    "查詢後沒有 [申請] 按鈕——目標日期可能還沒開放或該時段已滿。"
                    "（每天 00:00 開放「當天+12天」）")

            found = b.find_app_button(args.date, args.hour)
            if not found:
                raise ToolError(
                    f"在 {args.date} {args.hour}:00 找不到可申請鈕"
                    "（該日期不在本週窗口、或該時段已被借走）。")

            status = b.book_slot(args.dry_run)
            log(f"\n[結果] {args.date} {args.field} {args.hour}:00 → {status}")

            if status == "已加入" and not args.dry_run:
                b.confirm_lock()
                log("完成：申請已鎖定。請回系統「借用申請單」確認。")
            return 0
        finally:
            b.close()
    except ToolError as exc:
        print(f"\n[停止] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"\n[停止] {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
