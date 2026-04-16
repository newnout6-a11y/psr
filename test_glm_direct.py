import requests
import jwt
import time
import os
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("GLM_API_KEY")
print(f"Key loaded: {api_key[:10]}...")

id, secret = api_key.split(".")

payload = {
    "api_key": id,
    "exp": int(time.time()) + 3600,
    "timestamp": int(time.time()),
}
token = jwt.encode(payload, secret, algorithm="HS256", headers={"alg": "HS256", "sign_type": "SIGN"})

headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# Test glm-4
data = {
    "model": "glm-4",
    "messages": [{"role": "user", "content": "Say hello in Russian. 2 sentences."}],
    "max_tokens": 100,
}

print("Testing GLM-4...")
resp = requests.post("https://open.bigmodel.cn/api/paas/v4/chat/completions", headers=headers, json=data, timeout=30)
print(f"Status: {resp.status_code}")
if resp.status_code == 200:
    result = resp.json()
    content = result["choices"][0]["message"]["content"]
    print(f"OK! Content: {content[:200]}")
else:
    print(f"Error: {resp.text[:400]}")
