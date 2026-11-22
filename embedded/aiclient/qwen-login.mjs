#!/usr/bin/env node
/**
 * NEW FILE added by Free API Stack, 2026-11-21. Not part of upstream AIClient2API.
 * See NOTICE at the repository root for the applicable license terms.
 */
/**
 * Qwen OAuth 设备授权独立脚本（不依赖 aiclient 启动流程）
 *
 * 用法：
 *   node qwen-login.mjs start    # 申请设备码，打印授权链接，状态存 ~/.qwen/pending.json
 *   node qwen-login.mjs poll     # 用 pending.json 轮询换 token，写入 ~/.qwen/oauth_creds.json
 *   node qwen-login.mjs          # start + poll 一条龙
 *   node qwen-login.mjs status   # 看当前状态
 *
 * 为什么单独写：aiclient 内置流程把轮询上限硬编码成 300 秒，且会自己弹浏览器；
 * 这里把「拿链接」和「等授权」拆开，链接失效随时重开，不影响正在跑的服务。
 */

import fs from 'fs';
import os from 'os';
import path from 'path';
import crypto from 'crypto';

const DIR = path.join(os.homedir(), '.qwen');
const PENDING = path.join(DIR, 'pending.json');
const CREDS = path.join(DIR, 'oauth_creds.json');

const OAUTH = {
    clientId: 'f0304373b74a44d2b584a3fb70ca9e56',
    scope: 'openid profile email model.completion',
    deviceCode: 'https://chat.qwen.ai/api/v1/oauth2/device/code',
    token: 'https://chat.qwen.ai/api/v1/oauth2/token',
    grantType: 'urn:ietf:params:oauth:grant-type:device_code',
};

// WAF 会拦裸请求，必须装成浏览器
const HEADERS = {
    'Content-Type': 'application/x-www-form-urlencoded',
    Accept: 'application/json',
    'User-Agent':
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    Origin: 'https://chat.qwen.ai',
    Referer: 'https://chat.qwen.ai/',
    'Accept-Language': 'zh-CN,zh;q=0.9',
};

function form(obj) {
    return Object.entries(obj)
        .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
        .join('&');
}

async function post(url, bodyObj) {
    const res = await fetch(url, { method: 'POST', headers: HEADERS, body: form(bodyObj) });
    const text = await res.text();
    let json = null;
    try {
        json = JSON.parse(text);
    } catch {
        /* 非 JSON（通常是 WAF 拦截页） */
    }
    return { ok: res.ok, status: res.status, json, text };
}

async function start() {
    const verifier = crypto.randomBytes(32).toString('base64url');
    const challenge = crypto.createHash('sha256').update(verifier).digest('base64url');

    const r = await post(OAUTH.deviceCode, {
        client_id: OAUTH.clientId,
        scope: OAUTH.scope,
        code_challenge: challenge,
        code_challenge_method: 'S256',
    });

    if (!r.ok || !r.json?.device_code) {
        console.error(`[start] 失败 HTTP ${r.status}: ${(r.text || '').slice(0, 200)}`);
        process.exit(1);
    }

    const d = r.json;
    const state = {
        device_code: d.device_code,
        user_code: d.user_code,
        verification_uri: d.verification_uri,
        verification_uri_complete: d.verification_uri_complete,
        expires_in: d.expires_in || 900,
        code_verifier: verifier,
        created_at: Date.now(),
    };

    fs.mkdirSync(DIR, { recursive: true });
    fs.writeFileSync(PENDING, JSON.stringify(state, null, 2), 'utf8');

    const expireAt = new Date(state.created_at + state.expires_in * 1000);
    console.log('=== URL ===');
    console.log(state.verification_uri_complete);
    console.log('=== CODE ===');
    console.log(state.user_code);
    console.log('=== EXPIRE ===');
    console.log(expireAt.toLocaleString('zh-CN', { hour12: false }), `(还剩 ${state.expires_in} 秒)`);
}

async function poll({ maxMinutes = 60 } = {}) {
    if (!fs.existsSync(PENDING)) {
        console.error('[poll] 没有 pending.json，先跑 start');
        process.exit(1);
    }
    const s = JSON.parse(fs.readFileSync(PENDING, 'utf8'));
    const deadline = Math.min(Date.now() + maxMinutes * 60_000, s.created_at + s.expires_in * 1000);
    let interval = 5;

    while (Date.now() < deadline) {
        const r = await post(OAUTH.token, {
            client_id: OAUTH.clientId,
            device_code: s.device_code,
            grant_type: OAUTH.grantType,
            code_verifier: s.code_verifier,
        });

        const d = r.json;
        if (r.ok && d?.access_token) {
            const creds = {
                ...d,
                expiry_date: Date.now() + (d.expires_in || 3600) * 1000,
            };
            fs.mkdirSync(DIR, { recursive: true });
            fs.writeFileSync(CREDS, JSON.stringify(creds, null, 2), { encoding: 'utf8', mode: 0o600 });
            fs.rmSync(PENDING, { force: true });
            console.log('[poll] 授权成功，凭据已写入', CREDS);
            console.log('[poll] token_type =', d.token_type, '| expires_in =', d.expires_in);
            return true;
        }

        const err = d?.error || `http_${r.status}`;
        if (err === 'authorization_pending') {
            process.stdout.write('.');
        } else if (err === 'slow_down') {
            interval += 5;
            process.stdout.write('s');
        } else if (err === 'expired_token') {
            console.error('\n[poll] 设备码已过期，重新跑 start 拿新链接');
            return false;
        } else if (err === 'access_denied') {
            console.error('\n[poll] 用户拒绝授权');
            return false;
        } else {
            console.error(`\n[poll] 未知错误: ${err} ${JSON.stringify(d).slice(0, 200)}`);
            return false;
        }
        await new Promise((res) => setTimeout(res, interval * 1000));
    }

    console.error('\n[poll] 到点了还没授权（设备码过期或被中断）');
    return false;
}

const cmd = process.argv[2] || 'all';
if (cmd === 'start') {
    await start();
} else if (cmd === 'poll') {
    const ok = await poll();
    process.exit(ok ? 0 : 1);
} else if (cmd === 'status') {
    console.log('pending:', fs.existsSync(PENDING) ? JSON.parse(fs.readFileSync(PENDING, 'utf8')).user_code : '无');
    console.log('creds  :', fs.existsSync(CREDS) ? '存在' : '无');
    if (fs.existsSync(CREDS)) {
        const c = JSON.parse(fs.readFileSync(CREDS, 'utf8'));
        console.log('expiry :', c.expiry_date ? new Date(c.expiry_date).toLocaleString('zh-CN', { hour12: false }) : '未知');
    }
} else {
    await start();
    console.log('\n等待授权中（浏览器开着就点一下，最长 60 分钟）...');
    const ok = await poll();
    process.exit(ok ? 0 : 1);
}
