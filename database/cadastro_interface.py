import html
import ipaddress
import logging
import os
import re
import secrets
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from threading import RLock
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from flask import Flask, Response, g, jsonify, request, send_from_directory, session
from flask_cors import CORS
from supabase import Client, create_client
from werkzeug.security import check_password_hash, generate_password_hash
if __package__:
    from .spa_security import AdaptiveIPBlocker, RateLimiter, SecurityTools
else:
    from spa_security import AdaptiveIPBlocker, RateLimiter, SecurityTools

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR.parent / "database.env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
else:
    load_dotenv()

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

IS_PRODUCTION = os.getenv("APP_ENV", os.getenv("FLASK_ENV", "development")).strip().lower() == "production"


def valor_env(*nomes):
    """Retorna a primeira variável preenchida, ignorando placeholders não expandidos."""
    for nome in nomes:
        valor = str(os.getenv(nome) or "").strip()
        if valor and not re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", valor):
            return valor
    return None


SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = valor_env(
    "SUPABASE_SERVICE_ROLE_KEY",
    "service_role",  # compatibilidade com o ambiente já usado no projeto
)
SUPABASE_KEY = SUPABASE_SERVICE_KEY or valor_env("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL e SUPABASE_KEY são obrigatórias.")
if IS_PRODUCTION and not SUPABASE_SERVICE_KEY:
    raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY é obrigatória em produção para as rotas protegidas.")

SPA_TIMEZONE = ZoneInfo(os.getenv("SPA_TIMEZONE", "America/Sao_Paulo"))
OPENING_HOUR = 9
LAST_APPOINTMENT_HOUR = 19
OTP_TTL_MINUTES = 15
OTP_LENGTH = 6
CHAT_MAX_LENGTH = 1_000
VERSAO_BACKEND = "supabase-env-fix-20260913"
PASSWORD_PATTERN = re.compile(r"^(?=.*[a-z])(?=.*[A-Z])(?=.*[@$!%*?&#,.]).{8,}$")
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
RECOMPENSAS_FIDELIDADE = {
    "pausa": {"codigo": "pausa", "nome": "Pausa Panaceia", "pontos": 100, "desconto": 10.00, "descricao": "R$ 10 de crédito para usar em uma experiência."},
    "ritual": {"codigo": "ritual", "nome": "Ritual Panaceia", "pontos": 250, "desconto": 30.00, "descricao": "R$ 30 de crédito para reservar seu próximo ritual."},
    "renovar": {"codigo": "renovar", "nome": "Renovar Panaceia", "pontos": 500, "desconto": 70.00, "descricao": "R$ 70 de crédito para uma nova pausa de cuidado."},
}

app = Flask(__name__)
secret_key = os.getenv("FLASK_SECRET_KEY")
if not secret_key:
    if IS_PRODUCTION:
        raise RuntimeError("FLASK_SECRET_KEY é obrigatória em produção.")
    secret_key = secrets.token_hex(32)
    logger.warning("FLASK_SECRET_KEY não configurada; as sessões serão encerradas ao reiniciar o servidor.")

app.config.update(
    SECRET_KEY=secret_key,
    SESSION_COOKIE_NAME="spa_panaceia_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE=os.getenv("SESSION_COOKIE_SAMESITE", "Lax"),
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "true" if IS_PRODUCTION else "false").lower() == "true",
    SESSION_COOKIE_PATH="/",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=int(os.getenv("SESSION_TTL_HOURS", "12"))),
    MAX_CONTENT_LENGTH=32 * 1024,
    JSON_SORT_KEYS=False,
)
def normalizar_origem(valor):
    """Normaliza uma origem completa, sem aceitar caminho, credenciais ou curingas."""
    try:
        partes = urlsplit(str(valor or "").strip())
        if (partes.scheme not in {"http", "https"} or not partes.hostname
                or partes.username or partes.password or partes.path not in {"", "/"}
                or partes.query or partes.fragment):
            return None
        host = partes.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        porta = partes.port
        if porta and not ((partes.scheme == "http" and porta == 80) or (partes.scheme == "https" and porta == 443)):
            host = f"{host}:{porta}"
        return f"{partes.scheme}://{host}"
    except (TypeError, ValueError):
        return None


# O domínio público fica explícito porque o Host visto pelo Flask pode ser o
# endereço interno do proxy da hospedagem. CORS_ORIGINS apenas acrescenta
# instalações próprias; nunca use "*" junto de cookies de sessão.
origens_padrao = {
    "https://spapanaceia.com.br",
    "https://www.spapanaceia.com.br",
}
if not IS_PRODUCTION:
    origens_padrao.update({
        "http://127.0.0.1:5000",
        "http://localhost:5000",
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "http://192.168.18.220:5000",
    })
origens_configuradas = {
    origem
    for valor in os.getenv("CORS_ORIGINS", "").split(",")
    if (origem := normalizar_origem(valor))
}
cors_origins = sorted(origens_padrao | origens_configuradas)


def origem_permitida():
    recebida = request.headers.get("Origin")
    if not recebida:
        return True
    origem = normalizar_origem(recebida)
    if not origem:
        return False
    # Este cabeçalho é definido pelo navegador. Resolve o mesmo site público
    # encaminhado para um Host interno pelo proxy, sem confiar em X-Forwarded-*.
    if request.headers.get("Sec-Fetch-Site") == "same-origin":
        return True
    return origem in cors_origins or origem == normalizar_origem(request.host_url)


CORS(
    app,
    resources={r"/api/*": {"origins": cors_origins}, r"/cadastrar": {"origins": cors_origins}},
    supports_credentials=True,
)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY and OpenAI else None

security = SecurityTools(secret_key)
rate_limiter = RateLimiter()
spam_blocker = AdaptiveIPBlocker(limit=30, window_seconds=1, base_block_seconds=30)
# Recuperação de senha serializada entre threads de UM processo, sem migrações.
mutation_lock = RLock()
PUBLIC_AUTH_PATHS = {"/cadastrar", "/api/login", "/api/validar-codigo", "/api/reenviar-codigo", "/api/esqueci-senha", "/api/redefinir-senha"}


def obter_ip_cliente():
    """Retorna o IP do par ou um cabeçalho de proxy confiado explicitamente."""
    candidato = request.remote_addr or "desconhecido"
    cabecalho_proxy = os.getenv("TRUSTED_PROXY_IP_HEADER", "").strip()
    if cabecalho_proxy:
        encaminhado = request.headers.get(cabecalho_proxy, "").split(",", 1)[0].strip()
        try:
            ipaddress.ip_address(encaminhado)
        except ValueError:
            pass
        else:
            candidato = encaminhado
    return candidato


@app.before_request
def proteger_requisicao():
    # Primeira barreira do Flask: conta toda chamada, inclusive OPTIONS,
    # páginas, arquivos estáticos, API e endereços inexistentes.
    ip_cliente = obter_ip_cliente()
    allowed, retry, strike = spam_blocker.check(ip_cliente)
    if not allowed:
        logger.warning(
            "IP bloqueado por rajada: ip=%s metodo=%s rota=%s reincidencia=%s espera=%ss",
            ip_cliente,
            request.method,
            request.path,
            strike,
            retry,
        )
        return jsonify({
            "error": "Muitas requisições. Aguarde antes de tentar novamente.",
            "code": "ip_temporarily_blocked",
            "retry_after": retry,
        }), 429, {"Retry-After": str(retry)}
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    if not origem_permitida():
        logger.warning("Origem bloqueada em %s: %r", request.path, request.headers.get("Origin", "")[:200])
        return jsonify({"error": "Origem da requisição não permitida.", "code": "origin_blocked", "versao": VERSAO_BACKEND}), 403
    if request.method != "DELETE" and request.content_length and not request.is_json:
        return jsonify({"error": "Envie os dados no formato JSON."}), 415
    if session.get("usuario_email") and request.path not in PUBLIC_AUTH_PATHS:
        expected = session.get("csrf_token", "")
        provided = request.headers.get("X-CSRF-Token", "")
        if not expected or not secrets.compare_digest(expected, provided):
            return jsonify({"error": "Sua sessão precisa ser atualizada. Recarregue a página.", "code": "csrf_invalid"}), 403
    limits = {
        "/api/login": (20, 900, 10),
        "/api/esqueci-senha": (10, 900, 3),
        "/api/redefinir-senha": (20, 900, 5),
        "/api/validar-codigo": (20, 900, 5),
        "/api/reenviar-codigo": (10, 900, 3),
        "/cadastrar": (10, 3600, 3),
        "/api/chat": (30, 60, None),
    }
    if request.path in limits:
        ip_limit, window, account_limit = limits[request.path]
        # Não confiar em X-Forwarded-For enviado diretamente pelo cliente.
        keys = [(f"{request.path}:ip:{ip_cliente}", ip_limit)]
        email = normalizar_email(json_body().get("email"))
        if account_limit and email:
            keys.append((f"{request.path}:account:{security.fingerprint(email)}", account_limit))
        for key, limit in keys:
            allowed, retry = rate_limiter.allow(key, limit, window)
            if not allowed:
                return jsonify({"error": "Muitas tentativas. Aguarde antes de tentar novamente."}), 429, {"Retry-After": str(retry)}


@app.after_request
def proteger_resposta(response):
    response.headers["X-Spa-Version"] = VERSAO_BACKEND
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'self'; "
        "form-action 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
        "font-src 'self' https://fonts.gstatic.com data:; img-src 'self' data: blob: https:; "
        "connect-src 'self'"
    )
    if IS_PRODUCTION:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif request.path in {"/", "/cadastro"}:
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


def csrf_token():
    if not session.get("csrf_token"):
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def serializar_mutacao(funcao):
    @wraps(funcao)
    def protegida(*args, **kwargs):
        with mutation_lock:
            return funcao(*args, **kwargs)
    return protegida


def json_body():
    """Retorna um JSON seguro, inclusive quando o corpo está vazio ou malformado."""
    dados = request.get_json(silent=True)
    return dados if isinstance(dados, dict) else {}


def normalizar_email(value):
    email = str(value or "").strip().lower()
    return email if EMAIL_PATTERN.fullmatch(email) else None


def senha_valida(senha):
    return isinstance(senha, str) and len(senha) <= 256 and bool(PASSWORD_PATTERN.fullmatch(senha))


def gerar_otp():
    return f"{secrets.randbelow(10 ** OTP_LENGTH):0{OTP_LENGTH}d}"


def buscar_usuario(email, campos="*"):
    resposta = supabase.table("usuarios").select(campos).eq("email", email).limit(1).execute()
    return resposta.data[0] if resposta.data else None


def usuario_publico(usuario):
    return {
        "id": usuario.get("id"),
        "nome": usuario.get("nome"),
        "email": usuario.get("email"),
        "is_admin": bool(usuario.get("is_admin")),
        "pontos": usuario.get("pontos") or 0,
    }


def usuario_da_sessao():
    email = normalizar_email(session.get("usuario_email"))
    if not email:
        return None

    usuario = buscar_usuario(email, "id, nome, email, email_verificado, is_admin, pontos, senha")
    if (not usuario or not usuario.get("email_verificado")
            or str(usuario.get("id")) != str(session.get("usuario_id"))
            or not secrets.compare_digest(session.get("password_version", ""), security.fingerprint(usuario.get("senha") or ""))):
        session.clear()
        return None
    return usuario


def login_obrigatorio(funcao):
    @wraps(funcao)
    def protegida(*args, **kwargs):
        try:
            usuario = usuario_da_sessao()
        except Exception:
            logger.exception("Erro ao validar a sessão do usuário")
            return jsonify({"error": "Não foi possível validar sua sessão."}), 500
        if not usuario:
            return jsonify({"error": "Faça login para continuar."}), 401
        g.usuario = usuario
        return funcao(*args, **kwargs)

    return protegida


def admin_obrigatorio(funcao):
    @wraps(funcao)
    def protegida(*args, **kwargs):
        try:
            usuario = usuario_da_sessao()
        except Exception:
            logger.exception("Erro ao validar a sessão administrativa")
            return jsonify({"error": "Não foi possível validar sua sessão."}), 500
        if not usuario:
            return jsonify({"error": "Faça login para continuar."}), 401
        if not usuario.get("is_admin"):
            return jsonify({"error": "Acesso restrito a administradores."}), 403
        g.usuario = usuario
        return funcao(*args, **kwargs)

    return protegida


def buscar_agendamento(agendamento_id, campos="id, email_cliente, status"):
    resposta = (
        supabase.table("agendamentos")
        .select(campos)
        .eq("id", agendamento_id)
        .limit(1)
        .execute()
    )
    return resposta.data[0] if resposta.data else None


def usuario_pode_alterar_agendamento(usuario, agendamento):
    return bool(
        usuario
        and agendamento
        and (
            usuario.get("is_admin")
            or normalizar_email(agendamento.get("email_cliente")) == normalizar_email(usuario.get("email"))
        )
    )


def validar_data_agendamento(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Data e horário são obrigatórios.")

    try:
        data = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Data e horário em formato inválido.") from error

    if data.tzinfo:
        data = data.astimezone(SPA_TIMEZONE).replace(tzinfo=None)

    agora = datetime.now(SPA_TIMEZONE).replace(tzinfo=None)
    if data <= agora:
        raise ValueError("Não é possível escolher uma data ou horário que já passou.")
    if data.minute or data.second or data.microsecond:
        raise ValueError("Os agendamentos devem ser feitos em horários cheios (ex.: 09:00).")
    if data.weekday() == 0:
        raise ValueError("O Spa é fechado às segundas-feiras para manutenção.")
    if not OPENING_HOUR <= data.hour <= LAST_APPOINTMENT_HOUR:
        raise ValueError("O horário de atendimento é das 09:00 às 20:00.")

    return data.strftime("%Y-%m-%dT%H:%M")


def horario_esta_ocupado(data_atendimento, agendamento_id=None):
    consulta = (
        supabase.table("agendamentos")
        .select("id")
        .eq("data_atendimento", data_atendimento)
        .neq("status", "Cancelado")
    )
    if agendamento_id is not None:
        consulta = consulta.neq("id", agendamento_id)
    return bool(consulta.limit(1).execute().data)


def resposta_concierge_local(mensagem):
    texto = mensagem.lower()
    if any(termo in texto for termo in ("dor", "costas", "tenso", "tensão", "muscular")):
        return "Para aliviar tensões, recomendo a Massagem Terapêutica ou a Massagem com Pedras Quentes. Ambas favorecem o relaxamento muscular."
    if any(termo in texto for termo in ("estresse", "cansaço", "cansado", "ansiedade", "mente")):
        return "Para desacelerar e renovar as energias, a Aromaterapia ou o Banho de Imersão Panaceia são excelentes escolhas."
    if any(termo in texto for termo in ("pele", "rosto", "facial", "esfoliação")):
        return "Para revitalização da pele, recomendo a Limpeza de Pele Profunda ou o Ritual Facial Anti-aging."
    return "Explore nosso catálogo de bem-estar. Posso sugerir uma experiência para dores, estresse, cansaço ou cuidados faciais."


def enviar_email_transacional(destinatario, assunto, conteudo_html):
    api_key = os.getenv("BREVO_API_KEY")
    if not api_key:
        logger.error("BREVO_API_KEY não configurada; e-mail não enviado.")
        return False

    payload = {
        "sender": {
            "name": os.getenv("BREVO_SENDER_NAME", "Spa Panaceia"),
            "email": os.getenv("BREVO_SENDER_EMAIL", "spapanaceia@gmail.com"),
        },
        "to": [{"email": destinatario}],
        "subject": assunto,
        "htmlContent": conteudo_html,
    }
    try:
        resposta = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"accept": "application/json", "api-key": api_key, "content-type": "application/json"},
            json=payload,
            timeout=10,
        )
        resposta.raise_for_status()
        return True
    except requests.RequestException:
        logger.exception("Falha ao enviar e-mail transacional para %s", destinatario)
        return False


def email_de_ativacao(nome, codigo):
    nome_seguro = html.escape(nome)
    return f"""
    <div style="font-family:Arial,sans-serif;max-width:500px;margin:auto;padding:20px;border:1px solid #e0e0e0;border-radius:10px;color:#333">
      <h2 style="color:#6a11cb;text-align:center">Bem-vindo(a) ao Spa Panaceia, {nome_seguro}! 🌿</h2>
      <p>Seu código de verificação para ativar a conta é:</p>
      <div style="background:#f0f4ff;text-align:center;padding:15px;border-radius:8px;margin:20px 0">
        <span style="font-size:32px;font-weight:bold;letter-spacing:8px;color:#6a11cb">{codigo}</span>
      </div>
      <p style="font-size:13px;color:#666;text-align:center">Este código é válido por 15 minutos.</p>
    </div>
    """


def email_de_recuperacao(codigo):
    return f"""
    <div style="font-family:Arial,sans-serif;padding:20px;color:#333">
      <h3 style="color:#6a11cb">Recuperação de Senha — Spa Panaceia 🌸</h3>
      <p>Use o código abaixo para redefinir sua senha:</p>
      <h1 style="background:#f8fafc;padding:15px;border-radius:8px;letter-spacing:5px;color:#2575fc;display:inline-block">{codigo}</h1>
      <p>O código expira em 15 minutos. Conclua a alteração na mesma página onde iniciou a recuperação.</p>
      <p>Se você não solicitou esta alteração, ignore este e-mail.</p>
    </div>
    """


@app.route("/")
def pagina_principal():
    filename = os.getenv("SPA_FRONTEND_FILE") or ("spa-panaceia-profissional.html" if (BASE_DIR / "spa-panaceia-profissional.html").is_file() else "servicos.html")
    return send_from_directory(BASE_DIR, filename)


@app.route("/cadastro")
def pagina_cadastro():
    return pagina_principal()


@app.route("/api/health", methods=["GET"])
def verificar_saude():
    """Endpoint leve para o monitor de disponibilidade da hospedagem."""
    return jsonify({"status": "ok", "versao": VERSAO_BACKEND}), 200


@app.route("/manifest.webmanifest")
def manifest_aplicativo():
    response = jsonify({
        "name": "Spa Panaceia",
        "short_name": "Panaceia",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#100818",
        "theme_color": "#6a11cb",
        "description": "Agendamentos e experiências do Spa Panaceia.",
        "icons": [{
            "src": "/app-icon.svg",
            "sizes": "any",
            "type": "image/svg+xml",
            "purpose": "any maskable",
        }],
    })
    response.mimetype = "application/manifest+json"
    return response


@app.route("/spa-sw.js")
def service_worker_aplicativo():
    response = Response("""const CACHE_NAME = 'spa-panaceia-shell-v2';
const APP_SHELL = ['/'];
self.addEventListener('install', event => event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(APP_SHELL)).then(() => self.skipWaiting())));
self.addEventListener('activate', event => event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key)))).then(() => self.clients.claim())));
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return;
  event.respondWith(fetch(event.request).then(response => {
    if (response.ok) caches.open(CACHE_NAME).then(cache => cache.put(event.request, response.clone()));
    return response;
  }).catch(() => caches.match(event.request).then(cached => cached || caches.match('/'))));
});""", mimetype="application/javascript")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/app-icon.svg")
def icone_aplicativo():
    return Response("""<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 512 512\"><defs><linearGradient id=\"g\" x1=\"0\" y1=\"0\" x2=\"1\" y2=\"1\"><stop stop-color=\"#8b35e8\"/><stop offset=\"1\" stop-color=\"#3d087d\"/></linearGradient></defs><rect width=\"512\" height=\"512\" rx=\"116\" fill=\"url(#g)\"/><path d=\"M256 112c27 73 71 117 144 144-73 27-117 71-144 144-27-73-71-117-144-144 73-27 117-71 144-144Z\" fill=\"#fff3d8\"/><circle cx=\"256\" cy=\"256\" r=\"38\" fill=\"#6a11cb\"/></svg>""", mimetype="image/svg+xml")


@app.errorhandler(404)
def pagina_nao_encontrada(_error):
    if request.path.startswith("/api/"):
        return jsonify({
            "error": "Rota não encontrada.",
            "code": "route_not_found",
        }), 404
    return pagina_principal(), 404


@app.errorhandler(413)
def requisicao_grande_demais(_error):
    if request.path.startswith("/api/") or request.path == "/cadastrar":
        return jsonify({"error": "A requisição enviada é maior que o limite permitido."}), 413
    return pagina_principal(), 413


@app.route("/cadastrar", methods=["POST"])
def cadastrar():
    dados = json_body()
    nome = str(dados.get("nome") or "").strip()
    email = normalizar_email(dados.get("email"))
    senha = dados.get("senha")

    if not nome or not email or not senha:
        return jsonify({"error": "Nome, e-mail e senha são obrigatórios."}), 400
    if len(nome) > 100:
        return jsonify({"error": "O nome deve ter no máximo 100 caracteres."}), 400
    if not senha_valida(senha):
        return jsonify({"error": "A senha deve ter 8 caracteres, com maiúscula, minúscula e caractere especial."}), 400

    try:
        if buscar_usuario(email, "id"):
            return jsonify({"error": "Este e-mail já está cadastrado no sistema."}), 409

        codigo = gerar_otp()
        expiracao = (datetime.now(timezone.utc) + timedelta(minutes=OTP_TTL_MINUTES)).isoformat()
        supabase.table("usuarios").insert(
            {
                "nome": nome,
                "email": email,
                "senha": generate_password_hash(senha, method="pbkdf2:sha256"),
                "email_verificado": False,
                "codigo_otp": codigo,
                "codigo_expira_em": expiracao,
                "is_admin": False,
            }
        ).execute()

        if not enviar_email_transacional(email, "Seu código de ativação — Spa Panaceia", email_de_ativacao(nome, codigo)):
            logger.warning("Conta criada, mas e-mail de ativação não foi entregue para %s", email)

        return jsonify({"message": "Cadastro realizado! Digite o código de 6 dígitos enviado ao seu e-mail para ativar a conta.", "email": email}), 201
    except Exception:
        logger.exception("Erro ao criar conta")
        return jsonify({"error": "Erro ao criar conta. Tente novamente mais tarde."}), 500


@app.route("/api/validar-codigo", methods=["POST"])
def validar_codigo():
    dados = json_body()
    email = normalizar_email(dados.get("email"))
    codigo = str(dados.get("codigo") or "").strip()
    if not email or len(codigo) != OTP_LENGTH or not codigo.isdigit():
        return jsonify({"error": "E-mail e código de 6 dígitos são obrigatórios."}), 400

    try:
        usuario = buscar_usuario(email)
        if not usuario:
            return jsonify({"error": "Usuário não encontrado."}), 404
        if usuario.get("email_verificado"):
            return jsonify({"message": "Conta já verificada! Faça login para continuar."}), 200

        codigo_salvo = str(usuario.get("codigo_otp") or "")
        if not codigo_salvo or not secrets.compare_digest(codigo_salvo, codigo):
            return jsonify({"error": "Código de verificação incorreto."}), 400

        expiracao = usuario.get("codigo_expira_em")
        if not expiracao or datetime.now(timezone.utc) > datetime.fromisoformat(expiracao.replace("Z", "+00:00")):
            return jsonify({"error": "Código expirado. Solicite um novo código."}), 400

        supabase.table("usuarios").update({"email_verificado": True, "codigo_otp": None, "codigo_expira_em": None}).eq("email", email).execute()
        return jsonify({"message": "E-mail verificado com sucesso!"}), 200
    except (TypeError, ValueError):
        logger.exception("Data de expiração de OTP inválida")
        return jsonify({"error": "Não foi possível validar o código."}), 500
    except Exception:
        logger.exception("Erro ao validar OTP")
        return jsonify({"error": "Erro interno ao validar o código."}), 500


@app.route("/api/reenviar-codigo", methods=["POST"])
@serializar_mutacao
def reenviar_codigo():
    """Gera um novo código para contas ainda não ativadas, sem revelar cadastros."""
    email = normalizar_email(json_body().get("email"))
    if not email:
        return jsonify({"error": "Informe um e-mail válido."}), 400

    resposta_padrao = {"message": "Se a conta estiver aguardando ativação, um novo código será enviado."}
    try:
        usuario = buscar_usuario(email, "id, nome, email_verificado")
        if not usuario or usuario.get("email_verificado"):
            return jsonify(resposta_padrao), 200

        codigo = gerar_otp()
        expiracao = (datetime.now(timezone.utc) + timedelta(minutes=OTP_TTL_MINUTES)).isoformat()
        supabase.table("usuarios").update({"codigo_otp": codigo, "codigo_expira_em": expiracao}).eq("id", usuario["id"]).execute()
        if not enviar_email_transacional(email, "Novo código de ativação — Spa Panaceia", email_de_ativacao(usuario.get("nome") or "Cliente", codigo)):
            return jsonify({"error": "Não foi possível enviar o código agora. Tente novamente em alguns minutos."}), 503
        return jsonify(resposta_padrao), 200
    except Exception:
        logger.exception("Erro ao reenviar código de ativação")
        return jsonify({"error": "Não foi possível reenviar o código agora."}), 500


@app.route("/api/login", methods=["POST"])
def login():
    dados = json_body()
    email = normalizar_email(dados.get("email"))
    senha = dados.get("senha")
    if not email or not isinstance(senha, str) or len(senha) > 256:
        return jsonify({"error": "E-mail e senha são obrigatórios."}), 400

    try:
        usuario = buscar_usuario(email)
        if not usuario or not check_password_hash(usuario.get("senha") or "", senha):
            return jsonify({"error": "E-mail ou senha incorretos."}), 401
        if not usuario.get("email_verificado"):
            return jsonify({"error": "Conta não verificada. Verifique seu e-mail antes de entrar."}), 403

        session.clear()
        session.permanent = True
        session["usuario_id"] = usuario.get("id")
        session["usuario_email"] = usuario.get("email")
        session["password_version"] = security.fingerprint(usuario.get("senha") or "")

        return jsonify({"message": "Login realizado com sucesso!", "usuario": usuario_publico(usuario), "csrf_token": csrf_token()}), 200
    except Exception:
        logger.exception("Erro no login")
        return jsonify({"error": "Erro interno no servidor."}), 500


@app.route("/api/me", methods=["GET"])
@login_obrigatorio
def usuario_atual():
    return jsonify({"usuario": usuario_publico(g.usuario), "csrf_token": csrf_token()}), 200


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"message": "Sessão encerrada com sucesso."}), 200


@app.route("/api/esqueci-senha", methods=["POST"])
@serializar_mutacao
def esqueci_senha():
    dados = json_body()
    email = normalizar_email(dados.get("email"))
    if not email:
        return jsonify({"error": "Informe um e-mail válido."}), 400

    codigo = gerar_otp()
    resposta_padrao = {
        "message": "Se o e-mail estiver cadastrado, você receberá o código de recuperação.",
        "recuperacao_token": security.issue_recovery(email, codigo),
    }
    try:
        if not buscar_usuario(email, "id"):
            return jsonify(resposta_padrao), 200

        supabase.table("usuarios").update({"token_recuperacao": codigo}).eq("email", email).execute()
        enviar_email_transacional(email, "Código de Recuperação de Senha", email_de_recuperacao(codigo))
        return jsonify(resposta_padrao), 200
    except Exception:
        logger.exception("Erro ao solicitar redefinição de senha")
        return jsonify({"error": "Erro ao processar a solicitação."}), 500


@app.route("/api/redefinir-senha", methods=["POST"])
@serializar_mutacao
def redefinir_senha():
    dados = json_body()
    email = normalizar_email(dados.get("email"))
    codigo = str(dados.get("codigo") or "").strip()
    nova_senha = dados.get("nova_senha")
    if not email or len(codigo) != OTP_LENGTH or not codigo.isdigit() or not isinstance(nova_senha, str):
        return jsonify({"error": "Preencha todos os campos corretamente."}), 400
    if not senha_valida(nova_senha):
        return jsonify({"error": "A senha deve ter 8 caracteres, com maiúscula, minúscula e caractere especial."}), 400
    if not security.verify_recovery(dados.get("recuperacao_token"), email, codigo, max_age=OTP_TTL_MINUTES * 60):
        return jsonify({"error": "Código inválido ou expirado. Solicite uma nova recuperação nesta página."}), 400

    try:
        usuario = buscar_usuario(email, "token_recuperacao")
        if not usuario:
            return jsonify({"error": "Código inválido ou expirado."}), 400
        token_salvo = str(usuario.get("token_recuperacao") or "")
        if not token_salvo or not secrets.compare_digest(token_salvo, codigo):
            return jsonify({"error": "Código de verificação incorreto."}), 400

        resultado = supabase.table("usuarios").update({"senha": generate_password_hash(nova_senha, method="pbkdf2:sha256"), "token_recuperacao": None}).eq("email", email).eq("token_recuperacao", codigo).execute()
        if not resultado.data:
            return jsonify({"error": "Código já utilizado. Solicite uma nova recuperação."}), 409
        if normalizar_email(session.get("usuario_email")) == email:
            session.clear()
        return jsonify({"message": "Senha redefinida com sucesso! Faça login para continuar."}), 200
    except Exception:
        logger.exception("Erro ao redefinir senha")
        return jsonify({"error": "Erro ao redefinir a senha."}), 500


@app.route("/api/servicos", methods=["GET"])
def listar_servicos():
    try:
        resposta = supabase.table("servico").select("id, tipo, descricao, valor, imagem_url, contratos").order("tipo").execute()
        return jsonify(resposta.data or []), 200
    except Exception:
        logger.exception("Erro ao listar serviços")
        return jsonify({"error": "Erro ao ler a tabela de serviços."}), 500


@app.route("/api/relogio", methods=["GET"])
def relogio_spa():
    """Hora do servidor, sem leitura nem alteração do banco."""
    return jsonify({"agora": datetime.now(SPA_TIMEZONE).isoformat(), "fuso": str(SPA_TIMEZONE)})


@app.route("/api/agendar", methods=["POST"])
@login_obrigatorio
@serializar_mutacao
def criar_agendamento():
    dados = json_body()
    email = g.usuario["email"]
    try:
        servico_id = int(dados.get("servico_id"))
    except (TypeError, ValueError):
        servico_id = 0
    if servico_id <= 0:
        return jsonify({"error": "Serviço inválido."}), 400

    try:
        data_atendimento = validar_data_agendamento(dados.get("data"))
        if not supabase.table("servico").select("id").eq("id", servico_id).limit(1).execute().data:
            return jsonify({"error": "Serviço não encontrado."}), 404
        if horario_esta_ocupado(data_atendimento):
            return jsonify({"error": "Este horário já está reservado por outro cliente."}), 409

        supabase.table("agendamentos").insert({"email_cliente": email, "servico_id": servico_id, "data_atendimento": data_atendimento, "status": "Pendente"}).execute()

        servico = supabase.table("servico").select("contratos").eq("id", servico_id).limit(1).execute().data
        if servico:
            contratos = int(servico[0].get("contratos") or 0)
            supabase.table("servico").update({"contratos": contratos + 1}).eq("id", servico_id).execute()
        return jsonify({"message": "Agendamento realizado com sucesso!"}), 201
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except Exception:
        logger.exception("Erro ao criar agendamento")
        return jsonify({"error": "Erro ao salvar o agendamento no banco."}), 500


@app.route("/api/meus-agendamentos", methods=["GET"])
@login_obrigatorio
def meus_agendamentos():
    try:
        resposta = supabase.table("agendamentos").select("id, data_atendimento, status, avaliacao, servico_id, servico(tipo, valor)").eq("email_cliente", g.usuario["email"]).order("data_atendimento").execute()
        return jsonify(resposta.data or []), 200
    except Exception:
        logger.exception("Erro ao buscar agendamentos do usuário")
        return jsonify({"error": "Erro ao carregar a lista de agendamentos."}), 500


@app.route("/api/horarios-ocupados", methods=["GET"])
def horarios_ocupados():
    try:
        ignorar_id = request.args.get("ignorar_id", type=int)
        consulta = (
            supabase.table("agendamentos")
            .select("data_atendimento")
            .neq("status", "Cancelado")
        )
        if ignorar_id:
            usuario = usuario_da_sessao()
            agendamento = buscar_agendamento(ignorar_id)
            if usuario_pode_alterar_agendamento(usuario, agendamento):
                consulta = consulta.neq("id", ignorar_id)

        resposta = consulta.order("data_atendimento").execute()
        return jsonify([agendamento["data_atendimento"] for agendamento in resposta.data or []]), 200
    except Exception:
        logger.exception("Erro ao listar horários ocupados")
        return jsonify({"error": "Não foi possível carregar os horários ocupados."}), 500


@app.route("/api/agendamentos/<int:agendamento_id>", methods=["PUT"])
@login_obrigatorio
@serializar_mutacao
def alterar_horario(agendamento_id):
    try:
        agendamento = buscar_agendamento(agendamento_id)
        if not agendamento:
            return jsonify({"error": "Agendamento não encontrado."}), 404
        if not usuario_pode_alterar_agendamento(g.usuario, agendamento):
            return jsonify({"error": "Você não pode alterar este agendamento."}), 403

        nova_data = validar_data_agendamento(json_body().get("data"))
        if horario_esta_ocupado(nova_data, agendamento_id):
            return jsonify({"error": "Este horário já está reservado por outro cliente."}), 409
        supabase.table("agendamentos").update({"data_atendimento": nova_data}).eq("id", agendamento_id).execute()
        return jsonify({"message": "Horário atualizado com sucesso!"}), 200
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except Exception:
        logger.exception("Erro ao alterar agendamento")
        return jsonify({"error": "Erro interno ao reagendar."}), 500


@app.route("/api/agendamentos/<int:agendamento_id>", methods=["DELETE"])
@login_obrigatorio
@serializar_mutacao
def cancelar_agendamento(agendamento_id):
    try:
        agendamento = buscar_agendamento(agendamento_id)
        if not agendamento:
            return jsonify({"error": "Agendamento não encontrado."}), 404
        if not usuario_pode_alterar_agendamento(g.usuario, agendamento):
            return jsonify({"error": "Você não pode cancelar este agendamento."}), 403

        # A condição também é verificada no UPDATE: não pode desfazer uma
        # conclusão concorrente e permitir crédito de pontos pela segunda vez.
        alterados = supabase.table("agendamentos").update({"status": "Cancelado"}).eq("id", agendamento_id).eq("status", "Pendente").execute().data
        if not alterados:
            return jsonify({"error": "Somente atendimentos pendentes podem ser cancelados. Atualize a lista."}), 409
        return jsonify({"message": "Agendamento cancelado com sucesso."}), 200
    except Exception:
        logger.exception("Erro ao cancelar agendamento")
        return jsonify({"error": "Erro ao cancelar o agendamento."}), 500


@app.route("/api/agendamentos/<int:agendamento_id>/avaliar", methods=["POST"])
@login_obrigatorio
def avaliar_agendamento(agendamento_id):
    avaliacao = json_body().get("avaliacao")
    if avaliacao not in {"Bom", "Médio", "Ruim"}:
        return jsonify({"error": "Avaliação inválida."}), 400
    try:
        agendamento = buscar_agendamento(agendamento_id)
        if not agendamento:
            return jsonify({"error": "Agendamento não encontrado."}), 404
        if not usuario_pode_alterar_agendamento(g.usuario, agendamento):
            return jsonify({"error": "Você não pode avaliar este agendamento."}), 403
        if agendamento.get("status") != "Concluido":
            return jsonify({"error": "A avaliação é liberada após a conclusão do atendimento."}), 409

        supabase.table("agendamentos").update({"avaliacao": avaliacao}).eq("id", agendamento_id).execute()
        return jsonify({"message": "Avaliação registrada!"}), 200
    except Exception:
        logger.exception("Erro ao avaliar agendamento")
        return jsonify({"error": "Erro ao salvar avaliação."}), 500


@app.route("/api/admin/agendamentos", methods=["GET"])
@admin_obrigatorio
def admin_agendamentos():
    try:
        resposta = supabase.table("agendamentos").select("id, email_cliente, data_atendimento, status, avaliacao, servico(tipo, valor)").order("data_atendimento", desc=True).execute()
        return jsonify(resposta.data or []), 200
    except Exception:
        logger.exception("Erro ao listar agendamentos administrativos")
        return jsonify({"error": "Erro ao buscar dados globais."}), 500


@app.route("/api/admin/agendamentos/<int:agendamento_id>/concluir", methods=["POST"])
@admin_obrigatorio
@serializar_mutacao
def concluir_agendamento(agendamento_id):
    try:
        resposta = supabase.table("agendamentos").select("email_cliente, status, servico_id").eq("id", agendamento_id).limit(1).execute()
        if not resposta.data:
            return jsonify({"error": "Agendamento não encontrado."}), 404

        agendamento = resposta.data[0]
        if agendamento.get("status") == "Concluido":
            return jsonify({"message": "Este agendamento já foi concluído.", "pontos": 0}), 200
        if agendamento.get("status") == "Cancelado":
            return jsonify({"error": "Agendamentos cancelados não podem ser concluídos."}), 409

        pontos = 0
        servico_id = agendamento.get("servico_id")
        if servico_id:
            servico = supabase.table("servico").select("valor").eq("id", servico_id).limit(1).execute().data
            if servico:
                pontos = int(float(servico[0].get("valor") or 0) * 0.10)

        conclusao = supabase.table("agendamentos").update({"status": "Concluido"}).eq("id", agendamento_id).eq("status", "Pendente").execute().data or []
        if not conclusao:
            return jsonify({"message": "Este agendamento já foi atualizado por outro atendimento.", "pontos": 0}), 200
        cliente = buscar_usuario(agendamento.get("email_cliente"), "pontos")
        if cliente and pontos:
            supabase.table("usuarios").update({"pontos": int(cliente.get("pontos") or 0) + pontos}).eq("email", agendamento["email_cliente"]).execute()
        return jsonify({"message": f"Agendamento concluído e {pontos} pontos creditados ao cliente!", "pontos": pontos}), 200
    except Exception:
        logger.exception("Erro ao concluir agendamento")
        return jsonify({"error": "Erro ao concluir o serviço."}), 500


@app.route("/api/admin/vouchers/utilizar", methods=["POST"])
@admin_obrigatorio
@serializar_mutacao
def utilizar_voucher_admin():
    """Valida e consome um voucher no atendimento selecionado pelo administrador."""
    dados = json_body()
    codigo = str(dados.get("codigo_voucher") or "").strip().upper()
    try:
        agendamento_id = int(dados.get("agendamento_id"))
    except (TypeError, ValueError):
        agendamento_id = 0
    if not re.fullmatch(r"PAN-[A-F0-9]{10}", codigo) or agendamento_id <= 0:
        return jsonify({"error": "Informe um voucher e um atendimento válidos."}), 400

    try:
        agendamento = buscar_agendamento(agendamento_id, "id, email_cliente, status")
        if not agendamento:
            return jsonify({"error": "Agendamento não encontrado."}), 404
        if agendamento.get("status") == "Cancelado":
            return jsonify({"error": "Não é possível aplicar voucher em um atendimento cancelado."}), 409

        voucher = (
            supabase.table("resgates_pontos")
            .select("id, email_cliente, recompensa_nome, desconto, status, expira_em")
            .eq("codigo_voucher", codigo)
            .limit(1)
            .execute()
            .data or []
        )
        if not voucher:
            return jsonify({"error": "Voucher não encontrado."}), 404
        voucher = voucher[0]
        if normalizar_email(voucher.get("email_cliente")) != normalizar_email(agendamento.get("email_cliente")):
            return jsonify({"error": "Este voucher pertence a outro cliente."}), 409
        if voucher.get("status") != "ativo":
            return jsonify({"error": "Este voucher já foi utilizado ou não está mais ativo."}), 409

        expiracao = voucher.get("expira_em")
        if expiracao and datetime.now(timezone.utc) >= datetime.fromisoformat(str(expiracao).replace("Z", "+00:00")):
            return jsonify({"error": "Este voucher expirou."}), 409

        resposta = supabase.rpc("utilizar_voucher", {"p_codigo_voucher": codigo, "p_agendamento_id": agendamento_id}).execute()
        utilizado = resposta.data[0] if isinstance(resposta.data, list) and resposta.data else resposta.data
        if not utilizado:
            return jsonify({"error": "O voucher não pôde ser utilizado. Atualize a agenda e tente novamente."}), 409
        return jsonify({
            "message": f"Voucher aplicado: {voucher.get('recompensa_nome') or 'benefício de fidelidade'}.",
            "voucher": {"codigo_voucher": codigo, "desconto": voucher.get("desconto") or 0, "status": "utilizado"},
        }), 200
    except (TypeError, ValueError):
        return jsonify({"error": "A validade deste voucher está inconsistente."}), 409
    except Exception:
        logger.exception("Erro ao utilizar voucher no agendamento %s", agendamento_id)
        return jsonify({"error": "Não foi possível aplicar o voucher agora."}), 500


@app.route("/api/usuario/pontos", methods=["GET"])
@login_obrigatorio
def obter_pontos():
    try:
        cliente = buscar_usuario(g.usuario["email"], "pontos")
        if not cliente:
            return jsonify({"error": "Usuário não encontrado."}), 404
        return jsonify({"pontos": cliente.get("pontos") or 0}), 200
    except Exception:
        logger.exception("Erro ao buscar pontos")
        return jsonify({"error": "Erro ao buscar os pontos do usuário."}), 500



@app.route("/api/fidelidade", methods=["GET"])
@login_obrigatorio
def consultar_fidelidade():
    """Saldo, recompensas e vouchers ativos sempre vêm do Supabase."""
    try:
        email_cliente = str(g.usuario["email"] or "").strip().lower()
        cliente = buscar_usuario(email_cliente, "pontos")
        if not cliente:
            return jsonify({"error": "Usuário não encontrado."}), 404

        # Esta tela é somente de leitura. A expiração é validada no resgate/uso
        # do voucher; assim, abrir a página nunca altera dados por engano.
        vouchers = (
            supabase.table("resgates_pontos")
            .select("id, recompensa_nome, pontos_usados, desconto, codigo_voucher, status, criado_em, expira_em")
            .eq("email_cliente", email_cliente)
            .eq("status", "ativo")
            .gt("expira_em", datetime.now(timezone.utc).isoformat())
            .order("criado_em", desc=True)
            .execute()
            .data or []
        )
        logger.info("Consulta de fidelidade concluída: %d voucher(s) ativo(s)", len(vouchers))
        return jsonify({"pontos": int(cliente.get("pontos") or 0), "recompensas": list(RECOMPENSAS_FIDELIDADE.values()), "vouchers": vouchers, "total_vouchers": len(vouchers)}), 200
    except Exception:
        logger.exception("Erro ao consultar fidelidade")
        return jsonify({"error": "Não foi possível consultar sua fidelidade agora."}), 500


@app.route("/api/fidelidade/resgatar", methods=["POST"])
@login_obrigatorio
@serializar_mutacao
def resgatar_pontos():
    dados = json_body()
    recompensa = str(dados.get("recompensa") or "").strip().lower()
    if recompensa not in RECOMPENSAS_FIDELIDADE:
        return jsonify({"error": "Recompensa inválida."}), 400
    try:
        resposta = supabase.rpc("resgatar_pontos", {"p_email": g.usuario["email"], "p_recompensa_codigo": recompensa}).execute()
        resgate = resposta.data[0] if resposta.data else None
        if not resgate:
            raise RuntimeError("O resgate não retornou um voucher.")
        return jsonify({"message": "Pontos trocados com sucesso. Apresente o código no Spa.", "resgate": resgate}), 201
    except Exception as erro:
        logger.warning("Resgate de pontos recusado para %s: %s", g.usuario["email"], type(erro).__name__)
        mensagem = str(erro).lower()
        if "insuficiente" in mensagem:
            return jsonify({"error": "Você não tem pontos suficientes para essa recompensa."}), 409
        return jsonify({"error": "Não foi possível concluir o resgate agora. Tente novamente."}), 500


@app.route("/api/privacidade/exportar", methods=["GET"])
@login_obrigatorio
def exportar_dados_pessoais():
    """Entrega ao titular uma cópia somente dos dados ligados à própria conta."""
    try:
        usuario = usuario_publico(g.usuario)
        agendamentos = (
            supabase.table("agendamentos")
            .select("id, data_atendimento, status, avaliacao, servico(tipo, valor)")
            .eq("email_cliente", g.usuario["email"])
            .order("data_atendimento", desc=True)
            .execute()
            .data or []
        )
        return jsonify({
            "gerado_em": datetime.now(SPA_TIMEZONE).isoformat(),
            "finalidade": "Cópia dos dados pessoais e do histórico de reservas do Spa Panaceia.",
            "conta": usuario,
            "agendamentos": agendamentos,
        }), 200
    except Exception:
        logger.exception("Erro ao exportar dados pessoais")
        return jsonify({"error": "Não foi possível preparar sua cópia de dados agora."}), 500


@app.route("/api/admin/usuarios", methods=["GET"])
@admin_obrigatorio
def admin_usuarios():
    try:
        usuarios = supabase.table("usuarios").select("nome, email").order("nome").execute().data or []
        agendamentos = supabase.table("agendamentos").select("email_cliente, status, servico(valor)").execute().data or []
        resumo_por_email = defaultdict(lambda: {"total": 0, "pendentes": 0, "concluidos": 0, "cancelados": 0, "gastos": 0.0})

        for agendamento in agendamentos:
            resumo = resumo_por_email[agendamento.get("email_cliente")]
            resumo["total"] += 1
            status = agendamento.get("status") or "Pendente"
            if status == "Pendente":
                resumo["pendentes"] += 1
            elif status == "Concluido":
                resumo["concluidos"] += 1
                servico = agendamento.get("servico") or {}
                resumo["gastos"] += float(servico.get("valor") or 0)
            elif status == "Cancelado":
                resumo["cancelados"] += 1

        return jsonify([
            {"nome": usuario.get("nome"), "email": usuario.get("email"), **resumo_por_email[usuario.get("email")]}
            for usuario in usuarios
        ]), 200
    except Exception:
        logger.exception("Erro ao listar usuários administrativos")
        return jsonify({"error": "Erro interno ao carregar relatório de clientes."}), 500


def quantidade_inteira(value, campo):
    if isinstance(value, bool):
        raise ValueError(f"{campo} deve ser um número inteiro.")
    try:
        quantidade = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{campo} deve ser um número inteiro.") from error
    if quantidade < 0:
        raise ValueError(f"{campo} não pode ser negativa.")
    return quantidade


@app.route("/api/estoque", methods=["GET"])
@admin_obrigatorio
def listar_estoque():
    try:
        resposta = supabase.table("estoque").select("id, nome, quantidade, quantidade_minima, unidade").order("id").execute()
        return jsonify(resposta.data or []), 200
    except Exception:
        logger.exception("Erro ao listar estoque")
        return jsonify({"error": "Erro ao carregar o estoque."}), 500


@app.route("/api/estoque", methods=["POST"])
@admin_obrigatorio
def adicionar_estoque():
    dados = json_body()
    nome = str(dados.get("nome") or "").strip()
    unidade = str(dados.get("unidade") or "un").strip().lower()
    if not nome or len(nome) > 120 or not unidade or len(unidade) > 10:
        return jsonify({"error": "Nome ou unidade do produto inválidos."}), 400

    try:
        supabase.table("estoque").insert({"nome": nome, "quantidade": quantidade_inteira(dados.get("quantidade", 0), "Quantidade"), "quantidade_minima": quantidade_inteira(dados.get("quantidade_minima", 5), "Quantidade mínima"), "unidade": unidade}).execute()
        return jsonify({"message": "Item adicionado ao estoque!"}), 201
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except Exception:
        logger.exception("Erro ao inserir item no estoque")
        return jsonify({"error": "Erro ao cadastrar o produto no estoque."}), 500


@app.route("/api/estoque/<int:item_id>", methods=["PUT"])
@admin_obrigatorio
def atualizar_estoque(item_id):
    dados = json_body()
    try:
        quantidade = quantidade_inteira(dados.get("quantidade"), "Quantidade")
        supabase.table("estoque").update({"quantidade": quantidade}).eq("id", item_id).execute()
        return jsonify({"message": "Estoque atualizado!"}), 200
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except Exception:
        logger.exception("Erro ao atualizar estoque")
        return jsonify({"error": "Erro ao alterar quantidade."}), 500


@app.route("/api/estoque/<int:item_id>", methods=["DELETE"])
@admin_obrigatorio
def deletar_estoque(item_id):
    try:
        supabase.table("estoque").delete().eq("id", item_id).execute()
        return jsonify({"message": "Item removido do estoque!"}), 200
    except Exception:
        logger.exception("Erro ao remover item do estoque")
        return jsonify({"error": "Erro ao remover item do estoque."}), 500


@app.route("/api/chat", methods=["POST"])
def chat():
    mensagem = str(json_body().get("mensagem") or "").strip()
    if not mensagem:
        return jsonify({"error": "Escreva uma mensagem para que eu possa ajudar."}), 400
    if len(mensagem) > CHAT_MAX_LENGTH:
        return jsonify({"error": f"A mensagem deve ter no máximo {CHAT_MAX_LENGTH} caracteres."}), 400

    if not openai_client:
        return jsonify({"resposta": resposta_concierge_local(mensagem), "origem": "local"}), 200

    contexto = (
        "Você é o Concierge Virtual do Spa Panaceia. Responda em português, com tom acolhedor, "
        "natural e conciso (no máximo três frases), sem emojis nem jargão. Apresente os cuidados "
        "de bem-estar sem prometer benefícios médicos. Quando a pessoa quiser agendar, oriente "
        "a escolher um tratamento no catálogo. Nunca afirme que uma reserva foi feita ou aberta."
    )
    try:
        resposta = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "system", "content": contexto}, {"role": "user", "content": mensagem}],
            max_tokens=150,
            temperature=0.7,
        )
        texto = (resposta.choices[0].message.content or "").strip()
        if not texto:
            raise ValueError("Resposta vazia da OpenAI")
        return jsonify({"resposta": texto, "origem": "openai"}), 200
    except Exception:
        logger.exception("Falha no serviço de chat")
        return jsonify({"error": "O concierge está indisponível neste momento. Tente novamente em instantes."}), 503


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
