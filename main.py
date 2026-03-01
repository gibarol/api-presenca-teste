import os
import time
import random
import threading
import uuid
import requests
from flask import Flask, jsonify, request, Response

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
    digits = only_digits(cpf_raw)
    if len(digits) == 0:
        return ""
    if len(digits) < 11:
        digits = digits.zfill(11)
    if len(digits) > 11:
        return ""
    return digits

def random_br_mobile() -> str:
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

def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def safe_json(resp: requests.Response, limit: int = 8000):
    ct = resp.headers.get("Content-Type", "")
    if "application/json" in ct:
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text[:limit]}
    return {"raw": resp.text[:limit]}

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
# JOB STORE (memória) - TESTE
# =========================
JOBS = {}
JOBS_LOCK = threading.Lock()

def set_job(job_id: str, patch: dict):
    with JOBS_LOCK:
        cur = JOBS.get(job_id, {})
        cur.update(patch)
        JOBS[job_id] = cur

def get_job(job_id: str):
    with JOBS_LOCK:
        return JOBS.get(job_id)

# =========================
# CORE CALLS
# =========================
def presenca_gerar_termo(token: str, cpf: str, telefone: str):
    throttle()
    url = f"{PRESENCA_BASE_URL}/consultas/termo-inss"
    payload = {"cpf": cpf, "nome": "TESTE", "telefone": telefone, "produtoId": 28}
    resp = requests.post(url, json=payload, headers=auth_headers(token), timeout=TIMEOUT_SECONDS)
    body = safe_json(resp)
    termo_link = find_first_url(body)
    autorizacao_id = body.get("autorizacaoId") if isinstance(body, dict) else None
    return resp.status_code, termo_link, autorizacao_id, body

def presenca_vinculos(token: str, cpf: str):
    throttle()
    url = f"{PRESENCA_BASE_URL}/v3/operacoes/consignado-privado/consultar-vinculos"
    resp = requests.post(url, json={"cpf": cpf}, headers=auth_headers(token), timeout=TIMEOUT_SECONDS)
    return resp.status_code, safe_json(resp)

def presenca_margem(token: str, cpf: str, matricula: str, cnpj: str):
    throttle()
    url = f"{PRESENCA_BASE_URL}/v3/operacoes/consignado-privado/consultar-margem"
    payload = {"cpf": cpf, "matricula": matricula, "cnpj": cnpj}
    resp = requests.post(url, json=payload, headers=auth_headers(token), timeout=TIMEOUT_SECONDS)
    return resp.status_code, safe_json(resp)

def presenca_simulacao_disponiveis(token: str, margem_resp: dict, telefone: str, cpf: str):
    """
    Baseado na collection:
    POST /v5/operacoes/simulacao/disponiveis
    """
    throttle()
    url = f"{PRESENCA_BASE_URL}/v5/operacoes/simulacao/disponiveis"

    # Tentativas de extrair campos do retorno da margem:
    # (se algum nome vier diferente, a gente vê no log e ajusta)
    nome = (margem_resp.get("nome") or margem_resp.get("tomador", {}).get("nome") or "TESTE") if isinstance(margem_resp, dict) else "TESTE"
    data_nasc = (margem_resp.get("dataNascimento") or margem_resp.get("tomador", {}).get("dataNascimento") or "1982-10-05") if isinstance(margem_resp, dict) else "1982-10-05"
    nome_mae = (margem_resp.get("nomeMae") or margem_resp.get("tomador", {}).get("nomeMae") or "") if isinstance(margem_resp, dict) else ""
    sexo = (margem_resp.get("sexo") or margem_resp.get("tomador", {}).get("sexo") or "M") if isinstance(margem_resp, dict) else "M"

    cnpj_emp = None
    reg_emp = None
    # alguns retornos podem vir em estruturas diferentes
    for key in ["cnpjEmpregador", "cnpj"]:
        if isinstance(margem_resp, dict) and margem_resp.get(key):
            cnpj_emp = only_digits(str(margem_resp.get(key)))
    if isinstance(margem_resp, dict):
        ve = margem_resp.get("vinculoEmpregaticio") or {}
        if isinstance(ve, dict):
            cnpj_emp = cnpj_emp or only_digits(str(ve.get("cnpjEmpregador") or ve.get("cnpj") or ""))
            reg_emp = reg_emp or str(ve.get("registroEmpregaticio") or ve.get("matricula") or "")

    # valor da parcela/margem disponivel (tentativas)
    valor_parcela = None
    for k in ["valorMargemDisponivel", "margemDisponivel", "valorParcela", "parcelaMaxima"]:
        if isinstance(margem_resp, dict) and margem_resp.get(k) is not None:
            valor_parcela = margem_resp.get(k)
            break
    if valor_parcela is None:
        valor_parcela = 0

    ddd = telefone[:2] if telefone and len(telefone) >= 10 else ""
    numero = telefone[2:] if telefone and len(telefone) >= 10 else ""

    payload = {
        "tomador": {
            "telefone": {"ddd": ddd, "numero": numero},
            "cpf": cpf,
            "nome": nome,
            "dataNascimento": data_nasc,
            "nomeMae": nome_mae,
            "email": "email@.com",
            "sexo": sexo,
            "vinculoEmpregaticio": {
                "cnpjEmpregador": cnpj_emp or "",
                "registroEmpregaticio": reg_emp or ""
            },
            "dadosBancarios": {
                "codigoBanco": None,
                "agencia": None,
                "conta": None,
                "digitoConta": None,
                "formaCredito": None
            },
            "endereco": {
                "cep": "",
                "rua": "",
                "numero": "",
                "complemento": "",
                "cidade": "",
                "estado": "",
                "bairro": ""
            }
        },
        "proposta": {
            "valorSolicitado": 0,
            "quantidadeParcelas": 0,
            "produtoId": 28,
            "valorParcela": valor_parcela
        },
        "documentos": []
    }

    resp = requests.post(url, json=payload, headers=auth_headers(token), timeout=TIMEOUT_SECONDS)
    return resp.status_code, safe_json(resp)

# =========================
# WORKER DO TESTE (15s, tenta a cada 3s)
# =========================
def worker_fluxo(job_id: str):
    job = get_job(job_id)
    if not job:
        return

    cpf = job["cpf"]
    telefone = job["telefone"]
    start = time.time()

    print(f"[JOB {job_id}] INICIO worker: cpf={cpf} telefone={telefone}")

    while True:
        elapsed = time.time() - start
        if elapsed > 15:
            print(f"[JOB {job_id}] TIMEOUT 15s sem detectar aceite")
            set_job(job_id, {"state": "timeout"})
            return

        try:
            token = presenca_login_token()

            # a “verificação” do aceite será: tentar vinculos até passar
            st_v, vinc = presenca_vinculos(token, cpf)
            print(f"[JOB {job_id}] Tentativa vinculos http={st_v}")

            if st_v == 200:
                print(f"[JOB {job_id}] VINCULOS OK: {vinc}")
                set_job(job_id, {"state": "vinculos_ok", "vinculos": vinc})

                # tenta extrair matricula/cnpj do primeiro vínculo encontrado
                matricula = ""
                cnpj = ""

                # tentando encontrar lista de vínculos em qualquer formato
                candidatos = []
                if isinstance(vinc, dict):
                    for k in ["vinculos", "data", "items", "resultado"]:
                        if isinstance(vinc.get(k), list):
                            candidatos = vinc.get(k)
                            break
                if not candidatos and isinstance(vinc, list):
                    candidatos = vinc

                if candidatos and isinstance(candidatos[0], dict):
                    v0 = candidatos[0]
                    # nomes possíveis
                    matricula = str(v0.get("registroEmpregaticio") or v0.get("matricula") or v0.get("registro") or "")
                    cnpj = only_digits(str(v0.get("cnpjEmpregador") or v0.get("cnpj") or ""))

                if not matricula or not cnpj:
                    print(f"[JOB {job_id}] Nao encontrei matricula/cnpj no vinculo. Ajustar mapeamento.")
                    set_job(job_id, {"state": "vinculos_sem_matricula_cnpj"})
                    return

                st_m, margem = presenca_margem(token, cpf, matricula, cnpj)
                print(f"[JOB {job_id}] MARGEM http={st_m} resp={margem}")
                set_job(job_id, {"margem": margem})

                st_s, simul = presenca_simulacao_disponiveis(token, margem if isinstance(margem, dict) else {}, telefone, cpf)
                print(f"[JOB {job_id}] SIMULACAO http={st_s} resp={simul}")
                set_job(job_id, {"simulacao": simul, "state": "done"})
                return

        except Exception as e:
            print(f"[JOB {job_id}] ERRO worker: {e}")

        time.sleep(3)

# =========================
# ROUTES
# =========================
@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "api-presenca-teste"})

@app.get("/ip")
def my_ip():
    r = requests.get("https://api.ipify.org?format=json", timeout=TIMEOUT_SECONDS)
    return jsonify(r.json())

@app.get("/presenca/fluxo-status")
def fluxo_status():
    job_id = (request.args.get("job") or "").strip()
    job = get_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job_not_found"}), 404

    # retorna o mínimo pro front exibir
    return jsonify({
        "state": job.get("state"),
        "vinculos": job.get("vinculos"),
        "margem": job.get("margem"),
        "simulacao": job.get("simulacao")
    })

@app.get("/presenca/fluxo-teste")
def fluxo_teste():
    cpf_raw = (request.args.get("cpf") or "").strip()
    cpf = normalize_cpf(cpf_raw)
    if not cpf:
        return Response("CPF inválido", status=400, mimetype="text/plain")

    telefone = random_br_mobile()

    # gera termo e retorna HTML com link IMEDIATO
    try:
        token = presenca_login_token()
        st, termo_link, autorizacao_id, body = presenca_gerar_termo(token, cpf, telefone)

        print(f"[TERMO] cpf={cpf} telefone={telefone} http={st} resp={body}")

        if st != 200 or not termo_link:
            # para você, o ideal era só link, mas se não tem link, não dá.
            # aqui retornamos texto simples.
            return Response("Falhou ao gerar TERMO_LINK", status=400, mimetype="text/plain")

        job_id = str(uuid.uuid4())
        set_job(job_id, {
            "state": "waiting_signature",
            "cpf": cpf,
            "telefone": telefone,
            "termo_link": termo_link,
            "autorizacao_id": autorizacao_id,
            "created_at": time.time()
        })

        # dispara worker
        t = threading.Thread(target=worker_fluxo, args=(job_id,), daemon=True)
        t.start()

        # página na MESMA URL: mostra link e vai consultando /fluxo-status a cada 3s (por 15s)
        html = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Teste Presença - Fluxo</title>
  <style>
    body {{ font-family: Arial, sans-serif; padding: 16px; }}
    .box {{ padding: 12px; border: 1px solid #ddd; border-radius: 8px; margin-bottom: 12px; }}
    pre {{ white-space: pre-wrap; word-wrap: break-word; }}
    a {{ font-size: 18px; }}
  </style>
</head>
<body>
  <div class="box">
    <div><b>1) Clique e aceite o termo:</b></div>
    <div style="margin-top:8px;"><a href="{termo_link}" target="_blank">{termo_link}</a></div>
  </div>

  <div class="box">
    <div><b>2) Status (atualiza a cada 3s por até 15s):</b></div>
    <div id="status">Aguardando assinatura...</div>
  </div>

  <div class="box">
    <div><b>3) Resultado (quando assinar):</b></div>
    <pre id="result">-</pre>
  </div>

<script>
const job = "{job_id}";
let tries = 0;
const maxTries = 5; // 5 tentativas x 3s = 15s

async function poll() {{
  tries++;
  try {{
    const r = await fetch("/presenca/fluxo-status?job=" + job);
    const data = await r.json();
    document.getElementById("status").innerText = "state=" + data.state + " (tentativa " + tries + "/" + maxTries + ")";
    if (data.state === "done") {{
      document.getElementById("result").innerText = JSON.stringify(data, null, 2);
      return;
    }}
    if (data.state === "timeout" || data.state === "vinculos_sem_matricula_cnpj") {{
      document.getElementById("result").innerText = JSON.stringify(data, null, 2);
      return;
    }}
  }} catch(e) {{
    document.getElementById("status").innerText = "erro ao consultar status";
  }}
  if (tries < maxTries) {{
    setTimeout(poll, 3000);
  }} else {{
    document.getElementById("status").innerText = "tempo limite do teste (15s).";
  }}
}}
poll();
</script>
</body>
</html>
"""
        return Response(html, mimetype="text/html")

    except Exception as e:
        print(f"[ERRO fluxo_teste] {e}")
        return Response("Erro interno", status=500, mimetype="text/plain")
