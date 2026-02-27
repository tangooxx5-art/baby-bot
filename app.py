import os
import json
import tempfile
import logging

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

# 延遲初始化 - 避免啟動時因為缺少環境變數而崩潰
line_configuration = None
line_handler = None

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
        _process_image(event)


@app.route("/", methods=['GET'])
def health_check():
    """健康檢查路由 - 用於 Render 保持服務不休眠"""
    return "Baby Bot is running! 🍼"


@app.route("/callback", methods=['POST'])
def callback():
    from linebot.v3.exceptions import InvalidSignatureError

    # 取得 X-Line-Signature header
    signature = request.headers.get('X-Line-Signature', '')

    # 取得 request body
    body = request.get_data(as_text=True)
    logger.info("Request body: " + body)

    # 初始化 LINE SDK 並處理 webhook
    _, handler = get_line_config()

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("Invalid signature. Please check your channel access token/channel secret.")
        abort(400)

    return 'OK'


def _process_image(event):
    """處理使用者傳送的影像訊息"""
    import google.generativeai as genai
    from linebot.v3.messaging import (
        ApiClient,
        MessagingApi,
        MessagingApiBlob,
        ReplyMessageRequest,
        FlexMessage,
        FlexContainer
    )

    config, _ = get_line_config()

    # 設定 Gemini
    genai.configure(api_key=GEMINI_API_KEY)

    temp_file_path = None

    try:
        # 1. 取得圖片內容
        with ApiClient(config) as api_client:
            line_bot_blob_api = MessagingApiBlob(api_client)
            message_content = line_bot_blob_api.get_message_content(event.message.id)

            # 將圖片存入暫存檔以傳遞給 Gemini
            with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tf:
                tf.write(message_content)
                temp_file_path = tf.name

        # 2. 上傳圖片至 Gemini API
        sample_file = genai.upload_file(path=temp_file_path, display_name="Ultrasound Image")

        # 根據 PRD 使用 Gemini 1.5 Pro
        model = genai.GenerativeModel('gemini-1.5-pro')

        prompt = """
        請作為一名「暖心孕期助理」，處理傳入的影像：
        - OCR 提取：辨識 GA (週數)、EFW (體重)、EDD (預產期)。
        - 語境生成：
          1. 使用「第一人稱寶寶語氣」（例如：媽咪，我今天...）。
          2. 將重量與水果/食物對比（如：200g = 一顆大蘋果）。
          3. 偵測照片內容（若是 3D 臉部，稱讚鼻子或嘴巴；若是黑白 2D，強調心跳與成長）。
        - 輸出限制：僅輸出 JSON 格式，包含 `weeks`, `weight_status`, `message`, `suggested_color`。
        請勿輸出任何 markdown 標記 (如 ```json 等)，直接輸出乾淨的 JSON 字串。
        """

        response = model.generate_content([sample_file, prompt])

        # 嘗試解析 JSON (防呆處理)
        try:
            response_text = response.text.strip()
            if response_text.startswith("```json"):
                response_text = response_text[7:]
            if response_text.startswith("```"):
                response_text = response_text[3:]
            if response_text.endswith("```"):
                response_text = response_text[:-3]

            result_json = json.loads(response_text.strip())
        except (json.JSONDecodeError, AttributeError) as e:
            logger.error(f"JSON parse error: {e}")
            result_json = {
                "weeks": "?",
                "message": "媽咪好！我看不太清楚，可以再傳一次清晰的照片嗎？",
                "weight_status": "未知",
                "suggested_color": "#ffcccc"
            }

        # 刪除 Gemini 上的暫存檔案以節省空間
        try:
            genai.delete_file(sample_file.name)
        except Exception:
            pass

        # 3. 組裝 Flex Message (根據 PRD JSON 結構)
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

        # 4. 回傳訊息給使用者
        with ApiClient(config) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[flex_message]
                )
            )

    except Exception as e:
        logger.error(f"Error processing image: {e}", exc_info=True)
    finally:
        # 清除本地暫存圖檔
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
