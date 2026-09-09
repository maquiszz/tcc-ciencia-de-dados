"""Clube Panaceia. Todas as mutações de pontos são transações SQL via RPC.

Não há fallback de débito/crédito em Python. A migração é obrigatória.
As identidades sempre vêm da sessão Flask, nunca do corpo da requisição.
"""
import logging
import re
from uuid import UUID
from flask import g, jsonify, request

logger = logging.getLogger(__name__)
MENSAGENS = {
    'CLUBE_ALTERADO': ('As condições mudaram. Atualize a carteira e confira antes de resgatar.', 409),
    'CLUBE_USUARIO': ('Não foi possível identificar uma conta única para os pontos.', 409),
    'CLUBE_ADMIN': ('Acesso restrito a administradores.', 403),
    'CLUBE_DADOS': ('Confira os dados informados.', 400),
    'CLUBE_REPETICAO': ('Esta tentativa pertence a outro resgate. Atualize a carteira.', 409),
    'CLUBE_INATIVO': ('Essa recompensa não está disponível. Atualize a carteira.', 409),
    'CLUBE_SALDO': ('Seu saldo não é suficiente para essa recompensa.', 409),
    'CLUBE_NAO_ENCONTRADO': ('Benefício ou atendimento não encontrado.', 404),
    'CLUBE_USADO': ('Este benefício já foi utilizado ou cancelado.', 409),
    'CLUBE_STATUS': ('Este atendimento não permite essa ação.', 409),
    'CLUBE_FINALIZAR': ('Finalize o atendimento antes de validar o benefício.', 409),
    'CLUBE_TITULAR': ('O benefício não pertence ao cliente desse atendimento.', 409),
    'CLUBE_VALOR': ('Confira o valor do serviço. Ele deve cobrir o desconto do vale.', 409),
    'CLUBE_UM_VALE': ('Este atendimento já recebeu um benefício do Clube.', 409),
}


def erro_clube(erro):
    mensagem = str(getattr(erro, 'message', '') or erro)
    for codigo, (texto, status) in MENSAGENS.items():
        if codigo in mensagem:
            return jsonify({'error': texto, 'code': codigo}), status
    logger.exception('Falha na operação do Clube Panaceia')
    return jsonify({'error': 'O Clube está temporariamente indisponível. Confira a carteira antes de tentar novamente.', 'code': 'clube_indisponivel'}), 503


def uuid_valido(valor):
    try:
        return str(UUID(str(valor)))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('CLUBE_DADOS') from None


def codigo_valido(valor):
    codigo = str(valor or '').strip().upper()
    if not re.fullmatch(r'PAN-[A-F0-9]{20}', codigo):
        raise ValueError('CLUBE_DADOS')
    return codigo


def inteiro(valor, minimo, maximo):
    if isinstance(valor, bool) or not isinstance(valor, int) or not minimo <= valor <= maximo:
        raise ValueError('CLUBE_DADOS')
    return valor


def registrar_clube(app, banco, login_obrigatorio, admin_obrigatorio, json_body):
    @app.get('/api/fidelidade')
    @login_obrigatorio
    def carteira_clube():
        try:
            pagina = int(request.args.get('pagina', '0'))
            inteiro(pagina, 0, 10000)
            dados = banco.rpc('spa_carteira', {'p_usuario': str(g.usuario['id']), 'p_pagina': pagina}).execute().data
            if not isinstance(dados, dict):
                raise RuntimeError('Resposta inválida da carteira')
            dados['mais'] = len(dados.get('resgates', [])) > 20 or len(dados.get('movimentos', [])) > 20
            dados['resgates'] = dados.get('resgates', [])[:20]
            dados['movimentos'] = dados.get('movimentos', [])[:20]
            return jsonify(dados)
        except ValueError:
            return erro_clube(ValueError('CLUBE_DADOS'))
        except Exception as erro:
            return erro_clube(erro)

    @app.post('/api/fidelidade/resgatar')
    @login_obrigatorio
    def resgatar_clube():
        try:
            dados = json_body()
            recompensa = dados.get('recompensa')
            if recompensa not in {'pausa', 'ritual', 'essencial'}:
                raise ValueError('CLUBE_DADOS')
            resposta = banco.rpc('spa_resgatar', {
                'p_usuario': str(g.usuario['id']), 'p_recompensa': recompensa,
                'p_chave': uuid_valido(dados.get('chave')),
                'p_custo_esperado': inteiro(dados.get('custo'), 1, 1000000),
                'p_desconto_esperado': inteiro(dados.get('desconto_centavos'), 100, 100000),
            }).execute().data
            return jsonify(resposta)
        except Exception as erro:
            return erro_clube(erro)

    @app.post('/api/fidelidade/resgates/<resgate_id>/cancelar')
    @login_obrigatorio
    def cancelar_resgate_clube(resgate_id):
        try:
            return jsonify(banco.rpc('spa_cancelar_resgate', {'p_usuario': str(g.usuario['id']), 'p_resgate': uuid_valido(resgate_id)}).execute().data)
        except Exception as erro:
            return erro_clube(erro)

    @app.get('/api/admin/fidelidade/recompensas')
    @admin_obrigatorio
    def listar_recompensas_clube():
        try:
            return jsonify(banco.table('panaceia_recompensas').select('*').order('custo').execute().data or [])
        except Exception as erro:
            return erro_clube(erro)

    @app.post('/api/admin/fidelidade/recompensas/<recompensa_id>')
    @admin_obrigatorio
    def configurar_recompensa_clube(recompensa_id):
        try:
            dados = json_body()
            if recompensa_id not in {'pausa', 'ritual', 'essencial'} or not isinstance(dados.get('ativo'), bool):
                raise ValueError('CLUBE_DADOS')
            return jsonify(banco.rpc('spa_configurar_recompensa', {
                'p_admin': str(g.usuario['id']), 'p_id': recompensa_id,
                'p_custo': inteiro(dados.get('custo'), 1, 1000000),
                'p_desconto': inteiro(dados.get('desconto_centavos'), 100, 100000), 'p_ativo': dados['ativo'],
            }).execute().data)
        except Exception as erro:
            return erro_clube(erro)

    @app.get('/api/admin/fidelidade/consultar')
    @admin_obrigatorio
    def consultar_beneficio_clube():
        try:
            codigo = codigo_valido(request.args.get('codigo'))
            lista = banco.table('panaceia_resgates').select('*').eq('codigo', codigo).limit(1).execute().data or []
            if not lista:
                raise ValueError('CLUBE_NAO_ENCONTRADO')
            resgate = lista[0]
            usuarios = banco.table('usuarios').select('id, nome, email').eq('id', resgate['usuario_id']).limit(1).execute().data or []
            if not usuarios:
                raise ValueError('CLUBE_USUARIO')
            return jsonify({'resgate': resgate, 'cliente': usuarios[0]['nome'], 'email': usuarios[0]['email']})
        except Exception as erro:
            return erro_clube(erro)

    @app.post('/api/admin/fidelidade/utilizar')
    @admin_obrigatorio
    def utilizar_beneficio_clube():
        try:
            dados = json_body()
            agendamento = inteiro(dados.get('agendamento_id'), 1, 9007199254740991)
            return jsonify(banco.rpc('spa_usar_resgate', {'p_admin': str(g.usuario['id']), 'p_codigo': codigo_valido(dados.get('codigo')), 'p_agendamento': str(agendamento)}).execute().data)
        except Exception as erro:
            return erro_clube(erro)
