import os
import json
import tempfile
import logging
import threading
import time

from flask import Flask, request, abort
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# 設定 logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 載入環境變數
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '')

# 支援多把 Gemini API Key 輪替使用（動態掃描所有 GEMINI_API_KEY* 環境變數）
GEMINI_API_KEYS = []
_key_names = ['GEMINI_API_KEY'] + [f'GEMINI_API_KEY_{i}' for i in range(2, 21)]
for key_name in _key_names:
    key = os.environ.get(key_name, '')
    if key:
        GEMINI_API_KEYS.append(key)
        logger.info(f"Loaded key from {key_name}")
logger.info(f"Total Gemini API keys loaded: {len(GEMINI_API_KEYS)}")


class QuotaExhaustedError(Exception):
    """所有 API Key 配額都已耗盡"""
    pass


_current_key_index = 0  # 目前使用的 Key 索引

# --- 速率限制 & 冷卻機制 ---
_key_cooldown = {}          # {key_index: cooldown_until_timestamp}
_global_cooldown_until = 0  # 所有 key 都耗盡時的全域冷卻截止時間
_last_request_time = 0      # 上次 API 請求的時間戳
_rate_lock = threading.Lock()  # 保護共享狀態的鎖

# 冷卻時間設定（秒）
PER_KEY_COOLDOWN = 60       # 單把 key 被 429 後暫停 60 秒
GLOBAL_COOLDOWN = 120       # 所有 key 都耗盡後暫停 120 秒
MIN_REQUEST_INTERVAL = 2    # 連續 API 請求間最少間隔 2 秒

# 延遲初始化
line_configuration = None
line_handler = None

# 固定使用的 Gemini 模型（不再動態偵測，節省 API 配額）
GEMINI_MODEL = 'gemini-3-flash'


def get_line_config():
    global line_configuration, line_handler
    if line_configuration is None:
        from linebot.v3.messaging import Configuration
        from linebot.v3 import WebhookHandler
        line_configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
        line_handler = WebhookHandler(LINE_CHANNEL_SECRET)
        _register_handlers()
    return line_configuration, line_handler


def _register_handlers():
    """註冊 LINE webhook 事件處理器"""
    from linebot.v3.webhooks import MessageEvent, ImageMessageContent

    @line_handler.add(MessageEvent, message=ImageMessageContent)
    def handle_image_message(event):
        user_id = event.source.user_id
        message_id = event.message.id
        reply_token = event.reply_token
        thread = threading.Thread(
            target=_process_image_async,
            args=(user_id, message_id, reply_token)
        )
        thread.start()



@app.route("/", methods=['GET'])
def health_check():
    """健康檢查路由"""
    return "Baby Bot is running! 🍼"


@app.route("/callback", methods=['POST'])
def callback():
    from linebot.v3.exceptions import InvalidSignatureError

    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    logger.info("Request body: " + body)

    _, handler = get_line_config()

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("Invalid signature.")
        abort(400)
    except Exception as e:
        logger.error(f"Error in callback handler: {e}", exc_info=True)

    return 'OK'


def _is_in_global_cooldown():
    """檢查是否在全域冷卻期內"""
    now = time.time()
    if now < _global_cooldown_until:
        remaining = int(_global_cooldown_until - now)
        logger.info(f"Global cooldown active, {remaining}s remaining")
        return True, remaining
    return False, 0


def _throttle_request():
    """確保連續請求之間有最小間隔，避免瞬間大量呼叫"""
    global _last_request_time
    with _rate_lock:
        now = time.time()
        elapsed = now - _last_request_time
        if elapsed < MIN_REQUEST_INTERVAL:
            wait = MIN_REQUEST_INTERVAL - elapsed
            logger.info(f"Throttling: waiting {wait:.1f}s before next API call")
            time.sleep(wait)
        _last_request_time = time.time()


def _call_gemini_with_rotation(genai, image_path, prompt, max_rounds=3):
    """使用多把 API Key 輪替呼叫 Gemini，含速率限制、per-key 冷卻、指數退避重試"""
    global _current_key_index, _global_cooldown_until

    if not GEMINI_API_KEYS:
        raise ValueError("No Gemini API keys configured!")

    # 1. 檢查全域冷卻
    in_cooldown, remaining = _is_in_global_cooldown()
    if in_cooldown:
        raise QuotaExhaustedError(
            f"所有 API Key 配額耗盡，全域冷卻中（剩餘 {remaining} 秒）"
        )

    last_error = None

    for round_num in range(max_rounds):
        if round_num > 0:
            wait_seconds = min(15 * (2 ** (round_num - 1)), 60)  # 15s, 30s, 60s
            logger.info(f"All keys exhausted in round {round_num}, waiting {wait_seconds}s before retry...")
            time.sleep(wait_seconds)

        keys_tried = 0
        keys_in_cooldown = 0

        for attempt in range(len(GEMINI_API_KEYS)):
            key_index = (_current_key_index + attempt) % len(GEMINI_API_KEYS)
            now = time.time()

            # 2. 檢查此 key 是否在個別冷卻期
            cooldown_until = _key_cooldown.get(key_index, 0)
            if now < cooldown_until:
                remaining_cd = int(cooldown_until - now)
                logger.info(f"Key #{key_index + 1} in cooldown ({remaining_cd}s left), skipping")
                keys_in_cooldown += 1
                continue

            keys_tried += 1
            api_key = GEMINI_API_KEYS[key_index]
            logger.info(f"[Round {round_num + 1}/{max_rounds}] Trying Key #{key_index + 1}/{len(GEMINI_API_KEYS)}")

            # 3. 限流：確保請求間隔
            _throttle_request()

            try:
                genai.configure(api_key=api_key)
                sample_file = genai.upload_file(path=image_path, display_name="Ultrasound")
                logger.info(f"Using model: {GEMINI_MODEL}")
                model = genai.GenerativeModel(GEMINI_MODEL)
                response = model.generate_content([sample_file, prompt])

                # 清理 Gemini 暫存
                try:
                    genai.delete_file(sample_file.name)
                except Exception:
                    pass

                # 成功！更新索引到下一把，清除此 key 的冷卻
                _current_key_index = (key_index + 1) % len(GEMINI_API_KEYS)
                _key_cooldown.pop(key_index, None)
                return response

            except Exception as e:
                last_error = e
                error_str = str(e)
                if '429' in error_str or 'ResourceExhausted' in error_str or 'quota' in error_str.lower():
                    # 4. 記錄此 key 的冷卻截止時間
                    _key_cooldown[key_index] = time.time() + PER_KEY_COOLDOWN
                    logger.warning(
                        f"Key #{key_index + 1} hit 429, cooldown {PER_KEY_COOLDOWN}s until "
                        f"{time.strftime('%H:%M:%S', time.localtime(_key_cooldown[key_index]))}"
                    )
                    continue
                else:
                    raise

        # 如果這一輪所有 key 都在冷卻中（沒有實際嘗試），直接跳出
        if keys_tried == 0:
            logger.warning("All keys are in per-key cooldown, no keys available to try")
            break

    # 5. 所有嘗試失敗 ➜ 啟動全域冷卻，防止後續請求繼續連打
    _global_cooldown_until = time.time() + GLOBAL_COOLDOWN
    logger.error(
        f"All {len(GEMINI_API_KEYS)} keys exhausted after {max_rounds} rounds. "
        f"Global cooldown activated until {time.strftime('%H:%M:%S', time.localtime(_global_cooldown_until))}"
    )
    raise QuotaExhaustedError(
        f"所有 {len(GEMINI_API_KEYS)} 把 API Key 配額耗盡，已啟動 {GLOBAL_COOLDOWN} 秒全域冷卻"
    )


def _process_image_async(user_id, message_id, reply_token):
    """在背景處理圖片 — 使用 push message 回傳結果（不受 reply token 時限限制）"""
    import google.generativeai as genai
    from linebot.v3.messaging import (
        ApiClient,
        MessagingApi,
        MessagingApiBlob,
        ReplyMessageRequest,
        PushMessageRequest,
        TextMessage,
        FlexMessage,
        FlexContainer
    )

    config, _ = get_line_config()

    temp_file_path = None

    try:
        # 0. 先檢查全域冷卻，如果在冷卻中就直接回覆，不浪費資源下載圖片
        in_cooldown, remaining = _is_in_global_cooldown()
        if in_cooldown:
            logger.info(f"Skipping image processing — global cooldown active ({remaining}s left)")
            with ApiClient(config) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.push_message(
                    PushMessageRequest(
                        to=user_id,
                        messages=[TextMessage(
                            text=f"寶寶剛剛太忙了，正在休息中～請 {remaining} 秒後再傳照片給我哦 🍼💤"
                        )]
                    )
                )
            return

        # 1. 取得圖片內容
        logger.info(f"[1/4] Downloading image: {message_id}")
        with ApiClient(config) as api_client:
            line_bot_blob_api = MessagingApiBlob(api_client)
            message_content = line_bot_blob_api.get_message_content(message_id)

        # 將圖片存入暫存檔
        with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tf:
            if isinstance(message_content, bytes):
                tf.write(message_content)
            elif hasattr(message_content, 'read'):
                tf.write(message_content.read())
            elif hasattr(message_content, 'content'):
                tf.write(message_content.content)
            else:
                tf.write(bytes(message_content))
            temp_file_path = tf.name

        file_size = os.path.getsize(temp_file_path)
        logger.info(f"[2/4] Image saved: {temp_file_path} ({file_size} bytes)")

        if file_size == 0:
            raise ValueError("Downloaded image is empty (0 bytes)")

        # 2. 使用 Gemini API 分析圖片（支援多 Key 輪替 + 速率限制）
        logger.info("[3/4] Uploading to Gemini and analyzing...")

        prompt = """
        請作為一名「暖心孕期助理」，處理傳入的影像：
        - OCR 提取：辨識 GA (週數)、EFW (體重)、EDD (預產期)。
        - 語境生成：
          1. 使用「第一人稱寶寶語氣」（例如：媽咪，我今天...）。
          2. 將重量與水果/食物對比（如：200g = 一顆大蘋果）。
          3. 偵測照片內容（若是 3D 臉部，稱讚鼻子或嘴巴；若是黑白 2D，強調心跳與成長）。
        - 輸出限制：僅輸出 JSON 格式，包含 `weeks`, `weight_status`, `message`, `suggested_color`。
        請勿輸出任何 markdown 標記，直接輸出乾淨的 JSON 字串。
        """

        response = _call_gemini_with_rotation(genai, temp_file_path, prompt)

        # 3. 解析 JSON
        response_text = response.text.strip()
        logger.info(f"Gemini raw response: {response_text[:200]}")

        if response_text.startswith("```json"):
            response_text = response_text[7:]
        if response_text.startswith("```"):
            response_text = response_text[3:]
        if response_text.endswith("```"):
            response_text = response_text[:-3]

        try:
            result_json = json.loads(response_text.strip())
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse failed: {e}, raw: {response_text[:300]}")
            result_json = {
                "weeks": "?",
                "message": response_text[:300] if response_text else "媽咪好！我看不太清楚，可以再傳一次清晰的照片嗎？",
                "weight_status": "未知",
                "suggested_color": "#ffcccc"
            }

        # 4. 組裝 Flex Message 並回傳
        logger.info("[4/4] Sending Flex Message...")
        flex_dict = {
            "type": "bubble",
            "header": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {
                        "type": "text",
                        "text": f"第 {result_json.get('weeks', '?')} 週成長紀錄",
                        "weight": "bold",
                        "size": "xl",
                        "color": "#ff7fa8"
                    }
                ]
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {
                        "type": "text",
                        "text": result_json.get('message', '媽咪好，我是寶寶！'),
                        "wrap": True,
                        "size": "md"
                    }
                ]
            }
        }

        flex_container = FlexContainer.from_dict(flex_dict)
        flex_message = FlexMessage(alt_text="寶寶的超音波紀錄來囉！", contents=flex_container)

        with ApiClient(config) as api_client:
            line_bot_api = MessagingApi(api_client)

            # 先嘗試 reply（如果 token 還有效）
            try:
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=reply_token,
                        messages=[flex_message]
                    )
                )
                logger.info("Reply message sent successfully!")
            except Exception as reply_err:
                logger.warning(f"Reply failed ({reply_err}), using push message instead")
                line_bot_api.push_message(
                    PushMessageRequest(
                        to=user_id,
                        messages=[flex_message]
                    )
                )
                logger.info("Push message sent successfully!")

    except Exception as e:
        logger.error(f"Error processing image: {e}", exc_info=True)

        # 根據錯誤類型給出不同的友善訊息
        if isinstance(e, QuotaExhaustedError):
            user_msg = "寶寶現在有點忙碌，請過幾分鐘再傳一次照片給我哦 🍼💤"
        elif '429' in str(e) or 'quota' in str(e).lower():
            user_msg = "寶寶現在有點忙碌，請過幾分鐘再傳一次照片給我哦 🍼💤"
        else:
            user_msg = "抱歉，處理照片時出了點問題，請稍後再試 🙏"

        try:
            with ApiClient(config) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.push_message(
                    PushMessageRequest(
                        to=user_id,
                        messages=[TextMessage(text=user_msg)]
                    )
                )
        except Exception as push_err:
            logger.error(f"Failed to send error message: {push_err}")
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
