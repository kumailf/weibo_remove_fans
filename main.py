"""安全地扫描并移除微博“兴趣推荐”且未回关的粉丝。"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.sync_api import BrowserContext, Locator, Page, TimeoutError, sync_playwright


def configure_stdio() -> None:
    """保证中文日志在 Windows / Cursor 终端里正常显示。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass


DATA_DIR = Path(".data")
PROFILE_DIR = DATA_DIR / "weibo-profile"
SETTINGS_FILE = DATA_DIR / "settings.json"
CANDIDATES_JSON = DATA_DIR / "candidates.json"
CANDIDATES_CSV = DATA_DIR / "candidates.csv"
ACTION_LOG = DATA_DIR / "actions.jsonl"

CARD_SELECTOR = (
    "div.woo-box-flex.woo-box-justifyBetween"
    ":has(span.woo-pop-ctrl:has(i.woo-font--ellipsis))"
)
UID_PATTERNS = (
    re.compile(r"weibo\.com/u/(\d+)"),
    re.compile(r"/(?:u|profile)/(\d+)"),
    re.compile(r"(?:用户|UID[:：]?\s*)(\d{5,})"),
)


@dataclass(frozen=True)
class Candidate:
    uid: str
    name: str
    source: str
    relation: str
    profile_url: str


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def normalize_fans_url(url: str) -> str:
    """把关注页 URL 规范化为“我的粉丝”页。"""
    url = url.strip()
    if not re.match(r"^https://weibo\.com/u/page/follow/\d+", url):
        raise ValueError("粉丝页 URL 应类似 https://weibo.com/u/page/follow/123456?relate=fans")
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["relate"] = "fans"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def extract_uid(text: str, hrefs: Iterable[str]) -> str | None:
    for value in [*hrefs, text]:
        for pattern in UID_PATTERNS:
            match = pattern.search(value or "")
            if match:
                return match.group(1)
    return None


def read_settings() -> dict:
    if not SETTINGS_FILE.exists():
        return {}
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_fans_url(url: str) -> None:
    ensure_data_dir()
    settings = read_settings()
    settings["fans_url"] = normalize_fans_url(url)
    SETTINGS_FILE.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def launch_context(playwright) -> BrowserContext:
    ensure_data_dir()
    try:
        return playwright.chromium.launch_persistent_context(
            str(PROFILE_DIR.resolve()),
            headless=False,
            channel="chrome",
            no_viewport=True,
            args=["--start-maximized"],
        )
    except Exception as exc:
        raise RuntimeError(
            "无法启动 Chrome。请确认已安装 Google Chrome，且没有另一个脚本正在使用 "
            f"{PROFILE_DIR}。原始错误：{exc}"
        ) from exc


def current_page(context: BrowserContext) -> Page:
    return context.pages[0] if context.pages else context.new_page()


def has_login_cookie(context: BrowserContext) -> bool:
    cookies = context.cookies("https://weibo.com")
    # SUBP 可能在未登录时也存在；SUB 才是微博的用户会话 Cookie。
    return any(c["name"] == "SUB" and c["value"] for c in cookies)


def has_logged_in_ui(page: Page) -> bool:
    """用登录后才出现的导航元素辅助判断，避免残留 Cookie 误报。"""
    selectors = (
        'a[href*="/u/page/follow/"]',
        'a[href*="/profile/"] img',
        'img[alt="profile"]',
    )
    return any(
        page.locator(selector).first.is_visible(timeout=1000)
        for selector in selectors
    )


def is_logged_in(context: BrowserContext, page: Page) -> bool:
    if not has_login_cookie(context):
        return False
    try:
        return has_logged_in_ui(page)
    except Exception:
        return False


def ensure_login(context: BrowserContext, page: Page, timeout_seconds: int = 300) -> None:
    page.goto("https://weibo.com/", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    if is_logged_in(context, page):
        print("已复用保存的微博登录态。")
        return

    print("请在打开的 Chrome 中扫码登录微博；脚本会自动检测登录结果。")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if has_login_cookie(context):
            page.goto("https://weibo.com/", wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
        if is_logged_in(context, page):
            print("登录成功，登录态已保存在 .data/weibo-profile。")
            return
        page.wait_for_timeout(1000)
    raise TimeoutError("等待登录超时，请重新运行 python main.py login")


def resolve_fans_url(page: Page, supplied_url: str | None) -> str:
    if supplied_url:
        url = normalize_fans_url(supplied_url)
        save_fans_url(url)
        return url

    saved = read_settings().get("fans_url")
    if saved:
        return normalize_fans_url(saved)

    for candidate in (page.url, *page.locator('a[href*="/u/page/follow/"]').evaluate_all(
        "els => els.map(e => e.href)"
    )):
        if re.match(r"^https://weibo\.com/u/page/follow/\d+", candidate or ""):
            url = normalize_fans_url(candidate)
            save_fans_url(url)
            return url

    print("未能自动识别粉丝页地址。请在浏览器打开“我的粉丝”，然后回到终端。")
    input("打开后按 Enter 继续：")
    if re.match(r"^https://weibo\.com/u/page/follow/\d+", page.url):
        url = normalize_fans_url(page.url)
        save_fans_url(url)
        return url
    raise RuntimeError(
        "当前仍不是粉丝页。也可以通过 --fans-url 粘贴截图地址，例如："
        "--fans-url \"https://weibo.com/u/page/follow/你的UID?relate=fans\""
    )


def click_first_visible(page: Page, selectors: Iterable[str], description: str) -> None:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.is_visible(timeout=1500):
                locator.click()
                return
        except Exception:
            continue
    raise RuntimeError(f"未找到可点击的“{description}”元素，微博页面结构可能已变化。")


def navigate_to_sorted_fans(page: Page) -> str:
    """按真实 UI 流程进入粉丝页，并切换为关注时间倒序。"""
    print("正在进入个人主页……")
    page.goto("https://weibo.com/", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    click_first_visible(
        page,
        (
            'a:has(img[alt="profile"])',
            'a[href^="/u/"]:has(img)',
            'img[alt="profile"]',
        ),
        "顶部头像",
    )
    page.wait_for_timeout(2000)

    print("正在打开粉丝列表……")
    click_first_visible(
        page,
        (
            'a:has-text("粉丝")',
            '[role="link"]:has-text("粉丝")',
            'span:has-text("粉丝")',
        ),
        "粉丝",
    )
    page.wait_for_timeout(2000)

    # 初始排序一般显示“粉丝数倒序”。点击后选择“关注时间倒序”。
    current_sort = page.get_by_text("关注时间倒序", exact=True)
    if not current_sort.first.is_visible(timeout=1500):
        print("正在切换为关注时间倒序……")
        click_first_visible(
            page,
            (
                'button:has-text("粉丝数倒序")',
                ':text-is("粉丝数倒序")',
            ),
            "粉丝数倒序",
        )
        page.wait_for_timeout(500)
        click_first_visible(
            page,
            (
                '[role="menu"] :text-is("关注时间倒序")',
                '.woo-pop-main :text-is("关注时间倒序")',
                ':text-is("关注时间倒序")',
            ),
            "关注时间倒序",
        )
        page.wait_for_timeout(2000)

    if not re.match(r"^https://weibo\.com/u/page/follow/\d+", page.url):
        raise RuntimeError(f"导航后不是粉丝页：{page.url}")
    save_fans_url(page.url)
    return page.url


def wait_for_cards(page: Page) -> None:
    try:
        page.locator(CARD_SELECTOR).first.wait_for(state="visible", timeout=15_000)
    except TimeoutError as exc:
        raise RuntimeError(
            "未找到粉丝列表。请确认页面已登录并停留在“我的粉丝”；微博页面结构也可能已变化。"
        ) from exc


def card_candidate(card: Locator) -> Candidate | None:
    text = card.inner_text().strip()
    if "兴趣推荐" not in text or not card.get_by_role(
        "button", name="回粉", exact=True
    ).first.is_visible():
        return None

    links = card.locator("a[href]")
    hrefs = links.evaluate_all("els => els.map(e => e.href)")
    uid = extract_uid(text, hrefs)
    if not uid:
        return None

    name = ""
    for line in (line.strip() for line in text.splitlines()):
        if line and line not in {"回粉", "来自 兴趣推荐", "暂无简介"}:
            name = line
            break
    profile_url = next((href for href in hrefs if uid in href), "")
    return Candidate(uid, name or uid, "兴趣推荐", "未回关", profile_url)


def scroll_once(page: Page) -> None:
    """预加载：快速上滑半屏，再继续下滑加载更多。"""
    page.evaluate("window.scrollBy(0, -Math.floor(window.innerHeight * 0.5))")
    page.wait_for_timeout(120)
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(2000)


def scroll_one_screen(page: Page) -> None:
    """逐屏向下，用于顶部顺序扫描与取关定位。"""
    page.evaluate("window.scrollBy(0, Math.max(400, window.innerHeight * 0.8))")
    page.wait_for_timeout(350)


def visible_card_indices_top_to_bottom(page: Page) -> list[int]:
    """按屏幕纵向位置从上到下返回当前卡片索引（贴合关注时间倒序）。"""
    cards = page.locator(CARD_SELECTOR)
    indexed: list[tuple[float, int]] = []
    for index in range(cards.count()):
        try:
            top = float(
                cards.nth(index).evaluate("el => el.getBoundingClientRect().top")
            )
        except Exception:
            top = float("inf")
        indexed.append((top, index))
    indexed.sort(key=lambda item: item[0])
    return [index for _, index in indexed]


def harvest_visible_uids(page: Page) -> set[str]:
    """收集当前视口卡片中的 UID（用于预加载进度，不区分是否匹配规则）。"""
    uids: set[str] = set()
    cards = page.locator(CARD_SELECTOR)
    for index in range(cards.count()):
        try:
            hrefs = cards.nth(index).locator("a[href]").evaluate_all(
                "els => els.map(e => e.href)"
            )
            uid = extract_uid("", hrefs)
            if uid:
                uids.add(uid)
        except Exception:
            continue
    return uids


def preload_fan_list(page: Page, max_scrolls: int) -> int:
    """先把粉丝列表下拉加载完，不解析候选规则。

    max_scrolls：上滑半屏再下滑的最大次数。
    """
    seen: set[str] = set()
    stagnant_rounds = 0
    last_count = 0

    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(400)
    seen |= harvest_visible_uids(page)
    print(f"预加载起始：已载入约 {len(seen)} 个粉丝")
    last_count = len(seen)

    for scroll_no in range(max_scrolls):
        scroll_once(page)
        seen |= harvest_visible_uids(page)
        print(
            f"预加载 {scroll_no + 1}/{max_scrolls}：已载入约 {len(seen)} 个粉丝"
        )
        if len(seen) == last_count:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0
        last_count = len(seen)
        if stagnant_rounds >= 3:
            print(f"连续 {stagnant_rounds} 轮粉丝数未增加，预加载结束。")
            break
    else:
        print(
            f"已达到预加载上限 {max_scrolls} 次（上滑半屏再下滑算 1 次），停止加载。"
        )
    return len(seen)


def card_has_uid(card: Locator, uid: str) -> bool:
    """快速判断卡片是否对应该 UID，避免每次都做完整文案解析。"""
    return (
        card.locator(
            f'a[href*="/u/{uid}"], a[href*="/profile/{uid}"]'
        ).count()
        > 0
    )


def find_card_for_uid(
    page: Page, uid: str, max_search_steps: int, *, from_top: bool = False
) -> Locator | None:
    """定位指定候选。

    1. 默认先在当前位置等待刷新重试（移除后列表会上移）；
    2. 仍找不到则回到顶部，再逐屏往下找。
    整轮第一个候选可直接 from_top=True。
    """

    def scan_visible() -> Locator | None:
        cards = page.locator(CARD_SELECTOR)
        for index in visible_card_indices_top_to_bottom(page):
            card = cards.nth(index)
            try:
                if card_has_uid(card, uid):
                    return card
            except Exception:
                continue
        return None

    def search_down_from_top() -> Locator | None:
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(400)
        matched = scan_visible()
        if matched is not None:
            return matched
        for _ in range(max_search_steps):
            scroll_one_screen(page)
            matched = scan_visible()
            if matched is not None:
                return matched
        return None

    if from_top:
        return search_down_from_top()

    # 先在当前位置等列表刷新，不要立刻回顶。
    for _ in range(5):
        matched = scan_visible()
        if matched is not None:
            return matched
        page.wait_for_timeout(350)

    # 当前位置没有：从顶部重新往下翻找。
    return search_down_from_top()


def scan_candidates_from_top(
    page: Page, max_scrolls: int, stop_after: int | None = None
) -> list[Candidate]:
    """回顶后逐屏扫描，按关注时间倒序收集匹配候选。"""
    found: dict[str, Candidate] = {}
    stagnant_rounds = 0
    last_found = 0

    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(800)

    for round_no in range(max_scrolls + 1):
        cards = page.locator(CARD_SELECTOR)
        for index in visible_card_indices_top_to_bottom(page):
            card = cards.nth(index)
            try:
                hrefs = card.locator("a[href]").evaluate_all(
                    "els => els.map(e => e.href)"
                )
                known_uid = extract_uid("", hrefs)
                if known_uid and known_uid in found:
                    continue
                candidate = card_candidate(card)
                if candidate:
                    found[candidate.uid] = candidate
                    if stop_after is not None and len(found) >= stop_after:
                        return list(found.values())[:stop_after]
            except Exception as exc:
                print(f"警告：跳过一个无法解析的粉丝卡片：{exc}", file=sys.stderr)

        print(f"扫描轮次 {round_no + 1}：已发现 {len(found)} 个候选")
        if round_no == max_scrolls:
            break

        if len(found) == last_found:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0
        last_found = len(found)
        if stagnant_rounds >= 3:
            print(f"连续 {stagnant_rounds} 轮候选未增加，结束扫描。")
            break

        scroll_one_screen(page)
    return list(found.values())


def collect_candidates(
    page: Page, max_scrolls: int, stop_after: int | None = None
) -> list[Candidate]:
    """两阶段：先下拉加载完毕，再回顶按顺序一次性扫描候选。"""
    print("阶段 1/2：下拉加载粉丝列表……")
    loaded = preload_fan_list(page, max_scrolls)
    print(f"预加载完成，约 {loaded} 个粉丝卡片已载入。")
    print("阶段 2/2：回到顶部，按关注时间倒序扫描候选……")
    # 回顶逐屏扫描需要更多步，才能覆盖「跳到底部」预加载出来的列表长度。
    scan_steps = max(max_scrolls * 8, 200)
    return scan_candidates_from_top(page, scan_steps, stop_after=stop_after)


def write_candidates(candidates: list[Candidate]) -> None:
    ensure_data_dir()
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "rule": "来自兴趣推荐，并且显示回粉（我未回关）",
        "count": len(candidates),
        "candidates": [asdict(item) for item in candidates],
    }
    CANDIDATES_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with CANDIDATES_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(Candidate("", "", "", "", "")).keys()))
        writer.writeheader()
        writer.writerows(asdict(item) for item in candidates)


def load_candidates() -> list[Candidate]:
    if not CANDIDATES_JSON.exists():
        return []
    data = json.loads(CANDIDATES_JSON.read_text(encoding="utf-8"))
    candidates: list[Candidate] = []
    for item in data.get("candidates", []):
        try:
            candidates.append(
                Candidate(
                    uid=str(item["uid"]),
                    name=str(item.get("name") or item["uid"]),
                    source=str(item.get("source") or "兴趣推荐"),
                    relation=str(item.get("relation") or "未回关"),
                    profile_url=str(item.get("profile_url") or ""),
                )
            )
        except (KeyError, TypeError):
            continue
    return candidates


def drop_candidate(candidates: list[Candidate], uid: str) -> None:
    """从内存名单和磁盘名单中移除已成功处理的候选。"""
    candidates[:] = [item for item in candidates if item.uid != uid]
    write_candidates(candidates)


def append_action(candidate: Candidate, status: str, detail: str = "") -> None:
    ensure_data_dir()
    entry = {
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        **asdict(candidate),
        "status": status,
        "detail": detail,
    }
    with ACTION_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def remove_card(page: Page, card: Locator) -> None:
    menu = card.locator("span.woo-pop-ctrl:has(i.woo-font--ellipsis)").first
    remove = None
    for attempt in range(3):
        page.keyboard.press("Escape")
        menu.scroll_into_view_if_needed()
        menu.hover(force=attempt > 0)
        candidate = page.get_by_text("移除粉丝", exact=True)
        try:
            # 出现菜单即点，不再固定干等 500ms+。
            candidate.first.wait_for(state="visible", timeout=700 + attempt * 400)
        except TimeoutError:
            continue
        for index in range(candidate.count()):
            item = candidate.nth(index)
            if item.is_visible():
                remove = item
                break
        if remove is not None:
            break
    if remove is None:
        raise RuntimeError("连续悬停 3 次后仍未出现可见的“移除粉丝”菜单")
    remove.click()
    dialog = page.locator('[role="alertdialog"][aria-modal="true"]:visible')
    confirm = dialog.get_by_role("button", name="确认", exact=True)
    confirm.wait_for(state="visible", timeout=3000)
    confirm.click()
    try:
        dialog.wait_for(state="hidden", timeout=2000)
    except TimeoutError:
        page.wait_for_timeout(150)


def clean_candidates(
    page: Page,
    candidates: list[Candidate],
    min_delay: float,
    max_delay: float,
    removed_before: int = 0,
    target_total: int | None = None,
) -> tuple[int, int]:
    """按名单顺序移除；在当前位置等待刷新后继续找下一位，不反复回顶。"""
    removed = 0
    checked = 0
    locked_count = len(candidates)
    # 整轮开始时回顶一次；之后顺着往下走，跳过中间的正常粉丝。
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(400)

    for index, expected in enumerate(list(candidates)):
        if target_total is not None and removed_before + removed >= target_total:
            break
        max_search_steps = max(8, min(40, locked_count * 2))
        matched_card = find_card_for_uid(
            page,
            expected.uid,
            max_search_steps,
            from_top=(index == 0),
        )

        checked += 1
        if matched_card is None:
            print(
                f"停止：查找后仍找不到锁定候选 {expected.name}。"
                f"剩余 {len(candidates)} 个将保留在候选名单中。"
            )
            break
        try:
            remove_card(page, matched_card)
            drop_candidate(candidates, expected.uid)
            removed += 1
            append_action(expected, "removed")
            done = removed_before + removed
            if target_total is None:
                print(
                    f"[{done}] 已移除：{expected.name}；名单剩余 {len(candidates)} 个"
                )
            else:
                print(
                    f"[{done}/{target_total}] 已移除：{expected.name}；"
                    f"名单剩余 {len(candidates)} 个"
                )
            page.wait_for_timeout(int(random.uniform(min_delay, max_delay) * 1000))
        except Exception as exc:
            append_action(expected, "failed", str(exc))
            print(
                f"停止：移除 {expected.uid} 失败：{exc}；"
                f"剩余 {len(candidates)} 个将保留在候选名单中。",
                file=sys.stderr,
            )
            break
    return removed, checked


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--fans-url", help="粉丝页 URL；首次自动识别失败时提供")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("login", help="手动登录并保存持久化登录态")

    scan = subparsers.add_parser("scan", help="只读扫描，生成 JSON/CSV 候选清单")
    add_common_arguments(scan)
    scan.add_argument(
        "--max-scrolls",
        type=int,
        default=100,
        help="预加载最大次数：上滑半屏再下滑算 1 次（默认 100）",
    )

    clean = subparsers.add_parser("clean", help="按候选清单分批移除粉丝")
    add_common_arguments(clean)
    clean.add_argument(
        "--limit",
        type=int,
        default=None,
        help="本批最多移除数量；不加则扫描并移除所有匹配候选",
    )
    clean.add_argument(
        "--max-scrolls",
        type=int,
        default=100,
        help="预加载最大次数：上滑半屏再下滑算 1 次（默认 100）",
    )
    clean.add_argument(
        "--min-delay",
        type=float,
        default=0.4,
        help="两次移除之间的最短间隔秒数（默认 0.4）",
    )
    clean.add_argument(
        "--max-delay",
        type=float,
        default=1.0,
        help="两次移除之间的最长间隔秒数（默认 1.0）",
    )
    clean.add_argument(
        "--confirm",
        action="store_true",
        help="确认实际执行；没有此参数时 clean 会拒绝运行",
    )
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if getattr(args, "max_scrolls", 0) < 0:
        raise ValueError("--max-scrolls 不能为负数")
    if args.command == "clean":
        if not args.confirm:
            raise ValueError("clean 是破坏性操作，确认候选清单后请加 --confirm")
        if args.limit is not None and args.limit <= 0:
            raise ValueError("--limit 必须大于 0")
        if args.min_delay < 0 or args.max_delay < args.min_delay:
            raise ValueError("延迟参数无效，应满足 0 <= min-delay <= max-delay")


def run(args: argparse.Namespace) -> None:
    validate_args(args)
    with sync_playwright() as playwright:
        context = launch_context(playwright)
        try:
            page = current_page(context)
            ensure_login(context, page)
            if args.command == "login":
                print("登录准备完成，可以关闭浏览器。")
                return

            # 必须通过页面点击进入，微博才会初始化粉丝关系和排序状态。
            fans_url = navigate_to_sorted_fans(page)
            print(f"已进入粉丝页：{fans_url}")
            wait_for_cards(page)

            if args.command == "scan":
                candidates = collect_candidates(page, args.max_scrolls)
                write_candidates(candidates)
                print(f"扫描完成：{len(candidates)} 个候选。")
                print(f"请先检查 {CANDIDATES_CSV}，确认后再运行 clean。")
                return

            unlimited = args.limit is None
            total_removed = 0
            total_checked = 0
            consecutive_no_progress = 0
            round_no = 0

            # 首轮扫描锁定名单；之后失败重试只处理剩余候选，不再重新扫描。
            remaining_quota = None if unlimited else args.limit
            pending = collect_candidates(
                page, args.max_scrolls, stop_after=remaining_quota
            )
            write_candidates(pending)
            if not pending:
                print("没有找到可处理的匹配候选。")
            else:
                print(f"已锁定候选 {len(pending)} 个。")

            while pending and (unlimited or total_removed < args.limit):
                round_no += 1
                if round_no > 1:
                    print(
                        f"\n第 {round_no - 1} 轮未完成，重新进入粉丝页；"
                        f"继续处理剩余 {len(pending)} 个候选（不重新扫描）……"
                    )
                    fans_url = navigate_to_sorted_fans(page)
                    print(f"已重新进入粉丝页：{fans_url}")
                    wait_for_cards(page)

                print(f"第 {round_no} 轮候选明细（共 {len(pending)} 个）：")
                for index, candidate in enumerate(pending, start=1):
                    print(
                        f"  {index}. 微博名字={candidate.name} | "
                        f"来源={candidate.source} | 已回关=否"
                    )
                goal_text = "全部匹配" if unlimited else str(args.limit)
                print(
                    f"候选 {len(pending)} 个，总目标 {goal_text} 个，"
                    f"已完成 {total_removed} 个。"
                )
                removed, checked = clean_candidates(
                    page,
                    pending,
                    args.min_delay,
                    args.max_delay,
                    removed_before=total_removed,
                    target_total=None if unlimited else args.limit,
                )
                total_removed += removed
                total_checked += checked
                consecutive_no_progress = (
                    consecutive_no_progress + 1 if removed == 0 else 0
                )

                if consecutive_no_progress >= 3:
                    print(
                        "连续 3 轮没有成功移除，停止任务，避免无限重试；"
                        f"剩余 {len(pending)} 个仍保留在 {CANDIDATES_JSON}。"
                    )
                    break

            goal_summary = "全部匹配" if unlimited else str(args.limit)
            print(
                f"任务完成：移除 {total_removed}/{goal_summary} 个，"
                f"检查 {total_checked} 个候选；名单剩余 {len(pending)} 个；"
                f"日志：{ACTION_LOG}"
            )
        finally:
            try:
                context.close()
            except Exception:
                # 用户按 Ctrl+C 时 Playwright 驱动可能先于 context 退出。
                pass


def main() -> int:
    configure_stdio()
    try:
        run(build_parser().parse_args())
        return 0
    except (ValueError, RuntimeError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已安全停止；已完成的操作记录在日志中。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
