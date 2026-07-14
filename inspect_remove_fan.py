"""登录后打开粉丝页，悬停首个候选卡片并保存 DOM，供页面变更排查。"""

from pathlib import Path

from playwright.sync_api import sync_playwright

from main import (
    CARD_SELECTOR,
    card_candidate,
    current_page,
    ensure_login,
    launch_context,
    navigate_to_sorted_fans,
    wait_for_cards,
)


def main() -> None:
    with sync_playwright() as playwright:
        context = launch_context(playwright)
        try:
            page = current_page(context)
            ensure_login(context, page)
            navigate_to_sorted_fans(page)
            wait_for_cards(page)

            cards = page.locator(CARD_SELECTOR)
            for index in range(cards.count()):
                card = cards.nth(index)
                if card_candidate(card):
                    card.locator(
                        "span.woo-pop-ctrl:has(i.woo-font--ellipsis)"
                    ).first.hover()
                    page.wait_for_timeout(700)
                    remove_items = page.get_by_text("移除粉丝", exact=True)
                    print(f"找到 {remove_items.count()} 个‘移除粉丝’文本节点")
                    for item_index in range(remove_items.count()):
                        item = remove_items.nth(item_index)
                        if item.is_visible():
                            print(f"点击可见节点：{item.evaluate('(e) => e.outerHTML')}")
                            item.click()
                            page.wait_for_timeout(700)
                            print("点击后可见按钮：")
                            for text in page.locator("button:visible").all_inner_texts():
                                if text.strip():
                                    print(repr(text.strip()))
                            break
                    break

            output = Path(".data/debug_menu.html")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(page.content(), encoding="utf-8")
            page.screenshot(path=".data/debug_menu.png", full_page=False)
            print(f"已保存 {output}。按 Enter 关闭浏览器。")
            input()
        finally:
            context.close()


if __name__ == "__main__":
    main()
