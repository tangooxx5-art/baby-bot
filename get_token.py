"""
LINE Channel Access Token v2.1 取得工具
========================================
此腳本可幫助您完成取得 Channel Access Token v2.1 的完整流程。

使用方式：
  步驟 1: python get_token.py generate-keys
           → 產生 RSA 金鑰對 (private_key.json, public_key.json)
           → 將 public_key.json 的內容貼到 LINE Developers Console

  步驟 2: python get_token.py issue-token --kid YOUR_KID --channel-id YOUR_CHANNEL_ID
           → 使用私鑰產生 JWT 並換取 Channel Access Token
"""

import sys
import json
import time
import argparse
import requests

# ============================================================
# 步驟 1：產生 RSA 金鑰對
# ============================================================
def generate_keys():
    try:
        from jwcrypto import jwk
    except ImportError:
        print("錯誤：請先安裝 jwcrypto 套件")
        print("執行：pip install jwcrypto")
        sys.exit(1)

    # 產生 RSA 2048-bit 金鑰對
    key = jwk.JWK.generate(kty='RSA', alg='RS256', use='sig', size=2048)

    private_key = json.loads(key.export_private())
    public_key = json.loads(key.export_public())

    # 儲存私鑰
    with open('private_key.json', 'w') as f:
        json.dump(private_key, f, indent=2)

    # 儲存公鑰
    with open('public_key.json', 'w') as f:
        json.dump(public_key, f, indent=2)

    print("=" * 60)
    print("✅ 金鑰對已成功產生！")
    print("=" * 60)
    print()
    print(f"  🔒 私鑰已儲存至: private_key.json (請妥善保管，勿外洩)")
    print(f"  🔑 公鑰已儲存至: public_key.json")
    print()
    print("=" * 60)
    print("📋 接下來請到 LINE Developers Console 註冊公鑰：")
    print("=" * 60)
    print()
    print("  1. 前往 https://developers.line.biz/console/")
    print("  2. 選擇您的 Provider → 選擇您的 Messaging API Channel")
    print("  3. 點擊 「Basic settings」 頁籤")
    print("  4. 找到 「Assertion Signing Key」 區塊")
    print("  5. 點擊 「Register a public key」 按鈕")
    print("  6. 將以下公鑰內容 (整段 JSON) 貼入：")
    print()
    print(json.dumps(public_key, indent=2))
    print()
    print("  7. 按下 「Register」 後，系統會給您一個 kid 值")
    print("  8. 複製該 kid 值，然後執行步驟 2：")
    print()
    print("  python get_token.py issue-token --kid 你的KID值 --channel-id 你的CHANNEL_ID")
    print()
    print("  (Channel ID 也可以在 Basic settings 頁面最上方找到)")


# ============================================================
# 步驟 2：產生 JWT 並換取 Channel Access Token
# ============================================================
def issue_token(kid: str, channel_id: str):
    try:
        import jwt
        from jwt.algorithms import RSAAlgorithm
    except ImportError:
        print("錯誤：請先安裝 PyJWT 與 cryptography 套件")
        print("執行：pip install PyJWT cryptography")
        sys.exit(1)

    # 讀取私鑰
    try:
        with open('private_key.json', 'r') as f:
            private_key = json.load(f)
    except FileNotFoundError:
        print("錯誤：找不到 private_key.json！")
        print("請先執行：python get_token.py generate-keys")
        sys.exit(1)

    # 組裝 JWT Header
    headers = {
        "alg": "RS256",
        "typ": "JWT",
        "kid": kid
    }

    # 組裝 JWT Payload
    payload = {
        "iss": channel_id,          # Channel ID
        "sub": channel_id,          # Channel ID (與 iss 相同)
        "aud": "https://api.line.me/",
        "exp": int(time.time()) + (60 * 30),       # JWT 有效期: 30 分鐘
        "token_exp": 60 * 60 * 24 * 30              # Token 有效期: 30 天
    }

    # 使用私鑰簽署 JWT
    rsa_key = RSAAlgorithm.from_jwk(private_key)
    jwt_token = jwt.encode(payload, rsa_key, algorithm="RS256", headers=headers)

    print("✅ JWT 產生成功！")
    print()
    print("正在向 LINE API 換取 Channel Access Token...")
    print()

    # 向 LINE API 換取 Token
    response = requests.post(
        "https://api.line.me/oauth2/v2.1/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": jwt_token
        }
    )

    if response.status_code == 200:
        result = response.json()
        access_token = result.get("access_token", "")
        key_id = result.get("key_id", "")
        expires_in = result.get("expires_in", 0)

        print("=" * 60)
        print("🎉 Channel Access Token 取得成功！")
        print("=" * 60)
        print()
        print(f"  Access Token: {access_token[:50]}...")
        print(f"  Key ID:       {key_id}")
        print(f"  有效期:       {expires_in} 秒 ({expires_in // 86400} 天)")
        print()
        print("=" * 60)
        print("📋 下一步：將 Token 填入 .env 檔案")
        print("=" * 60)
        print()
        print(f"  LINE_CHANNEL_ACCESS_TOKEN={access_token}")
        print()

        # 也存一份到檔案以供備查
        with open('token_result.json', 'w') as f:
            json.dump(result, f, indent=2)
        print("  (完整結果也已儲存至 token_result.json)")
    else:
        print(f"❌ 取得 Token 失敗！HTTP {response.status_code}")
        print(f"  回應: {response.text}")
        print()
        print("常見錯誤原因：")
        print("  - kid 值不正確")
        print("  - Channel ID 不正確")
        print("  - 公鑰尚未在 Console 中註冊")
        print("  - private_key.json 與已註冊的公鑰不匹配")


# ============================================================
# CLI 入口
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="LINE Channel Access Token v2.1 取得工具"
    )
    subparsers = parser.add_subparsers(dest="command")

    # 子命令: generate-keys
    subparsers.add_parser("generate-keys", help="產生 RSA 金鑰對 (私鑰 + 公鑰)")

    # 子命令: issue-token
    issue_parser = subparsers.add_parser("issue-token", help="使用 JWT 換取 Channel Access Token")
    issue_parser.add_argument("--kid", required=True, help="從 Console 取得的 kid 值")
    issue_parser.add_argument("--channel-id", required=True, help="您的 Channel ID")

    args = parser.parse_args()

    if args.command == "generate-keys":
        generate_keys()
    elif args.command == "issue-token":
        issue_token(args.kid, args.channel_id)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
