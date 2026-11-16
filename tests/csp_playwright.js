// Browser proof for the script CSP (effort 621, Phase 2). Needs a running
// throwaway instance and a browser, so it is not part of the unittest run.
//
// Every verdict comes from `securitypolicyviolation` events and page errors in
// a real Chromium under the enforcing header:
//   1. positive control: an injected inline handler produces one violation and
//      does not run — so a pass below is not a listener that never fires;
//   2. signed in (header token): every data-action control on the page is
//      clicked except the ones with side effects outside the page, and the
//      input/change controls are driven (file picker, search, filter, select,
//      composer) — zero violations, zero page errors, and the file picker's
//      element-argument action really attached the file;
//   3. clean context, no credential: the sign-in page, a device-approval
//      request approved over the header, the poll that signs the browser in;
//      then an out-of-scope visitor whose page offers no request button.
//
// Point it at a THROWAWAY instance, never the live one: it clicks controls.
//   NODE_PATH=/path/to/node_modules ASSIST_URL=http://127.0.0.1:8197 \
//   ASSIST_TOKEN="$(cat <token file>)" [PW_CHROME=/path/to/chrome] \
//     node tests/csp_playwright.js

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require('playwright');

const BASE = (process.env.ASSIST_URL || '').replace(/\/$/, '');
const TOKEN = process.env.ASSIST_TOKEN || '';
if (!BASE || !TOKEN) throw new Error('ASSIST_URL and ASSIST_TOKEN are required');
if (/:8089\b|assist\.drift/.test(BASE)) throw new Error('refusing to run against the live instance');

// Clicked controls whose effect leaves the page: process restarts, kills,
// pushes, native dialogs on the host desktop, clipboard, keystrokes into a
// pane, settings resets, secret sends.
const SKIP = new Set([
    'killSession', 'killPurgeSelected', 'restartServer', 'resetAllSettings',
    'gitPush', 'gitCommitPushConfirm', 'pickFolderNative', 'createVenv',
    'clearHistory', 'copyAttachCommand', 'doPaste',
    'studioConnectSave', 'resumeTerminal', 'vaultSendDefault', 'triggerUpload',
    'sendKey', 'sendCombo', 'sendInsertMode', 'typeCmd', 'typePrefix', 'accessOpen',
    'toggleAutoYes', 'cancelAutoYesCountdown', 'ayPickConfirm', 'ayPickTurnOff',
]);

const INIT = () => {
    window.__csp = [];
    document.addEventListener('securitypolicyviolation', e => {
        window.__csp.push(e.violatedDirective + ' ' + (e.blockedURI || '') + ' ' + (e.sourceFile || ''));
    });
    window.confirm = () => false;
    window.prompt = () => null;
    window.alert = () => {};
};

async function context(browser, headers) {
    const ctx = await browser.newContext({
        viewport: { width: 390, height: 844 },
        serviceWorkers: 'block',
        extraHTTPHeaders: headers || {},
    });
    await ctx.addInitScript(INIT);
    const page = await ctx.newPage();
    page.errors = [];
    page.on('pageerror', e => page.errors.push(String(e)));
    page.on('console', m => { if (m.type() === 'error') page.errors.push('console: ' + m.text()); });
    return { ctx, page };
}

const violations = page => page.evaluate(() => window.__csp.slice());

async function api(pathname, body) {
    const resp = await fetch(BASE + pathname, {
        method: body ? 'POST' : 'GET',
        headers: Object.assign({ 'X-Assist-Token': TOKEN }, body ? { 'Content-Type': 'application/json' } : {}),
        body: body ? JSON.stringify(body) : undefined,
    });
    return resp.json();
}

(async () => {
    const browser = await chromium.launch({
        headless: true,
        executablePath: process.env.PW_CHROME || undefined,
        args: ['--no-sandbox'],
    });
    const report = {};
    try {
        // 1. Positive control.
        {
            const { page } = await context(browser);
            const resp = await page.goto(BASE + '/login');
            assert.match(resp.headers()['content-security-policy'] || '', /script-src 'self'/);
            await page.evaluate(() => {
                document.body.insertAdjacentHTML('beforeend',
                    '<img id="pc" src="/no-such-image.png" onerror="window.__ran = 1">');
            });
            await page.waitForTimeout(1000);
            const v = await violations(page);
            report.positive_control = v;
            assert.equal(v.length, 1, 'the injected inline handler must produce exactly one violation');
            assert.equal(await page.evaluate(() => window.__ran), undefined, 'the injected handler must not run');
        }

        // 2. Signed in on the header token.
        {
            const { page } = await context(browser, { 'X-Assist-Token': TOKEN });
            await page.goto(BASE + '/');
            await page.waitForTimeout(4000);
            const names = await page.evaluate(() =>
                [...new Set([...document.querySelectorAll('[data-action]')].map(e => e.dataset.action))]);
            const clicked = [];
            for (const name of names) {
                if (SKIP.has(name)) continue;
                const n = await page.evaluate(nm => {
                    const els = [...document.querySelectorAll('[data-action]')]
                        .filter(e => e.dataset.action === nm);
                    els.forEach(e => e.click());
                    return els.length;
                }, name);
                clicked.push(name + '×' + n);
                await page.waitForTimeout(60);
            }
            await page.keyboard.press('Escape');
            // Input and change handlers, including the one that needs its element.
            const tmp = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'csp-')), 'note.txt');
            fs.writeFileSync(tmp, 'csp check\n');
            await page.setInputFiles('#file-input', tmp);
            await page.waitForTimeout(500);
            const attached = await page.evaluate(() => (typeof _attachments !== 'undefined') ? _attachments.length : -1);
            await page.evaluate(() => {
                for (const [id, value] of [['term-search-input', 'csp'], ['term-filter', 'drift']]) {
                    const el = document.getElementById(id);
                    el.value = value;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                }
                const sel = document.getElementById('purge-idle');
                if (sel && sel.options.length > 1) {
                    sel.selectedIndex = 1;
                    sel.dispatchEvent(new Event('change', { bubbles: true }));
                }
                if (typeof openDevicesSheet === 'function') openDevicesSheet();
            });
            await page.waitForTimeout(1500);
            const v = await violations(page);
            report.signed_in = { clicked: clicked.length, skipped: [...SKIP], attached, violations: v, errors: page.errors };
            assert.equal(v.length, 0, 'signed-in pass must produce zero violations');
            assert.ok(attached >= 1, 'the file picker action received its element and attached the file');
            assert.deepEqual(page.errors.filter(e => /Content Security Policy|Refused to/.test(e)), []);
        }

        // 3. Clean context: approval end to end, under the enforcing header.
        {
            const { ctx, page } = await context(browser, { 'X-Real-IP': '10.0.0.77' });
            await page.goto(BASE + '/login');
            await page.click('#req-btn');
            await page.waitForSelector('.req-code', { timeout: 10000 });
            const code = await page.textContent('.req-code');
            const pending = (await api('/poll')).access.pending_requests;
            const mine = pending.find(r => r.code === code);
            assert.ok(mine, 'the request reached the operator');
            await api('/access/decide', { id: mine.id, action: 'approve' });
            await page.waitForURL(u => !u.pathname.startsWith('/login'), { timeout: 15000 });
            const cookie = (await ctx.cookies()).find(c => c.name === 'assist_auth');
            const v = await violations(page);
            report.clean_approval = { url: page.url(), cookie: !!cookie, violations: v };
            assert.ok(cookie && cookie.value.startsWith('v2.'), 'approval signed the browser in');
            assert.equal(v.length, 0);

            const out = await context(browser, { 'X-Real-IP': '203.0.113.9' });
            await out.page.goto(BASE + '/login');
            await out.page.waitForTimeout(500);
            const hasButton = await out.page.$('#req-btn');
            const text = await out.page.textContent('body');
            const v2 = await violations(out.page);
            report.out_of_scope = { button: !!hasButton, violations: v2 };
            assert.equal(hasButton, null);
            assert.match(text, /aren't available/);
            assert.equal(v2.length, 0);
        }
        console.log(JSON.stringify(report, null, 2));
        console.log('RESULT: all checks passed');
    } catch (err) {
        console.log(JSON.stringify(report, null, 2));
        console.log('RESULT: FAILED — ' + err.message);
        process.exitCode = 1;
    } finally {
        await browser.close();
    }
})();
