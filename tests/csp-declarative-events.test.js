const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const htmlPath = path.join(__dirname, '..', 'database', 'servicos.html');
const minHtmlPath = path.join(__dirname, '..', 'database', 'servicos.min.html');
const html = fs.readFileSync(htmlPath, 'utf8');
const minHtml = fs.readFileSync(minHtmlPath, 'utf8');

test('servicos.html e servicos.min.html não possuem atributos HTML inline on*= e usam nonce nos scripts inline', () => {
    const rawAttrRegex = /\s(on(?:click|submit|input|change|keypress))=/g;
    assert.deepEqual(html.match(rawAttrRegex) || [], []);
    assert.deepEqual(minHtml.match(rawAttrRegex) || [], []);

    const scriptTags = html.match(/<script\b[^>]*>/g) || [];
    const inlineScripts = scriptTags.filter(tag => !/\bsrc=/.test(tag));
    assert.equal(inlineScripts.length, 2);
    for (const tag of inlineScripts) {
        assert.match(tag, /nonce="__CSP_NONCE__"/);
    }
});

test('despachante declarativo CSP-safe interpreta argumentos e aciona funções permitidas sem eval', () => {
    const startIdx = html.indexOf('function voltarPaginaAnterior()');
    const endIdx = html.indexOf("document.addEventListener('DOMContentLoaded'", startIdx);
    assert.ok(startIdx > 0 && endIdx > startIdx);
    const snippet = html.slice(startIdx, endIdx);

    const chamadas = [];
    const context = {
        String,
        Number,
        WeakSet,
        document: {
            querySelectorAll: () => [],
            getElementById: (id) => ({
                scrollIntoView: (opts) => chamadas.push({ nome: 'scrollIntoView', args: [id, opts] }),
            }),
        },
        history: { length: 2, back: () => chamadas.push({ nome: 'history.back', args: [] }) },
        location: { assign: (url) => chamadas.push({ nome: 'location.assign', args: [url] }) },
        reservaAtual: { resultado: { id: 42 } },
        agendamentoIgnoradoNaConsulta: 99,
        avaliarServico: (...args) => chamadas.push({ nome: 'avaliarServico', args }),
        mudarMesBI: (...args) => chamadas.push({ nome: 'mudarMesBI', args }),
        baixarCalendarioReserva: (...args) => chamadas.push({ nome: 'baixarCalendarioReserva', args }),
    };

    vm.createContext(context);
    vm.runInContext(
        `${snippet}\nthis.executar = executarExpressaoDeclarativaSpa;`,
        context,
        { filename: 'database/servicos.html' }
    );

    const fakeElement = { id: 'botao-teste' };
    const fakeEvent = { key: 'Enter', preventDefault() { this.prevented = true; } };

    context.executar("avaliarServico(15, 'Bom', this)", fakeElement, fakeEvent);
    assert.deepEqual(chamadas.at(-1), {
        nome: 'avaliarServico',
        args: [15, 'Bom', fakeElement],
    });

    context.executar("mudarMesBI(-1)", fakeElement, fakeEvent);
    assert.deepEqual(chamadas.at(-1), {
        nome: 'mudarMesBI',
        args: [-1],
    });

    context.executar("voltarPaginaAnterior()", fakeElement, fakeEvent);
    assert.deepEqual(chamadas.at(-1), {
        nome: 'history.back',
        args: [],
    });

    context.executar("baixarCalendarioReservaAtual()", fakeElement, fakeEvent);
    assert.deepEqual(chamadas.at(-1), {
        nome: 'baixarCalendarioReserva',
        args: [42],
    });

    // Funções fora da allowlist nunca são executadas
    const antes = chamadas.length;
    const res = context.executar("funcaoNaoAutorizada('x')", fakeElement, fakeEvent);
    assert.equal(res, undefined);
    assert.equal(chamadas.length, antes);
});
