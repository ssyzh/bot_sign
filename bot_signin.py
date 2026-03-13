import asyncio
import json
import os
import random
import sys
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession


def load_config(config_path: str) -> dict:
    # 读取配置文件并解析为字典
    config_file = Path(config_path)
    if not config_file.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    with config_file.open("r", encoding="utf-8") as f:
        return json.load(f)


def normalize_steps(steps):
    # 将 steps 统一为二维数组结构，方便顺序执行
    normalized = []
    for step in steps:
        if isinstance(step, list):
            normalized.append([str(s) for s in step])
        else:
            normalized.append([str(step)])
    return normalized


async def click_button_by_text(
    client,
    bot_name,
    candidate_texts,
    poll_interval,
    poll_retries,
    post_click_delay,
):
    # 在最近消息里查找按钮并按文字点击，按配置轮询等待
    last_error = None
    messages = await client.get_messages(bot_name, limit=5)
    print(f"[{bot_name}] 拉取到 {len(messages)} 条消息")
    for message in messages:
        print(f"[{bot_name}] 消息ID: {message}")
        if message.message:
            preview = message.message.replace("\n", " ")[:80]
            print(f"[{bot_name}] 消息预览: {preview}")
        if not message.buttons:
            continue
        print(f"[{bot_name}] 发现按钮消息，准备匹配: {candidate_texts}")
        for row in message.buttons:
            for button in row:
                button_text = getattr(button, "text", "")
                if not button_text:
                    continue
                matched = next((t for t in candidate_texts if t in button_text), None)
                if matched is None:
                    continue
                try:
                    print(f"[{bot_name}] 尝试点击按钮: {button_text}")
                    await message.click(text=button_text)
                    print(f"[{bot_name}] 已点击按钮: {button_text}")
                    await asyncio.sleep(post_click_delay)
                    return {"status": "ok", "button": button_text}
                except Exception as exc:
                    print(f"[{bot_name}] 点击按钮失败: {button_text}，原因: {exc}")
                    last_error = exc
    if last_error:
        return {"status": "failed", "error": str(last_error)}
    return {"status": "failed", "error": "按钮未匹配"}


async def wait_for_result(client, bot_name, last_message_id, poll_interval, poll_retries):
    for attempt in range(1, poll_retries + 1):
        messages = await client.get_messages(bot_name, limit=1)
        if messages:
            message = messages[0]
            if last_message_id is None or message.id != last_message_id:
                text = message.message or ""
                preview = text.replace("\n", " ")[:120]
                print(f"[{bot_name}] 收到结果消息: {preview}")
                return {"status": "ok", "result": preview or str(message)}
        if isinstance(poll_interval, tuple):
            delay = random.uniform(poll_interval[0], poll_interval[1])
            print(f"[{bot_name}] 等待结果中，第 {attempt}/{poll_retries} 次，等待 {delay:.2f}s")
            await asyncio.sleep(delay)
        else:
            print(f"[{bot_name}] 等待结果中，第 {attempt}/{poll_retries} 次，等待 {poll_interval}s")
            await asyncio.sleep(poll_interval)
    return {"status": "timeout", "result": "未收到新消息"}


async def get_latest_message_preview(client, bot_name):
    messages = await client.get_messages(bot_name, limit=1)
    if not messages:
        return ""
    message = messages[0]
    text = message.message or ""
    return text.replace("\n", " ")[:120]


async def run_bot(client, bot, poll_interval, poll_retries, post_click_delay):
    bot_name = bot["name"]
    command = bot.get("command", "/start")
    steps = normalize_steps(bot.get("steps", []))
    last_message_id = None
    before_messages = await client.get_messages(bot_name, limit=1)
    if before_messages:
        last_message_id = before_messages[0].id
    print(f"[{bot_name}] 发送命令: {command}")
    await client.send_message(bot_name, command)
    step_results = []
    for candidate_texts in steps:
        print(f"[{bot_name}] 开始处理步骤: {candidate_texts}")
        click_result = await click_button_by_text(
            client,
            bot_name,
            candidate_texts,
            poll_interval,
            poll_retries,
            post_click_delay,
        )
        step_results.append(
            {
                "step": candidate_texts,
                "status": click_result["status"],
                "button": click_result.get("button"),
                "error": click_result.get("error"),
            }
        )
        if click_result["status"] != "ok":
            latest = await get_latest_message_preview(client, bot_name)
            return {
                "bot": bot_name,
                "status": click_result["status"],
                "result": click_result.get("error", "按钮匹配超时"),
                "last_message": latest,
                "steps": step_results,
            }
    result = await wait_for_result(client, bot_name, last_message_id, poll_interval, poll_retries)
    return {
        "bot": bot_name,
        "status": result["status"],
        "result": result["result"],
        "last_message": await get_latest_message_preview(client, bot_name),
        "steps": step_results,
    }


def resolve_polling(config):
    # 解析轮询策略：支持固定间隔或随机区间
    poll_retries = config.get("poll_retries", 20)
    poll_interval = config.get("poll_interval_seconds", 1)
    if isinstance(poll_retries, list) and len(poll_retries) == 2:
        poll_interval = (float(poll_retries[0]), float(poll_retries[1]))
        poll_retries = int(config.get("poll_max_attempts", 20))
    return poll_interval, poll_retries


def resolve_proxy(config):
    # 解析代理配置，兼容带账号密码的 SOCKS5
    proxy = config.get("proxy")
    if not isinstance(proxy, dict):
        return None
    proxy_type = proxy.get("type")
    addr = proxy.get("addr")
    port = proxy.get("port")
    if not proxy_type or not addr or not port:
        return None
    username = proxy.get("username")
    password = proxy.get("password")
    if username is None and password is None:
        return (proxy_type, addr, int(port))
    return (proxy_type, addr, int(port), username or "", password or "")


async def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    print(f"加载配置: {config_path}")
    config = load_config(config_path)
    is_actions = os.getenv("GITHUB_ACTIONS") == "true"
    api_id_env = os.getenv("TELE_API_ID")
    api_hash_env = os.getenv("TELE_API_HASH")
    session_string_env = os.getenv("TELE_SESSION_STRING")
    api_id_config = config["api_id"]
    api_hash_config = config["api_hash"]
    session = config.get("session", "session_name")
    session_string_config = config.get("session_string")
    if is_actions:
        api_id_value = api_id_env or api_id_config
        api_hash_value = api_hash_env or api_hash_config
        session_string = session_string_env or session_string_config
    else:
        api_id_value = api_id_config or api_id_env
        api_hash_value = api_hash_config or api_hash_env
        session_string = session_string_config or session_string_env
    try:
        api_id = int(api_id_value)
    except (TypeError, ValueError):
        api_id = api_id_config
    api_hash = api_hash_value
    bots = config.get("bots", [])
    poll_interval, poll_retries = resolve_polling(config)
    proxy = resolve_proxy(config)
    if is_actions:
        proxy = None
    post_click_delay = config.get("post_click_delay_seconds", 1)
    if not bots:
        raise ValueError("配置中未找到 bots")

    print(f"会话文件: {session}")
    print(f"代理配置: {proxy}")
    print(f"轮询间隔: {poll_interval}，最大重试: {poll_retries}")
    print(f"点击后等待: {post_click_delay}s")
    bots = list(bots)
    random.shuffle(bots)
    print(f"随机执行顺序: {[bot.get('name') for bot in bots]}")
    client_session = StringSession(session_string) if session_string else session
    async with TelegramClient(client_session, api_id, api_hash, proxy=proxy) as client:
        print("已连接 Telegram，开始执行任务")
        results = []
        for bot in bots:
            result = await run_bot(client, bot, poll_interval, poll_retries, post_click_delay)
            results.append(result)
        print("全部 bot 执行完成，结果汇总：")
        for item in results:
            print(f"[{item['bot']}] {item['status']} - {item['result']}")
            if item.get("last_message"):
                print(f"[{item['bot']}] 最后消息: {item['last_message']}")
            if item.get("steps"):
                for index, step in enumerate(item["steps"], start=1):
                    step_label = " / ".join(step["step"])
                    if step["status"] == "ok":
                        print(f"[{item['bot']}] 步骤{index}: ok - {step_label} -> {step.get('button')}")
                    elif step["status"] == "failed":
                        print(f"[{item['bot']}] 步骤{index}: failed - {step_label} -> {step.get('error')}")
                    else:
                        print(f"[{item['bot']}] 步骤{index}: timeout - {step_label}")


if __name__ == "__main__":
    asyncio.run(main())
