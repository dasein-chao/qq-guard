/**
 * Python <-> WorkBuddy 云 SDK 桥接服务
 *
 * 作用：让独立 Python 程序能间接调用平台免密钥模型。
 * Python 通过本地 HTTP 调本服务，本服务用官方 SDK 调平台。
 *
 * 凭证来源：publicConfig（endpoint + publishableKey）。
 * 必须由用户在 WorkBuddy 界面开通云服务后提供，不能硬编码、不能代用户确认。
 *
 * 启动：node server.js
 * 端点：
 *   GET  /health    -> 服务状态与凭证状态
 *   GET  /models    -> 可用模型列表
 *   POST /classify  -> 调用模型做 P0-P3 分级（流式累积）
 */
'use strict';

const http = require('http');
const fs = require('fs');
const path = require('path');

let createWorkBuddyCloud;
try {
  ({ createWorkBuddyCloud } = require('@tencent-ai/workbuddy-cloud-sdk'));
} catch (e) {
  console.error('[启动失败] SDK 未安装:', e.message);
  process.exit(1);
}

const CONFIG_PATH = path.join(__dirname, 'public-config.json');
const PORT = Number(process.env.BRIDGE_PORT || 8787);

function loadConfig() {
  if (!fs.existsSync(CONFIG_PATH)) return null;
  try {
    const c = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'));
    if (!c.endpoint || !c.publishableKey) return null;
    return c;
  } catch {
    return null;
  }
}

let cloudClient = null;
let configFingerprint = '';

function getClient() {
  const cfg = loadConfig();
  if (!cfg) return { error: 'NO_CREDENTIALS' };
  const fp = cfg.endpoint + '|' + cfg.publishableKey;
  if (!cloudClient || configFingerprint !== fp) {
    cloudClient = createWorkBuddyCloud({
      endpoint: cfg.endpoint,
      publishableKey: cfg.publishableKey,
    });
    configFingerprint = fp;
  }
  return { client: cloudClient };
}

function send(res, status, data) {
  const body = JSON.stringify(data, null, 2);
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = '';
    req.on('data', (c) => { data += c; });
    req.on('end', () => {
      try { resolve(JSON.parse(data || '{}')); }
      catch (e) { reject(e); }
    });
  });
}

/** 流式调用，累积 SSE 中的 content delta，返回完整文本 */
async function streamCollect(client, params) {
  let content = '';
  let reasoning = '';
  let usage = null;
  let finishReason = null;
  for await (const chunk of client.llm.chat.completions.create({
    ...params,
    stream: true,
    stream_options: { include_usage: true },
  })) {
    const delta = chunk.choices?.[0]?.delta || {};
    if (delta.content) content += delta.content;
    if (delta.reasoning_content) reasoning += delta.reasoning_content;
    if (chunk.choices?.[0]?.finish_reason) finishReason = chunk.choices[0].finish_reason;
    if (chunk.usage) usage = chunk.usage;
  }
  return { content, reasoning, usage, finishReason };
}

const SYSTEM_PROMPT = `你是 QQ 群消息分级器。只输出 JSON，不要其他文字。
字段：level(P0/P1/P2/P3)、category、title、deadline、needs_action、action_required、importance、urgency、confidence。
P0=错过有明确损失的紧急事（临时调整/临近截止/需立即确认）；P1=重要机会或正式通知（科研/竞赛/奖学金）；P2=一般有价值（资料/讨论）；P3=闲聊广告。
私聊一律 P3。未知字段填"待核实"。`;

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://127.0.0.1:${PORT}`);

  if (url.pathname === '/health') {
    const cfg = loadConfig();
    return send(res, 200, {
      ok: true,
      sdk: 'loaded',
      credentials: cfg ? 'ready' : 'missing',
      hint: cfg ? '' : '需要在 WorkBuddy 界面开通云服务，并把 publicConfig 写入 public-config.json',
    });
  }

  if (url.pathname === '/models') {
    const { client, error } = getClient();
    if (error) return send(res, 503, { ok: false, error, hint: '缺少云服务凭证' });
    try {
      const models = await client.llm.models.list();
      const safe = models.map((m) => ({
        id: m.id,
        name: m.name,
        enabled: m.disabled !== true && m.enabled !== false,
        credits: m.credits || '',
        maxInputTokens: m.maxInputTokens,
      }));
      return send(res, 200, { ok: true, count: safe.length, models: safe });
    } catch (e) {
      return send(res, 502, { ok: false, error: e?.error?.code || e.message, requestId: e?.requestId });
    }
  }

  if (url.pathname === '/classify' && req.method === 'POST') {
    const { client, error } = getClient();
    if (error) return send(res, 503, { ok: false, error, hint: '缺少云服务凭证' });
    let body;
    try { body = await readBody(req); }
    catch { return send(res, 400, { ok: false, error: 'invalid_json' }); }
    const messages = body.messages;
    if (!Array.isArray(messages) || messages.length === 0) {
      return send(res, 400, { ok: false, error: 'messages_required' });
    }
    // 强制 system 消息在首位（SDK 要求）
    const msgs = messages[0]?.role === 'system'
      ? messages
      : [{ role: 'system', content: SYSTEM_PROMPT }, ...messages];
    const model = body.model || 'glm-5.3-flash';
    try {
      const result = await streamCollect(client, {
        model,
        messages: msgs,
        temperature: body.temperature ?? 1,
      });
      return send(res, 200, {
        ok: true,
        model,
        content: result.content,
        finishReason: result.finishReason,
        usage: result.usage ? {
          promptTokens: result.usage.prompt_tokens,
          completionTokens: result.usage.completion_tokens,
          totalTokens: result.usage.total_tokens,
          credit: result.usage.credit,
        } : null,
      });
    } catch (e) {
      const code = e?.error?.code || e.message || 'unknown';
      const retryable = String(code).startsWith('gateway_') || String(code).startsWith('model_')
        || (String(code) === 'quota_rate_limited');
      return send(res, 502, {
        ok: false,
        error: code,
        retryable,
        requestId: e?.requestId || '',
        hint: String(code).startsWith('quota_') ? '额度不足，请切换到备用模型' : '',
      });
    }
  }

  return send(res, 404, { ok: false, error: 'not_found' });
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`[桥接服务] http://127.0.0.1:${PORT}`);
  console.log(`[凭证状态] ${loadConfig() ? '已配置' : '未配置（需用户在 WorkBuddy 界面开通云服务）'}`);
});
