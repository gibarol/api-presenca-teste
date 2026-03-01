import os
import time
import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

# =========================
# CONFIG (Render ENV)
# =========================
PRESENCA_BASE_URL = os.getenv("PRESENCA_BASE_URL", "").rstrip("/")
PRESENCA_LOGIN = os.getenv("PRESENCA_LOGIN", "")
PRESENCA_SENHA = os.getenv("PRESENCA_SENHA", "")
TIMEOUT_SECONDS = int(os.getenv("TIMEOUT_SECONDS", "30"))

# =========================
# CONTROLE DE RATE LIMIT (2s)
# =========================
_last_call_ts = 0.0

def throttle(min_seconds: float = 2.0):
    global _last_call_ts
    now = time.time()
    delta = now - _last_call_ts
    if delta < min_seconds:
        time.sleep(min_seconds - delta)
    _last_call_ts = time.time()

# =========================
# LOGIN
# =========================
def presenca_login_token() -> str:
    if not PRESENCA_BASE_URL:
        raise RuntimeError("PRESENCA_BASE_URL_nao_configurada")
    if not PRESENCA_LOGIN or not PRESENCA_SENHA:
        raise RuntimeError("PRESENCA_LOGIN_ou_SENHA_nao_configurada")

    throttle()

    url = f"{PRESENCA_BASE_URL}/login"
    payload = {"login": PRESENCA_LOGIN, "senha": PRESENCA_SENHA}

    resp = requests.post(url, json=payload, timeout=TIMEOUT_SECONDS)

    if not resp.ok:
        raise RuntimeError(f"login_falhou_http_{resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    token = data.get("token")

    if not token:
        raise RuntimeError("token_ausente_no_login")

    return token

def auth_headers(token: str):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

def token_mask(token: str):
    if not token or len(token) < 20:
        return "***"
    return token[:6] + "..." + token[-6:]

def find_first_url(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            found = find_first_url(v)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_first_url(item)
            if found:
                return found
    elif isinstance(obj, str):
        if obj.startswith("http://") or obj.startswith("https://"):
            return obj
    return None

# =========================
# HEALTH
# =========================
@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "api-presenca-teste"})

# =========================
# IP
# =========================
@app.route("/ip")
def ip():
    r = requests.get("https://api.ipify.org?format=json", timeout=TIMEOUT_SECONDS)
    return jsonify(r.json())

# =========================
# TEST LOGIN (SEGURO)
# =========================
@app.route("/presenca/test-login")
def presenca_test_login():
    try:
        token = presenca_login_token()
        return jsonify({
            "ok": True,
            "token_mask": token_mask(token),
            "message": "Login realizado com sucesso"
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# =========================
# GERAR TERMO (GET COM CPF)
# =========================
@app.route("/presenca/termo")
def presenca_gerar_termo():
    cpf = (request.args.get("cpf") or "").strip()

    if not cpf.isdigit() or len(cpf) != 11:
        return jsonify({"ok": False, "error": "cpf_invalido"}), 400

    try:
        token = presenca_login_token()
        throttle()

        url = f"{PRESENCA_BASE_URL}/consultas/termo-inss"

        payload = {
            "cpf": cpf,
            "nome": "TESTE",
            "telefone": "",
            "produtoId": 28
        }

        resp = requests.post(url, json=payload, headers=auth_headers(token), timeout=TIMEOUT_SECONDS)

        content_type = resp.headers.get("Content-Type", "")
        body = resp.json() if "application/json" in content_type else {"raw": resp.text}

        termo_link = find_first_url(body)

        return jsonify({
            "ok": resp.ok,
            "http_status": resp.status_code,
            "TERMO_LINK": termo_link,  # <-- DESTACADO
            "hint": "O fluxo pausa até o cliente abrir o TERMO_LINK e aceitar."
        }), (200 if resp.ok else 400)

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# =========================
# CONSULTAR VÍNCULOS
# =========================
@app.route("/presenca/vinculos", methods=["POST"])
def presenca_vinculos():
    data = request.get_json(silent=True) or {}
    cpf = (data.get("cpf") or "").strip()

    if not cpf.isdigit() or len(cpf) != 11:
        return jsonify({"ok": False, "error": "cpf_invalido"}), 400

    try:
        token = presenca_login_token()
        throttle()

        url = f"{PRESENCA_BASE_URL}/v3/operacoes/consignado-privado/consultar-vinculos"

        resp = requests.post(url, json={"cpf": cpf}, headers=auth_headers(token), timeout=TIMEOUT_SECONDS)

        return jsonify({
            "ok": resp.ok,
            "http_status": resp.status_code,
            "response": resp.json() if "application/json" in resp.headers.get("Content-Type", "") else resp.text[:2000]
        }), (200 if resp.ok else 400)

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
