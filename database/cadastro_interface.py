import atexit
import html
import ipaddress
import logging
import os
import re
import secrets
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from threading import Lock, RLock
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
)
from flask.sessions import SecureCookieSessionInterface
from flask_cors import CORS
from werkzeug.security import check_password_hash, generate_password_hash
if __package__:
    from .postgres_store import (
        InsufficientStockError,
        ScheduleConflictError,
        ServiceNotFoundError,
        StockItemNotFoundError,
        StockOperationConflictError,
        StockQuantityConflictError,
        create_database_client,
    )
    from .spa_security import (
        AdaptiveIPBlocker,
        RateLimiter,
        SecurityTools,
        SessionRevocationStore,
    )
else:
    from postgres_store import (
        InsufficientStockError,
        ScheduleConflictError,
        ServiceNotFoundError,
        StockItemNotFoundError,
        StockOperationConflictError,
        StockQuantityConflictError,
        create_database_client,
    )
    from spa_security import (
        AdaptiveIPBlocker,
        RateLimiter,
        SecurityTools,
        SessionRevocationStore,
    )

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


def _limpar_valor_env(valor):
    """Normaliza valores vindos de --env-file do Docker (remove CRLF e aspas externas)."""
    if not isinstance(valor, str):
        return ""
    limpo = valor.replace("\r", "").replace("\n", "").strip()
    if len(limpo) >= 2 and limpo[0] == limpo[-1] and limpo[0] in {'"', "'"}:
        limpo = limpo[1:-1].strip()
    return limpo


def _sanitizar_ambiente_processo():
    """Remove retornos de carro (\\r) e aspas residuais em variáveis carregadas via Docker --env-file."""
    for chave, valor in list(os.environ.items()):
        if not isinstance(valor, str):
            continue
        if "\r" in valor or (
            chave.startswith(("TELEGRAM_", "SPA_", "BREVO_", "DB_", "SUPABASE_", "APP_ENV", "FLASK_", "LOG_LEVEL"))
            and len(valor.strip()) >= 2
            and valor.strip()[0] == valor.strip()[-1]
            and valor.strip()[0] in {'"', "'"}
        ):
            os.environ[chave] = _limpar_valor_env(valor)


BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = Path(os.getenv("ENV_FILE") or (BASE_DIR.parent / "database.env"))
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
else:
    load_dotenv()
_sanitizar_ambiente_processo()

_log_level_name = _limpar_valor_env(os.getenv("LOG_LEVEL", "INFO")).upper() or "INFO"
_log_level = getattr(logging, _log_level_name, logging.INFO)
logging.basicConfig(level=_log_level)
logger = logging.getLogger(__name__)
logger.setLevel(_log_level)
telegram_logger = logging.getLogger("spa.telegram")
telegram_logger.setLevel(_log_level)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

APP_ENVIRONMENT = _limpar_valor_env(os.getenv("APP_ENV", os.getenv("FLASK_ENV", ""))).lower()
IS_RENDER = os.getenv("RENDER", "").strip().lower() == "true"
# Render always supplies RENDER=true. Treat it as production even if a custom
# start command bypasses the Procfile's APP_ENV=production prefix.
IS_PRODUCTION = APP_ENVIRONMENT == "production" or IS_RENDER

trusted_hosts_padrao = {
    "spapanaceia.com.br",
    "www.spapanaceia.com.br",
    "localhost",
    "127.0.0.1",
    "192.168.18.145",
    "192.168.18.220",
}
trusted_hosts = {
    host.strip().lower()
    for host in os.getenv("TRUSTED_HOSTS", "").split(",")
    if host.strip()
}
render_hostname = (os.getenv("RENDER_EXTERNAL_HOSTNAME") or "").strip().lower()
if render_hostname:
    # Render owns this exact service hostname; do not trust all *.onrender.com.
    trusted_hosts_padrao.add(render_hostname)


DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "site_db")
DB_USER = os.getenv("DB_USER", "site_user")
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip()
SUPABASE_SERVER_KEY = (
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    or os.getenv("service_role")
    or ""
).strip()
if not os.getenv("DB_PASSWORD") and not (SUPABASE_URL and SUPABASE_SERVER_KEY):
    raise RuntimeError("Configure o PostgreSQL local ou as credenciais do Supabase.")

SPA_TIMEZONE = ZoneInfo(os.getenv("SPA_TIMEZONE", "America/Sao_Paulo"))
OPENING_HOUR = 9
LAST_APPOINTMENT_HOUR = 19
OTP_TTL_MINUTES = 15
OTP_LENGTH = 6
CHAT_MAX_LENGTH = 1_000
VERSAO_BACKEND = "agenda-operations-20260929"
PASSWORD_PATTERN = re.compile(r"^(?=.*[a-z])(?=.*[A-Z])(?=.*[@$!%*?&#,.]).{8,}$")
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
RECOMPENSAS_FIDELIDADE = {
    "pausa": {"codigo": "pausa", "nome": "Pausa Panaceia", "pontos": 100, "desconto": 10.00, "descricao": "R$ 10 de crédito para usar em uma experiência."},
    "ritual": {"codigo": "ritual", "nome": "Ritual Panaceia", "pontos": 250, "desconto": 30.00, "descricao": "R$ 30 de crédito para reservar seu próximo ritual."},
    "renovar": {"codigo": "renovar", "nome": "Renovar Panaceia", "pontos": 500, "desconto": 70.00, "descricao": "R$ 70 de crédito para uma nova pausa de cuidado."},
}

app = Flask(
    __name__,
    static_folder=str(BASE_DIR.parent / "static"),
    static_url_path="/static",
    template_folder=str(BASE_DIR.parent / "templates"),
)
secret_key = os.getenv("FLASK_SECRET_KEY")
if not secret_key:
    if IS_PRODUCTION:
        raise RuntimeError("FLASK_SECRET_KEY é obrigatória em produção.")
    secret_key = secrets.token_hex(32)
    logger.warning("FLASK_SECRET_KEY não configurada; as sessões serão encerradas ao reiniciar o servidor.")

app.config.update(
    SECRET_KEY=secret_key,
    TRUSTED_HOSTS=sorted(trusted_hosts_padrao | trusted_hosts),
    SESSION_COOKIE_NAME="spa_panaceia_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE=os.getenv("SESSION_COOKIE_SAMESITE", "Lax"),
    SESSION_COOKIE_SECURE=(
        True if IS_PRODUCTION
        else os.getenv("SESSION_COOKIE_SECURE", "false").strip().lower() == "true"
    ),
    SESSION_COOKIE_PATH="/",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=int(os.getenv("SESSION_TTL_HOURS", "12"))),
    MAX_CONTENT_LENGTH=32 * 1024,
    JSON_SORT_KEYS=False,
)


class SessaoHttpPrivadaAware(SecureCookieSessionInterface):
    """Mantém Secure em produção para domínios públicos/HTTPS, mas permite cookies em HTTP direto na rede local.

    Navegadores não enviam cookies ``Secure`` em ``http://192.168.x.x``.
    Isso fazia o login parecer concluído no cliente, enquanto as rotas do
    perfil recebiam uma sessão vazia quando o site era aberto pelo IP local.
    Domínios públicos continuam protegidos pelo padrão de produção.
    """

    def get_cookie_secure(self, app):
        if (
            request.is_secure
            or request.headers.get("X-Forwarded-Proto", "").strip().lower() == "https"
            or "CF-Connecting-IP" in request.headers
        ):
            return True

        # Não usar request.host_url: com TRUSTED_HOSTS, ler request.host para
        # finalizar a sessão de uma requisição rejeitada pode lançar SecurityError.
        try:
            hostname = (urlsplit("//" + request.headers.get("Host", "")).hostname or "").strip("[]").lower()
        except ValueError:
            hostname = ""

        remote_addr = (request.remote_addr or "").strip("[]")
        try:
            ip_remoto = ipaddress.ip_address(remote_addr)
            conexao_lan_direta = ip_remoto.is_private or ip_remoto.is_loopback or ip_remoto.is_link_local
        except ValueError:
            conexao_lan_direta = False

        if conexao_lan_direta:
            if hostname == "localhost" or hostname.endswith(".localhost"):
                return False
            try:
                endereco_host = ipaddress.ip_address(hostname)
                if endereco_host.is_private or endereco_host.is_loopback or endereco_host.is_link_local:
                    return False
            except ValueError:
                pass

        if IS_PRODUCTION:
            return True

        configuracao_explicita = os.getenv("SESSION_COOKIE_SECURE")
        if configuracao_explicita is not None:
            return configuracao_explicita.strip().lower() in {"1", "true", "yes", "on"}
        return bool(app.config.get("SESSION_COOKIE_SECURE", False))


app.session_interface = SessaoHttpPrivadaAware()


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
    "http://127.0.0.1:5000",
    "http://localhost:5000",
    "http://192.168.18.145:5000",
    "http://192.168.18.220:5000",
}
if not IS_PRODUCTION:
    origens_padrao.update({
        "http://127.0.0.1:5500",
        "http://localhost:5500",
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
        # Em produção, requisições de navegador que alteram estado precisam
        # identificar a origem. Isso também mitiga login CSRF quando ainda não
        # existe uma sessão autenticada para exigir o token CSRF.
        if IS_PRODUCTION:
            return request.headers.get("Sec-Fetch-Site") == "same-origin"
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

db = create_database_client()
try:
    db.ensure_schema()
except Exception:
    logger.warning(
        "Nenhum banco respondeu durante a inicialização; "
        "a aplicação continuará ativa e tentará novamente nas próximas requisições.",
        exc_info=True,
    )

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY and OpenAI else None

security = SecurityTools(secret_key)
DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_urlsafe(32), method="pbkdf2:sha256")
account_rate_limiter = RateLimiter(max_keys=10_000)
challenge_attempt_limiter = RateLimiter(max_keys=10_000)
session_revocations = SessionRevocationStore(max_entries=20_000)


class _CompositeRateLimiter(RateLimiter):
    """Limitador principal por IP; ao limpar em testes, limpa também o de contas."""

    def clear(self) -> None:
        super().clear()
        account_rate_limiter.clear()
        challenge_attempt_limiter.clear()


rate_limiter = _CompositeRateLimiter(max_keys=10_000)
spam_blocker = AdaptiveIPBlocker(limit=30, window_seconds=1, base_block_seconds=30)
vpn_spam_blocker = AdaptiveIPBlocker(limit=15, window_seconds=1, base_block_seconds=60)

# Locks separados por contexto para evitar contenção global entre domínios
# independentes e impedir que chamadas HTTP externas bloqueiem mutações.
mutation_lock = RLock()
schedule_lock = RLock()
loyalty_lock = RLock()
stock_lock = RLock()
_AUTH_LOCK_SHARDS = tuple(RLock() for _ in range(64))

OTP_MAX_ATTEMPTS = 5
RECOVERY_MAX_ATTEMPTS = 5
ALLOWED_FRONTEND_FILES = frozenset({
    "servicos.html",
    "servicos.min.html",
    "spa-panaceia-profissional.html",
    "spa-panaceia-profissional.min.html",
})
PUBLIC_AUTH_PATHS = {"/cadastrar", "/api/login", "/api/validar-codigo", "/api/reenviar-codigo", "/api/esqueci-senha", "/api/redefinir-senha"}


@contextmanager
def lock_conta(email):
    """Serializa mutações de uma mesma conta sem bloquear outros usuários ou a agenda."""
    chave = (email or "").strip().casefold()
    indice = int.from_bytes(security.fingerprint(chave)[:4].encode("ascii"), "big") % len(_AUTH_LOCK_SHARDS)
    with _AUTH_LOCK_SHARDS[indice]:
        yield


def obter_csp_nonce():
    """Retorna o nonce criptográfico único da requisição atual."""
    nonce = getattr(g, "csp_nonce", None)
    if not nonce:
        nonce = secrets.token_urlsafe(24)
        g.csp_nonce = nonce
    return nonce


def carregar_redes_ip(nome_variavel):
    redes = []
    for entrada in os.getenv(nome_variavel, "").split(","):
        entrada = entrada.strip()
        if not entrada:
            continue
        try:
            redes.append(ipaddress.ip_network(entrada, strict=False))
        except ValueError:
            logger.warning("Entrada inválida ignorada em %s", nome_variavel)
    return tuple(redes)


def ip_pertence_as_redes(ip, redes):
    try:
        endereco = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(endereco.version == rede.version and endereco in rede for rede in redes)


BLOCKED_IP_NETWORKS = carregar_redes_ip("BLOCKED_IP_CIDRS")
ALLOWED_COUNTRIES = {
    codigo.strip().upper()
    for codigo in os.getenv("ALLOWED_COUNTRIES", "").split(",")
    if codigo.strip()
}
if any(not re.fullmatch(r"[A-Z]{2}", codigo) for codigo in ALLOWED_COUNTRIES):
    raise RuntimeError("ALLOWED_COUNTRIES deve conter códigos ISO de dois caracteres, separados por vírgulas.")

COUNTRY_HEADER = os.getenv("TRUSTED_COUNTRY_HEADER", "CF-IPCountry").strip()
VPN_SIGNAL_HEADER = os.getenv("TRUSTED_VPN_HEADER", "X-Spa-VPN").strip()


def carregar_proxies_confiaveis():
    return carregar_redes_ip("TRUSTED_PROXY_IPS")


TRUSTED_PROXY_NETWORKS = carregar_proxies_confiaveis()
if IS_PRODUCTION and os.getenv("TRUSTED_PROXY_IP_HEADER") and not TRUSTED_PROXY_NETWORKS:
    logger.warning(
        "TRUSTED_PROXY_IP_HEADER está configurado sem TRUSTED_PROXY_IPS; "
        "o cabeçalho será ignorado e os limites usarão o IP do proxy."
    )
if IS_PRODUCTION and ALLOWED_COUNTRIES and not TRUSTED_PROXY_NETWORKS:
    logger.error(
        "ALLOWED_COUNTRIES está ativo sem TRUSTED_PROXY_IPS; "
        "as requisições serão recusadas até o proxy confiável ser configurado."
    )


def requisicao_veio_de_proxy_confiavel():
    return ip_pertence_as_redes(request.remote_addr or "", TRUSTED_PROXY_NETWORKS)


def obter_ip_cliente():
    """Só confia no IP encaminhado quando a conexão veio de um proxy permitido."""
    ip_par = request.remote_addr or "desconhecido"
    cabecalho_proxy = os.getenv("TRUSTED_PROXY_IP_HEADER", "").strip()
    if not cabecalho_proxy or not TRUSTED_PROXY_NETWORKS:
        return ip_par
    if not ip_pertence_as_redes(ip_par, TRUSTED_PROXY_NETWORKS):
        return ip_par

    encaminhado = request.headers.get(cabecalho_proxy, "").strip()
    try:
        return str(ipaddress.ip_address(encaminhado))
    except ValueError:
        return ip_par


@app.before_request
def proteger_requisicao():
    # Primeira barreira do Flask: conta toda chamada, inclusive OPTIONS,
    # páginas, arquivos estáticos, API e endereços inexistentes.
    ip_cliente = obter_ip_cliente()
    if ip_pertence_as_redes(ip_cliente, BLOCKED_IP_NETWORKS):
        alertar_servidor_telegram(
            "SEGURANÇA",
            "Acesso recusado (Rede/CIDR na lista de bloqueio)",
            {"IP": ip_cliente, "Rota": f"{request.method} {request.path}"},
            chave_cooldown=f"cidr:{ip_cliente}",
            janela_segundos=300,
        )
        return jsonify({
            "error": "Acesso temporariamente indisponível para este endereço.",
            "code": "ip_blocked",
        }), 403

    veio_de_proxy = requisicao_veio_de_proxy_confiavel()
    if ALLOWED_COUNTRIES:
        if not veio_de_proxy or not COUNTRY_HEADER:
            logger.error("Filtro geográfico ativo, mas não foi possível validar a origem do proxy.")
            return jsonify({"error": "Filtro geográfico indisponível; tente mais tarde.", "code": "geo_unavailable"}), 503
        pais = request.headers.get(COUNTRY_HEADER, "").strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", pais) or pais not in ALLOWED_COUNTRIES:
            alertar_servidor_telegram(
                "SEGURANÇA",
                "Acesso bloqueado por filtro geográfico",
                {"IP": ip_cliente, "País": pais or "desconhecido", "Rota": f"{request.method} {request.path}"},
                chave_cooldown=f"geo:{ip_cliente}",
                janela_segundos=300,
            )
            return jsonify({"error": "Acesso disponível somente nos países atendidos.", "code": "country_blocked"}), 403

    sinal_vpn = (
        veio_de_proxy
        and bool(VPN_SIGNAL_HEADER)
        and request.headers.get(VPN_SIGNAL_HEADER, "").strip().lower() in {"1", "true", "yes", "vpn", "anonymizer"}
    )
    if sinal_vpn:
        vpn_allowed, vpn_retry, vpn_strike = vpn_spam_blocker.check(ip_cliente)
        if not vpn_allowed:
            logger.warning(
                "Limite de requisições para VPN excedido: metodo=%s rota=%s reincidencia=%s espera=%ss",
                request.method,
                request.path,
                vpn_strike,
                vpn_retry,
            )
            alertar_servidor_telegram(
                "SEGURANÇA",
                "Tráfego suspeito de VPN bloqueado",
                {
                    "IP": ip_cliente,
                    "Rota": f"{request.method} {request.path}",
                    "Reincidência": f"Strike #{vpn_strike}",
                    "Espera": f"{vpn_retry}s",
                },
                chave_cooldown=f"vpn:{ip_cliente}",
                janela_segundos=120,
            )
            return jsonify({
                "error": "Muitas requisições deste endereço. Aguarde antes de tentar novamente.",
                "code": "vpn_rate_limited",
                "retry_after": vpn_retry,
            }), 429, {"Retry-After": str(vpn_retry)}

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
        alertar_servidor_telegram(
            "SEGURANÇA",
            "IP bloqueado por rajada (Anti-DDoS)",
            {
                "IP": ip_cliente,
                "Rota": f"{request.method} {request.path}",
                "Reincidência": f"Strike #{strike}",
                "Bloqueio": f"{retry}s",
            },
            chave_cooldown=f"burst:{ip_cliente}",
            janela_segundos=120,
        )
        return jsonify({
            "error": "Muitas requisições. Aguarde antes de tentar novamente.",
            "code": "ip_temporarily_blocked",
            "retry_after": retry,
        }), 429, {"Retry-After": str(retry)}

    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None

    if not origem_permitida():
        origem_recebida = request.headers.get("Origin", "")[:200]
        logger.warning("Origem bloqueada em %s: %r", request.path, origem_recebida)
        alertar_servidor_telegram(
            "SEGURANÇA",
            "Tentativa de requisição de origem não autorizada (CORS)",
            {"IP": ip_cliente, "Rota": f"{request.method} {request.path}", "Origin": origem_recebida or "vazio"},
            chave_cooldown=f"origin:{ip_cliente}",
            janela_segundos=180,
        )
        return jsonify({"error": "Origem da requisição não permitida.", "code": "origin_blocked", "versao": VERSAO_BACKEND}), 403

    if request.method != "DELETE" and request.content_length and not request.is_json:
        return jsonify({"error": "Envie os dados no formato JSON."}), 415

    # Permite que /api/logout funcione mesmo se o token CSRF estiver dessincronizado
    if session.get("usuario_email") and request.path not in (PUBLIC_AUTH_PATHS | {"/api/logout"}):
        expected = session.get("csrf_token", "")
        provided = request.headers.get("X-CSRF-Token", "")
        if not expected or not secrets.compare_digest(expected, provided):
            alertar_servidor_telegram(
                "SEGURANÇA",
                "Falha de validação CSRF bloqueada",
                {"IP": ip_cliente, "Rota": f"{request.method} {request.path}", "Usuário": session.get("usuario_email")},
                chave_cooldown=f"csrf:{ip_cliente}",
                janela_segundos=180,
            )
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
        
        # 1. Verifica o limite por IP primeiro (evita esgotamento da memória por e-mails falsos)
        ip_key = f"{request.path}:ip:{ip_cliente}"
        allowed, retry = rate_limiter.allow(ip_key, ip_limit, window)
        if not allowed:
            alertar_servidor_telegram(
                "SEGURANÇA",
                "Limite de tentativas por IP excedido (Força Bruta)",
                {"IP": ip_cliente, "Rota": request.path, "Espera": f"{retry}s"},
                chave_cooldown=f"ratelimit_ip:{request.path}:{ip_cliente}",
                janela_segundos=120,
            )
            return jsonify({"error": "Muitas tentativas. Aguarde antes de tentar novamente."}), 429, {"Retry-After": str(retry)}

        # 2. Só consome slot de e-mail em limitador separado se o IP estiver dentro do limite
        email = normalizar_email(json_body().get("email"))
        if account_limit and email:
            acc_key = f"{request.path}:account:{security.fingerprint(email)}"
            allowed, retry = account_rate_limiter.allow(acc_key, account_limit, window)
            if not allowed:
                alertar_servidor_telegram(
                    "SEGURANÇA",
                    "Limite de tentativas por conta excedido (Força Bruta)",
                    {"IP": ip_cliente, "Rota": request.path, "Conta alvo": email, "Espera": f"{retry}s"},
                    chave_cooldown=f"ratelimit_acc:{request.path}:{email}",
                    janela_segundos=120,
                )
                return jsonify({"error": "Muitas tentativas. Aguarde antes de tentar novamente."}), 429, {"Retry-After": str(retry)}


@app.after_request
def proteger_resposta(response):
    nonce = obter_csp_nonce()
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
    response.headers["X-DNS-Prefetch-Control"] = "off"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Origin-Agent-Cluster"] = "?1"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
        f"form-action 'self'; script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net; "
        "script-src-attr 'none'; "
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


def serializar_agenda(funcao):
    @wraps(funcao)
    def protegida(*args, **kwargs):
        with schedule_lock:
            return funcao(*args, **kwargs)
    return protegida


def serializar_fidelidade(funcao):
    @wraps(funcao)
    def protegida(*args, **kwargs):
        with loyalty_lock:
            return funcao(*args, **kwargs)
    return protegida


def serializar_estoque(funcao):
    @wraps(funcao)
    def protegida(*args, **kwargs):
        with stock_lock:
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


def comparar_senha(usuario, senha):
    """Faz o mesmo trabalho de hash quando a conta não existe, reduzindo enumeração por tempo."""
    senha_salva = (usuario or {}).get("senha") or DUMMY_PASSWORD_HASH
    try:
        senha_correta = check_password_hash(senha_salva, senha)
    except (TypeError, ValueError):
        # Hash legado/corrompido não deve transformar uma falha de login em 500.
        check_password_hash(DUMMY_PASSWORD_HASH, senha)
        return False
    return bool(usuario) and senha_correta


def buscar_usuario(email, campos="*"):
    resposta = db.table("usuarios").select(campos).eq("email", email).limit(1).execute()
    return resposta.data[0] if resposta.data else None


def _ajustar_otp_compacto_se_necessario(payload, otp_compacto):
    if not otp_compacto or not isinstance(payload, dict) or "codigo_otp" not in payload:
        return payload
    valor = payload.get("codigo_otp")
    if isinstance(valor, str) and len(valor) > 6:
        copia = dict(payload)
        copia["codigo_otp"] = otp_compacto
        return copia
    return payload


def atualizar_usuario_compativel(filtros, payload_completo, payload_minimo=None, otp_compacto=None):
    """Atualiza colunas em `usuarios` com fallback transparente caso colunas opcionais ou VARCHAR(6) legado ainda existam."""
    try:
        consulta = db.table("usuarios").update(payload_completo)
        for coluna, valor in filtros.items():
            consulta = consulta.eq(coluna, valor)
        return consulta.execute()
    except Exception as exc:
        texto = str(exc).lower()
        eh_erro_tamanho_otp = ("22001" in texto or "character varying(6)" in texto or "value too long" in texto) and otp_compacto
        eh_erro_coluna = any(col in texto for col in (
            "token_recuperacao_expira_em",
            "token_recuperacao_tentativas",
            "codigo_otp_tentativas",
            "sessao_versao",
            "column",
            "schema cache",
        ))
        if not eh_erro_tamanho_otp and (payload_minimo is None or not eh_erro_coluna):
            raise
        alvo = payload_minimo if (payload_minimo is not None and eh_erro_coluna) else payload_completo
        if eh_erro_tamanho_otp:
            alvo = _ajustar_otp_compacto_se_necessario(alvo, otp_compacto)
        try:
            consulta = db.table("usuarios").update(alvo)
            for coluna, valor in filtros.items():
                consulta = consulta.eq(coluna, valor)
            return consulta.execute()
        except Exception as exc2:
            texto2 = str(exc2).lower()
            if otp_compacto and ("22001" in texto2 or "character varying(6)" in texto2 or "value too long" in texto2):
                alvo_compacto = _ajustar_otp_compacto_se_necessario(
                    payload_minimo if payload_minimo is not None else payload_completo,
                    otp_compacto,
                )
                consulta = db.table("usuarios").update(alvo_compacto)
                for coluna, valor in filtros.items():
                    consulta = consulta.eq(coluna, valor)
                return consulta.execute()
            raise


def usuario_publico(usuario):
    return {
        "id": usuario.get("id"),
        "nome": usuario.get("nome"),
        "email": usuario.get("email"),
        "is_admin": bool(usuario.get("is_admin")),
        "pontos": usuario.get("pontos") or 0,
    }


def _session_ttl_seconds():
    lifetime = app.config.get("PERMANENT_SESSION_LIFETIME")
    if isinstance(lifetime, timedelta):
        return max(60.0, float(lifetime.total_seconds()))
    return 43_200.0


def revogar_sessoes_usuario(email, incrementar_banco=True):
    """Revoga todas as sessões ativas de um usuário em memória e, quando suportado, no banco."""
    email_norm = normalizar_email(email)
    if not email_norm:
        return
    session_revocations.revoke_user(email_norm, ttl_seconds=_session_ttl_seconds())
    if incrementar_banco:
        try:
            usuario = buscar_usuario(email_norm, "*")
            if usuario and "sessao_versao" in usuario:
                nova_versao = int(usuario.get("sessao_versao") or 1) + 1
                db.table("usuarios").update({"sessao_versao": nova_versao}).eq("email", email_norm).execute()
        except Exception:
            logger.debug("Coluna sessao_versao indisponível ao revogar sessões de %s", email_norm)


def usuario_da_sessao():
    email = normalizar_email(session.get("usuario_email"))
    if not email:
        return None

    sid = session.get("sid")
    iat = session.get("iat")
    if not isinstance(sid, str) or not sid or session_revocations.is_revoked(sid, user_key=email, issued_at=iat):
        session.clear()
        return None

    usuario = buscar_usuario(email, "*")
    if (not usuario or not usuario.get("email_verificado")
            or str(usuario.get("id")) != str(session.get("usuario_id"))
            or not secrets.compare_digest(session.get("password_version", ""), security.fingerprint(usuario.get("senha") or ""))):
        session.clear()
        return None

    if usuario.get("sessao_versao") is not None:
        versao_sessao = int(session.get("sessao_versao") or 1)
        if versao_sessao != int(usuario.get("sessao_versao") or 1):
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
        db.table("agendamentos")
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


def normalizar_data_hora(value):
    """Converte datetime vindo do Postgres ou da Data API para a hora local do Spa."""
    if isinstance(value, datetime):
        data = value.astimezone(SPA_TIMEZONE) if value.tzinfo else value
    else:
        texto = str(value or "").strip().replace("Z", "+00:00")
        try:
            data = datetime.fromisoformat(texto)
        except ValueError:
            return texto[:16]
        if data.tzinfo:
            data = data.astimezone(SPA_TIMEZONE)
    return data.strftime("%Y-%m-%dT%H:%M")


def horarios_bloqueio_intervalo(inicio, fim):
    """Expande um bloqueio de uma única data em horas de atendimento."""
    def analisar(valor):
        if not isinstance(valor, str) or not valor.strip():
            raise ValueError("Informe o início e o fim do bloqueio.")
        try:
            data = datetime.fromisoformat(valor.strip().replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("Data ou horário do bloqueio inválido.") from error
        if data.tzinfo:
            data = data.astimezone(SPA_TIMEZONE).replace(tzinfo=None)
        if data.minute or data.second or data.microsecond:
            raise ValueError("O bloqueio deve começar e terminar em horas cheias.")
        return data

    primeiro, ultimo = analisar(inicio), analisar(fim)
    if primeiro.date() != ultimo.date():
        raise ValueError("Cada bloqueio deve corresponder a uma única data. Crie outro bloqueio para outro dia.")
    if primeiro <= datetime.now(SPA_TIMEZONE).replace(tzinfo=None):
        raise ValueError("O bloqueio deve começar em um horário futuro.")
    if ultimo <= primeiro:
        raise ValueError("Escolha um horário final posterior ao horário inicial.")
    slots = []
    horario = primeiro
    while horario < ultimo:
        if horario.weekday() != 0 and OPENING_HOUR <= horario.hour <= LAST_APPOINTMENT_HOUR:
            slots.append(horario.strftime("%Y-%m-%dT%H:%M"))
        horario += timedelta(hours=1)
    if not slots:
        raise ValueError("O período não contém horários de atendimento.")
    return slots


def resposta_concierge_local(mensagem):
    texto = mensagem.lower()
    if re.search(r"\b(agend|reserv|marc)\w*\b", texto):
        return "Claro. Vou ajudar você a escolher uma data e um horário para esse tratamento."
    if any(termo in texto for termo in ("dor", "costas", "tenso", "tensão", "muscular")):
        return "Para aliviar tensões, recomendo a Massagem Terapêutica ou a Massagem com Pedras Quentes. Ambas favorecem o relaxamento muscular."
    if any(termo in texto for termo in ("estresse", "cansaço", "cansado", "ansiedade", "mente")):
        return "Para desacelerar e renovar as energias, a Aromaterapia ou o Banho de Imersão Panaceia são excelentes escolhas."
    if any(termo in texto for termo in ("pele", "rosto", "facial", "esfoliação")):
        return "Para revitalização da pele, recomendo a Limpeza de Pele Profunda ou o Ritual Facial Anti-aging."
    return "Explore nosso catálogo de bem-estar. Posso sugerir uma experiência para dores, estresse, cansaço ou cuidados faciais."


def enviar_email_transacional(destinatario, assunto, conteudo_html):
    api_key = _limpar_valor_env(os.getenv("BREVO_API_KEY") or "")
    if not api_key:
        logger.error("BREVO_API_KEY não configurada; e-mail não enviado.")
        return False

    payload = {
        "sender": {
            "name": _limpar_valor_env(os.getenv("BREVO_SENDER_NAME") or "Spa Panaceia") or "Spa Panaceia",
            "email": _limpar_valor_env(os.getenv("BREVO_SENDER_EMAIL") or "spapanaceia@gmail.com") or "spapanaceia@gmail.com",
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
        if not resposta.ok:
            sufixo_chave = f"***{api_key[-4:]}" if len(api_key) >= 4 else "***"
            detalhe_brevo = (resposta.text or "")[:200]
            logger.error(
                "Brevo recusou envio para %s (HTTP %s, key_len=%d, key_end=%s): %s",
                destinatario,
                resposta.status_code,
                len(api_key),
                sufixo_chave,
                detalhe_brevo,
            )
            return False
        return True
    except requests.RequestException:
        logger.exception("Falha ao enviar e-mail transacional para %s", destinatario)
        return False


class BoundedTaskExecutor:
    """Executor de threads controlado com fila máxima limitada, isolamento de falhas e suporte pós-fork."""

    def __init__(self, max_workers=3, max_pending=50, thread_name_prefix="spa-worker"):
        if max_workers <= 0 or max_pending <= 0:
            raise ValueError("max_workers e max_pending devem ser inteiros positivos.")
        self.max_workers = int(max_workers)
        self.max_pending = int(max_pending)
        self._thread_name_prefix = str(thread_name_prefix or "spa-worker")
        self._capacity = self.max_workers + self.max_pending
        self._lock = Lock()
        self._in_flight = 0
        self._closed = False
        self._pid = os.getpid()
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix=self._thread_name_prefix,
        )

    def _ensure_fork_safe_locked(self):
        pid_atual = os.getpid()
        if pid_atual != self._pid:
            # Em workers criados via fork (ex.: Gunicorn no Linux/ZimaOS), threads
            # do processo pai não existem no processo filho; recria o pool de forma limpa.
            self._pid = pid_atual
            self._in_flight = 0
            self._closed = False
            self._executor = ThreadPoolExecutor(
                max_workers=self.max_workers,
                thread_name_prefix=self._thread_name_prefix,
            )

    @property
    def in_flight(self):
        with self._lock:
            self._ensure_fork_safe_locked()
            return self._in_flight

    def submit(self, fn, *args, **kwargs):
        with self._lock:
            self._ensure_fork_safe_locked()
            if self._closed or self._in_flight >= self._capacity:
                logger.warning(
                    "Fila de tarefas em segundo plano cheia (%d/%d); tarefa recusada: %s",
                    self._in_flight,
                    self._capacity,
                    getattr(fn, "__name__", str(fn)),
                )
                return False
            self._in_flight += 1
            executor_atual = self._executor

        def _wrapped():
            try:
                fn(*args, **kwargs)
            except Exception:
                logger.exception(
                    "Exceção tratada em tarefa de segundo plano (%s)",
                    getattr(fn, "__name__", str(fn)),
                )
            finally:
                with self._lock:
                    self._in_flight = max(0, self._in_flight - 1)

        try:
            executor_atual.submit(_wrapped)
            return True
        except RuntimeError:
            with self._lock:
                self._in_flight = max(0, self._in_flight - 1)
            logger.warning("Executor encerrado ao tentar submeter tarefa em segundo plano.")
            return False

    def shutdown(self, wait=False):
        with self._lock:
            self._closed = True
            executor_atual = self._executor
        executor_atual.shutdown(wait=wait, cancel_futures=not wait)


# Executores controlados para e-mails e notificações (evita criação indiscriminada de threads)
email_executor = BoundedTaskExecutor(max_workers=3, max_pending=50, thread_name_prefix="spa-email")
notificacao_executor = BoundedTaskExecutor(max_workers=2, max_pending=30, thread_name_prefix="spa-notify")
atexit.register(email_executor.shutdown, False)
atexit.register(notificacao_executor.shutdown, True)

# Anti-flood para o Telegram (evita que ataques de rajada travem o bot)
telegram_limiter = RateLimiter(max_keys=2_000)
_telegram_missing_config_logged = False
_TELEGRAM_TOKEN_PATTERN = re.compile(r"bot[0-9]{6,15}:[A-Za-z0-9_-]+", re.IGNORECASE)


def iniciar_email_em_segundo_plano(destinatario, assunto, conteudo_html):
    """Entrega alertas operacionais em executor controlado sem atrasar a confirmação da agenda."""
    if not destinatario:
        return False
    return email_executor.submit(enviar_email_transacional, destinatario, assunto, conteudo_html)


def obter_config_telegram():
    """Retorna (token, chat_id) sanitizados para suportar tanto python-dotenv quanto Docker --env-file."""
    token = _limpar_valor_env(os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN") or "")
    chat_id = _limpar_valor_env(os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT") or "")
    if token.lower().startswith("bot") and ":" in token[3:]:
        token = token[3:].strip()
    return token, chat_id


def _mascarar_segredos_telegram(texto, token=None):
    """Remove tokens do Telegram de mensagens de erro/URLs antes de gravar nos logs."""
    msg = str(texto or "")
    if token:
        msg = msg.replace(token, "<REDACTED_TOKEN>")
    msg = _TELEGRAM_TOKEN_PATTERN.sub("bot<REDACTED_TOKEN>", msg)
    return msg


def _mascarar_chat_id(chat_id):
    texto = str(chat_id or "").strip()
    if len(texto) <= 4:
        return "***"
    return f"***{texto[-4:]}"


def enviar_alerta_telegram(mensagem_html):
    """Envia alerta para o Telegram via HTTP com timeout, retry de falhas transitórias e logs seguros."""
    global _telegram_missing_config_logged
    token, chat_id = obter_config_telegram()
    if not token or not chat_id:
        if not _telegram_missing_config_logged:
            _telegram_missing_config_logged = True
            telegram_logger.warning(
                "Notificações do Telegram desativadas: TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID ausente no ambiente."
            )
        return False

    if ":" not in token:
        telegram_logger.warning("Formato inválido em TELEGRAM_BOT_TOKEN (esperado '<bot_id>:<segredo>').")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": str(mensagem_html or "")[:4000],  # Limite oficial de caracteres do Telegram
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    max_tentativas = 2
    for tentativa in range(1, max_tentativas + 1):
        try:
            resposta = requests.post(url, json=payload, timeout=(5, 10))
            dados_json = {}
            try:
                dados_json = resposta.json() if resposta.content else {}
            except ValueError:
                dados_json = {}

            if resposta.ok and dados_json.get("ok") is True:
                msg_id = (dados_json.get("result") or {}).get("message_id")
                telegram_logger.info(
                    "Alerta entregue ao Telegram com sucesso (chat=%s, message_id=%s).",
                    _mascarar_chat_id(chat_id),
                    msg_id,
                )
                return True

            status_http = resposta.status_code
            descricao = _mascarar_segredos_telegram(
                dados_json.get("description") or resposta.text[:200] or "sem descrição",
                token,
            )

            # Retry apenas em erro 5xx ou 429 com retry_after curto
            if tentativa < max_tentativas and (status_http >= 500 or status_http == 429):
                espera = min(int((dados_json.get("parameters") or {}).get("retry_after") or 1), 3)
                time.sleep(espera)
                continue

            telegram_logger.warning(
                "Falha ao entregar alerta ao Telegram (HTTP %s, chat=%s): %s",
                status_http,
                _mascarar_chat_id(chat_id),
                descricao,
            )
            return False
        except requests.RequestException as exc:
            erro_seguro = _mascarar_segredos_telegram(exc, token)
            if tentativa < max_tentativas:
                time.sleep(0.5)
                continue
            telegram_logger.warning(
                "Erro de conexão ao enviar alerta ao Telegram (%s, chat=%s): %s",
                type(exc).__name__,
                _mascarar_chat_id(chat_id),
                erro_seguro,
            )
            return False
        except Exception as exc:
            erro_seguro = _mascarar_segredos_telegram(exc, token)
            telegram_logger.warning(
                "Erro inesperado ao enviar alerta ao Telegram (%s): %s",
                type(exc).__name__,
                erro_seguro,
            )
            return False
    return False


def alertar_servidor_telegram(categoria, titulo, detalhes=None, chave_cooldown=None, janela_segundos=60, sincrono=False):
    """Central de telemetria: envia alertas de qualquer parte do servidor com filtro anti-spam."""
    global _telegram_missing_config_logged
    token, chat_id = obter_config_telegram()
    if not (token and chat_id):
        if not _telegram_missing_config_logged and APP_ENVIRONMENT != "test":
            _telegram_missing_config_logged = True
            telegram_logger.warning(
                "Alerta '%s' ignorado: defina TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID no ambiente/--env-file.",
                titulo,
            )
        return False

    if chave_cooldown:
        permitido, _ = telegram_limiter.allow(f"tg:{chave_cooldown}", 1, janela_segundos)
        if not permitido:
            return False

    icones = {
        "SEGURANÇA": "🛡️",
        "ERRO CRÍTICO": "🚨",
        "AGENDA": "📅",
        "ESTOQUE": "📦",
        "SISTEMA": "⚙️",
        "CONTA": "👤",
        "FIDELIDADE": "🎁",
        "AVALIAÇÃO": "⭐",
        "PRIVACIDADE": "🔒",
    }
    icone = icones.get(str(categoria).upper(), "🔔")
    agora_str = datetime.now(SPA_TIMEZONE).strftime("%d/%m/%Y %H:%M:%S")

    linhas = [
        f"{icone} <b>SPA PANACEIA · {html.escape(str(categoria).upper())}</b>",
        f"<b>{html.escape(str(titulo))}</b>",
        f"<i>Horário: {agora_str}</i>\n",
    ]
    for rotulo, valor in (detalhes or {}).items():
        if valor is not None and str(valor).strip() != "":
            linhas.append(f"• <b>{html.escape(str(rotulo))}:</b> {html.escape(str(valor)[:500])}")

    mensagem_final = "\n".join(linhas)
    if sincrono:
        return enviar_alerta_telegram(mensagem_final)
    return notificacao_executor.submit(enviar_alerta_telegram, mensagem_final)


class TelegramErrorLogHandler(logging.Handler):
    """Intercepta qualquer logger.error() ou logger.exception() do servidor e manda pro Telegram."""

    def emit(self, record):
        if record.levelno < logging.ERROR:
            return
        if record.name == "spa.telegram" or record.funcName == "enviar_alerta_telegram":
            return
        try:
            caminho_rota = request.path if request else "background/sistema"
            metodo_http = request.method if request else "-"
            ip_origem = obter_ip_cliente() if request else "servidor"
        except RuntimeError:
            caminho_rota, metodo_http, ip_origem = "background/sistema", "-", "servidor"

        resumo_excecao = ""
        if record.exc_info and record.exc_info[1]:
            resumo_excecao = f"{type(record.exc_info[1]).__name__}: {record.exc_info[1]}"

        chave = f"err:{record.module}:{record.lineno}"
        alertar_servidor_telegram(
            "ERRO CRÍTICO",
            record.getMessage(),
            {
                "Rota": f"{metodo_http} {caminho_rota}",
                "IP": ip_origem,
                "Local": f"{record.filename}:{record.lineno}",
                "Exceção": resumo_excecao or "Consulte o terminal do servidor",
            },
            chave_cooldown=chave,
            janela_segundos=120,  # Mesmo erro só avisa 1x a cada 2 minutos
        )


# Pluga o interceptador de erros diretamente no logger principal do servidor
if not any(isinstance(h, TelegramErrorLogHandler) for h in logger.handlers):
    _tg_handler = TelegramErrorLogHandler(level=logging.ERROR)
    logger.addHandler(_tg_handler)


def email_equipe_agenda(assunto, titulo, detalhes):
    # Dispara sempre no Telegram
    alerta_tg = alertar_servidor_telegram("AGENDA", titulo, detalhes)

    destinatario = normalizar_email(
        os.getenv("SPA_ALERT_EMAIL") or os.getenv("SPA_TEAM_EMAIL") or os.getenv("BREVO_SENDER_EMAIL")
    )
    if not destinatario:
        return alerta_tg
    corpo = "".join(
        f"<li style='margin:8px 0'><strong>{html.escape(str(rotulo))}:</strong> {html.escape(str(valor))}</li>"
        for rotulo, valor in detalhes.items()
    )
    conteudo = (
        "<div style='font-family:Arial,sans-serif;max-width:600px;margin:auto;padding:28px;color:#30223d'>"
        f"<p style='color:#6b21a8;font-weight:700;letter-spacing:.08em'>SPA PANACEIA · AGENDA</p>"
        f"<h2>{html.escape(titulo)}</h2><ul style='padding-left:20px;line-height:1.6'>{corpo}</ul>"
        "<p style='color:#756a7d;font-size:13px'>Acesse o painel gerencial para consultar ou atualizar a reserva.</p></div>"
    )
    alerta_email = iniciar_email_em_segundo_plano(destinatario, assunto, conteudo)
    return alerta_tg or alerta_email


def registrar_auditoria_agenda(
    agendamento_id, acao, ator, data_anterior=None, data_nova=None,
    status_anterior=None, status_novo=None,
):
    """Registra alterações de agenda sem permitir que falha de auditoria reverta a reserva."""
    try:
        db.table("agenda_auditoria").insert({
            "agendamento_id": int(agendamento_id),
            "ator_email": normalizar_email((ator or {}).get("email")) or "desconhecido",
            "ator_tipo": "admin" if (ator or {}).get("is_admin") else "cliente",
            "acao": str(acao)[:40],
            "data_anterior": str(data_anterior)[:40] if data_anterior else None,
            "data_nova": str(data_nova)[:40] if data_nova else None,
            "status_anterior": str(status_anterior)[:40] if status_anterior else None,
            "status_novo": str(status_novo)[:40] if status_novo else None,
        }).execute()
    except Exception:
        logger.exception("Não foi possível gravar o evento de auditoria da agenda")


def notificar_lista_espera_horario(data_atendimento):
    """Reivindica cada aviso com compare-and-set e notifica quem pediu o horário usando pool controlado."""
    try:
        chave_data = str(data_atendimento).replace(" ", "T")[:16]
        bloqueios = (
            db.table("agenda_bloqueios").select("id")
            .eq("data_atendimento", chave_data).limit(1).execute().data or []
        )
        if bloqueios:
            return 0
        entradas = (
            db.table("agenda_lista_espera")
            .select("id, email_cliente, servico_id")
            .eq("data_atendimento", chave_data)
            .eq("status", "Aguardando")
            .limit(100)
            .execute().data or []
        )
        if not entradas:
            return 0

        # Pré-carrega os nomes dos serviços uma única vez por lote para não abrir
        # conexões redundantes ao banco dentro de cada worker de envio.
        nomes_servicos = {}
        ids_servicos = {
            entrada.get("servico_id")
            for entrada in entradas
            if entrada.get("servico_id") is not None
        }
        for sid in ids_servicos:
            try:
                linhas_srv = db.table("servico").select("tipo").eq("id", sid).limit(1).execute().data or []
                if linhas_srv and linhas_srv[0].get("tipo"):
                    nomes_servicos[sid] = linhas_srv[0]["tipo"]
            except Exception:
                logger.warning("Não foi possível consultar o nome do serviço %s para lista de espera", sid)

        agendados = 0
        for entrada in entradas:
            reivindicada = (
                db.table("agenda_lista_espera")
                .update({"status": "Notificando"})
                .eq("id", entrada["id"])
                .eq("status", "Aguardando")
                .execute().data or []
            )
            if not reivindicada:
                continue

            nome_servico_item = nomes_servicos.get(entrada.get("servico_id"), "um tratamento")

            def entregar_aviso(item=entrada, data=chave_data, nome_servico=nome_servico_item):
                try:
                    data_legivel = datetime.fromisoformat(data).strftime("%d/%m/%Y às %H:%M")
                    sucesso = enviar_email_transacional(
                        item["email_cliente"],
                        "Um horário da sua lista de espera ficou livre — Spa Panaceia",
                        "<div style='font-family:Arial,sans-serif;max-width:600px;margin:auto;padding:28px;color:#30223d'>"
                        "<p style='color:#6b21a8;font-weight:700;letter-spacing:.08em'>SPA PANACEIA</p>"
                        "<h2>O horário que você queria ficou livre</h2>"
                        f"<p>{html.escape(str(nome_servico))} · {html.escape(data_legivel)}</p>"
                        "<p>Entre no site para conferir a disponibilidade e reservar. O horário é confirmado por ordem de agendamento.</p></div>",
                    )
                    estado_final = "Notificado" if sucesso else "Aguardando"
                    atualizacao = {"status": estado_final}
                    if sucesso:
                        atualizacao["notificado_em"] = datetime.now(timezone.utc).isoformat()
                    db.table("agenda_lista_espera").update(atualizacao).eq("id", item["id"]).eq("status", "Notificando").execute()
                except Exception:
                    logger.exception("Falha ao entregar aviso de lista de espera")
                    try:
                        db.table("agenda_lista_espera").update({"status": "Aguardando"}).eq("id", item["id"]).eq("status", "Notificando").execute()
                    except Exception:
                        logger.exception("Não foi possível liberar novamente o aviso de lista de espera")

            if email_executor.submit(entregar_aviso):
                agendados += 1
            else:
                try:
                    db.table("agenda_lista_espera").update({"status": "Aguardando"}).eq("id", entrada["id"]).eq("status", "Notificando").execute()
                except Exception:
                    logger.exception("Não foi possível reverter item da lista de espera quando a fila estava cheia")
        return agendados
    except Exception:
        logger.exception("Não foi possível processar a lista de espera do horário liberado")
        return 0


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


def obter_url_publica_canonica():
    """Retorna apenas uma URL HTTPS explicitamente configurada e confiável."""
    valor = os.getenv("PUBLIC_SITE_URL", "").strip()
    if not valor:
        return None
    try:
        partes = urlsplit(valor)
        host = (partes.hostname or "").lower()
        if (
            partes.scheme.lower() != "https"
            or not host
            or host not in app.config["TRUSTED_HOSTS"]
            or partes.username
            or partes.password
            or partes.path not in {"", "/"}
            or partes.query
            or partes.fragment
            or partes.port not in {None, 443}
        ):
            return None
    except (TypeError, ValueError):
        return None
    return f"https://{host}"


def template_publico(nome):
    """Prefere o HTML minificado gerado pelo build, mantendo o fonte como fallback."""
    raiz_templates = Path(app.root_path).parent / "templates"
    fonte = raiz_templates / nome
    minificado = fonte.with_name(f"{fonte.stem}.min{fonte.suffix}")
    escolhido = minificado.name if minificado.is_file() else fonte.name
    return render_template(escolhido, site_url=obter_url_publica_canonica(), csp_nonce=obter_csp_nonce())


def resolver_arquivo_frontend(configurado):
    """Valida e resolve um arquivo de frontend autorizado dentro de BASE_DIR."""
    if not isinstance(configurado, str):
        return None
    limpo = configurado.strip()
    if not limpo:
        return None
    if (
        "/" in limpo
        or "\\" in limpo
        or ".." in limpo
        or Path(limpo).name != limpo
        or not limpo.endswith(".html")
        or limpo not in ALLOWED_FRONTEND_FILES
    ):
        return None
    try:
        base_resolvida = BASE_DIR.resolve()
        candidato = (BASE_DIR / limpo).resolve()
    except OSError:
        return None
    if candidato.parent != base_resolvida or not candidato.is_file():
        return None
    return candidato


@app.route("/")
def pagina_principal():
    configurado = os.getenv("SPA_FRONTEND_FILE")
    if configurado is not None and configurado.strip() != "":
        escolhido = resolver_arquivo_frontend(configurado)
        if escolhido is None:
            logger.warning("SPA_FRONTEND_FILE rejeitado por política de segurança: %r", configurado[:120])
            return jsonify({
                "error": "Arquivo de frontend não autorizado.",
                "code": "invalid_frontend_file",
            }), 403
    else:
        nome = "spa-panaceia-profissional.html" if (BASE_DIR / "spa-panaceia-profissional.html").is_file() else "servicos.html"
        fonte = BASE_DIR / nome
        minificado = fonte.with_name(f"{fonte.stem}.min{fonte.suffix}")
        escolhido = minificado if minificado.is_file() else fonte

    conteudo = escolhido.read_text(encoding="utf-8")
    site_url = obter_url_publica_canonica()
    metadados = ""
    if site_url:
        url_escape = html.escape(site_url, quote=True)
        metadados = (
            f'<link rel="canonical" href="{url_escape}/">'
            f'<meta property="og:url" content="{url_escape}/">'
            f'<meta property="og:image" content="{url_escape}/static/img/logo_panaceia.png">'
            '<meta property="og:image:alt" content="Símbolo do Spa Panaceia">'
            f'<meta name="twitter:image" content="{url_escape}/static/img/logo_panaceia.png">'
        )
    conteudo = conteudo.replace("__SPA_CANONICAL_METADATA__", metadados)
    conteudo = conteudo.replace("__CSP_NONCE__", obter_csp_nonce())
    resposta = make_response(conteudo)
    resposta.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resposta


@app.route("/cadastro")
def pagina_cadastro():
    resposta = make_response(template_publico("index.html"))
    resposta.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resposta


@app.route("/privacidade")
def pagina_privacidade():
    return template_publico("privacidade.html")


@app.route("/robots.txt")
def robots_txt():
    linhas = ["User-agent: *", "Disallow: /api/", "Disallow: /cadastro", "Disallow: /cadastrar"]
    site_url = obter_url_publica_canonica()
    if site_url:
        linhas.append(f"Sitemap: {site_url}/sitemap.xml")
    return Response("\n".join(linhas) + "\n", mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    site_url = obter_url_publica_canonica()
    if not site_url:
        return Response(
            "PUBLIC_SITE_URL precisa apontar para o domínio canônico HTTPS confiável.",
            status=503,
            mimetype="text/plain",
        )
    paginas = ("/", "/privacidade")
    itens = "".join(
        f"<url><loc>{html.escape(site_url + caminho, quote=True)}</loc></url>"
        for caminho in paginas
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{itens}</urlset>"
    )
    return Response(xml, mimetype="application/xml")


@app.route("/favicon.ico")
def favicon():
    return redirect("/app-icon.svg", code=308)


@app.route("/api/health", methods=["GET"])
def verificar_saude():
    """Confirma aplicação e informa qual banco está atendendo as requisições."""
    tg_token, tg_chat = obter_config_telegram()
    telegram_configurado = bool(tg_token and ":" in tg_token and tg_chat)
    try:
        database = db.healthcheck()
        restricao_agenda = database.get("booking_constraint")
        return jsonify({
            "status": "ok",
            "versao": VERSAO_BACKEND,
            "database": "ok",
            "database_backend": database.get("backend"),
            "databases": database.get("backends", {}),
            # A Data API do Supabase não expõe catálogo de triggers/índices.
            # null significa "não verificável por este endpoint", não "ausente".
            "restricao_agenda": restricao_agenda,
            "restricao_agenda_verificavel": restricao_agenda is not None,
            "telegram_configurado": telegram_configurado,
        }), 200
    except Exception:
        logger.exception("Falha no healthcheck dos bancos configurados")
        return jsonify({
            "status": "degraded",
            "versao": VERSAO_BACKEND,
            "database": "indisponivel",
            "telegram_configurado": telegram_configurado,
        }), 503


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
    resposta = Response("""<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 512 512\"><defs><linearGradient id=\"g\" x1=\"0\" y1=\"0\" x2=\"1\" y2=\"1\"><stop stop-color=\"#8b35e8\"/><stop offset=\"1\" stop-color=\"#3d087d\"/></linearGradient></defs><rect width=\"512\" height=\"512\" rx=\"116\" fill=\"url(#g)\"/><path d=\"M256 112c27 73 71 117 144 144-73 27-117 71-144 144-27-73-71-117-144-144 73-27 117-71 144-144Z\" fill=\"#fff3d8\"/><circle cx=\"256\" cy=\"256\" r=\"38\" fill=\"#6a11cb\"/></svg>""", mimetype="image/svg+xml")
    resposta.headers["Cache-Control"] = "public, max-age=604800"
    return resposta


@app.errorhandler(404)
def pagina_nao_encontrada(_error):
    if request.path.startswith("/api/"):
        return jsonify({
            "error": "Rota não encontrada.",
            "code": "route_not_found",
        }), 404
    resposta = make_response(template_publico("404.html"), 404)
    resposta.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resposta


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

    resposta_sucesso = {
        "message": "Cadastro realizado! Digite o código de 6 dígitos enviado ao seu e-mail para ativar a conta.",
        "email": email,
    }

    try:
        codigo = gerar_otp()
        codigo_hash = security.hash_otp(email, codigo)
        codigo_hash_compacto = security.hash_otp_compact(email, codigo)
        expiracao = (datetime.now(timezone.utc) + timedelta(minutes=OTP_TTL_MINUTES)).isoformat()
        senha_hash = generate_password_hash(senha, method="pbkdf2:sha256")
        deve_enviar_email = False

        with lock_conta(email):
            existente = buscar_usuario(email, "id, nome, email, email_verificado")
            if existente is None:
                payload_novo = {
                    "nome": nome,
                    "email": email,
                    "senha": senha_hash,
                    "email_verificado": False,
                    "codigo_otp": codigo_hash,
                    "codigo_expira_em": expiracao,
                    "is_admin": False,
                }
                try:
                    db.table("usuarios").insert(payload_novo).execute()
                    challenge_attempt_limiter.reset_key(f"otp_try:{security.fingerprint(email)}")
                    deve_enviar_email = True
                except Exception as insert_err:
                    texto_err = str(insert_err).lower()
                    if "22001" in texto_err or "character varying(6)" in texto_err or "value too long" in texto_err:
                        payload_novo["codigo_otp"] = codigo_hash_compacto
                        try:
                            db.table("usuarios").insert(payload_novo).execute()
                            challenge_attempt_limiter.reset_key(f"otp_try:{security.fingerprint(email)}")
                            deve_enviar_email = True
                        except Exception as retry_err:
                            texto_retry = str(retry_err).lower()
                            if "23505" not in texto_retry and "duplicate key" not in texto_retry and "unique" not in texto_retry:
                                raise
                            existente = buscar_usuario(email, "id, nome, email, email_verificado")
                    elif "23505" in texto_err or "duplicate key" in texto_err or "unique" in texto_err:
                        existente = buscar_usuario(email, "id, nome, email, email_verificado")
                    else:
                        raise

            if existente is not None and not deve_enviar_email:
                if existente.get("email_verificado"):
                    # Conta já verificada: nunca sobrescreve nem revela a existência por 409.
                    deve_enviar_email = False
                else:
                    # Recuperação segura de pré-cadastro não verificado: substitui dados
                    # temporários, gera novo OTP e invalida códigos anteriores atomicamente.
                    atualizados = (
                        atualizar_usuario_compativel(
                            {"email": email, "email_verificado": False},
                            {
                                "nome": nome,
                                "senha": senha_hash,
                                "codigo_otp": codigo_hash,
                                "codigo_expira_em": expiracao,
                                "codigo_otp_tentativas": 0,
                            },
                            {
                                "nome": nome,
                                "senha": senha_hash,
                                "codigo_otp": codigo_hash,
                                "codigo_expira_em": expiracao,
                            },
                            otp_compacto=codigo_hash_compacto,
                        ).data
                        or []
                    )
                    if atualizados:
                        challenge_attempt_limiter.reset_key(f"otp_try:{security.fingerprint(email)}")
                        deve_enviar_email = True

        # Chamada HTTP externa sempre fora do lock
        if deve_enviar_email:
            if not enviar_email_transacional(email, "Seu código de ativação — Spa Panaceia", email_de_ativacao(nome, codigo)):
                logger.warning("Conta criada/atualizada, mas e-mail de ativação não foi entregue para %s", email)

        return jsonify(resposta_sucesso), 201
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

    erro_uniforme = {"error": "Código de verificação inválido ou expirado. Solicite um novo código."}
    chave_tentativas = f"otp_try:{security.fingerprint(email)}"

    try:
        with lock_conta(email):
            usuario = buscar_usuario(email)
            if not usuario or usuario.get("email_verificado"):
                # Igualamos trabalho criptográfico e retornamos 400 uniforme para
                # não revelar se o e-mail existe ou se já foi verificado.
                security.verify_otp("v2:" + "0" * 64, email, codigo)
                return jsonify(erro_uniforme), 400

            codigo_salvo = str(usuario.get("codigo_otp") or "")
            expiracao = usuario.get("codigo_expira_em")
            if not codigo_salvo or not expiracao:
                return jsonify(erro_uniforme), 400

            expirado = datetime.now(timezone.utc) > datetime.fromisoformat(str(expiracao).replace("Z", "+00:00"))
            if expirado:
                atualizar_usuario_compativel(
                    {"email": email, "email_verificado": False},
                    {"codigo_otp": None, "codigo_expira_em": None, "codigo_otp_tentativas": 0},
                    {"codigo_otp": None, "codigo_expira_em": None},
                )
                challenge_attempt_limiter.reset_key(chave_tentativas)
                return jsonify({"error": "Código expirado. Solicite um novo código."}), 400

            tentativas_banco = int(usuario.get("codigo_otp_tentativas") or 0)
            permitido_mem, _ = challenge_attempt_limiter.allow(chave_tentativas, OTP_MAX_ATTEMPTS, OTP_TTL_MINUTES * 60)
            if not permitido_mem or tentativas_banco >= OTP_MAX_ATTEMPTS:
                atualizar_usuario_compativel(
                    {"email": email, "email_verificado": False},
                    {"codigo_otp": None, "codigo_expira_em": None, "codigo_otp_tentativas": 0},
                    {"codigo_otp": None, "codigo_expira_em": None},
                )
                return jsonify({"error": "Limite de tentativas excedido. Solicite um novo código."}), 400

            if not security.verify_otp(codigo_salvo, email, codigo):
                novas_tentativas = tentativas_banco + 1
                if novas_tentativas >= OTP_MAX_ATTEMPTS:
                    atualizar_usuario_compativel(
                        {"email": email, "email_verificado": False},
                        {"codigo_otp": None, "codigo_expira_em": None, "codigo_otp_tentativas": 0},
                        {"codigo_otp": None, "codigo_expira_em": None},
                    )
                    return jsonify({"error": "Limite de tentativas excedido. Solicite um novo código."}), 400
                if "codigo_otp_tentativas" in usuario:
                    atualizar_usuario_compativel(
                        {"email": email, "email_verificado": False},
                        {"codigo_otp_tentativas": novas_tentativas},
                        {},
                    )
                return jsonify({"error": "Código de verificação incorreto."}), 400

            atualizados = (
                atualizar_usuario_compativel(
                    {"email": email, "email_verificado": False, "codigo_otp": codigo_salvo},
                    {"email_verificado": True, "codigo_otp": None, "codigo_expira_em": None, "codigo_otp_tentativas": 0},
                    {"email_verificado": True, "codigo_otp": None, "codigo_expira_em": None},
                ).data
                or []
            )
            if not atualizados:
                return jsonify(erro_uniforme), 400
            challenge_attempt_limiter.reset_key(chave_tentativas)
        alertar_servidor_telegram(
            "CONTA",
            "Nova conta de cliente ativada",
            {"Cliente": usuario.get("nome") or "Cliente", "E-mail": email},
            chave_cooldown=f"acct_active:{email}",
            janela_segundos=30,
        )
        return jsonify({"message": "E-mail verificado com sucesso!"}), 200
    except (TypeError, ValueError):
        logger.exception("Data de expiração de OTP inválida")
        return jsonify({"error": "Não foi possível validar o código."}), 500
    except Exception:
        logger.exception("Erro ao validar OTP")
        return jsonify({"error": "Erro interno ao validar o código."}), 500


@app.route("/api/reenviar-codigo", methods=["POST"])
def reenviar_codigo():
    """Gera um novo código para contas ainda não ativadas, sem revelar cadastros."""
    email = normalizar_email(json_body().get("email"))
    if not email:
        return jsonify({"error": "Informe um e-mail válido."}), 400

    resposta_padrao = {"message": "Se a conta estiver aguardando ativação, um novo código será enviado."}
    try:
        codigo = gerar_otp()
        codigo_hash = security.hash_otp(email, codigo)
        codigo_hash_compacto = security.hash_otp_compact(email, codigo)
        expiracao = (datetime.now(timezone.utc) + timedelta(minutes=OTP_TTL_MINUTES)).isoformat()
        nome_cliente = "Cliente"
        deve_enviar = False

        with lock_conta(email):
            usuario = buscar_usuario(email, "id, nome, email_verificado")
            if usuario and not usuario.get("email_verificado"):
                nome_cliente = usuario.get("nome") or "Cliente"
                atualizar_usuario_compativel(
                    {"id": usuario["id"], "email_verificado": False},
                    {"codigo_otp": codigo_hash, "codigo_expira_em": expiracao, "codigo_otp_tentativas": 0},
                    {"codigo_otp": codigo_hash, "codigo_expira_em": expiracao},
                    otp_compacto=codigo_hash_compacto,
                )
                challenge_attempt_limiter.reset_key(f"otp_try:{security.fingerprint(email)}")
                deve_enviar = True

        # Chamada HTTP externa realizada fora do lock
        if deve_enviar:
            if not enviar_email_transacional(email, "Novo código de ativação — Spa Panaceia", email_de_ativacao(nome_cliente, codigo)):
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
        if not comparar_senha(usuario, senha):
            return jsonify({"error": "E-mail ou senha incorretos."}), 401
        if not usuario.get("email_verificado"):
            return jsonify({"error": "Conta não verificada. Verifique seu e-mail antes de entrar."}), 403

        session.clear()
        session.permanent = True
        session["usuario_id"] = usuario.get("id")
        session["usuario_email"] = usuario.get("email")
        session["password_version"] = security.fingerprint(usuario.get("senha") or "")
        session["sid"] = secrets.token_urlsafe(24)
        session["iat"] = time.time()
        if usuario.get("sessao_versao") is not None:
            session["sessao_versao"] = int(usuario.get("sessao_versao") or 1)

        if usuario.get("is_admin"):
            alertar_servidor_telegram(
                "SEGURANÇA",
                "Acesso administrativo iniciado",
                {"Administrador": usuario.get("email"), "IP": obter_ip_cliente()},
                chave_cooldown=f"admin_login:{usuario.get('email')}:{obter_ip_cliente()}",
                janela_segundos=60,
            )

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
    sid = session.get("sid")
    email = normalizar_email(session.get("usuario_email"))
    dados = json_body()
    revogar_todas = bool(dados.get("revogar_todas") or dados.get("all_sessions"))

    if isinstance(sid, str) and sid:
        session_revocations.revoke_sid(sid, ttl_seconds=_session_ttl_seconds())
    if revogar_todas and email:
        revogar_sessoes_usuario(email, incrementar_banco=True)

    session.clear()
    return jsonify({"message": "Sessão encerrada com sucesso."}), 200


@app.route("/api/esqueci-senha", methods=["POST"])
def esqueci_senha():
    dados = json_body()
    email = normalizar_email(dados.get("email"))
    if not email:
        return jsonify({"error": "Informe um e-mail válido."}), 400

    codigo = gerar_otp()
    expira_dt = datetime.now(timezone.utc) + timedelta(minutes=OTP_TTL_MINUTES)
    expira_ts = int(expira_dt.timestamp())
    registro_token, nonce_id = security.encode_recovery_record(email, codigo, expira_ts)
    resposta_padrao = {
        "message": "Se o e-mail estiver cadastrado, você receberá o código de recuperação.",
        "recuperacao_token": security.issue_recovery(email, codigo, nonce_id=nonce_id),
    }
    try:
        deve_enviar = False
        with lock_conta(email):
            if buscar_usuario(email, "id"):
                atualizar_usuario_compativel(
                    {"email": email},
                    {
                        "token_recuperacao": registro_token,
                        "token_recuperacao_expira_em": expira_dt.isoformat(),
                        "token_recuperacao_tentativas": 0,
                    },
                    {"token_recuperacao": registro_token},
                )
                challenge_attempt_limiter.reset_key(f"rec_try:{security.fingerprint(email)}")
                deve_enviar = True

        # Chamada HTTP externa executada fora do lock
        if deve_enviar:
            enviar_email_transacional(email, "Código de Recuperação de Senha", email_de_recuperacao(codigo))
        return jsonify(resposta_padrao), 200
    except Exception:
        logger.exception("Erro ao solicitar redefinição de senha")
        return jsonify({"error": "Erro ao processar a solicitação."}), 500


@app.route("/api/redefinir-senha", methods=["POST"])
def redefinir_senha():
    dados = json_body()
    email = normalizar_email(dados.get("email"))
    codigo = str(dados.get("codigo") or "").strip()
    nova_senha = dados.get("nova_senha")
    token_assinado = dados.get("recuperacao_token")
    if not email or len(codigo) != OTP_LENGTH or not codigo.isdigit() or not isinstance(nova_senha, str):
        return jsonify({"error": "Preencha todos os campos corretamente."}), 400
    if not senha_valida(nova_senha):
        return jsonify({"error": "A senha deve ter 8 caracteres, com maiúscula, minúscula e caractere especial."}), 400

    chave_tentativas = f"rec_try:{security.fingerprint(email)}"

    try:
        with lock_conta(email):
            usuario = buscar_usuario(email)
            if not usuario:
                security.verify_recovery(token_assinado, email, codigo, max_age=OTP_TTL_MINUTES * 60)
                return jsonify({"error": "Código inválido ou expirado. Solicite uma nova recuperação nesta página."}), 400

            token_salvo = str(usuario.get("token_recuperacao") or "")
            if not token_salvo:
                return jsonify({"error": "Código inválido ou expirado. Solicite uma nova recuperação nesta página."}), 400

            # Verifica expiração persistida na coluna dedicada (quando presente)
            expira_coluna = usuario.get("token_recuperacao_expira_em")
            expirado_coluna = False
            if expira_coluna:
                try:
                    dt_exp = (
                        expira_coluna
                        if isinstance(expira_coluna, datetime)
                        else datetime.fromisoformat(str(expira_coluna).replace("Z", "+00:00"))
                    )
                    if dt_exp.tzinfo is None:
                        dt_exp = dt_exp.replace(tzinfo=timezone.utc)
                    expirado_coluna = datetime.now(timezone.utc) > dt_exp
                except ValueError:
                    expirado_coluna = True

            registro_valido, expirado_registro, nonce_id_salvo = security.verify_recovery_record(
                token_salvo, email, codigo
            )
            if expirado_coluna or expirado_registro:
                atualizar_usuario_compativel(
                    {"email": email},
                    {"token_recuperacao": None, "token_recuperacao_expira_em": None, "token_recuperacao_tentativas": 0},
                    {"token_recuperacao": None},
                )
                challenge_attempt_limiter.reset_key(chave_tentativas)
                return jsonify({"error": "Código inválido ou expirado. Solicite uma nova recuperação nesta página."}), 400

            tentativas_banco = int(usuario.get("token_recuperacao_tentativas") or 0)
            permitido_mem, _ = challenge_attempt_limiter.allow(chave_tentativas, RECOVERY_MAX_ATTEMPTS, OTP_TTL_MINUTES * 60)
            if not permitido_mem or tentativas_banco >= RECOVERY_MAX_ATTEMPTS:
                atualizar_usuario_compativel(
                    {"email": email},
                    {"token_recuperacao": None, "token_recuperacao_expira_em": None, "token_recuperacao_tentativas": 0},
                    {"token_recuperacao": None},
                )
                return jsonify({"error": "Limite de tentativas excedido. Solicite uma nova recuperação."}), 400

            token_assinado_valido = security.verify_recovery(
                token_assinado,
                email,
                codigo,
                max_age=OTP_TTL_MINUTES * 60,
                expected_nonce_id=nonce_id_salvo,
            )

            if not registro_valido or not token_assinado_valido:
                novas_tentativas = tentativas_banco + 1
                if novas_tentativas >= RECOVERY_MAX_ATTEMPTS:
                    atualizar_usuario_compativel(
                        {"email": email},
                        {"token_recuperacao": None, "token_recuperacao_expira_em": None, "token_recuperacao_tentativas": 0},
                        {"token_recuperacao": None},
                    )
                    return jsonify({"error": "Limite de tentativas excedido. Solicite uma nova recuperação."}), 400
                if "token_recuperacao_tentativas" in usuario:
                    atualizar_usuario_compativel(
                        {"email": email},
                        {"token_recuperacao_tentativas": novas_tentativas},
                        {},
                    )
                return jsonify({"error": "Código inválido ou expirado. Solicite uma nova recuperação nesta página."}), 400

            nova_senha_hash = generate_password_hash(nova_senha, method="pbkdf2:sha256")
            resultado = atualizar_usuario_compativel(
                {"email": email, "token_recuperacao": token_salvo},
                {
                    "senha": nova_senha_hash,
                    "token_recuperacao": None,
                    "token_recuperacao_expira_em": None,
                    "token_recuperacao_tentativas": 0,
                },
                {
                    "senha": nova_senha_hash,
                    "token_recuperacao": None,
                },
            )
            if not resultado.data:
                return jsonify({"error": "Código já utilizado. Solicite uma nova recuperação."}), 409

            challenge_attempt_limiter.reset_key(chave_tentativas)
            revogar_sessoes_usuario(email, incrementar_banco=True)
            if normalizar_email(session.get("usuario_email")) == email:
                session.clear()
        alertar_servidor_telegram(
            "CONTA",
            "Senha redefinida pelo usuário",
            {"E-mail": email, "IP": obter_ip_cliente()},
            chave_cooldown=f"pwd_reset:{email}",
            janela_segundos=60,
        )
        return jsonify({"message": "Senha redefinida com sucesso! Faça login para continuar."}), 200
    except Exception:
        logger.exception("Erro ao redefinir senha")
        return jsonify({"error": "Erro ao redefinir a senha."}), 500


@app.route("/api/servicos", methods=["GET"])
def listar_servicos():
    try:
        # ``contratos`` é um contador operacional de reservas, não parte do catálogo público.
        resposta = db.table("servico").select("id, tipo, descricao, valor, imagem_url").order("tipo").execute()
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
        with schedule_lock:
            criado = db.create_appointment(email, servico_id, data_atendimento)
            linhas = getattr(criado, "data", None) or []
            agendamento = linhas[0] if linhas else {}
            if agendamento.get("id"):
                registrar_auditoria_agenda(
                    agendamento["id"], "criado", g.usuario,
                    status_novo="Pendente",
                )
        try:
            nome_servico = "Tratamento"
            servicos = db.table("servico").select("tipo").eq("id", servico_id).limit(1).execute().data or []
            if servicos:
                nome_servico = servicos[0].get("tipo") or nome_servico
            email_equipe_agenda("Nova reserva", "Nova reserva recebida", {
                "Tratamento": nome_servico,
                "Data e hora": datetime.fromisoformat(data_atendimento).strftime("%d/%m/%Y às %H:%M"),
                "Cliente": email,
                "Reserva": agendamento.get("id", "consulte o painel"),
            })
        except Exception:
            logger.exception("Reserva confirmada, mas não foi possível preparar o alerta da equipe")
        return jsonify({"message": "Agendamento realizado com sucesso!"}), 201
    except ServiceNotFoundError as error:
        return jsonify({"error": str(error)}), 404
    except ScheduleConflictError as error:
        return jsonify({"error": str(error)}), 409
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except Exception:
        logger.exception("Erro ao criar agendamento")
        return jsonify({"error": "Erro ao salvar o agendamento no banco."}), 500


@app.route("/api/meus-agendamentos", methods=["GET"])
@login_obrigatorio
def meus_agendamentos():
    try:
        resposta = db.table("agendamentos").select("id, data_atendimento, status, avaliacao, servico_id, servico(tipo, valor)").eq("email_cliente", g.usuario["email"]).order("data_atendimento").execute()
        return jsonify(resposta.data or []), 200
    except Exception:
        logger.exception("Erro ao buscar agendamentos do usuário")
        return jsonify({"error": "Erro ao carregar a lista de agendamentos."}), 500


@app.route("/api/lista-espera", methods=["GET"])
@login_obrigatorio
def minha_lista_espera():
    try:
        resposta = (
            db.table("agenda_lista_espera")
            .select("id, servico_id, data_atendimento, status, criado_em, notificado_em")
            .eq("email_cliente", g.usuario["email"])
            .order("criado_em", desc=True)
            .limit(30)
            .execute()
        )
        return jsonify(resposta.data or []), 200
    except Exception:
        logger.exception("Erro ao consultar a lista de espera do usuário")
        return jsonify({"error": "Não foi possível consultar seus avisos de horário."}), 500


@app.route("/api/lista-espera/<int:entrada_id>", methods=["DELETE"])
@login_obrigatorio
@serializar_agenda
def sair_lista_espera(entrada_id):
    try:
        resposta = (
            db.table("agenda_lista_espera")
            .update({"status": "Cancelado"})
            .eq("id", entrada_id)
            .eq("email_cliente", g.usuario["email"])
            .eq("status", "Aguardando")
            .execute()
        )
        if not resposta.data:
            return jsonify({"error": "Esse aviso não está mais aguardando ou não pertence à sua conta."}), 404
        return jsonify({"message": "Aviso removido da lista de espera."}), 200
    except Exception:
        logger.exception("Erro ao remover usuário da lista de espera")
        return jsonify({"error": "Não foi possível remover esse aviso agora."}), 500


@app.route("/api/lista-espera", methods=["POST"])
@login_obrigatorio
@serializar_agenda
def entrar_lista_espera():
    dados = json_body()
    try:
        servico_id = int(dados.get("servico_id"))
    except (TypeError, ValueError):
        servico_id = 0
    if servico_id <= 0:
        return jsonify({"error": "Tratamento inválido."}), 400

    try:
        data_atendimento = validar_data_agendamento(dados.get("data"))
        servico = db.table("servico").select("id").eq("id", servico_id).limit(1).execute().data or []
        if not servico:
            return jsonify({"error": "Tratamento não encontrado."}), 404
        ocupado = (
            db.table("agendamentos").select("id")
            .eq("data_atendimento", data_atendimento)
            .neq("status", "Cancelado").limit(1).execute().data or []
        )
        if not ocupado:
            return jsonify({"error": "Esse horário acabou de ficar livre. Atualize a agenda para reservá-lo."}), 409
        existente = (
            db.table("agenda_lista_espera").select("id, status")
            .eq("email_cliente", g.usuario["email"])
            .eq("data_atendimento", data_atendimento)
            .eq("status", "Aguardando")
            .limit(1).execute().data or []
        )
        if existente:
            return jsonify({"message": "Você já está na lista de espera para esse horário."}), 200
        try:
            db.table("agenda_lista_espera").insert({
                "email_cliente": g.usuario["email"],
                "servico_id": servico_id,
                "data_atendimento": data_atendimento,
                "status": "Aguardando",
            }).execute()
        except Exception as error:
            if "23505" not in str(error) and "duplicate key" not in str(error).lower():
                raise
            return jsonify({"message": "Você já está na lista de espera para esse horário."}), 200
        alertar_servidor_telegram(
            "AGENDA",
            "Cliente entrou na lista de espera",
            {"Cliente": g.usuario["email"], "Data e hora": data_atendimento, "Serviço ID": servico_id},
            chave_cooldown=f"waitlist_join:{g.usuario['email']}:{data_atendimento}",
            janela_segundos=30,
        )
        return jsonify({"message": "Pronto. Avisaremos por e-mail se esse horário ficar livre."}), 201
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except Exception:
        logger.exception("Erro ao adicionar cliente à lista de espera")
        return jsonify({"error": "Não foi possível entrar na lista de espera agora."}), 500


def consultar_linhas_paginadas(criar_consulta, tamanho=500):
    """Evita o limite implícito de linhas da Data API ao consultar a agenda."""
    linhas = []
    for inicio in range(0, 100_000, tamanho):
        pagina = criar_consulta().range(inicio, inicio + tamanho - 1).execute().data or []
        linhas.extend(pagina)
        if len(pagina) < tamanho:
            return linhas
    raise RuntimeError("A consulta da agenda excedeu o limite seguro de paginação.")


@app.route("/api/horarios-ocupados", methods=["GET"])
def horarios_ocupados():
    try:
        ignorar_id = request.args.get("ignorar_id", type=int)
        agora = datetime.now(SPA_TIMEZONE).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M")
        ignorar_autorizado = False
        if ignorar_id:
            usuario = usuario_da_sessao()
            agendamento = buscar_agendamento(ignorar_id)
            ignorar_autorizado = usuario_pode_alterar_agendamento(usuario, agendamento)

        def consulta_reservas():
            consulta = (
                db.table("agendamentos").select("data_atendimento")
                .neq("status", "Cancelado").gt("data_atendimento", agora)
                .order("data_atendimento")
            )
            return consulta.neq("id", ignorar_id) if ignorar_autorizado else consulta

        reservas = consultar_linhas_paginadas(consulta_reservas)
        horarios = set()
        for agendamento in reservas:
            data_atendimento = agendamento["data_atendimento"]
            horarios.add(
                data_atendimento.isoformat()
                if isinstance(data_atendimento, datetime)
                else str(data_atendimento)
            )
        bloqueios = consultar_linhas_paginadas(lambda: (
            db.table("agenda_bloqueios").select("data_atendimento")
            .gt("data_atendimento", agora).order("data_atendimento")
        ))
        horarios_bloqueados = set()
        for bloqueio in bloqueios:
            data_atendimento = bloqueio["data_atendimento"]
            horarios_bloqueados.add(
                data_atendimento.isoformat()
                if isinstance(data_atendimento, datetime)
                else str(data_atendimento)
            )
        horarios.update(horarios_bloqueados)
        if request.args.get("detalhes") == "1":
            return jsonify({
                "ocupados": sorted(horarios),
                "bloqueados": sorted(horarios_bloqueados),
            }), 200
        return jsonify(sorted(horarios)), 200
    except Exception:
        logger.exception("Erro ao listar horários ocupados")
        return jsonify({"error": "Não foi possível carregar os horários ocupados."}), 500


@app.route("/api/agendamentos/<int:agendamento_id>", methods=["PUT"])
@login_obrigatorio
def alterar_horario(agendamento_id):
    try:
        nova_data = validar_data_agendamento(json_body().get("data"))
        with schedule_lock:
            agendamento = buscar_agendamento(agendamento_id, "id, email_cliente, status, data_atendimento, servico_id")
            if not agendamento:
                return jsonify({"error": "Agendamento não encontrado."}), 404
            if not usuario_pode_alterar_agendamento(g.usuario, agendamento):
                return jsonify({"error": "Você não pode alterar este agendamento."}), 403
            if agendamento.get("status") != "Pendente":
                return jsonify({"error": "Somente atendimentos pendentes podem ser remarcados."}), 409

            anterior = normalizar_data_hora(agendamento.get("data_atendimento"))
            db.reschedule_appointment(agendamento_id, nova_data)
            registrar_auditoria_agenda(agendamento_id, "reagendado", g.usuario, anterior, nova_data)
        notificar_lista_espera_horario(anterior)
        email_equipe_agenda("Horário liberado", "Um horário ficou disponível", {
            "Reserva": agendamento_id,
            "Data e hora liberada": anterior,
            "Nova data e hora": nova_data,
            "Alteração feita por": g.usuario["email"],
        })
        return jsonify({"message": "Horário atualizado com sucesso!"}), 200
    except ScheduleConflictError as error:
        return jsonify({"error": str(error)}), 409
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except Exception:
        logger.exception("Erro ao alterar agendamento")
        return jsonify({"error": "Erro interno ao reagendar."}), 500


@app.route("/api/agendamentos/<int:agendamento_id>", methods=["DELETE"])
@login_obrigatorio
def cancelar_agendamento(agendamento_id):
    try:
        with schedule_lock:
            agendamento = buscar_agendamento(agendamento_id, "id, email_cliente, status, data_atendimento, servico_id")
            if not agendamento:
                return jsonify({"error": "Agendamento não encontrado."}), 404
            if not usuario_pode_alterar_agendamento(g.usuario, agendamento):
                return jsonify({"error": "Você não pode cancelar este agendamento."}), 403

            # A condição também é verificada no UPDATE: não pode desfazer uma
            # conclusão concorrente e permitir crédito de pontos pela segunda vez.
            alterados = db.table("agendamentos").update({"status": "Cancelado"}).eq("id", agendamento_id).eq("status", "Pendente").execute().data
            if not alterados:
                return jsonify({"error": "Somente atendimentos pendentes podem ser cancelados. Atualize a lista."}), 409
            data_liberada = normalizar_data_hora(agendamento.get("data_atendimento"))
            registrar_auditoria_agenda(
                agendamento_id, "cancelado", g.usuario,
                status_anterior="Pendente", status_novo="Cancelado",
            )
        avisos = notificar_lista_espera_horario(data_liberada)
        email_equipe_agenda("Reserva cancelada", "Uma reserva foi cancelada", {
            "Reserva": agendamento_id,
            "Horário liberado": data_liberada,
            "Cliente da reserva": agendamento.get("email_cliente", "não informado"),
            "Cancelamento feito por": g.usuario["email"],
            "Pessoas avisadas na lista de espera": avisos,
        })
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

        db.table("agendamentos").update({"avaliacao": avaliacao}).eq("id", agendamento_id).execute()
        titulo_av = "Atenção: Avaliação negativa recebida" if avaliacao == "Ruim" else "Nova avaliação de atendimento"
        alertar_servidor_telegram(
            "AVALIAÇÃO",
            titulo_av,
            {
                "Reserva": agendamento_id,
                "Nota": avaliacao,
                "Cliente": agendamento.get("email_cliente") or g.usuario["email"],
            },
            chave_cooldown=f"rating:{agendamento_id}:{avaliacao}",
            janela_segundos=30,
        )
        return jsonify({"message": "Avaliação registrada!"}), 200
    except Exception:
        logger.exception("Erro ao avaliar agendamento")
        return jsonify({"error": "Erro ao salvar avaliação."}), 500


@app.route("/api/admin/agendamentos", methods=["GET"])
@admin_obrigatorio
def admin_agendamentos():
    try:
        resposta = db.table("agendamentos").select("id, email_cliente, data_atendimento, status, avaliacao, servico(tipo, valor)").order("data_atendimento", desc=True).execute()
        return jsonify(resposta.data or []), 200
    except Exception:
        logger.exception("Erro ao listar agendamentos administrativos")
        return jsonify({"error": "Erro ao buscar dados globais."}), 500


@app.route("/api/admin/bloqueios", methods=["GET"])
@admin_obrigatorio
def listar_bloqueios_agenda():
    try:
        agora = datetime.now(SPA_TIMEZONE).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M")
        bloqueios = consultar_linhas_paginadas(lambda: (
            db.table("agenda_bloqueios")
            .select("id, data_atendimento, motivo, criado_por, criado_em")
            .gt("data_atendimento", agora).order("data_atendimento")
        ))
        return jsonify(bloqueios), 200
    except Exception:
        logger.exception("Erro ao listar bloqueios da agenda")
        return jsonify({"error": "Não foi possível carregar os bloqueios."}), 500


@app.route("/api/admin/bloqueios", methods=["POST"])
@admin_obrigatorio
@serializar_agenda
def criar_bloqueio_agenda():
    dados = json_body()
    motivo = str(dados.get("motivo") or "").strip()
    if not 3 <= len(motivo) <= 160:
        return jsonify({"error": "Informe um motivo de 3 a 160 caracteres."}), 400
    try:
        slots = horarios_bloqueio_intervalo(dados.get("data_inicio"), dados.get("data_fim"))
        resposta = db.create_schedule_block(slots, motivo, g.usuario["email"])
        alertar_servidor_telegram(
            "AGENDA",
            "Horários bloqueados na agenda",
            {
                "Quantidade": f"{len(slots)} horário(s)",
                "Período": f"{slots[0]} até {slots[-1]}" if len(slots) > 1 else slots[0],
                "Motivo": motivo,
                "Criado por": g.usuario["email"],
            },
            chave_cooldown=f"block_create:{slots[0]}:{len(slots)}",
            janela_segundos=10,
        )
        return jsonify({
            "message": f"{len(slots)} horário(s) bloqueado(s) com sucesso.",
            "bloqueios": getattr(resposta, "data", None) or [],
        }), 201
    except ScheduleConflictError as error:
        return jsonify({"error": str(error)}), 409
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except Exception:
        logger.exception("Erro ao bloquear horários da agenda")
        return jsonify({"error": "Não foi possível bloquear os horários."}), 500


@app.route("/api/admin/bloqueios/<int:bloqueio_id>", methods=["DELETE"])
@admin_obrigatorio
def remover_bloqueio_agenda(bloqueio_id):
    try:
        with schedule_lock:
            removidos = db.table("agenda_bloqueios").delete().eq("id", bloqueio_id).execute().data or []
            if not removidos:
                return jsonify({"error": "Bloqueio não encontrado."}), 404
        email_equipe_agenda("Horário liberado", "Um bloqueio da agenda foi removido", {
            "Data e hora": normalizar_data_hora(removidos[0].get("data_atendimento")),
            "Motivo anterior": removidos[0].get("motivo") or "não informado",
            "Removido por": g.usuario["email"],
        })
        return jsonify({"message": "Horário liberado com sucesso."}), 200
    except Exception:
        logger.exception("Erro ao liberar horário bloqueado")
        return jsonify({"error": "Não foi possível liberar esse horário."}), 500


@app.route("/api/admin/agendamentos/<int:agendamento_id>/historico", methods=["GET"])
@admin_obrigatorio
def historico_agendamento_admin(agendamento_id):
    try:
        if not buscar_agendamento(agendamento_id, "id"):
            return jsonify({"error": "Agendamento não encontrado."}), 404
        resposta = (
            db.table("agenda_auditoria")
            .select("id, ator_email, ator_tipo, acao, data_anterior, data_nova, status_anterior, status_novo, criado_em")
            .eq("agendamento_id", agendamento_id)
            .order("criado_em", desc=True)
            .limit(50)
            .execute()
        )
        return jsonify(resposta.data or []), 200
    except Exception:
        logger.exception("Erro ao carregar o histórico de um agendamento")
        return jsonify({"error": "Não foi possível carregar o histórico da reserva."}), 500


@app.route("/api/admin/agendamentos/<int:agendamento_id>/concluir", methods=["POST"])
@admin_obrigatorio
@serializar_agenda
def concluir_agendamento(agendamento_id):
    try:
        resposta = db.complete_appointment(agendamento_id)
        resultado = (resposta.data or [None])[0]
        if not resultado:
            raise RuntimeError("A conclusão não retornou um resultado.")
        if resultado.get("resultado") == "nao_encontrado":
            return jsonify({"error": "Agendamento não encontrado."}), 404
        if resultado.get("resultado") == "ja_concluido":
            return jsonify({"message": "Este agendamento já foi concluído.", "pontos": 0}), 200
        if resultado.get("resultado") == "status_invalido":
            return jsonify({"error": "Somente agendamentos pendentes podem ser concluídos."}), 409
        if resultado.get("resultado") != "concluido":
            raise RuntimeError("A conclusão retornou um estado desconhecido.")
        pontos = int(resultado["pontos"])
        registrar_auditoria_agenda(
            agendamento_id, "concluido", g.usuario,
            status_anterior="Pendente", status_novo="Concluido",
        )
        alertar_servidor_telegram(
            "AGENDA",
            "Atendimento concluído e pontos creditados",
            {
                "Reserva": agendamento_id,
                "Pontos creditados": f"+{pontos} pts",
                "Concluído por": g.usuario["email"],
            },
            chave_cooldown=f"appt_done:{agendamento_id}",
            janela_segundos=30,
        )
        return jsonify({"message": f"Agendamento concluído e {pontos} pontos creditados ao cliente!", "pontos": pontos}), 200
    except Exception:
        logger.exception("Erro ao concluir agendamento")
        return jsonify({"error": "Erro ao concluir o serviço."}), 500


@app.route("/api/admin/vouchers/utilizar", methods=["POST"])
@admin_obrigatorio
@serializar_fidelidade
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
            db.table("resgates_pontos")
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

        resposta = db.rpc("utilizar_voucher", {"p_codigo_voucher": codigo, "p_agendamento_id": agendamento_id}).execute()
        utilizado = resposta.data[0] if isinstance(resposta.data, list) and resposta.data else resposta.data
        if not utilizado:
            return jsonify({"error": "O voucher não pôde ser utilizado. Atualize a agenda e tente novamente."}), 409
        alertar_servidor_telegram(
            "FIDELIDADE",
            "Voucher de fidelidade utilizado",
            {
                "Voucher": codigo,
                "Benefício": voucher.get("recompensa_nome") or "Benefício de fidelidade",
                "Desconto": f"R$ {float(voucher.get('desconto') or 0):.2f}",
                "Cliente": voucher.get("email_cliente"),
                "Reserva": agendamento_id,
                "Aplicado por": g.usuario["email"],
            },
            chave_cooldown=f"voucher_use:{codigo}",
            janela_segundos=30,
        )
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
    """Consulta saldo, recompensas e vouchers ativos no PostgreSQL local."""
    try:
        email_cliente = str(g.usuario["email"] or "").strip().lower()
        cliente = buscar_usuario(email_cliente, "pontos")
        if not cliente:
            return jsonify({"error": "Usuário não encontrado."}), 404

        # Esta tela é somente de leitura. A expiração é validada no resgate/uso
        # do voucher; assim, abrir a página nunca altera dados por engano.
        vouchers = (
            db.table("resgates_pontos")
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
@serializar_fidelidade
def resgatar_pontos():
    dados = json_body()
    recompensa = str(dados.get("recompensa") or "").strip().lower()
    if recompensa not in RECOMPENSAS_FIDELIDADE:
        return jsonify({"error": "Recompensa inválida."}), 400
    try:
        resposta = db.rpc("resgatar_pontos", {"p_email": g.usuario["email"], "p_recompensa_codigo": recompensa}).execute()
        resgate = resposta.data[0] if resposta.data else None
        if not resgate:
            raise RuntimeError("O resgate não retornou um voucher.")
        info_rec = RECOMPENSAS_FIDELIDADE[recompensa]
        alertar_servidor_telegram(
            "FIDELIDADE",
            "Novo voucher de fidelidade resgatado",
            {
                "Cliente": g.usuario["email"],
                "Recompensa": info_rec["nome"],
                "Pontos": f"-{info_rec['pontos']} pts",
                "Desconto": f"R$ {info_rec['desconto']:.2f}",
                "Voucher": resgate.get("codigo_voucher") if isinstance(resgate, dict) else "gerado",
            },
            chave_cooldown=f"redeem:{g.usuario['email']}:{recompensa}",
            janela_segundos=10,
        )
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
            db.table("agendamentos")
            .select("id, data_atendimento, status, avaliacao, servico(tipo, valor)")
            .eq("email_cliente", g.usuario["email"])
            .order("data_atendimento", desc=True)
            .execute()
            .data or []
        )
        lista_espera = (
            db.table("agenda_lista_espera")
            .select("data_atendimento, status, criado_em, notificado_em")
            .eq("email_cliente", g.usuario["email"])
            .order("criado_em", desc=True)
            .limit(100)
            .execute()
            .data or []
        )
        alertar_servidor_telegram(
            "PRIVACIDADE",
            "Exportação de dados pessoais solicitada (LGPD)",
            {"Titular": g.usuario["email"], "IP": obter_ip_cliente()},
            chave_cooldown=f"lgpd_export:{g.usuario['email']}",
            janela_segundos=300,
        )
        return jsonify({
            "gerado_em": datetime.now(SPA_TIMEZONE).isoformat(),
            "finalidade": "Cópia dos dados pessoais e do histórico de reservas do Spa Panaceia.",
            "conta": usuario,
            "agendamentos": agendamentos,
            "lista_espera": lista_espera,
        }), 200
    except Exception:
        logger.exception("Erro ao exportar dados pessoais")
        return jsonify({"error": "Não foi possível preparar sua cópia de dados agora."}), 500


@app.route("/api/admin/usuarios", methods=["GET"])
@admin_obrigatorio
def admin_usuarios():
    try:
        usuarios = db.table("usuarios").select("nome, email").order("nome").execute().data or []
        agendamentos = db.table("agendamentos").select("email_cliente, status, servico(valor)").execute().data or []
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


ESTOQUE_QUANTIDADE_MAXIMA = 2_147_483_647


def quantidade_inteira(value, campo):
    mensagem_inteiro = f"{campo} deve ser um número inteiro não negativo."
    if isinstance(value, bool):
        raise ValueError(mensagem_inteiro)

    if isinstance(value, int):
        quantidade = value
    elif isinstance(value, str):
        texto = value.strip()
        if texto.startswith("-") and texto[1:].isascii() and texto[1:].isdigit():
            raise ValueError(f"{campo} não pode ser negativa.")
        if not texto or not texto.isascii() or not texto.isdigit():
            raise ValueError(mensagem_inteiro)
        digitos_significativos = texto.lstrip("0") or "0"
        if len(digitos_significativos) > len(str(ESTOQUE_QUANTIDADE_MAXIMA)):
            raise ValueError(
                f"{campo} excede o limite máximo de {ESTOQUE_QUANTIDADE_MAXIMA}."
            )
        quantidade = int(digitos_significativos)
    else:
        # Não converte float/Decimal silenciosamente; a API espera inteiros JSON.
        raise ValueError(mensagem_inteiro)

    if quantidade < 0:
        raise ValueError(f"{campo} não pode ser negativa.")
    if quantidade > ESTOQUE_QUANTIDADE_MAXIMA:
        raise ValueError(
            f"{campo} excede o limite máximo de {ESTOQUE_QUANTIDADE_MAXIMA}."
        )
    return quantidade


@app.route("/api/estoque", methods=["GET"])
@admin_obrigatorio
def listar_estoque():
    try:
        resposta = db.table("estoque").select("id, nome, quantidade, quantidade_minima, unidade").order("id").execute()
        return jsonify(resposta.data or []), 200
    except Exception:
        logger.exception("Erro ao listar estoque")
        return jsonify({"error": "Erro ao carregar o estoque."}), 500


@app.route("/api/estoque", methods=["POST"])
@admin_obrigatorio
@serializar_estoque
def adicionar_estoque():
    dados = json_body()
    nome = str(dados.get("nome") or "").strip()
    unidade = str(dados.get("unidade") or "un").strip().lower()
    if not nome or len(nome) > 120 or not unidade or len(unidade) > 10:
        return jsonify({"error": "Nome ou unidade do produto inválidos."}), 400

    try:
        quantidade = quantidade_inteira(dados.get("quantidade", 0), "Quantidade")
        quantidade_minima = quantidade_inteira(
            dados.get("quantidade_minima", 5), "Quantidade mínima"
        )
        db.table("estoque").insert({
            "nome": nome,
            "quantidade": quantidade,
            "quantidade_minima": quantidade_minima,
            "unidade": unidade,
        }).execute()
        alertar_servidor_telegram(
            "ESTOQUE",
            "Novo produto cadastrado no estoque",
            {
                "Produto": nome,
                "Saldo inicial": f"{quantidade} {unidade}",
                "Estoque mínimo": f"{quantidade_minima} {unidade}",
                "Cadastrado por": g.usuario["email"],
            },
            chave_cooldown=f"stock_add:{nome.lower()}",
            janela_segundos=10,
        )
        return jsonify({"message": "Item adicionado ao estoque!"}), 201
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except Exception:
        logger.exception("Erro ao inserir item no estoque")
        return jsonify({"error": "Erro ao cadastrar o produto no estoque."}), 500


@app.route("/api/estoque/<int:item_id>/movimentar", methods=["PUT"])
@admin_obrigatorio
@serializar_estoque
def atualizar_estoque(item_id):
    dados = json_body()
    try:
        quantidade = quantidade_inteira(dados.get("quantidade"), "Quantidade")
        acao = str(dados.get("acao") or "").strip().lower()
        if acao:
            if acao not in {"adicionar", "somar", "remover", "subtrair"}:
                return jsonify({"error": "Ação de estoque inválida."}), 400
            if quantidade == 0:
                return jsonify({"error": "A quantidade da movimentação deve ser maior que zero."}), 400
            try:
                operacao_id = str(uuid.UUID(str(dados.get("operacao_id") or "")))
            except (ValueError, TypeError, AttributeError):
                return jsonify({"error": "Identificador da movimentação inválido."}), 400
            delta = quantidade if acao in {"adicionar", "somar"} else -quantidade
            resposta = db.move_stock(item_id, delta, operacao_id)
        else:
            quantidade_atual = quantidade_inteira(
                dados.get("quantidade_atual"), "Quantidade atual"
            )
            resposta = db.set_stock_quantity(item_id, quantidade, quantidade_atual)

        item = resposta.data[0]
        if not item.get("repetida"):
            qtd_atual = int(item.get("quantidade") or 0)
            qtd_min = int(item.get("quantidade_minima") or 0)
            unid = item.get("unidade") or "un"
            nome_prod = item.get("nome") or f"Item #{item_id}"
            if qtd_atual == 0:
                titulo_est = "🚨 PRODUTO ESGOTADO NO ESTOQUE"
            elif qtd_atual <= qtd_min:
                titulo_est = "⚠️ Alerta de estoque mínimo atingido"
            else:
                titulo_est = "Movimentação de estoque registrada"
            alertar_servidor_telegram(
                "ESTOQUE",
                titulo_est,
                {
                    "Produto": nome_prod,
                    "Saldo atual": f"{qtd_atual} {unid}",
                    "Mínimo recomendado": f"{qtd_min} {unid}",
                    "Operador": g.usuario["email"],
                },
                chave_cooldown=f"stock_move:{item_id}:{qtd_atual}",
                janela_segundos=5,
            )
        return jsonify({
            "message": "Estoque atualizado com sucesso!",
            "nova_quantidade": item.get("quantidade"),
            "item": item,
        }), 200
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except StockItemNotFoundError as error:
        return jsonify({"error": str(error)}), 404
    except InsufficientStockError as error:
        return jsonify({"error": str(error)}), 409
    except (StockOperationConflictError, StockQuantityConflictError) as error:
        return jsonify({"error": str(error)}), 409
    except Exception:
        logger.exception("Erro ao atualizar estoque")
        return jsonify({"error": "Erro ao alterar quantidade."}), 500


@app.route("/api/estoque/<int:item_id>", methods=["DELETE"])
@admin_obrigatorio
@serializar_estoque
def deletar_estoque(item_id):
    try:
        removidos = db.table("estoque").delete().eq("id", item_id).execute().data or []
        if not removidos:
            return jsonify({"error": "Item do estoque não encontrado."}), 404
        item_rem = removidos[0]
        alertar_servidor_telegram(
            "ESTOQUE",
            "Produto removido do estoque",
            {
                "Produto": item_rem.get("nome") or f"Item #{item_id}",
                "Removido por": g.usuario["email"],
            },
            chave_cooldown=f"stock_del:{item_id}",
            janela_segundos=10,
        )
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
        "de bem-estar sem prometer benefícios médicos. Quando a pessoa quiser agendar, acolha o pedido "
        "e diga que ela poderá escolher uma data e um horário no fluxo de reserva. Nunca afirme que uma "
        "reserva foi feita ou aberta."
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


def diagnosticar_e_notificar_boot_telegram():
    """Valida a configuração do Telegram no boot e envia o alerta inicial fora do modo de teste."""
    if APP_ENVIRONMENT == "test" or app.config.get("TESTING"):
        return False
    token, chat_id = obter_config_telegram()
    if not token or not chat_id:
        telegram_logger.warning(
            "Notificações do Telegram desativadas no boot: configure TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID no ambiente/--env-file."
        )
        return False
    telegram_logger.info(
        "Notificações do Telegram configuradas (chat=%s, ambiente=%s).",
        _mascarar_chat_id(chat_id),
        "producao" if IS_PRODUCTION else "desenvolvimento",
    )
    return alertar_servidor_telegram(
        "SISTEMA",
        "Servidor Spa Panaceia iniciado",
        {"Ambiente": "Produção" if IS_PRODUCTION else "Desenvolvimento", "Versão": VERSAO_BACKEND},
        chave_cooldown="server_boot",
        janela_segundos=10,
    )


# Dispara tanto no Gunicorn (Docker/ZimaOS) quanto no Python direto
diagnosticar_e_notificar_boot_telegram()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
