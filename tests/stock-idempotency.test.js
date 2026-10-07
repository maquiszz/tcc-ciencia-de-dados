// Exercises the real browser helpers in isolation. The mocked API is not a
// substitute for a PostgreSQL/Supabase concurrency integration test.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const htmlPath = path.join(__dirname, '..', 'database', 'servicos.html');
const html = fs.readFileSync(htmlPath, 'utf8');
const helperStart = html.indexOf('function novoIdOperacaoEstoque()');
const actionStart = html.indexOf('async function alterarEstoqueQtd', helperStart);
const actionEnd = html.indexOf('function solicitarExclusaoEstoque', actionStart);
assert.ok(helperStart >= 0 && actionStart > helperStart && actionEnd > actionStart);
const stockFunctions = html.slice(helperStart, actionEnd);
let nextUuidAcrossTabs = 1;

class MemoryStorage {
    constructor({ failSet = false, failRemove = false } = {}) {
        this.values = new Map();
        this.failSet = failSet;
        this.failRemove = failRemove;
    }
    getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
    setItem(key, value) {
        if (this.failSet) throw new Error('storage blocked');
        this.values.set(key, String(value));
    }
    removeItem(key) {
        if (this.failRemove) throw new Error('storage blocked');
        this.values.delete(key);
    }
}

function createTab({ apiFetch = async () => ({ ok: true, json: async () => ({}) }), sessionStorage = new MemoryStorage(), localStorage = new MemoryStorage() } = {}) {
    const notices = [];
    const document = {
        getElementById(id) {
            if (id.startsWith('qtd-mudar-')) return { value: '2', focus() {} };
            return null;
        },
    };
    const context = {
        window: {
            crypto: {
                randomUUID() {
                    const suffix = String(nextUuidAcrossTabs++).padStart(12, '0');
                    return `00000000-0000-4000-8000-${suffix}`;
                },
            },
            sessionStorage,
            localStorage,
        },
        document,
        estoqueEstado: { itens: [{ id: 7, quantidade: 10 }], operacoesPendentes: new Map() },
        BACKEND_URL: '',
        apiFetch,
        exibirNotificacaoSpa: (...args) => notices.push(args),
        carregarEstoque: async () => {},
        atualizarResumoEstoque: () => {},
        renderizarEstoque: () => {},
        console,
        Map,
        JSON,
        Number,
        Array,
        Error,
        Uint8Array,
    };
    vm.createContext(context);
    vm.runInContext(stockFunctions, context, { filename: htmlPath });
    return { context, notices, sessionStorage, localStorage };
}

function jsonResponse(payload, status = 200) {
    return { ok: status >= 200 && status < 300, status, json: async () => payload };
}

test('operações intencionais iguais em abas diferentes usam chaves diferentes', () => {
    const firstTab = createTab();
    const secondTab = createTab();
    const first = firstTab.context.obterOperacaoEstoquePendente(7, 'adicionar', 2);
    const second = secondTab.context.obterOperacaoEstoquePendente(7, 'adicionar', 2);
    assert.notEqual(first.operacaoId, second.operacaoId);
});

test('pendência após resultado incerto mantém a chave; após confirmação, nova intenção recebe outra', async () => {
    const requests = [];
    let firstRequest = true;
    const tab = createTab({
        apiFetch: async (_url, options) => {
            const payload = JSON.parse(options.body);
            requests.push(payload);
            if (firstRequest) {
                firstRequest = false;
                throw new Error('timeout depois do envio');
            }
            return jsonResponse({ item: { id: 7, quantidade: 12, repetida: true } });
        },
    });

    await tab.context.alterarEstoqueQtd(7, 'adicionar');
    await tab.context.alterarEstoqueQtd(7, 'adicionar');
    assert.equal(requests.length, 2, 'não deve repetir automaticamente uma escrita');
    assert.equal(requests[0].operacao_id, requests[1].operacao_id);

    await tab.context.alterarEstoqueQtd(7, 'adicionar');
    assert.notEqual(requests[1].operacao_id, requests[2].operacao_id);
});

test('não envia movimentação se não consegue persistir a chave de idempotência', async () => {
    let requestCount = 0;
    const tab = createTab({
        sessionStorage: new MemoryStorage({ failSet: true }),
        apiFetch: async () => { requestCount += 1; return jsonResponse({}); },
    });
    await tab.context.alterarEstoqueQtd(7, 'adicionar');
    assert.equal(requestCount, 0);
    assert.equal(tab.notices.at(-1)[0], 'Movimentação não enviada');
});

test('resposta 2xx incompleta mantém a chave para repetição segura', async () => {
    const requests = [];
    let firstResponse = true;
    const tab = createTab({
        apiFetch: async (_url, options) => {
            requests.push(JSON.parse(options.body));
            if (firstResponse) {
                firstResponse = false;
                return jsonResponse({});
            }
            return jsonResponse({ item: { id: 7, quantidade: 12, repetida: true } });
        },
    });
    await tab.context.alterarEstoqueQtd(7, 'adicionar');
    await tab.context.alterarEstoqueQtd(7, 'adicionar');
    assert.equal(requests.length, 2);
    assert.equal(requests[0].operacao_id, requests[1].operacao_id);
});

test('pendência legada compartilhada em localStorage fica bloqueada sem enviar nova chave', () => {
    const legacyStorage = new MemoryStorage();
    legacyStorage.setItem('spa-estoque-operacoes:7', JSON.stringify({ 'adicionar:2': '11111111-1111-4111-8111-111111111111' }));
    const tab = createTab({ localStorage: legacyStorage });
    assert.throws(
        () => tab.context.obterOperacaoEstoquePendente(7, 'adicionar', 2),
        /resultado incerto/,
    );
});

test('mudança de ação ou quantidade cria chaves próprias dentro da aba', () => {
    const tab = createTab();
    const addTwo = tab.context.obterOperacaoEstoquePendente(7, 'adicionar', 2);
    const addThree = tab.context.obterOperacaoEstoquePendente(7, 'adicionar', 3);
    const removeTwo = tab.context.obterOperacaoEstoquePendente(7, 'remover', 2);
    assert.equal(new Set([addTwo.operacaoId, addThree.operacaoId, removeTwo.operacaoId]).size, 3);
});
