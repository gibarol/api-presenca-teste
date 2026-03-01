import os
import requests
from flask import Flask, jsonify

app = Flask(__name__)

PRESENCA_BASE_URL = os.getenv("PRESENCA_BASE_URL", "").rstrip("/")
PRESENCA_LOGIN = os.getenv("PRESENCA_LOGIN", "")
PRESENCA_SENHA = os.getenv("PRESENCA_SENHA", "")
TIMEOUT_SECONDS = 30

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "api-presenca-teste"})

@app.route("/ip")
def ip():
    r = requests.get("https://api.ipify.org?format=json", timeout=TIMEOUT_SECONDS)
    return jsonify(r.json())

@app.route("/presenca/test-login")
def presenca_test_login():
    if not PRESENCA_BASE_URL:
        return jsonify({"ok": False, "error": "BASE_URL_nao_configurada"}), 500
    if not PRESENCA_LOGIN or not PRESENCA_SENHA:
        return jsonify({"ok": False, "error": "LOGIN_ou_SENHA_nao_configurada"}), 500

    url = f"{PRESENCA_BASE_URL}/login"
    payload = {
        "login": PRESENCA_LOGIN,
        "senha": PRESENCA_SENHA
    }

    try:
        resp = requests.post(url, json=payload, timeout=TIMEOUT_SECONDS)
        data = resp.json()

        token = data.get("token")
        token_mask = token[:6] + "..." + token[-6:] if token else None

        return jsonify({
            "ok": resp.ok,
            "http_status": resp.status_code,
            "token_mask": token_mask,
            "raw_response": data
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
