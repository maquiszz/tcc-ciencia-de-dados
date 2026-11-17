(function () {
    'use strict';

    const METADADOS_SERVICOS = {
        'acupuntura': { categoria: 'Terapias Orientais', duracao: '50 min' },
        'sangria': { categoria: 'Terapias Orientais', duracao: '40 min' },
        'banho de lama': { categoria: 'Banhos & Termais', duracao: '60 min' },
        'fonte termal': { categoria: 'Banhos & Termais', duracao: '60 min' },
        'quiropraxia': { categoria: 'Corporal & Alinhamento', duracao: '50 min' },
        'massagem': { categoria: 'Massagens & Relaxamento', duracao: '60 min' },
        'aromaterapia': { categoria: 'Massagens & Relaxamento', duracao: '50 min' },
        'pedras quentes': { categoria: 'Massagens & Relaxamento', duracao: '75 min' }
    };

    const state = {
        favoritos: new Set(),
        categoriaAtiva: 'todos',
        buscaTexto: '',
        comparacaoIds: [],
        analyticsPeriodo: '30d',
        analyticsCharts: {}
    };

    function obterMetaServico(servico) {
        const nome = String((servico && servico.tipo) || '').toLowerCase();
        for (const chave of Object.keys(METADADOS_SERVICOS)) {
            if (nome.includes(chave)) return METADADOS_SERVICOS[chave];
        }
        return { categoria: 'Bem-estar', duracao: '60 min' };
    }

    function formatarMoedaBR(valor) {
        const num = Number(valor || 0);
        return 'R$ ' + num.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    async function carregarFavoritosServidor() {
        if (typeof window.usuarioLogado === 'undefined' || !window.usuarioLogado) {
            state.favoritos.clear();
            atualizarEstadoFavoritosUI();
            return;
        }
        try {
            const resp = await window.apiFetch(`${window.BACKEND_URL}/api/favoritos`);
            if (!resp.ok) return;
            const data = await resp.json();
            state.favoritos = new Set((data.favoritos || []).map(Number));
            atualizarEstadoFavoritosUI();
            renderizarFavoritosPerfil();
        } catch (_) {
            // Falha silenciosa para não bloquear navegação
        }
    }

    async function alternarFavorito(servicoId) {
        const id = Number(servicoId);
        if (!Number.isSafeInteger(id) || id <= 0) return;

        if (!window.usuarioLogado) {
            window.exibirNotificacaoSpa(
                'Entre para salvar favoritos',
                'Faça login na sua conta para favoritar seus tratamentos preferidos.',
                'aviso'
            );
            if (typeof window.alternarAbas === 'function') window.alternarAbas('login_tela');
            return;
        }

        const jaFavorito = state.favoritos.has(id);
        const metodo = jaFavorito ? 'DELETE' : 'POST';
        try {
            const resp = await window.apiFetch(`${window.BACKEND_URL}/api/favoritos/${id}`, { method: metodo });
            if (!resp.ok) throw new Error('Falha ao atualizar favorito.');
            if (jaFavorito) {
                state.favoritos.delete(id);
                window.exibirNotificacaoSpa('Favorito removido', 'O tratamento foi removido dos seus favoritos.', 'sucesso');
            } else {
                state.favoritos.add(id);
                window.exibirNotificacaoSpa('Salvo nos favoritos', 'Você poderá encontrá-lo rapidamente na sua área do cliente.', 'sucesso');
            }
            atualizarEstadoFavoritosUI();
            renderizarFavoritosPerfil();
            if (state.categoriaAtiva === 'favoritos') aplicarFiltrosCatalogo();
        } catch (err) {
            window.exibirNotificacaoSpa('Não foi possível salvar', err.message || 'Tente novamente em instantes.', 'erro');
        }
    }

    function atualizarEstadoFavoritosUI() {
        document.querySelectorAll('.btn-favorito-card').forEach((btn) => {
            const id = Number(btn.dataset.servicoId);
            const ativo = state.favoritos.has(id);
            btn.setAttribute('aria-pressed', ativo ? 'true' : 'false');
            btn.setAttribute('aria-label', ativo ? 'Remover dos favoritos' : 'Adicionar aos favoritos');
            btn.title = ativo ? 'Remover dos favoritos' : 'Favoritar tratamento';
            btn.textContent = ativo ? '♥' : '♡';
        });
    }

    function aplicarFiltrosCatalogo() {
        const container = document.getElementById('container-servicos');
        if (!container) return;
        const cards = container.querySelectorAll('.card[data-servico-id]');
        let visiveis = 0;
        const termo = state.buscaTexto.trim().toLowerCase();

        cards.forEach((card) => {
            const id = Number(card.dataset.servicoId);
            const cat = (card.dataset.categoria || '').toLowerCase();
            const texto = (card.dataset.busca || '').toLowerCase();

            const passaCategoria =
                state.categoriaAtiva === 'todos' ||
                (state.categoriaAtiva === 'favoritos' && state.favoritos.has(id)) ||
                cat === state.categoriaAtiva.toLowerCase();

            const passaBusca = !termo || texto.includes(termo);
            const mostrar = passaCategoria && passaBusca;
            card.style.display = mostrar ? '' : 'none';
            if (mostrar) visiveis += 1;
        });

        let vazioEl = document.getElementById('catalogo-filtro-vazio');
        if (visiveis === 0 && cards.length > 0) {
            if (!vazioEl) {
                vazioEl = document.createElement('div');
                vazioEl.id = 'catalogo-filtro-vazio';
                vazioEl.className = 'catalogo-vazio';
                vazioEl.setAttribute('role', 'status');
                vazioEl.innerHTML = '<p>Nenhum tratamento encontrado para esse filtro.</p><button type="button" class="btn-reserva-voltar" id="btn-limpar-filtro-catalogo">Mostrar todos os tratamentos</button>';
                container.appendChild(vazioEl);
            }
            vazioEl.hidden = false;
        } else if (vazioEl) {
            vazioEl.hidden = true;
        }
    }

    function renderizarFavoritosPerfil() {
        const secao = document.getElementById('perfil-favoritos-secao');
        const grade = document.getElementById('perfil-favoritos-grade');
        if (!secao || !grade) return;

        if (!window.usuarioLogado || !Array.isArray(window.servicosDados)) {
            secao.hidden = true;
            return;
        }

        const lista = window.servicosDados.filter((s) => state.favoritos.has(Number(s.id)));
        secao.hidden = false;

        if (lista.length === 0) {
            grade.innerHTML = '<p style="margin:0;color:var(--ink-soft);font-size:0.9rem;">Você ainda não favoritou nenhum tratamento. Clique no coração (♡) nos cards do catálogo para salvar seus preferidos.</p>';
            return;
        }

        grade.innerHTML = lista.map((s) => {
            const meta = obterMetaServico(s);
            return `
                <article class="perfil-favorito-card">
                    <div>
                        <small style="color:var(--brand-strong);font-weight:700;font-size:0.74rem;">${window.escaparHtml(meta.categoria)} · ${window.escaparHtml(meta.duracao)}</small>
                        <h3 style="margin:4px 0;font-size:1rem;">${window.escaparHtml(s.tipo)}</h3>
                        <strong style="font-size:0.95rem;">${formatarMoedaBR(s.valor)}</strong>
                    </div>
                    <div class="perfil-favorito-acoes">
                        <button type="button" class="btn-contratar" data-acao="agendar-direto" data-servico-id="${Number(s.id)}">Agendar</button>
                        <button type="button" class="clientes-botao" data-acao="desfavoritar" data-servico-id="${Number(s.id)}" aria-label="Remover ${window.escaparHtml(s.tipo)} dos favoritos">Remover</button>
                    </div>
                </article>
            `;
        }).join('');
    }

    function atualizarAtalhoRepetirUltimo(agendamentos) {
        const box = document.getElementById('perfil-repetir-rapido');
        const texto = document.getElementById('perfil-repetir-texto');
        const btn = document.getElementById('btn-repetir-ultimo');
        if (!box || !texto || !btn) return;

        if (!Array.isArray(agendamentos) || agendamentos.length === 0) {
            box.hidden = true;
            return;
        }

        const concluidos = agendamentos
            .filter((a) => String(a.status || '').toLowerCase().startsWith('conclu'))
            .sort((a, b) => String(b.data_atendimento).localeCompare(String(a.data_atendimento)));

        const alvo = concluidos[0] || agendamentos[agendamentos.length - 1];
        const servicoId = Number(alvo && (alvo.servico_id || (alvo.servico && alvo.servico.id)));
        if (!Number.isSafeInteger(servicoId) || servicoId <= 0) {
            box.hidden = true;
            return;
        }

        const nomeServico = (alvo.servico && alvo.servico.tipo) || 'seu último tratamento';
        texto.textContent = `Deseja repetir "${nomeServico}"? Você escolherá uma nova data e horário.`;
        btn.dataset.servicoId = String(servicoId);
        box.hidden = false;
    }

    function alternarComparacao(servicoId) {
        const id = Number(servicoId);
        const idx = state.comparacaoIds.indexOf(id);
        if (idx >= 0) {
            state.comparacaoIds.splice(idx, 1);
        } else {
            if (state.comparacaoIds.length >= 3) {
                window.exibirNotificacaoSpa('Limite de comparação', 'Você pode comparar até 3 tratamentos por vez.', 'aviso');
                return;
            }
            state.comparacaoIds.push(id);
        }
        atualizarBarraComparacao();
    }

    function atualizarBarraComparacao() {
        const barra = document.getElementById('comparacao-barra');
        const info = document.getElementById('comparacao-info');
        if (!barra || !info) return;
        document.querySelectorAll('.btn-comparar-servico').forEach((b) => {
            const ativo = state.comparacaoIds.includes(Number(b.dataset.servicoId));
            b.setAttribute('aria-pressed', ativo ? 'true' : 'false');
            b.textContent = ativo ? 'Selecionado ✓' : 'Comparar';
        });
        if (state.comparacaoIds.length === 0) {
            barra.hidden = true;
            return;
        }
        info.textContent = `${state.comparacaoIds.length} tratamento(s) selecionado(s) para comparar.`;
        barra.hidden = false;
    }

    function abrirModalComparacao() {
        const grid = document.getElementById('comparacao-grid');
        if (!grid || !Array.isArray(window.servicosDados)) return;
        const selecionados = window.servicosDados.filter((s) => state.comparacaoIds.includes(Number(s.id)));
        if (selecionados.length === 0) return;

        grid.innerHTML = selecionados.map((s) => {
            const meta = obterMetaServico(s);
            return `
                <article class="comparacao-item">
                    <span style="font-size:0.74rem;font-weight:700;color:var(--brand-strong);">${window.escaparHtml(meta.categoria)}</span>
                    <h4 style="margin:6px 0 4px;">${window.escaparHtml(s.tipo)}</h4>
                    <p style="margin:0 0 8px;font-weight:700;font-size:1.05rem;">${formatarMoedaBR(s.valor)}</p>
                    <p style="margin:0 0 8px;font-size:0.84rem;color:var(--ink-soft);">Duração estimada: <strong>${window.escaparHtml(meta.duracao)}</strong></p>
                    <p style="margin:0 0 12px;font-size:0.86rem;line-height:1.45;">${window.escaparHtml(s.descricao || 'Atendimento personalizado no Spa Panaceia.')}</p>
                    <button type="button" class="btn-contratar" data-acao="agendar-do-comparador" data-servico-id="${Number(s.id)}">Agendar este</button>
                </article>
            `;
        }).join('');

        if (typeof window.abrirModal === 'function') window.abrirModal('modal-comparacao-servicos');
    }

    async function carregarDashboardAnalytics(periodoParam) {
        const painel = document.getElementById('painel-analytics-admin');
        if (!painel) return;

        if (periodoParam) state.analyticsPeriodo = periodoParam;

        document.querySelectorAll('.analytics-periodo-btn').forEach((b) => {
            b.classList.toggle('ativo', b.dataset.periodo === state.analyticsPeriodo);
        });
        const customBox = document.getElementById('analytics-custom-datas');
        if (customBox) customBox.hidden = state.analyticsPeriodo !== 'custom';

        const statusEl = document.getElementById('analytics-status');
        let query = `periodo=${encodeURIComponent(state.analyticsPeriodo)}`;
        if (state.analyticsPeriodo === 'custom') {
            const ini = document.getElementById('analytics-inicio')?.value || '';
            const fim = document.getElementById('analytics-fim')?.value || '';
            if (!ini || !fim) {
                if (statusEl) statusEl.textContent = 'Selecione as datas de início e fim e clique em Aplicar.';
                return;
            }
            query += `&inicio=${encodeURIComponent(ini)}&fim=${encodeURIComponent(fim)}`;
        }

        if (statusEl) statusEl.textContent = 'Carregando indicadores reais do PostgreSQL…';

        try {
            const resp = await window.apiFetch(`${window.BACKEND_URL}/api/admin/analytics?${query}`);
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.error || 'Erro ao carregar indicadores.');

            renderizarKPIsAnalytics(data);
            await renderizarGraficosAnalytics(data);
            if (statusEl) {
                statusEl.textContent = `Período analisado: ${data.periodo.inicio} até ${data.periodo.fim} (${data.periodo.dias} dias).`;
            }
        } catch (err) {
            if (statusEl) statusEl.textContent = err.message || 'Não foi possível carregar os dados analíticos.';
        }
    }

    function renderizarKPIsAnalytics(data) {
        const r = data.resumo || {};
        const set = (id, val) => {
            const el = document.getElementById(id);
            if (el) el.textContent = val;
        };
        set('kpi-ana-total', String(r.total ?? 0));
        set('kpi-ana-concluidos', String(r.concluidos ?? 0));
        set('kpi-ana-pendentes', String(r.pendentes ?? 0));
        set('kpi-ana-cancelados', String(r.cancelados ?? 0));
        set('kpi-ana-comparecimento', r.taxa_comparecimento !== null && r.taxa_comparecimento !== undefined ? `${r.taxa_comparecimento}%` : 'Sem base');
        set('kpi-ana-cancelamento', r.taxa_cancelamento !== null && r.taxa_cancelamento !== undefined ? `${r.taxa_cancelamento}%` : 'Sem base');
        set('kpi-ana-faturamento', formatarMoedaBR(r.faturamento_estimado || 0));

        const limEl = document.getElementById('analytics-limitacoes');
        if (limEl) {
            const lista = Array.isArray(data.limitacoes) ? data.limitacoes : [];
            if (lista.length > 0) {
                limEl.innerHTML = '<strong>Transparência dos dados:</strong><ul style="margin:6px 0 0 18px;">' +
                    lista.map((item) => `<li>${window.escaparHtml(item)}</li>`).join('') + '</ul>';
                limEl.hidden = false;
            } else {
                limEl.hidden = true;
            }
        }
    }

    async function renderizarGraficosAnalytics(data) {
        if (typeof window.carregarChartJs !== 'function') return;
        const ChartLib = await window.carregarChartJs();
        if (!ChartLib) return;

        const destroyIfExists = (key) => {
            if (state.analyticsCharts[key]) {
                state.analyticsCharts[key].destroy();
                delete state.analyticsCharts[key];
            }
        };

        const ctxEvo = document.getElementById('chart-ana-evolucao');
        if (ctxEvo) {
            destroyIfExists('evolucao');
            state.analyticsCharts.evolucao = new ChartLib(ctxEvo, {
                type: 'line',
                data: {
                    labels: (data.evolucao || []).map((d) => d.label),
                    datasets: [
                        { label: 'Total', data: (data.evolucao || []).map((d) => d.total), borderColor: '#5b14b8', backgroundColor: 'rgba(91,20,184,0.12)', fill: true, tension: 0.3 },
                        { label: 'Concluídos', data: (data.evolucao || []).map((d) => d.concluidos), borderColor: '#059669', backgroundColor: 'transparent', tension: 0.3 },
                        { label: 'Cancelados', data: (data.evolucao || []).map((d) => d.cancelados), borderColor: '#dc2626', backgroundColor: 'transparent', tension: 0.3 }
                    ]
                },
                options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: 'bottom' } }, scales: { y: { beginAtZero: true, ticks: { precision: 0 } } } }
            });
        }

        const ctxServ = document.getElementById('chart-ana-servicos');
        if (ctxServ) {
            destroyIfExists('servicos');
            const top = (data.servicos_populares || []).slice(0, 8);
            state.analyticsCharts.servicos = new ChartLib(ctxServ, {
                type: 'bar',
                data: {
                    labels: top.map((s) => s.nome),
                    datasets: [{ label: 'Agendamentos', data: top.map((s) => s.quantidade), backgroundColor: '#6d28d9', borderRadius: 6 }]
                },
                options: { indexAxis: 'y', responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { beginAtZero: true, ticks: { precision: 0 } } } }
            });
        }

        const ctxSem = document.getElementById('chart-ana-semana');
        if (ctxSem) {
            destroyIfExists('semana');
            state.analyticsCharts.semana = new ChartLib(ctxSem, {
                type: 'bar',
                data: {
                    labels: (data.por_dia_semana || []).map((d) => d.dia),
                    datasets: [{ label: 'Agendamentos', data: (data.por_dia_semana || []).map((d) => d.quantidade), backgroundColor: '#0ea5e9', borderRadius: 6 }]
                },
                options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, ticks: { precision: 0 } } } }
            });
        }

        const ctxHora = document.getElementById('chart-ana-horarios');
        if (ctxHora) {
            destroyIfExists('horarios');
            state.analyticsCharts.horarios = new ChartLib(ctxHora, {
                type: 'bar',
                data: {
                    labels: (data.horarios_pico || []).map((h) => h.hora),
                    datasets: [{ label: 'Atendimentos', data: (data.horarios_pico || []).map((h) => h.quantidade), backgroundColor: '#10b981', borderRadius: 6 }]
                },
                options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, ticks: { precision: 0 } } } }
            });
        }
    }

    // Delegação de eventos segura (compatível com CSP script-src-attr 'none')
    document.addEventListener('click', (e) => {
        const chip = e.target.closest('.catalogo-chip');
        if (chip) {
            state.categoriaAtiva = chip.dataset.categoria || 'todos';
            document.querySelectorAll('.catalogo-chip').forEach((c) => c.setAttribute('aria-pressed', c === chip ? 'true' : 'false'));
            aplicarFiltrosCatalogo();
            return;
        }

        if (e.target.closest('#btn-limpar-filtro-catalogo')) {
            state.categoriaAtiva = 'todos';
            state.buscaTexto = '';
            const buscaInput = document.getElementById('catalogo-busca');
            if (buscaInput) buscaInput.value = '';
            document.querySelectorAll('.catalogo-chip').forEach((c) => c.setAttribute('aria-pressed', c.dataset.categoria === 'todos' ? 'true' : 'false'));
            aplicarFiltrosCatalogo();
            return;
        }

        const favBtn = e.target.closest('.btn-favorito-card');
        if (favBtn) {
            e.stopPropagation();
            alternarFavorito(favBtn.dataset.servicoId);
            return;
        }

        const compBtn = e.target.closest('.btn-comparar-servico');
        if (compBtn) {
            alternarComparacao(compBtn.dataset.servicoId);
            return;
        }

        if (e.target.closest('#btn-abrir-comparacao')) {
            abrirModalComparacao();
            return;
        }

        if (e.target.closest('#btn-limpar-comparacao')) {
            state.comparacaoIds = [];
            atualizarBarraComparacao();
            return;
        }

        const acaoEl = e.target.closest('[data-acao]');
        if (acaoEl) {
            const acao = acaoEl.dataset.acao;
            const sid = Number(acaoEl.dataset.servicoId);
            if (acao === 'agendar-direto') {
                window.abrirModalAgendamentoPorId(sid, false);
            } else if (acao === 'desfavoritar') {
                alternarFavorito(sid);
            } else if (acao === 'agendar-do-comparador') {
                if (typeof window.fecharModal === 'function') window.fecharModal('modal-comparacao-servicos');
                window.abrirModalAgendamentoPorId(sid, false);
            }
            return;
        }

        const repBtn = e.target.closest('#btn-repetir-ultimo');
        if (repBtn && repBtn.dataset.servicoId && typeof window.refazerReserva === 'function') {
            window.refazerReserva(Number(repBtn.dataset.servicoId));
            return;
        }

        const perBtn = e.target.closest('.analytics-periodo-btn');
        if (perBtn) {
            carregarDashboardAnalytics(perBtn.dataset.periodo);
            return;
        }

        if (e.target.closest('#btn-analytics-aplicar-custom')) {
            carregarDashboardAnalytics('custom');
        }
    });

    document.addEventListener('input', (e) => {
        if (e.target && e.target.id === 'catalogo-busca') {
            state.buscaTexto = e.target.value || '';
            aplicarFiltrosCatalogo();
        }
    });

    window.SpaEnhancements = {
        obterMetaServico,
        carregarFavoritosServidor,
        aplicarFiltrosCatalogo,
        atualizarEstadoFavoritosUI,
        renderizarFavoritosPerfil,
        atualizarAtalhoRepetirUltimo,
        carregarDashboardAnalytics
    };
})();
