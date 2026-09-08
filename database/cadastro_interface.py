import html
import logging
import os
import re
import secrets
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from supabase import Client, create_client
from werkzeug.security import check_password_hash, generate_password_hash

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

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL e SUPABASE_KEY são obrigatórias.")

SPA_TIMEZONE = ZoneInfo(os.getenv("SPA_TIMEZONE", "America/Sao_Paulo"))
OPENING_HOUR = 9
LAST_APPOINTMENT_HOUR = 19
OTP_TTL_MINUTES = 15
OTP_LENGTH = 6
CHAT_MAX_LENGTH = 1_000
PASSWORD_PATTERN = re.compile(r"^(?=.*[a-z])(?=.*[A-Z])(?=.*[@$!%*?&#,.]).{8,}$")
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

app = Flask(__name__)
cors_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://127.0.0.1:5500,http://localhost:5500,https://seu-site-hospedado.com",
    ).split(",")
    if origin.strip()
]
CORS(app, resources={r"/api/*": {"origins": cors_origins}, r"/cadastrar": {"origins": cors_origins}})

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY and OpenAI else None


def json_body():
    """Retorna um JSON seguro, inclusive quando o corpo está vazio ou malformado."""
    return request.get_json(silent=True) or {}


def normalizar_email(value):
    email = str(value or "").strip().lower()
    return email if EMAIL_PATTERN.fullmatch(email) else None


def senha_valida(senha):
    return isinstance(senha, str) and bool(PASSWORD_PATTERN.fullmatch(senha))


def gerar_otp():
    return f"{secrets.randbelow(10 ** OTP_LENGTH):0{OTP_LENGTH}d}"


def buscar_usuario(email, campos="*"):
    resposta = supabase.table("usuarios").select(campos).eq("email", email).limit(1).execute()
    return resposta.data[0] if resposta.data else None


def admin_autorizado(email):
    """Mantém o contrato atual baseado em e-mail; migre para sessão/JWT antes de produção."""
    email = normalizar_email(email)
    if not email:
        return False
    usuario = buscar_usuario(email, "is_admin")
    return bool(usuario and usuario.get("is_admin"))


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
      <p>Se você não solicitou esta alteração, ignore este e-mail.</p>
    </div>
    """


@app.route("/")
def pagina_principal():
    return send_from_directory(BASE_DIR, "servicos.html")


@app.route("/cadastro")
def pagina_cadastro():
    return send_from_directory(BASE_DIR, "index.html")


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


@app.route("/api/login", methods=["POST"])
def login():
    dados = json_body()
    email = normalizar_email(dados.get("email"))
    senha = dados.get("senha")
    if not email or not isinstance(senha, str):
        return jsonify({"error": "E-mail e senha são obrigatórios."}), 400

    try:
        usuario = buscar_usuario(email)
        if not usuario or not check_password_hash(usuario.get("senha") or "", senha):
            return jsonify({"error": "E-mail ou senha incorretos."}), 401
        if not usuario.get("email_verificado"):
            return jsonify({"error": "Conta não verificada. Verifique seu e-mail antes de entrar."}), 403

        return jsonify({"message": "Login realizado com sucesso!", "usuario": {"id": usuario.get("id"), "nome": usuario.get("nome"), "email": usuario.get("email"), "is_admin": bool(usuario.get("is_admin")), "pontos": usuario.get("pontos") or 0}}), 200
    except Exception:
        logger.exception("Erro no login")
        return jsonify({"error": "Erro interno no servidor."}), 500


@app.route("/api/esqueci-senha", methods=["POST"])
def esqueci_senha():
    dados = json_body()
    email = normalizar_email(dados.get("email"))
    if not email:
        return jsonify({"error": "Informe um e-mail válido."}), 400

    resposta_padrao = {"message": "Se o e-mail estiver cadastrado, você receberá o código de recuperação."}
    try:
        if not buscar_usuario(email, "id"):
            return jsonify(resposta_padrao), 200

        codigo = gerar_otp()
        supabase.table("usuarios").update({"token_recuperacao": codigo}).eq("email", email).execute()
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
    if not email or len(codigo) != OTP_LENGTH or not codigo.isdigit() or not isinstance(nova_senha, str):
        return jsonify({"error": "Preencha todos os campos corretamente."}), 400
    if not senha_valida(nova_senha):
        return jsonify({"error": "A senha deve ter 8 caracteres, com maiúscula, minúscula e caractere especial."}), 400

    try:
        usuario = buscar_usuario(email, "token_recuperacao")
        if not usuario:
            return jsonify({"error": "Usuário não encontrado."}), 404
        token_salvo = str(usuario.get("token_recuperacao") or "")
        if not token_salvo or not secrets.compare_digest(token_salvo, codigo):
            return jsonify({"error": "Código de verificação incorreto."}), 400

        supabase.table("usuarios").update({"senha": generate_password_hash(nova_senha, method="pbkdf2:sha256"), "token_recuperacao": None}).eq("email", email).execute()
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


@app.route("/api/agendar", methods=["POST"])
def criar_agendamento():
    dados = json_body()
    email = normalizar_email(dados.get("email"))
    try:
        servico_id = int(dados.get("servico_id"))
    except (TypeError, ValueError):
        servico_id = 0
    if not email or servico_id <= 0:
        return jsonify({"error": "E-mail e serviço válidos são obrigatórios."}), 400

    try:
        data_atendimento = validar_data_agendamento(dados.get("data"))
        usuario = buscar_usuario(email, "email_verificado")
        if not usuario:
            return jsonify({"error": "Usuário inexistente. Crie uma conta antes de agendar."}), 404
        if not usuario.get("email_verificado"):
            return jsonify({"error": "Verifique seu e-mail antes de agendar."}), 403
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
def meus_agendamentos():
    email = normalizar_email(request.args.get("email"))
    if not email:
        return jsonify({"error": "E-mail do usuário não informado."}), 400
    try:
        resposta = supabase.table("agendamentos").select("id, data_atendimento, status, avaliacao, servico_id, servico(tipo, valor)").eq("email_cliente", email).order("data_atendimento").execute()
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
            consulta = consulta.neq("id", ignorar_id)

        resposta = consulta.order("data_atendimento").execute()
        return jsonify([agendamento["data_atendimento"] for agendamento in resposta.data or []]), 200
    except Exception:
        logger.exception("Erro ao listar horários ocupados")
        return jsonify({"error": "Não foi possível carregar os horários ocupados."}), 500


@app.route("/api/agendamentos/<int:agendamento_id>", methods=["PUT"])
def alterar_horario(agendamento_id):
    try:
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
def cancelar_agendamento(agendamento_id):
    try:
        supabase.table("agendamentos").update({"status": "Cancelado"}).eq("id", agendamento_id).execute()
        return jsonify({"message": "Agendamento cancelado com sucesso."}), 200
    except Exception:
        logger.exception("Erro ao cancelar agendamento")
        return jsonify({"error": "Erro ao cancelar o agendamento."}), 500


@app.route("/api/agendamentos/<int:agendamento_id>/avaliar", methods=["POST"])
def avaliar_agendamento(agendamento_id):
    avaliacao = json_body().get("avaliacao")
    if avaliacao not in {"Bom", "Médio", "Ruim"}:
        return jsonify({"error": "Avaliação inválida."}), 400
    try:
        supabase.table("agendamentos").update({"avaliacao": avaliacao}).eq("id", agendamento_id).execute()
        return jsonify({"message": "Avaliação registrada!"}), 200
    except Exception:
        logger.exception("Erro ao avaliar agendamento")
        return jsonify({"error": "Erro ao salvar avaliação."}), 500


@app.route("/api/admin/agendamentos", methods=["GET"])
def admin_agendamentos():
    if not admin_autorizado(request.args.get("admin_email")):
        return jsonify({"error": "Acesso não autorizado."}), 403
    try:
        resposta = supabase.table("agendamentos").select("id, email_cliente, data_atendimento, status, avaliacao, servico(tipo, valor)").order("data_atendimento", desc=True).execute()
        return jsonify(resposta.data or []), 200
    except Exception:
        logger.exception("Erro ao listar agendamentos administrativos")
        return jsonify({"error": "Erro ao buscar dados globais."}), 500


@app.route("/api/admin/agendamentos/<int:agendamento_id>/concluir", methods=["POST"])
def concluir_agendamento(agendamento_id):
    if not admin_autorizado(json_body().get("admin_email")):
        return jsonify({"error": "Acesso não autorizado."}), 403
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

        supabase.table("agendamentos").update({"status": "Concluido"}).eq("id", agendamento_id).execute()
        cliente = buscar_usuario(agendamento.get("email_cliente"), "pontos")
        if cliente and pontos:
            supabase.table("usuarios").update({"pontos": int(cliente.get("pontos") or 0) + pontos}).eq("email", agendamento["email_cliente"]).execute()
        return jsonify({"message": f"Agendamento concluído e {pontos} pontos creditados ao cliente!", "pontos": pontos}), 200
    except Exception:
        logger.exception("Erro ao concluir agendamento")
        return jsonify({"error": "Erro ao concluir o serviço."}), 500


@app.route("/api/usuario/pontos", methods=["GET"])
def obter_pontos():
    email = normalizar_email(request.args.get("email"))
    if not email:
        return jsonify({"error": "E-mail é obrigatório."}), 400
    try:
        cliente = buscar_usuario(email, "pontos")
        if not cliente:
            return jsonify({"error": "Usuário não encontrado."}), 404
        return jsonify({"pontos": cliente.get("pontos") or 0}), 200
    except Exception:
        logger.exception("Erro ao buscar pontos")
        return jsonify({"error": "Erro ao buscar os pontos do usuário."}), 500


@app.route("/api/admin/usuarios", methods=["GET"])
def admin_usuarios():
    if not admin_autorizado(request.args.get("admin_email")):
        return jsonify({"error": "Acesso não autorizado."}), 403
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
def listar_estoque():
    try:
        resposta = supabase.table("estoque").select("id, nome, quantidade, quantidade_minima, unidade").order("id").execute()
        return jsonify(resposta.data or []), 200
    except Exception:
        logger.exception("Erro ao listar estoque")
        return jsonify({"error": "Erro ao carregar o estoque."}), 500


@app.route("/api/estoque", methods=["POST"])
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
        "sofisticado e conciso (no máximo três frases). Para dores, estresse ou cansaço, sugira "
        "uma experiência do spa. Quando a pessoa quiser agendar, celebre e diga que a agenda será aberta."
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
