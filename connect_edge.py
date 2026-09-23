from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    context = browser.contexts[0]
    page = context.pages[0] if context.pages else context.new_page()
    print("已连接到手动打开的 Edge，当前 URL：", page.url)
    input("确认已登录后，按 Enter 保存状态...")
    context.storage_state(path='c5game_state_remote.json')
    print("✅ 登录状态已保存到 c5game_state_remote.json")
    browser.close()