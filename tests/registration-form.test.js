const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'registration.js'), 'utf8');

function createElement(id) {
    const listeners = new Map();
    return {
        id,
        value: '',
        textContent: '',
        className: '',
        hidden: id === 'verificacaoForm' || id === 'loginAfterVerify',
        disabled: false,
        attributes: {},
        listeners,
        addEventListener(type, callback) { listeners.set(type, callback); },
        setAttribute(name, value) { this.attributes[name] = value; },
        removeAttribute(name) { delete this.attributes[name]; },
        reportValidity() { return true; },
        focus() { this.focused = true; }
    };
}

function setup(fetchImpl, options = {}) {
    const elements = new Map();
    const ids = [
        'cadastroForm', 'verificacaoForm', 'botaoCadastro', 'botaoVerificacao',
        'botaoReenviar', 'status', 'nome', 'email', 'senha', 'emailVerificacao',
        'codigoVerificacao', 'loginAfterVerify'
    ];
    ids.forEach((id) => elements.set(id, createElement(id)));
    const document = { getElementById: (id) => elements.get(id) || null };
    const storage = options.storage || new Map();
    const window = {
        setTimeout: options.setTimeout || global.setTimeout,
        clearTimeout: options.clearTimeout || global.clearTimeout,
        localStorage: {
            getItem: (key) => storage.get(key) ?? null,
            setItem: (key, value) => storage.set(key, value),
            removeItem: (key) => storage.delete(key)
        }
    };
    vm.runInNewContext(source, { document, window, fetch: fetchImpl, AbortController, Promise, JSON, Error });
    return { elements, storage };
}

function response(status, payload) {
    return { ok: status >= 200 && status < 300, json: async () => payload };
}

function submit(elements, id) {
    const form = elements.get(id);
    return form.listeners.get('submit')({
        currentTarget: form,
        preventDefault() {}
    });
}

test('cadastro envia nome, e-mail e senha e abre a etapa de confirmação', async () => {
    let request;
    const { elements } = setup(async (_url, options) => {
        request = options;
        return response(201, { email: 'ana@example.test', message: 'Cadastro iniciado' });
    });
    elements.get('nome').value = 'Ana Silva';
    elements.get('email').value = 'ANA@example.test';
    elements.get('senha').value = 'SenhaSegura!';

    await submit(elements, 'cadastroForm');

    assert.equal(request.method, 'POST');
    assert.deepEqual(JSON.parse(request.body), { nome: 'Ana Silva', email: 'ana@example.test', senha: 'SenhaSegura!' });
    assert.equal(elements.get('cadastroForm').hidden, true);
    assert.equal(elements.get('verificacaoForm').hidden, false);
    assert.equal(elements.get('emailVerificacao').textContent, 'ana@example.test');
    assert.equal(elements.get('codigoVerificacao').focused, true);
    assert.equal(elements.get('senha').value, '');
});

test('senha inválida é bloqueada no navegador sem chamada de cadastro', async () => {
    let calls = 0;
    const { elements } = setup(async () => {
        calls += 1;
        return response(201, {});
    });
    elements.get('nome').value = 'Ana Silva';
    elements.get('email').value = 'ana@example.test';
    elements.get('senha').value = 'SenhaSemSimbolo';

    await submit(elements, 'cadastroForm');

    assert.equal(calls, 0);
    assert.match(elements.get('status').textContent, /Use uma senha/);
    assert.equal(elements.get('senha').focused, true);
});

test('cliques repetidos enquanto o cadastro está pendente geram uma única requisição', async () => {
    let resolveResponse;
    let calls = 0;
    const { elements } = setup(() => {
        calls += 1;
        return new Promise((resolve) => { resolveResponse = resolve; });
    });
    elements.get('nome').value = 'Ana Silva';
    elements.get('email').value = 'ana@example.test';
    elements.get('senha').value = 'SenhaSegura!';

    const first = submit(elements, 'cadastroForm');
    await submit(elements, 'cadastroForm');
    assert.equal(elements.get('botaoCadastro').disabled, true);
    assert.equal(calls, 1);
    resolveResponse(response(201, { email: 'ana@example.test' }));
    await first;
    assert.equal(calls, 1);
});

test('confirmação ativa a conta sem enviar solicitações adicionais', async () => {
    const calls = [];
    const { elements } = setup(async (url, options) => {
        calls.push({ url, body: JSON.parse(options.body) });
        return url === '/cadastrar'
            ? response(201, { email: 'ana@example.test' })
            : response(200, { message: 'E-mail verificado' });
    });
    elements.get('nome').value = 'Ana Silva';
    elements.get('email').value = 'ana@example.test';
    elements.get('senha').value = 'SenhaSegura!';
    await submit(elements, 'cadastroForm');
    elements.get('codigoVerificacao').value = '123456';

    await submit(elements, 'verificacaoForm');

    assert.equal(calls.length, 2);
    assert.deepEqual(calls[1].body, { email: 'ana@example.test', codigo: '123456' });
    assert.equal(elements.get('verificacaoForm').hidden, true);
    assert.equal(elements.get('loginAfterVerify').hidden, false);
    assert.match(elements.get('status').textContent, /conta está ativa/);
});

test('a etapa de confirmação pode ser retomada depois de recarregar a página', async () => {
    const storage = new Map();
    const first = setup(async () => response(201, { email: 'ana@example.test' }), { storage });
    first.elements.get('nome').value = 'Ana Silva';
    first.elements.get('email').value = 'ana@example.test';
    first.elements.get('senha').value = 'SenhaSegura!';
    await submit(first.elements, 'cadastroForm');

    const second = setup(async () => response(200, { message: 'Verificado' }), { storage });
    assert.equal(second.elements.get('cadastroForm').hidden, true);
    assert.equal(second.elements.get('verificacaoForm').hidden, false);
    assert.equal(second.elements.get('emailVerificacao').textContent, 'ana@example.test');
    second.elements.get('codigoVerificacao').value = '123456';
    await submit(second.elements, 'verificacaoForm');
    assert.equal(storage.has('email_em_verificacao'), false);
});

test('erro de servidor preserva campos e permite nova tentativa manual', async () => {
    let calls = 0;
    const { elements } = setup(async () => {
        calls += 1;
        return response(409, { error: 'Este e-mail já está cadastrado no sistema.' });
    });
    elements.get('nome').value = 'Ana Silva';
    elements.get('email').value = 'ana@example.test';
    elements.get('senha').value = 'SenhaSegura!';

    await submit(elements, 'cadastroForm');

    assert.equal(calls, 1);
    assert.match(elements.get('status').textContent, /já está cadastrado/);
    assert.equal(elements.get('nome').value, 'Ana Silva');
    assert.equal(elements.get('email').value, 'ana@example.test');
    assert.equal(elements.get('senha').value, 'SenhaSegura!');
    assert.equal(elements.get('botaoCadastro').disabled, false);
});

test('resposta 2xx sem JSON não afirma sucesso e mantém os campos', async () => {
    let calls = 0;
    const { elements } = setup(async () => {
        calls += 1;
        return { ok: true, json: async () => { throw new SyntaxError('invalid JSON'); } };
    });
    elements.get('nome').value = 'Ana Silva';
    elements.get('email').value = 'ana@example.test';
    elements.get('senha').value = 'SenhaSegura!';

    await submit(elements, 'cadastroForm');

    assert.equal(calls, 1);
    assert.equal(elements.get('cadastroForm').hidden, false);
    assert.equal(elements.get('verificacaoForm').hidden, true);
    assert.match(elements.get('status').textContent, /cadastro pode ter sido iniciado/);
    assert.equal(elements.get('nome').value, 'Ana Silva');
    assert.equal(elements.get('email').value, 'ana@example.test');
    assert.equal(elements.get('senha').value, 'SenhaSegura!');
});

test('resposta 2xx sem o e-mail esperado não avança para confirmação', async () => {
    const { elements } = setup(async () => response(201, { message: 'Cadastro iniciado' }));
    elements.get('nome').value = 'Ana Silva';
    elements.get('email').value = 'ana@example.test';
    elements.get('senha').value = 'SenhaSegura!';

    await submit(elements, 'cadastroForm');

    assert.equal(elements.get('cadastroForm').hidden, false);
    assert.equal(elements.get('verificacaoForm').hidden, true);
    assert.match(elements.get('status').textContent, /não pôde ser confirmada/);
    assert.equal(elements.get('email').value, 'ana@example.test');
    assert.equal(elements.get('senha').value, 'SenhaSegura!');
});

test('timeout não repete o cadastro e mantém os dados preenchidos', async () => {
    let calls = 0;
    const { elements } = setup(() => {
        calls += 1;
        return new Promise(() => {});
    }, {
        setTimeout(callback) {
            queueMicrotask(callback);
            return 1;
        },
        clearTimeout() {}
    });
    elements.get('nome').value = 'Ana Silva';
    elements.get('email').value = 'ana@example.test';
    elements.get('senha').value = 'SenhaSegura!';

    await submit(elements, 'cadastroForm');

    assert.equal(calls, 1);
    assert.match(elements.get('status').textContent, /pode ter sido iniciado/);
    assert.equal(elements.get('nome').value, 'Ana Silva');
    assert.equal(elements.get('email').value, 'ana@example.test');
    assert.equal(elements.get('senha').value, 'SenhaSegura!');
    assert.equal(elements.get('botaoCadastro').disabled, false);
});
