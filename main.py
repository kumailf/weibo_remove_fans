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
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    # 首次扫描时给微博的异步列表足够时间加载，避免过早判断已经到底。
    page.wait_for_timeout(9000)


def unstick_fan_list(page: Page, level: int) -> None:
    """打断卡在底部哨兵的无限加载。

    仅靠小幅上滑常常不够：加载中状态要求视口明显离开底部。
    level 越大，离开底部越远（约 3 屏 → 页面中部 → 接近顶部）。
    """
    page.evaluate(
        """(level) => {
            const h = Math.max(
                document.body.scrollHeight,
                document.documentElement.scrollHeight
            );
            const vh = window.innerHeight;
            let y;
            if (level <= 1) {
                y = Math.max(0, h - vh * 3.5);
            } else if (level === 2) {
                y = Math.max(0, Math.floor(h * 0.45));
            } else {
                y = Math.min(vh, Math.max(0, Math.floor(h * 0.05)));
            }
            window.scrollTo(0, y);
        }""",
        level,
    )
    page.wait_for_timeout(2000 + level * 800)


def scroll_one_screen(page: Page) -> None:
    """逐屏向下定位虚拟列表中的已锁定候选，避免直接跳过中间卡片。"""
    page.evaluate("window.scrollBy(0, Math.max(400, window.innerHeight * 0.8))")
    page.wait_for_timeout(500)


def collect_candidates(
    page: Page, max_scrolls: int, stop_after: int | None = None
) -> list[Candidate]:
    found: dict[str, Candidate] = {}
    stagnant_rounds = 0
    last_height = 0
    last_found = 0
    unstick_level = 0
    max_unstick_level = 3

    for round_no in range(max_scrolls + 1):
        cards = page.locator(CARD_SELECTOR)
        for index in range(cards.count()):
            try:
                candidate = card_candidate(cards.nth(index))
                if candidate:
                    found[candidate.uid] = candidate
                    if stop_after is not None and len(found) >= stop_after:
                        return list(found.values())[:stop_after]
            except Exception as exc:
                print(f"警告：跳过一个无法解析的粉丝卡片：{exc}", file=sys.stderr)

        print(f"扫描轮次 {round_no + 1}：已发现 {len(found)} 个候选")
        if round_no == max_scrolls:
            break

        height = page.evaluate(
            "Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"
        )
        no_progress = height == last_height and len(found) == last_found
        if no_progress:
            if unstick_level < max_unstick_level:
                unstick_level += 1
                print(
                    f"列表未增长，第 {unstick_level}/{max_unstick_level} 次"
                    "离开底部以解除加载卡住……"
                )
                unstick_fan_list(page, unstick_level)
                scroll_once(page)
                continue
            # 三档解锁都无效，才记为一次真正的停滞。
            stagnant_rounds += 1
            unstick_level = 0
        else:
            stagnant_rounds = 0
            unstick_level = 0

        if stagnant_rounds >= 3:
            break
        last_height = height
        last_found = len(found)
        scroll_once(page)
    return list(found.values())


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


def load_candidate_uids() -> set[str]:
    if not CANDIDATES_JSON.exists():
        raise RuntimeError("找不到候选清单，请先运行 python main.py scan")
    data = json.loads(CANDIDATES_JSON.read_text(encoding="utf-8"))
    return {str(item["uid"]) for item in data.get("candidates", [])}


def load_whitelist(path: Path) -> set[str]:
    if not path.exists():
        return set()
    values: set[str] = set()
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        value = line.strip().split(",", 1)[0].strip()
        if value and not value.startswith("#") and value.lower() != "uid":
            values.add(value)
    return values


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
        page.wait_for_timeout(500 + attempt * 300)
        remove_items = page.get_by_text("移除粉丝", exact=True)
        for index in range(remove_items.count()):
            item = remove_items.nth(index)
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
    confirm.wait_for(state="visible", timeout=5000)
    confirm.click()
    page.wait_for_timeout(500)


def clean_candidates(
    page: Page,
    candidates: list[Candidate],
    whitelist: set[str],
    min_delay: float,
    max_delay: float,
    removed_before: int = 0,
    target_total: int | None = None,
) -> tuple[int, int]:
    """只处理启动时锁定的候选；仅为定位这些候选做有限滚动。"""
    removed = 0
    checked = 0
    # 扫描候选时页面可能停在下方；先回到顶部，确保第一个候选重新进入 DOM。
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(1200)

    for expected in candidates:
        if expected.uid in whitelist:
            continue
        matched_card = None
        # 每个候选都从顶部逐屏向下定位。上限与本批人数相关，绝不无限滚动。
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(500)
        max_search_steps = max(12, len(candidates) * 3)
        for attempt in range(max_search_steps + 1):
            cards = page.locator(CARD_SELECTOR)
            for index in range(cards.count()):
                card = cards.nth(index)
                try:
                    candidate = card_candidate(card)
                except Exception as exc:
                    print(f"警告：跳过无法解析的卡片：{exc}", file=sys.stderr)
                    continue
                if candidate and candidate.uid == expected.uid:
                    matched_card = card
                    break
            if matched_card is not None or attempt == max_search_steps:
                break
            scroll_one_screen(page)

        checked += 1
        if matched_card is None:
            print(
                f"停止：从顶部逐屏查找 {max_search_steps} 次后仍找不到锁定候选 "
                f"{expected.name}。"
            )
            break
        try:
            remove_card(page, matched_card)
            removed += 1
            append_action(expected, "removed")
            done = removed_before + removed
            if target_total is None:
                print(f"[{done}] 已移除：{expected.name}")
            else:
                print(f"[{done}/{target_total}] 已移除：{expected.name}")
            page.wait_for_timeout(int(random.uniform(min_delay, max_delay) * 1000))
        except Exception as exc:
            append_action(expected, "failed", str(exc))
            print(f"停止：移除 {expected.uid} 失败：{exc}", file=sys.stderr)
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
    scan.add_argument("--max-scrolls", type=int, default=500, help="最大滚动次数（默认 500）")

    clean = subparsers.add_parser("clean", help="按候选清单分批移除粉丝")
    add_common_arguments(clean)
    clean.add_argument(
        "--limit",
        type=int,
        default=None,
        help="本批最多移除数量；不加则扫描并移除所有匹配候选",
    )
    clean.add_argument("--max-scrolls", type=int, default=500)
    clean.add_argument("--whitelist", type=Path, default=Path("whitelist.txt"))
    clean.add_argument("--min-delay", type=float, default=2.0)
    clean.add_argument("--max-delay", type=float, default=6.0)
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

            whitelist = load_whitelist(args.whitelist)
            unlimited = args.limit is None
            total_removed = 0
            total_checked = 0
            consecutive_no_progress = 0
            round_no = 0

            while unlimited or total_removed < args.limit:
                round_no += 1
                remaining = None if unlimited else args.limit - total_removed
                if round_no > 1:
                    remaining_text = "全部匹配候选" if unlimited else f"{remaining} 个"
                    print(
                        f"\n第 {round_no - 1} 轮未完成，重新进入粉丝页并扫描；"
                        f"还需移除 {remaining_text}……"
                    )
                    fans_url = navigate_to_sorted_fans(page)
                    print(f"已重新进入粉丝页：{fans_url}")
                    wait_for_cards(page)

                # 有 --limit 时只扫描剩余数量；不加则全量扫描匹配候选。
                batch = collect_candidates(page, args.max_scrolls, stop_after=remaining)
                if not batch:
                    consecutive_no_progress += 1
                    print("本轮没有找到匹配候选，将重新扫描。")
                else:
                    if remaining is not None and len(batch) < remaining:
                        print(f"本轮只找到 {len(batch)} 个匹配候选，将按实际数量处理。")
                    write_candidates(batch)
                    print(f"第 {round_no} 轮候选明细：")
                    for index, candidate in enumerate(batch, start=1):
                        print(
                            f"  {index}. 微博名字={candidate.name} | "
                            f"来源={candidate.source} | 已回关=否"
                        )
                    goal_text = "全部匹配" if unlimited else str(args.limit)
                    print(
                        f"候选 {len(batch)} 个，白名单 {len(whitelist)} 个，"
                        f"总目标 {goal_text} 个，已完成 {total_removed} 个。"
                    )
                    removed, checked = clean_candidates(
                        page,
                        batch,
                        whitelist,
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
                    print("连续 3 轮没有成功移除，停止任务，避免无限重试。")
                    break

            goal_summary = "全部匹配" if unlimited else str(args.limit)
            print(
                f"任务完成：移除 {total_removed}/{goal_summary} 个，"
                f"检查 {total_checked} 个候选；日志：{ACTION_LOG}"
            )
        finally:
            try:
                context.close()
            except Exception:
                # 用户按 Ctrl+C 时 Playwright 驱动可能先于 context 退出。
                pass


def main() -> int:
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
