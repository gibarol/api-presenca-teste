import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

# =========================
# ENV / CONFIG
# =========================
PRESENCA_BASE_URL = os.getenv("PRESENCA_BASE_URL", "").rstrip("/")
PRESENCA_LOGIN = os.getenv("PRESENCA_LOGIN", "")
PRESENCA_SENHA = os.getenv("PRESENCA_SENHA", "")
TIMEOUT_SECONDS = int(os.getenv("TIMEOUT_SECONDS", "30"))

PRESENCA_DEVICE_USER_AGENT = os.getenv("PRESENCA_DEVICE_USER_AGENT", "")
PRESENCA_DEVICE_OS = os.getenv("PRESENCA_DEVICE_OS", "")
PRESENCA_DEVICE_MODEL = os.getenv("PRESENCA_DEVICE_MODEL", "")
PRESENCA_DEVICE_NAME = os.getenv("PRESENCA_DEVICE_NAME", "")
PRESENCA_DEVICE_TYPE = os.getenv("PRESENCA_DEVICE_TYPE", "")
PRESENCA_GEO_LAT = os.getenv("PRESENCA_GEO_LAT", "-1.0")
PRESENCA_GEO_LON = os.getenv("PRESENCA_GEO_LON", "-5.0")

# recomendação do banco: 1 requisição a cada 2 segundos
_LAST_CALL_TS = 0.0


# =========================
# HELPERS
# =========================
def throttle() -> None:
    global _LAST_CALL_TS
    now = time.time()
    wait = 2.0 - (now - _LAST_CALL_TS)
    if wait > 0:
        time.sleep(wait)
    _LAST_CALL_TS = time.time()


def normalize_digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def normalize_cpf(value: Any) -> str:
    cpf = normalize_digits(value)
    return cpf if len(cpf) == 11 else ""


def split_phone(phone: str) -> Tuple[str, str]:
    digits = normalize_digits(phone)
    if len(digits) >= 11:
        return digits[:2], digits[2:]
    if len(digits) == 10:
        return digits[:2], digits[2:]
    return "11", "999999999"


def safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return {"raw_text": resp.text[:2000]}


def auth_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }


def find_first_url(obj: Any) -> Optional[str]:
    if isinstance(obj, dict):
        for _, v in obj.items():
            found = find_first_url(v)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_first_url(item)
            if found:
                return found
    elif isinstance(obj, str) and obj.startswith("http"):
        return obj
    return None


def find_first_id(obj: Any) -> Optional[str]:
    id_keys = {"id", "autorizacaoId", "authorizationId", "termoId"}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in id_keys and v:
                return str(v)
        for _, v in obj.items():
            found = find_first_id(v)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_first_id(item)
            if found:
                return found
    return None


def extract_candidates_vinculos(body: Any) -> List[dict]:
    if isinstance(body, list):
        return [x for x in body if isinstance(x, dict)]
    if isinstance(body, dict):
        for key in ["data", "result", "vinculos", "items", "content"]:
            val = body.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
        return [body]
    return []


def normalize_cnpj_like(value: Any) -> str:
    digits = normalize_digits(value)
    if not digits:
        return ""
    if len(digits) >= 14:
        return digits[-14:]
    return digits.zfill(14)


def pick_vinculo(vinculos: List[dict]) -> Optional[dict]:
    if not vinculos:
        return None

    elegiveis = []
    for v in vinculos:
        elegivel = v.get("elegivel")
        if elegivel is True or str(elegivel).lower() in {"true", "sim", "1"}:
            elegiveis.append(v)

    return elegiveis[0] if elegiveis else vinculos[0]


def extract_valor_parcela(margem_resp: dict) -> float:
    for k in ["valorMargemDisponivel", "margemDisponivel", "valorParcela", "parcelaMaxima"]:
        val = margem_resp.get(k)
        if val is not None:
            try:
                return float(str(val).replace(",", "."))
            except Exception:
                pass
    return 0.0


def extract_oferta(simul_resp: Any, fallback_parcela: float) -> Tuple[float, float]:
    if isinstance(simul_resp, list) and simul_resp:
        first = simul_resp[0]
        if isinstance(first, dict):
            valor = first.get("valorLiberado") or first.get("valor") or first.get("valorDisponivel") or 0
            parcela = first.get("valorParcela") or first.get("parcela") or fallback_parcela or 0
            try:
                return float(str(valor).replace(",", ".")), float(str(parcela).replace(",", "."))
            except Exception:
                return 0.0, fallback_parcela

    if isinstance(simul_resp, dict):
        for key in ["data", "result", "items", "content"]:
            val = simul_resp.get(key)
            if isinstance(val, list) and val:
                return extract_oferta(val, fallback_parcela)

        valor = simul_resp.get("valorLiberado") or simul_resp.get("valor") or simul_resp.get("valorDisponivel") or 0
        parcela = simul_resp.get("valorParcela") or simul_resp.get("parcela") or fallback_parcela or 0
        try:
            return float(str(valor).replace(",", ".")), float(str(parcela).replace(",", "."))
        except Exception:
            return 0.0, fallback_parcela

    return 0.0, fallback_parcela


# =========================
# PRESENÇA CORE
# =========================
def presenca_login_token() -> str:
    if not PRESENCA_BASE_URL:
        raise RuntimeError("PRESENCA_BASE_URL_nao_configurada")
    if not PRESENCA_LOGIN or not PRESENCA_SENHA:
        raise RuntimeError("PRESENCA_LOGIN_ou_SENHA_nao_configurada")

    throttle()
    url = f"{PRESENCA_BASE_URL}/login"
    payload = {"login": PRESENCA_LOGIN, "senha": PRESENCA_SENHA}
    print(f"[PRESENCA] LOGIN -> {url}")

    resp = requests.post(url, json=payload, timeout=TIMEOUT_SECONDS)
    print(f"[PRESENCA] LOGIN STATUS -> {resp.status_code}")

    if not resp.ok:
        raise RuntimeError(f"login_falhou_http_{resp.status_code}: {resp.text[:300]}")

    data = safe_json(resp)
    token = data.get("token")
    if not token:
        raise RuntimeError(f"token_ausente_no_login: {data}")
    return token


def presenca_gerar_termo(token: str, cpf: str, nome: str, telefone: str):
    throttle()
    url = f"{PRESENCA_BASE_URL}/consultas/termo-inss"
    payload = {
        "cpf": cpf,
        "nome": nome,
        "telefone": normalize_digits(telefone),
        "produtoId": 28
    }
    print(f"[PRESENCA] TERMO -> {url}")
    print(f"[PRESENCA] TERMO PAYLOAD -> {payload}")

    resp = requests.post(url, json=payload, headers=auth_headers(token), timeout=TIMEOUT_SECONDS)
    body = safe_json(resp)

    termo_link = find_first_url(body)
    autorizacao_id = None
    if isinstance(body, dict):
        autorizacao_id = body.get("autorizacaoId") or body.get("id")
    if not autorizacao_id:
        autorizacao_id = find_first_id(body)

    print(f"[PRESENCA] TERMO STATUS -> {resp.status_code}")
    print(f"[PRESENCA] TERMO ID -> {autorizacao_id}")
    print(f"[PRESENCA] TERMO LINK -> {termo_link}")
    print(f"[PRESENCA] TERMO BODY -> {body}")

    return resp.status_code, termo_link, autorizacao_id, body


def presenca_assinar_termo(token: str, autorizacao_id: str):
    throttle()
    url = f"{PRESENCA_BASE_URL}/consultas/termo-inss/{autorizacao_id}"
    headers = auth_headers(token)
    headers["tenant-id"] = "superuser"

    payload = {
        "userAgent": PRESENCA_DEVICE_USER_AGENT,
        "OperationalSystem": PRESENCA_DEVICE_OS,
        "DeviceModel": PRESENCA_DEVICE_MODEL,
        "DeviceName": PRESENCA_DEVICE_NAME,
        "DeviceType": PRESENCA_DEVICE_TYPE,
        "GeoLocation": {
            "Latitude": PRESENCA_GEO_LAT,
            "Longitude": PRESENCA_GEO_LON
        }
    }

    print(f"[PRESENCA] ASSINAR TERMO -> {url}")
    print(f"[PRESENCA] ASSINAR TERMO PAYLOAD -> {payload}")

    resp = requests.put(url, json=payload, headers=headers, timeout=TIMEOUT_SECONDS)
    body = safe_json(resp)

    print(f"[PRESENCA] ASSINAR TERMO STATUS -> {resp.status_code}")
    print(f"[PRESENCA] ASSINAR TERMO BODY -> {body}")

    return resp.status_code, body


def presenca_vinculos(token: str, cpf: str):
    throttle()
    url = f"{PRESENCA_BASE_URL}/v3/operacoes/consignado-privado/consultar-vinculos"
    payload = {"cpf": cpf}
    print(f"[PRESENCA] VINCULOS -> {url}")
    print(f"[PRESENCA] VINCULOS PAYLOAD -> {payload}")

    resp = requests.post(url, json=payload, headers=auth_headers(token), timeout=TIMEOUT_SECONDS)
    body = safe_json(resp)

    print(f"[PRESENCA] VINCULOS STATUS -> {resp.status_code}")
    print(f"[PRESENCA] VINCULOS BODY -> {body}")

    return resp.status_code, body


def presenca_margem(token: str, cpf: str, matricula: str, cnpj: str):
    throttle()
    url = f"{PRESENCA_BASE_URL}/v3/operacoes/consignado-privado/consultar-margem"
    payload = {"cpf": cpf, "matricula": matricula, "cnpj": cnpj}
    print(f"[PRESENCA] MARGEM -> {url}")
    print(f"[PRESENCA] MARGEM PAYLOAD -> {payload}")

    resp = requests.post(url, json=payload, headers=auth_headers(token), timeout=TIMEOUT_SECONDS)
    body = safe_json(resp)

    print(f"[PRESENCA] MARGEM STATUS -> {resp.status_code}")
    print(f"[PRESENCA] MARGEM BODY -> {body}")

    return resp.status_code, body


def presenca_simulacao_disponiveis(token: str, margem_resp: dict, telefone: str, cpf: str, cnpj: str, matricula: str):
    throttle()
    url = f"{PRESENCA_BASE_URL}/v5/operacoes/simulacao/disponiveis"

    nome = margem_resp.get("nome") or margem_resp.get("tomador", {}).get("nome") or "CLIENTE"
    data_nasc = margem_resp.get("dataNascimento") or margem_resp.get("tomador", {}).get("dataNascimento") or "1982-10-05"
    nome_mae = margem_resp.get("nomeMae") or margem_resp.get("tomador", {}).get("nomeMae") or "NAO INFORMADO"
    sexo = margem_resp.get("sexo") or margem_resp.get("tomador", {}).get("sexo") or "M"

    valor_parcela = extract_valor_parcela(margem_resp)
    ddd, numero = split_phone(telefone)

    payload = {
        "tomador": {
            "telefone": {
                "ddd": ddd,
                "numero": numero
            },
            "cpf": cpf,
            "nome": nome,
            "dataNascimento": data_nasc,
            "nomeMae": nome_mae,
            "email": "sememail@teste.com",
            "sexo": sexo,
            "vinculoEmpregaticio": {
                "cnpjEmpregador": cnpj,
                "registroEmpregaticio": matricula
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

    print(f"[PRESENCA] SIMULACAO -> {url}")
    print(f"[PRESENCA] SIMULACAO PAYLOAD -> {payload}")

    resp = requests.post(url, json=payload, headers=auth_headers(token), timeout=TIMEOUT_SECONDS)
    body = safe_json(resp)

    print(f"[PRESENCA] SIMULACAO STATUS -> {resp.status_code}")
    print(f"[PRESENCA] SIMULACAO BODY -> {body}")

    return resp.status_code, body, payload


def rodar_fluxo_presenca(cpf: str, nome: str, telefone: str, autorizacao_id: Optional[str] = None) -> Dict[str, Any]:
    print("=== INICIO FLUXO PRESENCA ===")
    print("cpf:", cpf)
    print("nome:", nome)
    print("telefone:", telefone)
    print("autorizacao_id recebida:", autorizacao_id)

    token = presenca_login_token()
    print("[PRESENCA] login ok")

    if autorizacao_id:
        st_put, put_body = presenca_assinar_termo(token, autorizacao_id)
        if st_put not in (200, 201, 204):
            print("[PRESENCA] falha ao assinar termo")
            return {
                "status": "erro",
                "mensagem": "Falha ao assinar termo",
                "autorizacao_id": autorizacao_id,
                "detalhe": put_body
            }

    st_termo, termo_link, termo_id, termo_body = presenca_gerar_termo(token, cpf, nome, telefone)

    st_v, vinc_body = presenca_vinculos(token, cpf)
    vinculos = extract_candidates_vinculos(vinc_body)

    if st_v != 200 or not vinculos:
        print("[PRESENCA] fluxo parou em aguardando_autorizacao")
        print("=== FIM FLUXO PRESENCA ===")
        return {
            "status": "aguardando_autorizacao",
            "mensagem": "Cliente precisa concluir autorização",
            "autorizacao_id": autorizacao_id or termo_id,
            "link_autorizacao": termo_link,
            "termo_http": st_termo,
            "vinculos_http": st_v,
            "detalhe_termo": termo_body,
            "detalhe_vinculos": vinc_body
        }

    vinculo = pick_vinculo(vinculos)
    print("[PRESENCA] vinculo escolhido:", vinculo)

    if not vinculo:
        print("[PRESENCA] nenhum vinculo encontrado")
        print("=== FIM FLUXO PRESENCA ===")
        return {
            "status": "sem_oferta",
            "elegibilidade": "nao",
            "valor_disponivel": 0,
            "parcela": 0,
            "mensagem": "Nenhum vínculo encontrado"
        }

    elegivel = vinculo.get("elegivel")
    elegivel_bool = elegivel is True or str(elegivel).lower() in {"true", "sim", "1"}

    matricula = str(
        vinculo.get("matricula")
        or vinculo.get("registroEmpregaticio")
        or vinculo.get("registro")
        or vinculo.get("matriculaRegistro")
        or ""
    )
    cnpj = normalize_cnpj_like(
        vinculo.get("numeroInscricaoEmpregador")
        or vinculo.get("cnpjEmpregador")
        or vinculo.get("cnpj")
        or ""
    )

    print("[PRESENCA] matricula:", matricula)
    print("[PRESENCA] cnpj:", cnpj)

    if not matricula or not cnpj:
        print("[PRESENCA] falha ao extrair matricula/cnpj")
        print("=== FIM FLUXO PRESENCA ===")
        return {
            "status": "erro",
            "mensagem": "Não foi possível extrair matrícula/cnpj do vínculo",
            "detalhe_vinculo": vinculo
        }

    st_m, margem_body = presenca_margem(token, cpf, matricula, cnpj)
    if st_m != 200:
        print("[PRESENCA] falha ao consultar margem")
        print("=== FIM FLUXO PRESENCA ===")
        return {
            "status": "erro",
            "mensagem": "Falha ao consultar margem",
            "detalhe": margem_body
        }

    valor_parcela = extract_valor_parcela(margem_body)
    print("[PRESENCA] valor_parcela:", valor_parcela)

    st_s, simul_body, payload_sim = presenca_simulacao_disponiveis(
        token=token,
        margem_resp=margem_body if isinstance(margem_body, dict) else {},
        telefone=telefone,
        cpf=cpf,
        cnpj=cnpj,
        matricula=matricula
    )

    if st_s != 200:
        print("[PRESENCA] falha na simulacao")
        print("=== FIM FLUXO PRESENCA ===")
        return {
            "status": "erro",
            "mensagem": "Falha na simulação",
            "elegibilidade": "sim" if elegivel_bool else "nao",
            "valor_disponivel": 0,
            "parcela": valor_parcela,
            "detalhe": simul_body,
            "payload_simulacao": payload_sim
        }

    valor_disponivel, parcela = extract_oferta(simul_body, valor_parcela)
    print("[PRESENCA] valor_disponivel:", valor_disponivel)
    print("[PRESENCA] parcela:", parcela)
    print("=== FIM FLUXO PRESENCA ===")

    return {
        "status": "sucesso" if elegivel_bool else "sem_oferta",
        "elegibilidade": "sim" if elegivel_bool else "nao",
        "valor_disponivel": valor_disponivel,
        "parcela": parcela,
        "autorizacao_id": autorizacao_id or termo_id,
        "link_autorizacao": termo_link
    }


# =========================
# ROUTES
# =========================
@app.route("/")
def home():
    return jsonify({
        "status": "ok",
        "mensagem": "API Kommo Presença online"
    })


@app.route("/consulta", methods=["GET", "POST"])
def consulta():
    try:
        cpf = ""
        nome = ""
        telefone = ""
        lead_id = None
        autorizacao_id = None

        if request.method == "GET":
            cpf = normalize_cpf(request.args.get("cpf"))
            nome = (request.args.get("nome") or "CLIENTE").strip()
            telefone = request.args.get("telefone") or "11999999999"
            autorizacao_id = (request.args.get("autorizacao_id") or "").strip() or None

        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            cpf = normalize_cpf(data.get("cpf"))
            nome = (data.get("nome") or "CLIENTE").strip()
            telefone = data.get("telefone") or "11999999999"
            lead_id = data.get("lead_id")
            autorizacao_id = (data.get("autorizacao_id") or "").strip() or None

        if not cpf:
            return jsonify({
                "status": "erro",
                "mensagem": "CPF não informado ou inválido"
            }), 400

        resultado = rodar_fluxo_presenca(
            cpf=cpf,
            nome=nome,
            telefone=telefone,
            autorizacao_id=autorizacao_id
        )

        return jsonify({
            "lead_id": lead_id,
            **resultado
        })

    except Exception as e:
        print("[ERRO GERAL]", str(e))
        return jsonify({
            "status": "erro",
            "mensagem": str(e)
        }), 500


if __name__ == "__main__":
    porta = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=porta)
