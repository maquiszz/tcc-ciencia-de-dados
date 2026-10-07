const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'database', 'servicos.html'), 'utf8');
const helperStart = html.indexOf('async function buscarLeiturasIndependentesDoPerfil()');
const helperEnd = html.indexOf('async function carregarMeusAgendamentos()', helperStart);
assert.ok(helperStart >= 0 && helperEnd > helperStart);
const helperSource = html.slice(helperStart, helperEnd);

function createLoader(apiFetch) {
    const context = { apiFetch, BACKEND_URL: 'https://mock.test' };
    vm.createContext(context);
    vm.runInContext(
        `${helperSource}\nglobalThis.buscar = buscarLeiturasIndependentesDoPerfil;`,
        context,
        { filename: 'database/servicos.html' },
    );
    return context.buscar;
}

test('inicia saldo e agenda em paralelo e devolve as duas respostas', async () => {
    const iniciadas = [];
    const liberar = new Map();
    const buscar = createLoader(url => {
        iniciadas.push(url);
        return new Promise(resolve => liberar.set(url, resolve));
    });

    const pendente = buscar();
    assert.deepEqual(iniciadas, [
        'https://mock.test/api/usuario/pontos',
        'https://mock.test/api/meus-agendamentos',
    ]);
    liberar.get(iniciadas[0])({ ok: true, json: async () => ({ pontos: 12 }) });
    liberar.get(iniciadas[1])({ ok: true, json: async () => [] });
    const resultado = await pendente;

    assert.equal(resultado.pontos.resposta.ok, true);
    assert.equal(resultado.agendamentos.resposta.ok, true);
});

test('falha de uma leitura não rejeita nem descarta a outra', async () => {
    const buscar = createLoader(url => url.endsWith('/pontos')
        ? Promise.reject(new Error('saldo indisponível'))
        : Promise.resolve({ ok: true, json: async () => [] }));

    const resultado = await buscar();
    assert.match(resultado.pontos.erro.message, /saldo indisponível/);
    assert.equal(resultado.agendamentos.resposta.ok, true);
});
