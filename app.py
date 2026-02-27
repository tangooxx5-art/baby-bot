import os
import json
import tempfile
import logging
import threading

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
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')

# 延遲初始化
line_configuration = None
line_handler = None

# 快取偵測到的模型名稱
_cached_model_name = None


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


def _get_best_model(genai):
    """自動偵測最佳可用的 Gemini 模型（不呼叫 list_models 以節省配額）"""
    global _cached_model_name
    if _cached_model_name:
        return _cached_model_name

    # 依偏好順序嘗試，第一個能用的就快取
    candidates = [
        'gemini-2.0-flash',
        'gemini-2.0-pro',
        'gemini-1.5-flash',
        'gemini-1.5-pro',
        'gemini-pro',
    ]

    for name in candidates:
        try:
            model = genai.GenerativeModel(name)
            # 用最輕量的方式測試模型是否存在
            model.count_tokens("test")
            _cached_model_name = name
            logger.info(f"Auto-detected model: {name}")
            return _cached_model_name
        except Exception as e:
            logger.info(f"Model {name} not available: {e}")
            continue

    # 全部失敗就用預設
    _cached_model_name = 'gemini-2.0-flash'
    logger.warning(f"All model checks failed, defaulting to {_cached_model_name}")
    return _cached_model_name


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
    genai.configure(api_key=GEMINI_API_KEY)

    temp_file_path = None

    try:
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

        # 2. 上傳圖片至 Gemini API 並分析（自動偵測模型）
        logger.info("[3/4] Uploading to Gemini and analyzing...")
        sample_file = genai.upload_file(path=temp_file_path, display_name="Ultrasound")
        model_name = _get_best_model(genai)
        logger.info(f"Using model: {model_name}")
        model = genai.GenerativeModel(model_name)

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

        response = model.generate_content([sample_file, prompt])

        # 清理 Gemini 暫存
        try:
            genai.delete_file(sample_file.name)
        except Exception:
            pass

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
        try:
            with ApiClient(config) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.push_message(
                    PushMessageRequest(
                        to=user_id,
                        messages=[TextMessage(text=f"抱歉，處理照片時出了點問題，請稍後再試 🙏\n錯誤: {str(e)[:100]}")]
                    )
                )
        except Exception as push_err:
            logger.error(f"Failed to send error message: {push_err}")
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
