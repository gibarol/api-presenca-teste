import os
import time
import random
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
# RATE LIMIT (2s)
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
# HELPERS
# =========================
def only_digits(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())

def normalize_cpf(cpf_raw: str) -> str:
    """
    - Remove tudo que não é número
    - Se tiver menos de 11 dígitos, completa com zeros à esquerda
    - Se tiver mais de 11 dígitos, considera inválido (provável dado errado)
    """
    digits = only_digits(cpf_raw)
    if len(digits) == 0:
        return ""
    if len(digits) < 11:
        digits = digits.zfill(11)
    if len(digits) > 11:
        return ""  # inválido
    return digits

def random_br_mobile() -> str:
    """
    Gera um celular BR aleatório no padrão DDD + 9 + 8 dígitos
    Ex: 11987654321
    """
    ddd = random.choice([
        "11","12","13","14","15","16","17","18","19",
        "21","22","24","27","28",
        "31","32","33","34","35","37","38",
        "41","42","43","44","45","46",
        "47","48","49",
        "51","53","54","55",
        "61","62","63","64","65","66","67","68","69",
        "71","73","74","75","77","79",
        "81","82","83","84","85","86","87","88","89",
        "91","92","93","94","95","96","97","98","99"
    ])
    numero = "9" + str(random.randint(10000000, 99999999))
    return ddd + numero

def token_mask(token: str) -> str:
    if not token or len(token) < 20:
        return "***"
    return token[:6] + "..." + token[-6:]

def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def safe_json(resp: requests.Response, limit: int = 4000):
    ct = resp.headers.get("Content-Type", "")
    if "application/json" in ct:
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text[:limit]}
    return {"raw": resp.text[:limit]}

def find_first_url(obj):
    """Procura a primeira URL dentro de dict/list/str."""
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
# PRESENÇA LOGIN (TOKEN)
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

# =========================
# ROUTES BÁSICAS
# =========================
@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "api-presenca-teste"})

@app.get("/ip")
def my_ip():
    r = requests.get("https://api.ipify.org?format=json", timeout=TIMEOUT_SECONDS)
    return jsonify(r.json())

# =========================
# 1) TESTE LOGIN (SEGURO)
# =========================
@app.get("/presenca/test-login")
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
# 2) GERAR TERMO (GET)
# - telefone é gerado automaticamente
# - cpf é normalizado (remove não-números e zfill p/ 11)
# =========================
@app.get("/presenca/termo")
def presenca_gerar_termo():
    cpf_raw = (request.args.get("cpf") or "").strip()
    cpf = normalize_cpf(cpf_raw)

    if not cpf:
        return jsonify({"ok": False, "error": "cpf_invalido", "cpf_recebido": cpf_raw}), 400

    telefone = random_br_mobile()

    try:
        token = presenca_login_token()
        throttle()

        url = f"{PRESENCA_BASE_URL}/consultas/termo-inss"
        payload = {
            "cpf": cpf,
            "nome": "TESTE",
            "telefone": telefone,
            "produtoId": 28
        }

        resp = requests.post(url, json=payload, headers=auth_headers(token), timeout=TIMEOUT_SECONDS)
        body = safe_json(resp)
        termo_link = find_first_url(body)

        return jsonify({
            "ok": resp.ok,
            "http_status": resp.status_code,
            "cpf_normalizado": cpf,
            "telefone_gerado": telefone,
            "TERMO_LINK": termo_link,  # <-- DESTACADO
            "presenca_response": body,
            "hint": "O fluxo pausa até o cliente abrir o TERMO_LINK e aceitar."
        }), (200 if resp.ok else 400)

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# =========================
# 3) CONSULTAR VÍNCULOS (POST)
# Body: { "cpf": "..." }
# =========================
@app.post("/presenca/vinculos")
def presenca_vinculos():
    data = request.get_json(silent=True) or {}
    cpf_raw = (data.get("cpf") or "").strip()
    cpf = normalize_cpf(cpf_raw)

    if not cpf:
        return jsonify({"ok": False, "error": "cpf_invalido", "cpf_recebido": cpf_raw}), 400

    try:
        token = presenca_login_token()
        throttle()

        url = f"{PRESENCA_BASE_URL}/v3/operacoes/consignado-privado/consultar-vinculos"
        resp = requests.post(url, json={"cpf": cpf}, headers=auth_headers(token), timeout=TIMEOUT_SECONDS)

        return jsonify({
            "ok": resp.ok,
            "http_status": resp.status_code,
            "cpf_normalizado": cpf,
            "presenca_response": safe_json(resp)
        }), (200 if resp.ok else 400)

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# =========================
# 4) CONSULTAR MARGEM (POST)
# Body: { "cpf": "...", "matricula": "...", "cnpj": "..." }
# =========================
@app.post("/presenca/margem")
def presenca_margem():
    data = request.get_json(silent=True) or {}
    cpf_raw = (data.get("cpf") or "").strip()
    cpf = normalize_cpf(cpf_raw)
    matricula = (data.get("matricula") or "").strip()
    cnpj = only_digits(data.get("cnpj") or "")

    if not cpf:
        return jsonify({"ok": False, "error": "cpf_invalido", "cpf_recebido": cpf_raw}), 400
    if not matricula or not cnpj:
        return jsonify({"ok": False, "error": "matricula_e_cnpj_obrigatorios"}), 400

    try:
        token = presenca_login_token()
        throttle()

        url = f"{PRESENCA_BASE_URL}/v3/operacoes/consignado-privado/consultar-margem"
        payload = {"cpf": cpf, "matricula": matricula, "cnpj": cnpj}

        resp = requests.post(url, json=payload, headers=auth_headers(token), timeout=TIMEOUT_SECONDS)

        return jsonify({
            "ok": resp.ok,
            "http_status": resp.status_code,
            "cpf_normalizado": cpf,
            "presenca_response": safe_json(resp)
        }), (200 if resp.ok else 400)

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# =========================
# 5) TABELAS / SIMULAÇÃO (POST)
# Body: (payload que vamos montar depois)
# =========================
@app.post("/presenca/tabelas")
def presenca_tabelas():
    data = request.get_json(silent=True) or {}
    try:
        token = presenca_login_token()
        throttle()

        # Começamos pelo endpoint citado na doc (v3). Se precisar, trocamos pro v5 depois.
        url = f"{PRESENCA_BASE_URL}/v3/tabelas/simulacao/inss/disponiveis"
        resp = requests.post(url, json=data, headers=auth_headers(token), timeout=TIMEOUT_SECONDS)

        return jsonify({
            "ok": resp.ok,
            "http_status": resp.status_code,
            "presenca_response": safe_json(resp)
        }), (200 if resp.ok else 400)

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# =========================
# 6) CRIAR OPERAÇÃO (POST)
# Body: (payload de proposta)
# =========================
@app.post("/presenca/criar-operacao")
def presenca_criar_operacao():
    data = request.get_json(silent=True) or {}
    try:
        token = presenca_login_token()
        throttle()

        url = f"{PRESENCA_BASE_URL}/v3/operacoes"
        resp = requests.post(url, json=data, headers=auth_headers(token), timeout=TIMEOUT_SECONDS)

        return jsonify({
            "ok": resp.ok,
            "http_status": resp.status_code,
            "presenca_response": safe_json(resp, limit=6000)
        }), (200 if resp.ok else 400)

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# =========================
# 7) LINK FORMALIZAÇÃO / DETALHE
# =========================
@app.get("/presenca/operacoes/<int:op_id>/link-formalizacao")
def presenca_link_formalizacao(op_id: int):
    try:
        token = presenca_login_token()
        throttle()

        url = f"{PRESENCA_BASE_URL}/operacoes/{op_id}/link-formalizacao"
        resp = requests.get(url, headers=auth_headers(token), timeout=TIMEOUT_SECONDS)

        return jsonify({
            "ok": resp.ok,
            "http_status": resp.status_code,
            "presenca_response": safe_json(resp)
        }), (200 if resp.ok else 400)

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/presenca/operacoes/<int:op_id>/detalhe")
def presenca_detalhe(op_id: int):
    try:
        token = presenca_login_token()
        throttle()

        url = f"{PRESENCA_BASE_URL}/operacoes/{op_id}/detalhe"
        resp = requests.get(url, headers=auth_headers(token), timeout=TIMEOUT_SECONDS)

        return jsonify({
            "ok": resp.ok,
            "http_status": resp.status_code,
            "presenca_response": safe_json(resp, limit=6000)
        }), (200 if resp.ok else 400)

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
