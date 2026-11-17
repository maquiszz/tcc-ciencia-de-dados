"""Serviço de agregação analítica do SPA Panaceia.

Separa a lógica de cálculo de indicadores e séries temporais da camada HTTP,
operando exclusivamente sobre dados reais do PostgreSQL.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any


DIAS_SEMANA_PT = ("Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom")


def _parse_data_iso(valor: str | None) -> date | None:
    if not valor:
        return None
    texto = str(valor).strip()[:10]
    try:
        return date.fromisoformat(texto)
    except ValueError:
        return None


def _parse_timestamp_atendimento(valor: Any) -> datetime | None:
    if isinstance(valor, datetime):
        return valor.replace(tzinfo=None)
    texto = str(valor or "").strip()
    if not texto:
        return None
    texto_norm = texto.replace("Z", "").replace(" ", "T")
    try:
        return datetime.fromisoformat(texto_norm[:19])
    except ValueError:
        try:
            return datetime.strptime(texto_norm[:16], "%Y-%m-%dT%H:%M")
        except ValueError:
            return None


def resolver_intervalo_analytics(
    periodo: str | None,
    inicio_str: str | None,
    fim_str: str | None,
    hoje: date,
) -> tuple[date, date, str]:
    """Resolve o intervalo de datas para os filtros 7d, 30d, 90d ou custom."""
    chave = (periodo or "30d").strip().lower()
    if chave == "custom":
        inicio = _parse_data_iso(inicio_str)
        fim = _parse_data_iso(fim_str)
        if inicio is None or fim is None:
            raise ValueError("Informe as datas de início e fim válidas para o período personalizado.")
        if inicio > fim:
            raise ValueError("A data inicial não pode ser maior que a data final.")
        if (fim - inicio).days > 730:
            raise ValueError("O período personalizado deve ter no máximo 730 dias.")
        return inicio, fim, "custom"

    dias_por_chave = {"7d": 7, "30d": 30, "90d": 90}
    dias = dias_por_chave.get(chave, 30)
    chave_final = chave if chave in dias_por_chave else "30d"
    inicio = hoje - timedelta(days=dias - 1)
    return inicio, hoje, chave_final


def calcular_dashboard_analitico(
    agendamentos: list[dict[str, Any]],
    servicos_por_id: dict[int, dict[str, Any]],
    data_inicio: date,
    data_fim: date,
) -> dict[str, Any]:
    """Calcula KPIs, séries temporais e distribuições a partir de registros reais."""
    total = 0
    concluidos = 0
    cancelados = 0
    pendentes = 0
    faturamento_estimado = 0.0
    possui_valores_servico = bool(servicos_por_id)

    evolucao_map: dict[date, dict[str, int]] = {}
    cursor = data_inicio
    while cursor <= data_fim:
        evolucao_map[cursor] = {"total": 0, "concluidos": 0, "cancelados": 0}
        cursor += timedelta(days=1)

    por_servico: dict[str, dict[str, Any]] = {}
    por_dia_semana = [0] * 7
    por_hora: dict[int, int] = defaultdict(int)

    for item in agendamentos:
        dt = _parse_timestamp_atendimento(item.get("data_atendimento"))
        if dt is None:
            continue
        dia = dt.date()
        if dia < data_inicio or dia > data_fim:
            continue

        status_raw = str(item.get("status") or "Pendente").strip().lower()
        eh_concluido = status_raw in {"concluido", "concluído"}
        eh_cancelado = status_raw == "cancelado"

        total += 1
        if eh_concluido:
            concluidos += 1
        elif eh_cancelado:
            cancelados += 1
        else:
            pendentes += 1

        bucket_dia = evolucao_map.get(dia)
        if bucket_dia is not None:
            bucket_dia["total"] += 1
            if eh_concluido:
                bucket_dia["concluidos"] += 1
            elif eh_cancelado:
                bucket_dia["cancelados"] += 1

        por_dia_semana[dia.weekday()] += 1
        por_hora[dt.hour] += 1

        servico_id = item.get("servico_id")
        servico_emb = item.get("servico") if isinstance(item.get("servico"), dict) else None
        servico_info = servicos_por_id.get(servico_id) if servico_id is not None else None

        nome_servico = (
            (servico_emb or {}).get("tipo")
            or (servico_info or {}).get("tipo")
            or "Serviço não identificado"
        )
        valor_raw = (servico_emb or {}).get("valor")
        if valor_raw is None and servico_info is not None:
            valor_raw = servico_info.get("valor")
        try:
            valor_servico = float(valor_raw) if valor_raw is not None else 0.0
        except (TypeError, ValueError):
            valor_servico = 0.0

        if eh_concluido:
            faturamento_estimado += valor_servico

        if nome_servico not in por_servico:
            por_servico[nome_servico] = {
                "servico_id": servico_id,
                "nome": nome_servico,
                "quantidade": 0,
                "concluidos": 0,
                "receita_estimada": 0.0,
            }
        por_servico[nome_servico]["quantidade"] += 1
        if eh_concluido:
            por_servico[nome_servico]["concluidos"] += 1
            por_servico[nome_servico]["receita_estimada"] = round(
                por_servico[nome_servico]["receita_estimada"] + valor_servico, 2
            )

    encerrados = concluidos + cancelados
    taxa_comparecimento = round((concluidos / encerrados) * 100, 1) if encerrados > 0 else None
    taxa_cancelamento = round((cancelados / total) * 100, 1) if total > 0 else None

    evolucao = [
        {
            "data": d.isoformat(),
            "label": d.strftime("%d/%m"),
            "total": vals["total"],
            "concluidos": vals["concluidos"],
            "cancelados": vals["cancelados"],
        }
        for d, vals in sorted(evolucao_map.items(), key=lambda x: x[0])
    ]

    servicos_populares = sorted(
        por_servico.values(),
        key=lambda s: (-s["quantidade"], s["nome"]),
    )

    distribuicao_semana = [
        {"dia": DIAS_SEMANA_PT[idx], "quantidade": por_dia_semana[idx]}
        for idx in range(7)
    ]

    horas_ordenadas = sorted(por_hora.keys()) if por_hora else list(range(8, 20))
    horarios_pico = [
        {"hora": f"{h:02d}:00", "quantidade": por_hora.get(h, 0)}
        for h in horas_ordenadas
    ]

    limitacoes: list[str] = []
    if total == 0:
        limitacoes.append(
            "Nenhum agendamento encontrado no período selecionado; os gráficos refletem ausência real de registros."
        )
    if encerrados == 0 and total > 0:
        limitacoes.append(
            "Taxa de comparecimento indisponível: todos os agendamentos do período ainda estão pendentes."
        )
    if not possui_valores_servico:
        limitacoes.append("Catálogo de serviços sem valores cadastrados para estimar faturamento.")
    else:
        limitacoes.append(
            "Faturamento estimado calculado pelo preço atual de catálogo dos atendimentos com status Concluído (não há tabela de pagamentos efetivados)."
        )

    return {
        "periodo": {
            "inicio": data_inicio.isoformat(),
            "fim": data_fim.isoformat(),
            "dias": (data_fim - data_inicio).days + 1,
        },
        "resumo": {
            "total": total,
            "concluidos": concluidos,
            "cancelados": cancelados,
            "pendentes": pendentes,
            "encerrados": encerrados,
            "taxa_comparecimento": taxa_comparecimento,
            "taxa_cancelamento": taxa_cancelamento,
            "faturamento_estimado": round(faturamento_estimado, 2),
        },
        "evolucao": evolucao,
        "servicos_populares": servicos_populares,
        "por_dia_semana": distribuicao_semana,
        "horarios_pico": horarios_pico,
        "limitacoes": limitacoes,
    }
