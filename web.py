"""Embedded Flask Web UI for Selfie Image."""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import threading
from typing import Any, Optional

from .utils import redact_sensitive_data, redact_sensitive_text


try:
    from flask import Flask, jsonify, request, send_file
    from werkzeug.serving import make_server
except Exception:  # pragma: no cover - handled at runtime in AstrBot env
    Flask = None  # type: ignore
    jsonify = None  # type: ignore
    request = None  # type: ignore
    send_file = None  # type: ignore
    make_server = None  # type: ignore


WEB_TASK_ID_RE = re.compile(r"^web-\d{8,}-\d+$")
MAX_WEB_TOKEN_LENGTH = 4096
MAX_WEB_TASK_ID_LENGTH = 64
MAX_CACHE_IMAGE_PATH_LENGTH = 512
MAX_WEB_RECORD_ID_LENGTH = 128
MAX_RECORD_PAGE_LIMIT = 100
WEAK_WEB_TOKENS = {"changeme", "change-me", "change_me", "password", "admin", "123456", "test"}


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Selfie Image 管理面板</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f6f8;
      --panel: #ffffff;
      --line: #d8dee4;
      --muted: #667085;
      --text: #1f2328;
      --primary: #1769e0;
      --primary-weak: #e8f1ff;
      --danger: #c8212f;
      --ok: #138a43;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); }
    header { background: #20242b; color: #fff; padding: 16px 22px; justify-content: space-between; gap: 16px; align-items: center; }
    h1 { font-size: 19px; margin: 0; font-weight: 650; }
    h2 { font-size: 17px; margin: 0 0 12px; }
    h3 { font-size: 14px; margin: 16px 0 8px; }
    main { max-width: 1280px; margin: 0 auto; padding: 16px; display: grid; gap: 14px; }
    .app-shell { display: none; }
    body.authed header.app-shell { display: flex; }
    body.authed main.app-shell { display: grid; }
    .login-page { min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 18px; }
    body.authed .login-page { display: none; }
    .login-box { width: min(420px, 100%); background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 18px; }
    .login-box h1 { color: var(--text); margin-bottom: 8px; }
    nav { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; padding-bottom: 2px; }
    nav button { border: 1px solid var(--line); background: #fff; color: var(--text); border-radius: 6px; padding: 9px 8px; white-space: nowrap; min-width: 0; width: 100%; }
    nav button.active { background: var(--primary); border-color: var(--primary); color: #fff; }
    section { display: none; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 16px; }
    section.active { display: block; }
    label { display: block; font-size: 12px; font-weight: 650; color: #344054; margin: 9px 0 5px; }
    input, select, textarea, button { font: inherit; }
    input, select, textarea {
      width: 100%; border: 1px solid #c9d1d9; border-radius: 6px; padding: 8px 10px; background: #fff; color: var(--text);
    }
    textarea { min-height: 92px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 13px; resize: vertical; }
    button { cursor: pointer; border-radius: 6px; border: 1px solid var(--primary); background: var(--primary); color: #fff; padding: 8px 12px; }
    button.secondary { background: #fff; border-color: var(--line); color: var(--text); }
    button.danger { background: var(--danger); border-color: var(--danger); color: #fff; }
    button.ok { background: var(--ok); border-color: var(--ok); color: #fff; }
    button:disabled { opacity: .55; cursor: not-allowed; }
    .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .grid3 { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
    .grid4 { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
    .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
    .status, pre {
      white-space: pre-wrap; background: #f6f8fa; border: 1px solid var(--line); border-radius: 6px; padding: 10px; min-height: 24px;
      max-width: 100%; overflow-wrap: anywhere; word-break: break-word;
    }
    .muted { color: var(--muted); font-size: 13px; line-height: 1.45; }
    .card { border: 1px solid var(--line); border-radius: 8px; padding: 12px; background: #fff; margin-bottom: 12px; }
    .soft { background: #f8fafc; }
    .between { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
    .channel-row { display: grid; grid-template-columns: minmax(160px, 1fr) 130px 90px auto; gap: 10px; align-items: center; }
    .pill { display: inline-flex; align-items: center; gap: 5px; border-radius: 999px; padding: 3px 8px; background: var(--primary-weak); color: #164a9f; font-size: 12px; }
    .pill.green { background: #e7f7ed; color: #116735; }
    .pill.gray { background: #f1f3f5; color: #57606a; }
    .modal-mask { position: fixed; inset: 0; display: none; align-items: center; justify-content: center; background: rgba(31,35,40,.45); padding: 14px; z-index: 50; }
    .modal-mask.show { display: flex; }
    .modal { width: min(900px, 100%); max-height: 92vh; display: flex; flex-direction: column; overflow: hidden; background: #fff; border-radius: 8px; border: 1px solid var(--line); padding: 16px; box-shadow: 0 18px 60px rgba(31,35,40,.22); }
    .modal-body { overflow: auto; padding-right: 4px; }
    .modal-footer { position: sticky; bottom: 0; margin-top: 14px; padding-top: 12px; background: linear-gradient(180deg, rgba(255,255,255,0), #fff 24px); }
    .toast-wrap { position: fixed; right: 16px; top: 16px; z-index: 100; display: grid; gap: 8px; pointer-events: none; }
    .toast { min-width: 220px; max-width: min(360px, calc(100vw - 32px)); padding: 10px 12px; border-radius: 8px; background: rgba(32,36,43,.96); color: #fff; box-shadow: 0 14px 32px rgba(0,0,0,.16); }
    .toast.ok { background: rgba(19,138,67,.96); }
    .toast.bad { background: rgba(200,33,47,.96); }
    .detail-title { display: flex; align-items: center; gap: 8px; margin: 16px 0 8px; }
    .detail-title h3 { margin: 0; }
    .copy-btn { width: 28px; height: 28px; display: inline-flex; align-items: center; justify-content: center; padding: 0; background: #fff; border-color: var(--line); color: #344054; }
    .copy-btn:hover { border-color: var(--primary); color: var(--primary); background: var(--primary-weak); }
    .copy-btn svg { width: 15px; height: 15px; display: block; }
    .tabs-inline { display: flex; gap: 8px; margin: 10px 0 14px; flex-wrap: wrap; }
    .tabs-inline button { background: #fff; border-color: var(--line); color: var(--text); }
    .tabs-inline button.active { background: var(--primary); border-color: var(--primary); color: #fff; }
    .channel-pane { display: none; }
    .channel-pane.active { display: block; }
    .model-panel { border: 1px solid var(--line); border-radius: 8px; padding: 10px; background: #fff; min-height: 120px; }
    .model-list { display: grid; gap: 7px; margin-top: 8px; }
    .model-list.collapsed { max-height: 240px; overflow-y: auto; border: 1px dashed var(--line); padding: 4px; border-radius: 6px; }
    .model-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; align-items: center; border: 1px solid var(--line); border-radius: 6px; padding: 7px 8px; background: #f8fafc; }
    .model-row.with-provider { grid-template-columns: minmax(0, 1fr) minmax(150px, 190px) auto; }
    .model-row .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .model-row .actions { margin-top: 0; }
    .model-provider { min-width: 150px; padding: 5px 8px; font-size: 12px; }
    .mini { padding: 5px 8px; font-size: 12px; }
    .table { width: 100%; border-collapse: collapse; font-size: 13px; }
    .table th, .table td { text-align: left; border-bottom: 1px solid var(--line); padding: 8px; vertical-align: top; }
    .table th { background: #f6f8fa; font-weight: 650; }
    .preview { max-width: 260px; border: 1px solid var(--line); border-radius: 8px; display: none; margin-top: 10px; }
    .images { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 10px; margin-top: 12px; }
    .images img { width: 100%; border: 1px solid var(--line); border-radius: 8px; background: #fff; }
    .test-panel { display: none; margin-top: 12px; }
    .test-panel.active { display: block; }
    .checkline { display: flex; align-items: center; gap: 8px; min-height: 38px; }
    .checkline input { width: auto; }
    .topline { display: flex; align-items: center; gap: 8px; }
    .topline input { max-width: 280px; }
    @media (max-width: 1100px) {
      .grid4 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .channel-row { grid-template-columns: minmax(160px, 1fr) 120px 90px; }
      .channel-row .actions { grid-column: 1 / -1; }
    }
    @media (max-width: 760px) {
      body.authed header.app-shell { display: block; }
      .grid, .grid3, .grid4, .channel-row { grid-template-columns: 1fr; }
      main { padding: 10px; }
      header .topline { margin-top: 10px; justify-content: flex-start; }
      nav { gap: 6px; }
      nav button { font-size: 13px; padding: 8px 4px; }
      .modal { max-height: 96vh; padding: 12px; }
      .model-row, .model-row.with-provider { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div id="loginPage" class="login-page">
    <div class="login-box">
      <h1>Selfie Image 管理登录</h1>
      <p class="muted">输入 AstrBot 插件配置里的 Web Token。</p>
      <label>Web Token</label>
      <input id="loginToken" type="password" placeholder="Web Token" autocomplete="current-password">
      <div class="actions">
        <button id="loginBtn" class="ok">登录</button>
      </div>
      <div id="loginStatus" class="status"></div>
    </div>
  </div>

  <header class="app-shell">
    <h1>Selfie Image 生图自拍管理</h1>
    <div class="topline">
      <button id="reloadAll">刷新</button>
      <button id="logoutBtn" class="secondary">退出登录</button>
    </div>
  </header>
  <main class="app-shell">
    <nav>
      <button data-tab="base" class="active">基础设置</button>
      <button data-tab="channels">渠道管理</button>
      <button data-tab="monitor">渠道监控</button>
      <button data-tab="test">渠道测试</button>
      <button data-tab="image">生图设置</button>
      <button data-tab="selfie">形象设置</button>
      <button data-tab="audit">生图审核</button>
      <button data-tab="raw">JSON</button>
    </nav>

    <section id="base" class="active">
      <div class="between">
        <h2>基础设置</h2>
        <span id="healthPill" class="pill">未连接</span>
      </div>
      <label>Web 状态</label><div id="health" class="status">未连接</div>
      <div class="grid">
        <div><label>生图缓存上限（MB）</label><input id="cacheLimitMB" type="number" min="10" max="102400"></div>
        <div><label>缓存说明</label><div class="status">请求图 and 生成图会保存在同一个缓存目录；超过上限后自动清理最旧缓存图片直到低于上限。</div></div>
      </div>
      <h3>权限</h3>
      <div class="grid">
        <div><label>可使用人员白名单</label><textarea id="usableUsers" placeholder="留空表示所有人可用"></textarea></div>
        <div><label>用户黑名单</label><textarea id="blockedUsers"></textarea></div>
        <div><label>白名单用户</label><textarea id="whitelistUsers"></textarea></div>
        <div><label>白名单群组</label><textarea id="whitelistGroups"></textarea></div>
      </div>
      <div id="baseStatus" class="status" style="margin-top:12px"></div>
    </section>

    <section id="channels">
      <div class="between">
        <h2>渠道管理</h2>
        <div class="actions" style="margin-top:0">
          <button onclick="addChannel()">添加生图渠道</button>
          <button class="secondary" onclick="addAuditChannel()">添加审核渠道</button>
        </div>
      </div>
      <p class="muted">支持生图渠道类型：openai、gemini、gemini_openai、z_image_gitee、jimeng2api、grok、agnes。列表只显示概要，点编辑管理接口、缓存模型和启用模型顺序。</p>
      <div class="tabs-inline">
        <button id="channelTabImage" class="active" type="button" onclick="switchChannelPane('image')">生图渠道</button>
        <button id="channelTabAudit" type="button" onclick="switchChannelPane('audit')">审核渠道</button>
      </div>
      <div id="channelPaneImage" class="channel-pane active"><div id="channelList"></div></div>
      <div id="channelPaneAudit" class="channel-pane"><div id="auditChannelList"></div></div>
      <h3>生图模型优先级</h3>
      <div class="grid">
        <div><label>选择已启用模型</label><select id="priorityPicker"></select></div>
        <div><label>操作</label><div class="actions" style="margin-top:0"><button onclick="addPriority()">加入优先级</button><button class="secondary" onclick="clearPriority()">清空优先级</button></div></div>
      </div>
      <textarea id="priorityList" style="display:none"></textarea>
      <div id="priorityRows" class="model-list"></div>
      <div id="channelStatus" class="status" style="margin-top:12px"></div>
    </section>

    <section id="monitor">
      <div class="between">
        <h2>渠道监控</h2>
        <div class="actions" style="margin-top:0">
          <button class="secondary" onclick="loadRecords()">刷新记录</button>
          <button class="danger" onclick="clearRecords()">清空记录</button>
        </div>
      </div>
      <div class="grid4">
        <div><label>来源筛选</label><input id="monitorSource" list="monitorSourceList" placeholder="输入来源关键词"><datalist id="monitorSourceList"></datalist></div>
        <div><label>模型筛选</label><select id="monitorModel"><option value="">全部</option></select></div>
        <div><label>状态</label><select id="monitorSuccess"><option value="">全部</option><option value="true">成功</option><option value="false">失败</option></select></div>
        <div><label>统计</label><div id="monitorStats" class="status"></div></div>
      </div>
      <div style="overflow:auto;margin-top:12px"><table class="table" id="recordTable"></table></div>
      <div id="monitorPager" class="actions"></div>
    </section>

    <section id="test">
      <h2>渠道测试</h2>
      <div class="grid">
        <div><label>生图渠道</label><select id="testChannel"></select></div>
        <div><label>模型</label><select id="testModel"></select></div>
        <div><label>宽高比</label><select id="testAspect"></select></div>
        <div><label>分辨率</label><select id="testResolution"><option>1K</option><option>2K</option><option>4K</option></select></div>
      </div>
      <label>测试提示词</label><textarea id="testPrompt">一只可爱的白色猫咪，坐在樱花树下，柔和光线，精致插画风格</textarea>
      <div class="grid">
        <label class="checkline"><input id="promptEnhance" type="checkbox" checked> 提示词增强</label>
        <label class="checkline"><input id="useSelfie" type="checkbox"> 使用 AI 自拍形象参考图</label>
        <div><label>额外参考图</label><input id="testRefs" type="file" accept="image/png,image/jpeg,image/webp,image/gif,image/bmp,image/avif,image/heic,image/heif,image/tiff,image/svg+xml" multiple></div>
      </div>
      <div class="actions">
        <button class="ok" id="testImageBtn">开始测试</button>
        <button class="secondary" onclick="showTestPanel('request')">请求数据</button>
        <button class="secondary" onclick="showTestPanel('response')">响应数据</button>
        <button class="secondary" onclick="showTestPanel('result')">查看结果</button>
        <button class="danger" onclick="clearTestData()">清空数据</button>
      </div>
      <div id="testStatus" class="status"></div>
      <div id="testRequestPanel" class="test-panel"><h3>请求数据</h3><pre id="testRequestData"></pre></div>
      <div id="testResponsePanel" class="test-panel"><h3>响应数据</h3><pre id="testResponseData"></pre></div>
      <div id="testResultPanel" class="test-panel active"><h3>生成结果</h3><div id="testImages" class="images"></div></div>
    </section>

    <section id="image">
      <h2>生图设置</h2>
      <div class="grid4">
        <div><label>默认宽高比</label><select id="defaultAspect"></select></div>
        <div><label>默认分辨率</label><select id="defaultResolution"><option>1K</option><option>2K</option><option>4K</option></select></div>
        <div><label>最大并发</label><input id="maxConcurrent" type="number" min="1" max="20"></div>
        <div><label>全局超时（秒）</label><input id="globalTimeout" type="number" min="10" max="900"></div>
        <div><label>参考图最大 MB</label><input id="maxImageSize" type="number" min="1" max="100"></div>
        <div><label>单次最多调用次数</label><input id="maxBatchCount" type="number" min="1" max="8"></div>
        <div><label>用户冷却秒数</label><input id="rateLimitSeconds" type="number" min="0"></div>
        <div><label>每日基础额度</label><input id="dailyLimitCount" type="number" min="1"></div>
      </div>
      <div class="grid">
        <label class="checkline"><input id="enableLLMTool" type="checkbox"> 启用 LLM 生图/自拍工具</label>
        <label class="checkline"><input id="showGenerationInfo" type="checkbox"> 命令回复生成耗时/数量</label>
        <label class="checkline"><input id="showModelInfo" type="checkbox"> 命令回复使用模型</label>
        <label class="checkline"><input id="enableDailyLimit" type="checkbox"> 启用每日用户额度</label>
      </div>
      <div id="imageStatus" class="status"></div>
    </section>

    <section id="selfie">
      <h2>形象设置</h2>
      <div class="grid">
        <div><label>机器人名称</label><input id="selfieBotName"></div>
        <div><label>默认自拍比例</label><select id="selfieAspect"></select></div>
      </div>
      <label>自拍人设</label><textarea id="selfiePersonality"></textarea>
      <h3>自拍形象参考图</h3>
      <input id="selfieFile" type="file" accept="image/png,image/jpeg,image/webp,image/gif,image/bmp,image/avif,image/heic,image/heif,image/tiff,image/svg+xml">
      <img id="selfiePreview" class="preview" alt="selfie reference">
      <div class="actions">
        <button id="uploadSelfie">上传并保存</button>
        <button class="secondary" onclick="refreshSelfie()">刷新预览</button>
        <button class="ok" onclick="refreshDailySelfie()">刷新今日穿搭</button>
        <button class="danger" onclick="clearSelfie()">清除参考图</button>
      </div>
      <div id="selfieStatus" class="status"></div>
    </section>

    <section id="audit">
      <h2>生图审核</h2>
      <div class="grid">
        <label class="checkline"><input id="enablePromptAudit" type="checkbox"> 启用提示词审核</label>
        <label class="checkline"><input id="enableOutputAudit" type="checkbox"> 启用出图审核</label>
        <div><label>提示词审核模型</label><select id="promptAuditModel"></select></div>
        <div><label>出图审核模型</label><select id="outputAuditModel"></select></div>
        <div><label>OCR / 识图模型</label><select id="ocrModel"></select></div>
      </div>
      <label>提示词屏蔽词</label><textarea id="blockedWords"></textarea>
      <label>提示词审核模板</label><textarea id="promptAuditTemplate"></textarea>
      <label>出图审核模板</label><textarea id="outputAuditTemplate"></textarea>
      <div id="auditStatus" class="status">提示词屏蔽词、提示词审核、出图审核会在命令、LLM 工具和 Web 渠道测试中生效。出图审核需要选择支持视觉的 OpenAI/Gemini 兼容模型。</div>
    </section>

    <section id="raw">
      <h2>独立配置 JSON</h2>
      <p class="muted">这里编辑的是插件独立配置，不包含 AstrBot 启动用的 Web host/port/token。</p>
      <textarea id="configText" style="min-height:360px"></textarea>
      <div class="actions">
        <button onclick="loadConfig()">读取 JSON</button>
        <button class="ok" onclick="saveJsonConfig()">保存 JSON</button>
      </div>
      <div id="configStatus" class="status"></div>
    </section>
  </main>

  <div id="channelModal" class="modal-mask">
    <div class="modal">
      <div class="between">
        <h2 id="channelModalTitle">编辑生图渠道</h2>
        <button class="secondary" type="button" onclick="closeChannelModal()">关闭</button>
      </div>
      <div class="modal-body">
      <div class="grid">
        <div><label>渠道名</label><input id="modalChannelName"></div>
        <div><label>类型</label><select id="modalProvider"></select></div>
        <div><label>Base URL</label><input id="modalBaseUrl"></div>
        <div><label>API Key</label><input id="modalApiKey" type="password"></div>
        <div><label>代理 URL</label><input id="modalProxy" placeholder="http://127.0.0.1:7890"></div>
        <div><label>默认模型</label><input id="modalModel"></div>
        <div><label>超时（秒）</label><input id="modalTimeout" type="number" min="10" max="900"></div>
      </div>
      <label class="checkline"><input id="modalEnabled" type="checkbox"> 启用渠道</label>
      
      <div class="grid" style="margin-top: 12px;">
        <div class="model-panel">
          <div class="between">
            <h3>缓存模型</h3>
            <span id="cacheCount" class="pill gray">0</span>
          </div>
          <label>搜索缓存</label><input id="cacheSearch" placeholder="输入模型名筛选">
          <div id="cacheModels" class="model-list"></div>
        </div>
        <div class="model-panel">
          <div class="between">
            <h3>已启用模型顺序</h3>
            <span id="enabledCount" class="pill green">0</span>
          </div>
          <div class="grid">
            <div><label>手动添加模型</label><input id="manualModel" placeholder="model-id"></div>
            <div><label>操作</label><button class="secondary" type="button" id="manualAdd">添加</button></div>
          </div>
          <div id="enabledModels" class="model-list"></div>
        </div>
      </div>

      </div>
      <div class="modal-footer">
        <div class="actions" style="margin-top: 0; justify-content: flex-end;">
          <button id="modalRefreshModels" type="button">刷新模型缓存</button>
          <button class="secondary" id="modalEnableAll" type="button">移除全部启用</button>
          <button class="ok" id="modalSave" type="button">保存渠道</button>
        </div>
        <div id="modalStatus" class="status"></div>
      </div>
    </div>
  </div>

  <div id="recordModal" class="modal-mask">
    <div class="modal">
      <div class="between">
        <h2>监控详情</h2>
        <button class="secondary" type="button" onclick="closeRecordDetail()">关闭</button>
      </div>
      <div class="modal-body"><div id="recordDetailBody"></div></div>
    </div>
  </div>

  <div id="toastWrap" class="toast-wrap"></div>

  <script>
    const $ = id => document.getElementById(id);
    const ASPECTS = ['自动','1:1','2:3','3:2','3:4','4:3','4:3','4:5','5:4','9:16','16:9','21:9'].filter((v,i,a)=>a.indexOf(v)===i);
    const PROVIDERS = ['openai','gemini','gemini_openai','z_image_gitee','jimeng2api','grok','agnes'];
    const AUDIT_PROVIDERS = ['openai','gemini','gemini_openai'];
    const MONITOR_PAGE_SIZE = 20;
    let CONFIG = {};
    let RECORDS = [];
    let RECORD_META = {total: 0, filtered: 0, offset: 0, limit: MONITOR_PAGE_SIZE};
    let MONITOR_PAGE = 1;
    let AUTH_TOKEN = localStorage.getItem('selfieImageToken') || localStorage.getItem('aicatToken') || '';
    let IS_FILLING = false;
    let AUTO_SAVE_TIMER = null;
    let MONITOR_LOAD_TIMER = null;
    let ACTIVE_CHANNEL_PANE = 'image';
    let EDITING_CHANNEL_INDEX = -1;
    let EDITING_CHANNEL_KIND = 'image';
    let CURRENT_RECORD = null;
    let TEST_TASK_POLL_TIMER = null;
    let TEST_TASK_ID = '';

    $('loginToken').value = AUTH_TOKEN;

    function headers() { return {'Content-Type':'application/json','X-Selfie-Image-Token':AUTH_TOKEN}; }
    async function api(path, options = {}) {
      const opts = Object.assign({headers: headers()}, options);
      const res = await fetch(path, opts);
      const data = await res.json();
      if (!res.ok || data.success === false) throw new Error(data.error || ('HTTP ' + res.status));
      return data;
    }
    function textList(id) { return ($(id).value || '').split(/[\n,]+/).map(s => s.trim()).filter(Boolean); }
    function setTextList(id, value) { $(id).value = Array.isArray(value) ? value.join('\n') : String(value || ''); }
    function setSelectOptions(id, values, selected = '') {
      const el = $(id);
      el.innerHTML = '';
      for (const value of values) {
        const opt = document.createElement('option');
        opt.value = value;
        opt.textContent = value || '留空';
        if (value === selected) opt.selected = true;
        el.appendChild(opt);
      }
    }
    function showToast(text, type = 'info') {
      const wrap = $('toastWrap');
      if (!wrap || !text) return;
      const node = document.createElement('div');
      node.className = 'toast' + (type === 'ok' ? ' ok' : type === 'bad' ? ' bad' : '');
      node.textContent = text;
      wrap.appendChild(node);
      setTimeout(() => node.remove(), 2600);
    }
    function switchChannelPane(kind = 'image') {
      ACTIVE_CHANNEL_PANE = kind === 'audit' ? 'audit' : 'image';
      $('channelTabImage').classList.toggle('active', ACTIVE_CHANNEL_PANE === 'image');
      $('channelTabAudit').classList.toggle('active', ACTIVE_CHANNEL_PANE === 'audit');
      $('channelPaneImage').classList.toggle('active', ACTIVE_CHANNEL_PANE === 'image');
      $('channelPaneAudit').classList.toggle('active', ACTIVE_CHANNEL_PANE === 'audit');
    }
    function ensureConfig() {
      CONFIG.bot_name ??= '啊呜';
      CONFIG.personality ??= '可爱猫娘助手，说话带“喵”等语气词，活泼俏皮会撒娇';
      CONFIG.permission ??= {};
      CONFIG.image ??= {};
      CONFIG.image_channels ??= [];
      CONFIG.audit_channels ??= [];
      CONFIG.enabled_image_model_priority ??= [];
      const img = CONFIG.image;
      img.enable_llm_tool ??= true;
      img.default_aspect_ratio ??= '自动';
      img.default_resolution ??= '1K';
      img.max_concurrent_tasks ??= 3;
      img.global_timeout ??= 280;
      img.max_image_size_mb ??= 10;
      img.cache_limit_mb ??= 100;
      img.max_batch_count ??= 2;
      img.rate_limit_seconds ??= 0;
      img.enable_daily_limit ??= false;
      img.daily_limit_count ??= 10;
      img.show_generation_info ??= false;
      img.show_model_info ??= false;
      img.blocked_words ??= [];
      img.enable_prompt_audit ??= false;
      img.enable_output_audit ??= false;
      img.prompt_audit_model ??= '';
      img.output_audit_model ??= '';
      img.ocr_model ??= '';
      img.prompt_audit_template ??= '你是生图安全审核员。请判断以下提示词是否安全。提示词：{prompt}。仅输出 JSON：{"allow":true/false,"reason":"原因"}';
      img.output_audit_template ??= '你是图像安全审核员。请判断以下图片是否适合普通用户。仅输出 JSON：{"allow":true/false,"reason":"原因"}';
    }

    function fillForms() {
      IS_FILLING = true;
      try {
        ensureConfig();
        normalizeChannels();
        normalizeAuditChannels();
        const p = CONFIG.permission, img = CONFIG.image;
        setTextList('usableUsers', p.usable_users);
        setTextList('blockedUsers', p.blocked_users);
        setTextList('whitelistUsers', p.whitelist_users);
        setTextList('whitelistGroups', p.whitelist_groups);

        for (const id of ['defaultAspect','selfieAspect','testAspect']) setSelectOptions(id, ASPECTS, img.default_aspect_ratio || '自动');
        $('defaultResolution').value = img.default_resolution || '1K';
        $('testResolution').value = img.default_resolution || '1K';
        $('maxConcurrent').value = img.max_concurrent_tasks;
        $('globalTimeout').value = img.global_timeout;
        $('maxImageSize').value = img.max_image_size_mb;
        $('cacheLimitMB').value = img.cache_limit_mb;
        $('maxBatchCount').value = img.max_batch_count;
        $('rateLimitSeconds').value = img.rate_limit_seconds;
        $('dailyLimitCount').value = img.daily_limit_count;
        $('enableLLMTool').checked = !!img.enable_llm_tool;
        $('showGenerationInfo').checked = !!img.show_generation_info;
        $('showModelInfo').checked = !!img.show_model_info;
        $('enableDailyLimit').checked = !!img.enable_daily_limit;

        $('selfieBotName').value = CONFIG.bot_name || '';
        $('selfiePersonality').value = CONFIG.personality || '';
        $('selfieAspect').value = img.default_aspect_ratio || '自动';

        $('enablePromptAudit').checked = !!img.enable_prompt_audit;
        $('enableOutputAudit').checked = !!img.enable_output_audit;
        setTextList('blockedWords', img.blocked_words);
        $('promptAuditTemplate').value = img.prompt_audit_template || '';
        $('outputAuditTemplate').value = img.output_audit_template || '';

        $('priorityList').value = (CONFIG.enabled_image_model_priority || []).join('\n');
        renderChannels();
        renderAuditChannels();
        refreshModelSelectors();
        renderPriorityRows();
        $('configText').value = JSON.stringify(CONFIG, null, 2);
      } finally {
        IS_FILLING = false;
      }
    }

    function collectForms() {
      ensureConfig();
      CONFIG.bot_name = $('selfieBotName').value.trim() || '啊呜';
      CONFIG.personality = $('selfiePersonality').value || '';
      CONFIG.permission = {
        usable_users: textList('usableUsers'),
        blocked_users: textList('blockedUsers'),
        whitelist_users: textList('whitelistUsers'),
        whitelist_groups: textList('whitelistGroups')
      };
      CONFIG.image.default_aspect_ratio = $('defaultAspect').value || $('selfieAspect').value || '自动';
      CONFIG.image.default_resolution = $('defaultResolution').value || '1K';
      CONFIG.image.max_concurrent_tasks = Number($('maxConcurrent').value || 3);
      CONFIG.image.global_timeout = Number($('globalTimeout').value || 280);
      CONFIG.image.max_image_size_mb = Number($('maxImageSize').value || 10);
      CONFIG.image.cache_limit_mb = Number($('cacheLimitMB').value || 100);
      CONFIG.image.max_batch_count = Number($('maxBatchCount').value || 2);
      CONFIG.image.rate_limit_seconds = Number($('rateLimitSeconds').value || 0);
      CONFIG.image.daily_limit_count = Number($('dailyLimitCount').value || 10);
      CONFIG.image.enable_llm_tool = $('enableLLMTool').checked;
      CONFIG.image.show_generation_info = $('showGenerationInfo').checked;
      CONFIG.image.show_model_info = $('showModelInfo').checked;
      CONFIG.image.enable_daily_limit = $('enableDailyLimit').checked;
      CONFIG.image.enable_prompt_audit = $('enablePromptAudit').checked;
      CONFIG.image.enable_output_audit = $('enableOutputAudit').checked;
      CONFIG.image.prompt_audit_model = $('promptAuditModel').value || '';
      CONFIG.image.output_audit_model = $('outputAuditModel').value || '';
      CONFIG.image.ocr_model = $('ocrModel').value || '';
      delete CONFIG.image.audit_whitelist;
      CONFIG.image.blocked_words = textList('blockedWords');
      CONFIG.image.prompt_audit_template = $('promptAuditTemplate').value;
      CONFIG.image.output_audit_template = $('outputAuditTemplate').value;
      collectChannels();
      prunePriorityList();
      CONFIG.enabled_image_model_priority = textList('priorityList');
      return CONFIG;
    }

    function uniq(values) {
      const out = [], seen = new Set();
      for (const value of values || []) {
        const text = String(value || '').trim();
        if (text && !seen.has(text)) {
          seen.add(text);
          out.push(text);
        }
      }
      return out;
    }
    function modelId(item) {
      if (typeof item === 'string') return item.trim();
      if (item && typeof item === 'object') return String(item.id || item.model || item.name || '').trim();
      return '';
    }
    function normalizeProviderType(value) {
      const raw = String(value || '').trim().toLowerCase().replace(/-/g, '_');
      const aliases = {
        openai_image: 'openai',
        openai_images: 'openai',
        openai_chat: 'gemini_openai',
        openai_compatible: 'gemini_openai',
        chat_completions: 'gemini_openai',
        google: 'gemini',
        google_gemini: 'gemini',
        zimage: 'z_image_gitee',
        z_image: 'z_image_gitee',
        gitee: 'z_image_gitee',
        jimeng: 'jimeng2api',
        jimeng2: 'jimeng2api',
        xai: 'grok',
        x_ai: 'grok'
      };
      const normalized = aliases[raw] || raw;
      return PROVIDERS.includes(normalized) ? normalized : '';
    }
    function inferProviderTypeFromModel(model) {
      const compact = String(model || '').trim().toLowerCase().replace(/[\s_]+/g, '-');
      if (!compact) return '';
      if (compact.includes('agnes')) return 'agnes';
      if (compact.includes('z-image') || compact.startsWith('zimage')) return 'z_image_gitee';
      if (compact.includes('jimeng') || compact.includes('seedream') || compact.includes('doubao-seedream')) return 'jimeng2api';
      if (compact.includes('grok') || compact.includes('xai') || compact.includes('x-ai')) return 'grok';
      if (compact.includes('gpt-image') || compact.includes('dall-e') || compact.includes('dalle')) return 'openai';
      if (compact.includes('gemini') || compact.includes('nano-banana')) return 'gemini';
      return '';
    }
    function resolveModelProviderType(model, defaultProviderType, manualProviderType = '') {
      return normalizeProviderType(manualProviderType)
        || inferProviderTypeFromModel(model)
        || normalizeProviderType(defaultProviderType)
        || 'openai';
    }
    function collectModelProviderTypes(ch, enabled) {
      const enabledSet = new Set(enabled || []);
      const out = {};
      const sources = [ch.model_provider_types, ch.modelProviderTypes, ch.provider_types, ch.providerTypes];
      for (const source of sources) {
        if (!source || typeof source !== 'object' || Array.isArray(source)) continue;
        for (const [model, provider] of Object.entries(source)) {
          const name = String(model || '').trim();
          const resolved = normalizeProviderType(provider);
          if (name && enabledSet.has(name) && resolved) out[name] = resolved;
        }
      }
      for (const item of ch.enabled_models || ch.enabledModels || []) {
        if (!item || typeof item !== 'object') continue;
        const name = modelId(item);
        const resolved = normalizeProviderType(item.provider_type || item.providerType || item.api_type || item.apiType);
        if (name && enabledSet.has(name) && resolved) out[name] = resolved;
      }
      return out;
    }
    function compactModelProviderTypes(ch) {
      const enabledSet = new Set(ch.enabled_models || []);
      const out = {};
      for (const [model, provider] of Object.entries(ch.model_provider_types || {})) {
        const resolved = normalizeProviderType(provider);
        if (enabledSet.has(model) && resolved) out[model] = resolved;
      }
      ch.model_provider_types = out;
      return ch;
    }
    function normalizeChannel(ch) {
      ch = ch && typeof ch === 'object' ? ch : {};
      const enabled = uniq((ch.enabled_models || ch.enabledModels || (ch.model ? [ch.model] : [])).map(modelId));
      const cache = uniq((ch.models_cache || ch.modelsCache || ch.available_models || ch.availableModels || []).map(modelId));
      const providerType = normalizeProviderType(ch.provider_type || ch.providerType || ch.api_type || ch.apiType) || 'openai';
      return compactModelProviderTypes(applyProviderDefaults({
        name: String(ch.name || ch.id || 'new-channel').trim(),
        provider_type: providerType,
        base_url: String(ch.base_url || ch.baseUrl || '').trim(),
        api_key: String(ch.api_key || ch.apiKey || '').trim(),
        model: String(ch.model || enabled[0] || '').trim(),
        timeout: Number(ch.timeout || 280),
        enabled: ch.enabled !== false,
        enabled_models: enabled,
        model_provider_types: collectModelProviderTypes(ch, enabled),
        models_cache: cache,
        proxy: String(ch.proxy || '').trim(),
        extra: ch.extra && typeof ch.extra === 'object' ? ch.extra : {}
      }));
    }
    function normalizeChannels() {
      CONFIG.image_channels = (CONFIG.image_channels || []).map(normalizeChannel);
    }
    function normalizeAuditChannels() {
      CONFIG.audit_channels = (CONFIG.audit_channels || []).map(ch => {
        ch = normalizeChannel(ch);
        if (!AUDIT_PROVIDERS.includes(ch.provider_type)) ch.provider_type = 'openai';
        return ch;
      });
    }
    function channelListFor(kind) {
      return kind === 'audit' ? CONFIG.audit_channels : CONFIG.image_channels;
    }
    function setChannelListFor(kind, list) {
      if (kind === 'audit') CONFIG.audit_channels = list;
      else CONFIG.image_channels = list;
    }
    function setChannelEnabledModels(ch, list) {
      ch.enabled_models = uniq(list);
      ch.model = ch.enabled_models[0] || '';
      compactModelProviderTypes(ch);
    }
    function newChannel() {
      return normalizeChannel({name:'new-channel', provider_type:'openai', base_url:'https://api.openai.com', api_key:'', model:'', enabled_models:[], timeout:280, enabled:true, models_cache:[]});
    }
    function newAuditChannel() {
      return normalizeChannel({name:'audit-channel', provider_type:'openai', base_url:'https://api.openai.com', api_key:'', model:'', enabled_models:[], timeout:280, enabled:true, models_cache:[]});
    }
    function applyProviderDefaults(ch, force = false) {
      ch = ch && typeof ch === 'object' ? ch : {};
      if (ch.provider_type === 'agnes') {
        if (force || !ch.base_url) ch.base_url = 'https://apihub.agnes-ai.com';
        if (force || !ch.model) ch.model = 'agnes-image-2.1-flash';
        if (!Array.isArray(ch.enabled_models) || !ch.enabled_models.length || force) ch.enabled_models = ['agnes-image-2.1-flash'];
        if (!Array.isArray(ch.models_cache) || !ch.models_cache.length || force) ch.models_cache = ['agnes-image-2.1-flash'];
      }
      return ch;
    }
    function addChannel() {
      ensureConfig();
      normalizeChannels();
      CONFIG.image_channels.push(newChannel());
      renderChannels();
      refreshModelSelectors();
      openChannelModal(CONFIG.image_channels.length - 1, 'image');
    }
    function addAuditChannel() {
      ensureConfig();
      normalizeAuditChannels();
      CONFIG.audit_channels.push(newAuditChannel());
      renderAuditChannels();
      refreshModelSelectors();
      openChannelModal(CONFIG.audit_channels.length - 1, 'audit');
    }
    function removeChannel(index, kind = 'image') {
      if (!confirm('确认删除这个渠道？')) return;
      channelListFor(kind).splice(index, 1);
      renderChannels();
      renderAuditChannels();
      refreshModelSelectors();
      scheduleAutoSave('渠道已删除并自动生效');
      showToast('渠道已删除', 'ok');
    }
    function duplicateChannel(index, kind = 'image') {
      const list = channelListFor(kind);
      const copy = JSON.parse(JSON.stringify(list[index] || (kind === 'audit' ? newAuditChannel() : newChannel())));
      copy.name = (copy.name || 'channel') + '-copy';
      list.splice(index + 1, 0, normalizeChannel(copy));
      renderChannels();
      renderAuditChannels();
      refreshModelSelectors();
      scheduleAutoSave('渠道已复制并自动生效');
      showToast('渠道已复制', 'ok');
    }
    function renderChannels() {
      normalizeChannels();
      const box = $('channelList');
      box.innerHTML = '';
      if (!CONFIG.image_channels.length) {
        box.innerHTML = '<div class="card soft muted">还没有生图渠道。</div>';
        return;
      }
      (CONFIG.image_channels || []).forEach((ch, i) => {
        const card = document.createElement('div');
        card.className = 'card';
        card.innerHTML = `
          <div class="channel-row">
            <div>
              <b>${escapeHtml(ch.name || '未命名')}</b>
              <div class="actions" style="margin-top:6px">
                <span class="pill">${escapeHtml(ch.provider_type || 'openai')}</span>
                <span class="pill gray">缓存 ${Number((ch.models_cache || []).length)}</span>
                <span class="pill green">启用 ${Number((ch.enabled_models || []).length)}</span>
              </div>
            </div>
            <div><span class="pill">${escapeHtml(ch.provider_type || 'openai')}</span></div>
            <label class="checkline"><input type="checkbox" ${ch.enabled !== false ? 'checked' : ''}>启用</label>
            <div class="actions" style="margin-top:0">
              <button type="button" onclick="openChannelModal(${i}, 'image')">编辑</button>
              <button class="secondary" type="button" onclick="duplicateChannel(${i}, 'image')">复制</button>
              <button class="danger" type="button" onclick="removeChannel(${i}, 'image')">删除</button>
            </div>
          </div>
        `;
        card.querySelector('input[type="checkbox"]').onchange = event => {
          CONFIG.image_channels[i].enabled = event.target.checked;
          refreshModelSelectors();
          scheduleAutoSave('渠道启用状态已自动生效');
        };
        box.appendChild(card);
      });
    }
    function renderAuditChannels() {
      normalizeAuditChannels();
      const box = $('auditChannelList');
      box.innerHTML = '';
      if (!CONFIG.audit_channels.length) {
        box.innerHTML = '<div class="card soft muted">还没有审核渠道。</div>';
        return;
      }
      (CONFIG.audit_channels || []).forEach((ch, i) => {
        const card = document.createElement('div');
        card.className = 'card';
        card.innerHTML = `
          <div class="channel-row">
            <div>
              <b>${escapeHtml(ch.name || '未命名')}</b>
              <div class="actions" style="margin-top:6px">
                <span class="pill">${escapeHtml(ch.provider_type || 'openai')}</span>
                <span class="pill gray">缓存 ${Number((ch.models_cache || []).length)}</span>
                <span class="pill green">启用 ${Number((ch.enabled_models || []).length)}</span>
              </div>
            </div>
            <div><span class="pill">${escapeHtml(ch.provider_type || 'openai')}</span></div>
            <label class="checkline"><input type="checkbox" ${ch.enabled !== false ? 'checked' : ''}>启用</label>
            <div class="actions" style="margin-top:0">
              <button type="button" onclick="openChannelModal(${i}, 'audit')">编辑</button>
              <button class="secondary" type="button" onclick="duplicateChannel(${i}, 'audit')">复制</button>
              <button class="danger" type="button" onclick="removeChannel(${i}, 'audit')">删除</button>
            </div>
          </div>
        `;
        card.querySelector('input[type="checkbox"]').onchange = event => {
          CONFIG.audit_channels[i].enabled = event.target.checked;
          refreshModelSelectors();
          scheduleAutoSave('审核渠道启用状态已自动生效');
        };
        box.appendChild(card);
      });
    }
    function collectChannels() {
      normalizeChannels();
      normalizeAuditChannels();
    }
    function removeAllEnabledModels() {
      const ch = currentModalChannel();
      setChannelEnabledModels(ch, []);
      $('modalModel').value = '';
      renderModalModels(ch);
      refreshModelSelectors();
    }
    function collectModalChannel() {
      const list = channelListFor(EDITING_CHANNEL_KIND);
      const source = normalizeChannel(list[EDITING_CHANNEL_INDEX] || (EDITING_CHANNEL_KIND === 'audit' ? newAuditChannel() : newChannel()));
      const enabled = Array.from(document.querySelectorAll('#enabledModels .model-row .name')).map(el => el.textContent || '');
      return normalizeChannel(Object.assign({}, source, {
        name: $('modalChannelName').value.trim(),
        provider_type: $('modalProvider').value,
        base_url: $('modalBaseUrl').value.trim(),
        api_key: $('modalApiKey').value.trim(),
        proxy: $('modalProxy').value.trim(),
        model: $('modalModel').value.trim(),
        timeout: Number($('modalTimeout').value || 280),
        enabled: $('modalEnabled').checked,
        enabled_models: enabled,
        model_provider_types: collectModalProviderTypes(enabled),
        models_cache: source.models_cache || []
      }));
    }
    function collectModalProviderTypes(enabled) {
      const enabledSet = new Set(enabled || []);
      const out = {};
      document.querySelectorAll('#enabledModels .model-row').forEach(row => {
        const model = String(row.querySelector('.name')?.textContent || '').trim();
        const provider = normalizeProviderType(row.querySelector('.model-provider')?.value || '');
        if (model && enabledSet.has(model) && provider) out[model] = provider;
      });
      return out;
    }
    function openChannelModal(index, kind = 'image') {
      normalizeChannels();
      normalizeAuditChannels();
      EDITING_CHANNEL_INDEX = index;
      EDITING_CHANNEL_KIND = kind === 'audit' ? 'audit' : 'image';
      const list = channelListFor(EDITING_CHANNEL_KIND);
      const ch = normalizeChannel(list[index] || (EDITING_CHANNEL_KIND === 'audit' ? newAuditChannel() : newChannel()));
      applyProviderDefaults(ch);
      list[index] = ch;
      $('channelModalTitle').textContent = EDITING_CHANNEL_KIND === 'audit' ? '编辑审核渠道' : '编辑生图渠道';
      setSelectOptions('modalProvider', EDITING_CHANNEL_KIND === 'audit' ? AUDIT_PROVIDERS : PROVIDERS, ch.provider_type || 'openai');
      $('modalChannelName').value = ch.name || '';
      $('modalProvider').value = ch.provider_type || 'openai';
      $('modalBaseUrl').value = ch.base_url || '';
      $('modalApiKey').value = ch.api_key || '';
      $('modalProxy').value = ch.proxy || '';
      $('modalModel').value = ch.model || '';
      $('modalTimeout').value = ch.timeout || 280;
      $('modalEnabled').checked = ch.enabled !== false;
      $('cacheSearch').value = '';
      $('manualModel').value = '';
      $('modalStatus').textContent = '';
      renderModalModels(ch);
      $('channelModal').classList.add('show');
    }
    function modalProviderChanged() {
      const ch = currentModalChannel();
      applyProviderDefaults(ch, true);
      $('modalBaseUrl').value = ch.base_url || '';
      $('modalModel').value = ch.model || '';
      renderModalModels(ch);
      refreshModelSelectors();
      scheduleAutoSave('渠道类型已自动生效');
    }
    function closeChannelModal() {
      $('channelModal').classList.remove('show');
      EDITING_CHANNEL_INDEX = -1;
      EDITING_CHANNEL_KIND = 'image';
    }
    function renderModalModels(ch) {
      const list = channelListFor(EDITING_CHANNEL_KIND);
      ch = normalizeChannel(ch || list[EDITING_CHANNEL_INDEX] || (EDITING_CHANNEL_KIND === 'audit' ? newAuditChannel() : newChannel()));
      const enabled = ch.enabled_models || [];
      const search = $('cacheSearch').value.trim().toLowerCase();
      const cacheItems = (ch.models_cache || []).filter(item => !search || item.toLowerCase().includes(search));
      $('cacheCount').textContent = String((ch.models_cache || []).length);
      $('enabledCount').textContent = String(enabled.length);
      
      const cacheModelsEl = $('cacheModels');
      if ((ch.models_cache || []).length > 10) {
        cacheModelsEl.classList.add('collapsed');
      } else {
        cacheModelsEl.classList.remove('collapsed');
      }

      cacheModelsEl.innerHTML = cacheItems.map(item => {
        const active = enabled.includes(item);
        return `<div class="model-row" data-model="${escapeHtml(item)}"><div class="name">${escapeHtml(item)}</div><div class="actions"><button class="${active ? 'secondary' : ''} mini" type="button" onclick="${active ? `removeEnabledModel('${escapeJs(item)}')` : `addEnabledModel('${escapeJs(item)}')`}">${active ? '取消' : '启用'}</button></div></div>`;
      }).join('') || '<div class="muted">没有匹配的缓存模型。</div>';
      $('enabledModels').innerHTML = enabled.map((item, i) => `
        <div class="model-row with-provider">
          <div class="name">${escapeHtml(item)}</div>
          ${modelProviderSelectHtml(ch, item, i)}
          <div class="actions">
            <button class="secondary mini" type="button" onclick="moveEnabledModel(${i}, -1)">上移</button>
            <button class="secondary mini" type="button" onclick="moveEnabledModel(${i}, 1)">下移</button>
            <button class="danger mini" type="button" onclick="removeEnabledModel('${escapeJs(item)}')">移除</button>
          </div>
        </div>`).join('') || '<div class="muted">还没有启用模型。</div>';
    }
    function modelProviderSelectHtml(ch, model, index) {
      const manual = normalizeProviderType((ch.model_provider_types || {})[model] || '');
      let choices = EDITING_CHANNEL_KIND === 'audit' ? AUDIT_PROVIDERS.slice() : PROVIDERS.slice();
      if (manual && !choices.includes(manual)) choices.unshift(manual);
      const auto = resolveModelProviderType(model, ch.provider_type, '');
      const options = [`<option value="" ${manual ? '' : 'selected'}>自动：${escapeHtml(auto)}</option>`]
        .concat(choices.map(provider => `<option value="${escapeHtml(provider)}" ${manual === provider ? 'selected' : ''}>${escapeHtml(provider)}</option>`));
      return `<select class="model-provider" title="模型类型，留在自动会按模型名识别，识别不到再使用渠道默认类型" onchange="setModelProviderType(${index}, this.value)">${options.join('')}</select>`;
    }
    function currentModalChannel() {
      const ch = collectModalChannel();
      channelListFor(EDITING_CHANNEL_KIND)[EDITING_CHANNEL_INDEX] = ch;
      return ch;
    }
    function setModelProviderType(index, provider) {
      const ch = currentModalChannel();
      const model = (ch.enabled_models || [])[index] || '';
      if (!model) return;
      ch.model_provider_types ||= {};
      const resolved = normalizeProviderType(provider);
      if (resolved) ch.model_provider_types[model] = resolved;
      else delete ch.model_provider_types[model];
      compactModelProviderTypes(ch);
      channelListFor(EDITING_CHANNEL_KIND)[EDITING_CHANNEL_INDEX] = ch;
      renderModalModels(ch);
      refreshModelSelectors();
      scheduleAutoSave('模型类型已自动生效');
    }
    function addEnabledModel(name) {
      const ch = currentModalChannel();
      setChannelEnabledModels(ch, (ch.enabled_models || []).concat([name]));
      renderModalModels(ch);
      refreshModelSelectors();
      scheduleAutoSave('启用模型已自动生效');
    }
    function removeEnabledModel(name) {
      const ch = currentModalChannel();
      setChannelEnabledModels(ch, (ch.enabled_models || []).filter(item => item !== name));
      renderModalModels(ch);
      refreshModelSelectors();
      scheduleAutoSave('启用模型已自动生效');
    }
    function moveEnabledModel(index, delta) {
      const ch = currentModalChannel();
      const next = index + delta;
      if (next < 0 || next >= ch.enabled_models.length) return;
      const list = ch.enabled_models.slice();
      const item = list.splice(index, 1)[0];
      list.splice(next, 0, item);
      setChannelEnabledModels(ch, list);
      renderModalModels(ch);
      refreshModelSelectors();
      scheduleAutoSave('启用模型顺序已自动生效');
    }
    async function refreshChannelModels(index = EDITING_CHANNEL_INDEX) {
      const list = channelListFor(EDITING_CHANNEL_KIND);
      const ch = index === EDITING_CHANNEL_INDEX ? currentModalChannel() : normalizeChannel(list[index]);
      $('modalStatus').textContent = `正在刷新 ${ch.name} 模型...`;
      try {
        const res = await api('/api/refresh-image-models', {method:'POST', body: JSON.stringify({channel: ch})});
        ch.models_cache = res.data || [];
        list[index] = ch;
        renderModalModels(ch);
        renderChannels();
        renderAuditChannels();
        refreshModelSelectors();
        scheduleAutoSave('模型缓存已刷新并自动保存');
        $('modalStatus').textContent = `刷新成功：${ch.models_cache.length} 个模型`;
        showToast(`模型缓存已刷新：${ch.models_cache.length} 个`, 'ok');
      } catch (e) { $('modalStatus').textContent = e.message; }
    }
    async function saveChannelModal() {
      const ch = currentModalChannel();
      if (!ch.name) {
        $('modalStatus').textContent = '渠道名不能为空';
        return;
      }
      if (!ch.model && ch.enabled_models.length) ch.model = ch.enabled_models[0];
      channelListFor(EDITING_CHANNEL_KIND)[EDITING_CHANNEL_INDEX] = ch;
      renderChannels();
      renderAuditChannels();
      refreshModelSelectors();
      $('modalStatus').textContent = '保存中...';
      const ok = await persistConfig(false, '渠道已保存并生效');
      $('modalStatus').textContent = ok ? '已保存' : '保存失败，请检查上方提示';
      if (ok) showToast('渠道已保存', 'ok');
      if (ok) closeChannelModal();
    }

    function allModelLabels() {
      collectChannels();
      const labels = [];
      for (const ch of CONFIG.image_channels || []) {
        if (ch.enabled === false) continue;
        for (const model of (ch.enabled_models?.length ? ch.enabled_models : [ch.model]).filter(Boolean)) labels.push(`${ch.name}/${model}`);
      }
      return labels;
    }
    function activeImageModelKeys() {
      collectChannels();
      const keys = [];
      for (const ch of CONFIG.image_channels || []) {
        if (ch.enabled === false || !ch.name) continue;
        for (const model of (ch.enabled_models?.length ? ch.enabled_models : [ch.model]).filter(Boolean)) {
          keys.push(`${ch.name}/${model}`, `${ch.name}:${model}`, model);
        }
      }
      return uniq(keys);
    }
    function auditModelLabels() {
      collectChannels();
      const labels = [];
      for (const ch of CONFIG.audit_channels || []) {
        if (ch.enabled === false) continue;
        for (const model of (ch.enabled_models?.length ? ch.enabled_models : [ch.model]).filter(Boolean)) labels.push(`${ch.name}/${model}`);
      }
      return labels;
    }
    function refreshModelSelectors() {
      const labels = allModelLabels();
      setSelectOptions('priorityPicker', labels, labels[0] || '');
      prunePriorityList();
      const auditLabels = [''].concat(auditModelLabels());
      setSelectOptions('promptAuditModel', auditLabels, CONFIG.image?.prompt_audit_model || '');
      setSelectOptions('outputAuditModel', auditLabels, CONFIG.image?.output_audit_model || '');
      setSelectOptions('ocrModel', auditLabels, CONFIG.image?.ocr_model || '');
      const testChannels = (CONFIG.image_channels || []).filter(c => c.enabled !== false && c.name);
      const currentTestChannel = $('testChannel').value;
      const selectedTestChannel = testChannels.some(c => c.name === currentTestChannel) ? currentTestChannel : (testChannels[0]?.name || '');
      setSelectOptions('testChannel', testChannels.map(c => c.name), selectedTestChannel);
      refreshTestModels();
    }
    function refreshTestModels() {
      const name = $('testChannel').value;
      const ch = (CONFIG.image_channels || []).find(c => c.enabled !== false && c.name === name) || {};
      const models = (ch.enabled_models?.length ? ch.enabled_models : [ch.model]).filter(Boolean);
      setSelectOptions('testModel', models, models.includes(ch.model) ? ch.model : (models[0] || ''));
    }
    function addPriority() {
      const value = $('priorityPicker').value;
      if (!value) return;
      const current = textList('priorityList');
      if (!current.includes(value)) current.push(value);
      $('priorityList').value = current.join('\n');
      renderPriorityRows();
      scheduleAutoSave('模型优先级已自动生效');
    }
    function clearPriority() {
      $('priorityList').value = '';
      renderPriorityRows();
      scheduleAutoSave('模型优先级已清空并自动生效');
    }
    function setPriorityItems(items) {
      $('priorityList').value = uniq(items).join('\n');
      renderPriorityRows();
      scheduleAutoSave('模型优先级已自动生效');
    }
    function prunePriorityList() {
      const allowed = new Set(activeImageModelKeys());
      const current = textList('priorityList');
      const next = current.filter(item => allowed.has(item));
      if (next.length !== current.length || next.some((item, i) => item !== current[i])) {
        $('priorityList').value = next.join('\n');
        renderPriorityRows();
      }
      return next;
    }
    function movePriority(index, delta) {
      const items = textList('priorityList');
      const next = index + delta;
      if (next < 0 || next >= items.length) return;
      const item = items.splice(index, 1)[0];
      items.splice(next, 0, item);
      setPriorityItems(items);
    }
    function removePriority(index) {
      const items = textList('priorityList');
      items.splice(index, 1);
      setPriorityItems(items);
    }
    function renderPriorityRows() {
      const items = textList('priorityList');
      const box = $('priorityRows');
      box.innerHTML = items.map((item, i) => `
        <div class="model-row">
          <div class="name">${escapeHtml(item)}</div>
          <div class="actions">
            <button class="secondary mini" type="button" onclick="movePriority(${i}, -1)">上移</button>
            <button class="secondary mini" type="button" onclick="movePriority(${i}, 1)">下移</button>
            <button class="danger mini" type="button" onclick="removePriority(${i})">移除</button>
          </div>
        </div>`).join('') || '<div class="muted">未设置优先级时按渠道顺序尝试。</div>';
    }

    async function loadConfig() {
      try {
        const res = await api('/api/config');
        CONFIG = res.data || {};
        fillForms();
        setStatus('configStatus', '配置已读取');
      } catch (e) {
        setStatus('configStatus', e.message);
      }
    }
    async function persistConfig(renderAfterSave = false, okText = '配置已保存到插件独立配置文件并生效') {
      try {
        collectForms();
        const res = await api('/api/config', {method:'POST', body: JSON.stringify({config: CONFIG})});
        CONFIG = res.data || CONFIG;
        ensureConfig();
        $('configText').value = JSON.stringify(CONFIG, null, 2);
        if (renderAfterSave) fillForms();
        setMultiStatus(okText);
        showToast(okText, 'ok');
        return true;
      } catch (e) {
        setMultiStatus(e.message);
        showToast(e.message, 'bad');
        return false;
      }
    }
    async function saveAll() {
      await persistConfig(true, '配置已保存到插件独立配置文件并生效');
    }
    function scheduleAutoSave(okText = '配置已自动保存并生效') {
      if (IS_FILLING || !document.body.classList.contains('authed')) return;
      clearTimeout(AUTO_SAVE_TIMER);
      setMultiStatus('正在自动保存...');
      AUTO_SAVE_TIMER = setTimeout(() => persistConfig(false, okText), 650);
    }
    async function saveJsonConfig() {
      try {
        CONFIG = JSON.parse($('configText').value || '{}');
        const res = await api('/api/config', {method:'POST', body: JSON.stringify({config: CONFIG})});
        CONFIG = res.data || CONFIG;
        fillForms();
        setStatus('configStatus', 'JSON 配置已保存');
      } catch (e) { setStatus('configStatus', e.message); }
    }

    async function checkHealth() {
      try {
        const res = await api('/api/health');
        const d = res.data || {};
        $('health').innerHTML = `
          <div><b>状态：</b>${escapeHtml(d.status || 'ok')}</div>
          <div><b>监听：</b>${escapeHtml(String(d.host || ''))}:${escapeHtml(String(d.port || ''))}</div>
          <div><b>Token：</b>${d.auth ? '已启用' : '未启用'}</div>
          <div><b>图片缓存：</b>${escapeHtml(String(d.cache_size_mb ?? 0))} / ${escapeHtml(String(d.cache_limit_mb ?? 100))} MB</div>
          <div><b>缓存目录：</b>${escapeHtml(d.cache_dir || '')}</div>
          <div><b>监控记录：</b>${escapeHtml(d.records_path || '')}</div>
          <div><b>配置文件：</b>${escapeHtml(d.config_path || '')}</div>
        `;
        $('healthPill').textContent = '已连接';
      } catch (e) {
        $('health').textContent = e.message;
        $('healthPill').textContent = '未连接';
      }
    }
    function monitorQueryPath(page = MONITOR_PAGE) {
      const params = new URLSearchParams();
      const source = $('monitorSource').value.trim();
      const model = $('monitorModel').value.trim();
      const success = $('monitorSuccess').value;
      if (source) params.set('source', source);
      if (model) params.set('model', model);
      if (success) params.set('success', success);
      params.set('limit', String(MONITOR_PAGE_SIZE));
      params.set('offset', String((Math.max(1, page) - 1) * MONITOR_PAGE_SIZE));
      return '/api/records?' + params.toString();
    }
    async function loadRecords(showRefreshToast = true) {
      try {
        const res = await api(monitorQueryPath(MONITOR_PAGE));
        RECORDS = res.data || [];
        RECORD_META = {
          total: Number(res.total ?? RECORDS.length),
          filtered: Number(res.filtered ?? RECORDS.length),
          offset: Number(res.offset ?? ((MONITOR_PAGE - 1) * MONITOR_PAGE_SIZE)),
          limit: Number(res.limit ?? MONITOR_PAGE_SIZE)
        };
        const totalPages = Math.max(1, Math.ceil((RECORD_META.filtered || 0) / MONITOR_PAGE_SIZE));
        if (!RECORDS.length && RECORD_META.filtered > 0 && MONITOR_PAGE > totalPages) {
          MONITOR_PAGE = totalPages;
          return await loadRecords(showRefreshToast);
        }
        renderRecords();
        if (showRefreshToast) showToast('记录已刷新', 'ok');
      } catch (e) { $('monitorStats').textContent = e.message; }
    }
    async function clearRecords() {
      try {
        await api('/api/records/clear', {method:'POST', body:'{}'});
        RECORDS = [];
        RECORD_META = {total: 0, filtered: 0, offset: 0, limit: MONITOR_PAGE_SIZE};
        renderRecords();
        showToast('记录已清空', 'ok');
      } catch (e) { $('monitorStats').textContent = e.message; }
    }
    function setMonitorSourceOptions(values) {
      const list = $('monitorSourceList');
      list.innerHTML = '';
      for (const value of values) {
        const opt = document.createElement('option');
        opt.value = value;
        list.appendChild(opt);
      }
    }
    function monitorSourceText(record) {
      return [
        record.source_label || '',
        record.source || '',
        record.group_id || '',
        record.user_id || ''
      ].join(' ');
    }
    function setMonitorPage(page) {
      MONITOR_PAGE = page;
      loadRecords(false);
    }
    function monitorFilterChanged() {
      MONITOR_PAGE = 1;
      clearTimeout(MONITOR_LOAD_TIMER);
      MONITOR_LOAD_TIMER = setTimeout(() => loadRecords(false), 260);
    }
    function renderRecords() {
      const model = $('monitorModel').value.trim();
      const sourceOptions = uniq(RECORDS.map(r => String(r.source_label || r.source || '').trim()).filter(Boolean));
      const modelOptions = uniq(RECORDS.map(r => String(r.used_model || '').trim()).filter(Boolean));
      if (model && !modelOptions.includes(model)) modelOptions.unshift(model);
      setMonitorSourceOptions(sourceOptions);
      setSelectOptions('monitorModel', [''].concat(modelOptions), model);
      const rows = RECORDS;
      const ok = rows.filter(r=>r.success).length;
      const avg = rows.length ? rows.reduce((s,r)=>s+Number(r.elapsed_seconds||0),0)/rows.length : 0;
      const filteredCount = Number(RECORD_META.filtered ?? rows.length);
      const totalCount = Number(RECORD_META.total ?? rows.length);
      const totalPages = Math.max(1, Math.ceil(filteredCount / MONITOR_PAGE_SIZE));
      MONITOR_PAGE = Math.min(Math.max(1, MONITOR_PAGE), totalPages);
      const start = Number(RECORD_META.offset ?? ((MONITOR_PAGE - 1) * MONITOR_PAGE_SIZE));
      const pageRows = rows;
      $('monitorStats').textContent = `记录 ${filteredCount} / 总计 ${totalCount} / 本页成功 ${ok} / 本页失败 ${rows.length-ok} / 本页平均 ${avg.toFixed(2)}s / 第 ${MONITOR_PAGE}/${totalPages} 页`;
      $('recordTable').innerHTML = '<thead><tr><th>时间</th><th>来源</th><th>状态</th><th>模型</th></tr></thead><tbody>' +
        (pageRows.length ? pageRows.map(r => `<tr style="cursor:pointer" title="点击查看详情" onclick="openRecordDetail('${escapeJs(r.id || '')}')"><td>${escapeHtml(r.time||'')}</td><td>${escapeHtml(r.source_label || r.source || '')}</td><td>${r.success?'成功':'失败'}</td><td>${escapeHtml(r.used_model||'')}</td></tr>`).join('') : '<tr><td colspan="4" class="muted">没有匹配的监控记录</td></tr>') +
        '</tbody>';
      $('monitorPager').innerHTML = `
        <button class="secondary mini" type="button" onclick="setMonitorPage(1)" ${MONITOR_PAGE <= 1 ? 'disabled' : ''}>首页</button>
        <button class="secondary mini" type="button" onclick="setMonitorPage(${MONITOR_PAGE - 1})" ${MONITOR_PAGE <= 1 ? 'disabled' : ''}>上一页</button>
        <span class="pill gray">每页 ${MONITOR_PAGE_SIZE} 条，显示 ${pageRows.length ? start + 1 : 0}-${start + pageRows.length}</span>
        <button class="secondary mini" type="button" onclick="setMonitorPage(${MONITOR_PAGE + 1})" ${MONITOR_PAGE >= totalPages ? 'disabled' : ''}>下一页</button>
        <button class="secondary mini" type="button" onclick="setMonitorPage(${totalPages})" ${MONITOR_PAGE >= totalPages ? 'disabled' : ''}>末页</button>
      `;
    }

    function cacheImageUrl(path) {
      return `/api/cache-image?path=${encodeURIComponent(path)}`;
    }
    async function loadProtectedImage(img, path) {
      try {
        const res = await fetch(cacheImageUrl(path), {headers: headers()});
        if (!res.ok) throw new Error('图片已清理');
        const blob = await res.blob();
        const objectUrl = URL.createObjectURL(blob);
        img.onload = () => URL.revokeObjectURL(objectUrl);
        img.src = objectUrl;
      } catch (e) {
        const div = document.createElement('div');
        div.className = 'status';
        div.textContent = e.message || '图片已清理';
        img.replaceWith(div);
      }
    }
    function loadProtectedImages(root = document) {
      root.querySelectorAll('img[data-cache-path]').forEach(img => {
        const path = img.getAttribute('data-cache-path') || '';
        img.removeAttribute('data-cache-path');
        if (path) loadProtectedImage(img, path);
      });
    }
    function imageThumbs(paths) {
      const items = (paths || []).filter(Boolean);
      if (!items.length) return '<div class="muted">无图片</div>';
      return `<div class="images">${items.map(path => `<div><img data-cache-path="${escapeHtml(path)}" alt="${escapeHtml(path)}"><div class="muted">${escapeHtml(path)}</div></div>`).join('')}</div>`;
    }
    function copyIconSvg() {
      return '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>';
    }
    function promptDetailBlock(title, field, text) {
      return `
        <div class="detail-title">
          <h3>${escapeHtml(title)}</h3>
          <button class="copy-btn" type="button" title="复制${escapeHtml(title)}" aria-label="复制${escapeHtml(title)}" onclick="copyRecordField('${field}')">${copyIconSvg()}</button>
        </div>
        <pre>${escapeHtml(text || '')}</pre>
      `;
    }
    async function copyTextToClipboard(text) {
      const value = String(text ?? '');
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(value);
        return;
      }
      const area = document.createElement('textarea');
      area.value = value;
      area.setAttribute('readonly', '');
      area.style.position = 'fixed';
      area.style.left = '-9999px';
      document.body.appendChild(area);
      area.focus();
      area.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(area);
      if (!ok) throw new Error('复制失败');
    }
    async function copyRecordField(field) {
      const r = CURRENT_RECORD || {};
      const text = field === 'request_prompt'
        ? (r.request_prompt || r.prompt || '')
        : field === 'request_data'
          ? JSON.stringify(r.request_data || {}, null, 2)
          : field === 'response_data'
            ? JSON.stringify(r.response_data || {}, null, 2)
            : (r.original_prompt || '');
      if (!text) {
        showToast('内容为空', 'bad');
        return;
      }
      try {
        await copyTextToClipboard(text);
        showToast('已复制到剪贴板', 'ok');
      } catch (e) {
        showToast(e.message || '复制失败', 'bad');
      }
    }
    async function openRecordDetail(id) {
      let r = RECORDS.find(item => String(item.id || '') === String(id || ''));
      try {
        const res = await api('/api/records/' + encodeURIComponent(String(id || '')));
        r = res.data || r;
      } catch (e) {
        showToast(e.message || '记录详情读取失败', 'bad');
      }
      if (!r) return;
      CURRENT_RECORD = r;
      $('recordDetailBody').innerHTML = `
        <div class="grid">
          <div><label>时间</label><div class="status">${escapeHtml(r.time || '')}</div></div>
          <div><label>来源</label><div class="status">${escapeHtml(r.source_label || '')}</div></div>
          <div><label>状态</label><div class="status">${r.success ? '成功' : '失败'}</div></div>
          <div><label>模型</label><div class="status">${escapeHtml(r.used_model || '')}</div></div>
          <div><label>调用入口</label><div class="status">${escapeHtml(r.source || '')}</div></div>
          <div><label>群号</label><div class="status">${escapeHtml(r.group_id || '')}</div></div>
          <div><label>Q号</label><div class="status">${escapeHtml(r.user_id || '')}</div></div>
        </div>
        ${promptDetailBlock('原始提示词', 'original_prompt', r.original_prompt || '')}
        ${promptDetailBlock('请求提示词', 'request_prompt', r.request_prompt || r.prompt || '')}
        ${promptDetailBlock('请求数据', 'request_data', JSON.stringify(r.request_data || {}, null, 2))}
        ${promptDetailBlock('响应数据', 'response_data', JSON.stringify(r.response_data || {}, null, 2))}
        <h3>请求图</h3>${imageThumbs(r.request_image_paths || [])}
        <h3>生成图</h3>${imageThumbs(r.generated_image_paths || [])}
      `;
      $('recordModal').classList.add('show');
      loadProtectedImages($('recordDetailBody'));
    }
    function closeRecordDetail() {
      $('recordModal').classList.remove('show');
      CURRENT_RECORD = null;
    }

    async function readFileDataUrl(file) {
      return await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ''));
        reader.onerror = reject;
        reader.readAsDataURL(file);
      });
    }
    function clearTestTaskPoll() {
      if (TEST_TASK_POLL_TIMER) {
        clearTimeout(TEST_TASK_POLL_TIMER);
        TEST_TASK_POLL_TIMER = null;
      }
    }
    function setTestBusy(busy) {
      $('testImageBtn').disabled = !!busy;
      $('testImageBtn').textContent = busy ? '后台生成中' : '开始测试';
    }
    function renderImageTestResult(data) {
      $('testResponseData').textContent = JSON.stringify(data || {}, null, 2);
      if (!data || data.success === false) {
        $('testStatus').textContent = `失败：${(data && data.error) || '这次没顺好'}`;
        showTestPanel('response');
        return;
      }
      $('testStatus').textContent = `成功：${data.used_model || ''}，耗时 ${data.elapsed_seconds}s，参考图 ${data.reference_images} 张`;
      $('testImages').innerHTML = '';
      for (const path of data.generated_image_paths || []) {
        const img = document.createElement('img');
        $('testImages').appendChild(img);
        loadProtectedImage(img, path);
      }
      showTestPanel('result');
    }
    async function pollImageTestTask(taskId) {
      clearTestTaskPoll();
      TEST_TASK_ID = taskId || '';
      if (!TEST_TASK_ID) return;
      try {
        const res = await api('/api/test-image-channel/tasks/' + encodeURIComponent(TEST_TASK_ID));
        if (TEST_TASK_ID !== taskId) return;
        const task = res.data || {};
        $('testResponseData').textContent = JSON.stringify(task, null, 2);
        if (task.request_data && !$('testRequestData').textContent.trim()) {
          $('testRequestData').textContent = JSON.stringify(task.request_data, null, 2);
        }
        if (task.status === 'queued' || task.status === 'running') {
          setTestBusy(true);
          const label = task.status === 'queued' ? '排队中' : '生成中';
          $('testStatus').textContent = `后台任务 ${task.task_id || TEST_TASK_ID} ${label}，已用 ${task.running_seconds || 0}s。关闭页面不会停止任务。`;
          TEST_TASK_POLL_TIMER = setTimeout(() => pollImageTestTask(TEST_TASK_ID), 2000);
          return;
        }
        setTestBusy(false);
        localStorage.removeItem('selfieImageLastTestTaskId');
        renderImageTestResult(task.result || {success:false, error: task.error || '任务未返回结果'});
        try { await loadRecords(); } catch (_) {}
      } catch (e) {
        if (TEST_TASK_ID !== taskId) return;
        setTestBusy(false);
        $('testStatus').textContent = e.message;
        $('testResponseData').textContent = JSON.stringify({success:false, error:e.message}, null, 2);
        showTestPanel('response');
      }
    }
    async function resumeImageTestTask() {
      const taskId = localStorage.getItem('selfieImageLastTestTaskId') || '';
      if (!taskId) return;
      try {
        const res = await api('/api/test-image-channel/tasks/' + encodeURIComponent(taskId));
        const task = res.data || {};
        if (task.status === 'queued' || task.status === 'running') {
          TEST_TASK_ID = taskId;
          $('testResponseData').textContent = JSON.stringify(task, null, 2);
          if (task.request_data) $('testRequestData').textContent = JSON.stringify(task.request_data, null, 2);
          pollImageTestTask(taskId);
        } else {
          localStorage.removeItem('selfieImageLastTestTaskId');
          $('testResponseData').textContent = JSON.stringify(task, null, 2);
          if (task.request_data) $('testRequestData').textContent = JSON.stringify(task.request_data, null, 2);
          renderImageTestResult(task.result || {success:false, error: task.error || '任务未返回结果'});
          try { await loadRecords(); } catch (_) {}
        }
      } catch (_) {
        localStorage.removeItem('selfieImageLastTestTaskId');
      }
    }
    async function runImageTest() {
      collectForms();
      clearTestData(false);
      setTestBusy(true);
      $('testStatus').textContent = '正在提交后台生图任务...';
      try {
        if (!$('testChannel').value) throw new Error('没有可用的启用生图渠道，请先启用渠道');
        if (!$('testModel').value) throw new Error('当前渠道没有可用模型，请先启用模型');
        const images = [];
        for (const file of $('testRefs').files) images.push(await readFileDataUrl(file));
        const payload = {
          channel: $('testChannel').value,
          model: $('testModel').value,
          prompt: $('testPrompt').value.trim(),
          aspect_ratio: $('testAspect').value,
          resolution: $('testResolution').value,
          prompt_enhance: $('promptEnhance').checked,
          use_selfie_reference: $('useSelfie').checked,
          images
        };
        $('testRequestData').textContent = JSON.stringify({...payload, images: `[${images.length} images]`}, null, 2);
        showTestPanel('request');
        const res = await api('/api/test-image-channel/tasks', {method:'POST', body: JSON.stringify(payload)});
        const task = res.data || {};
        TEST_TASK_ID = task.task_id || '';
        if (!TEST_TASK_ID) throw new Error('后台任务提交失败：未返回 task_id');
        localStorage.setItem('selfieImageLastTestTaskId', TEST_TASK_ID);
        $('testResponseData').textContent = JSON.stringify(task, null, 2);
        $('testStatus').textContent = `后台任务 ${TEST_TASK_ID} 已提交，关闭页面不会停止任务。`;
        pollImageTestTask(TEST_TASK_ID);
      } catch (e) {
        setTestBusy(false);
        $('testStatus').textContent = e.message;
        $('testResponseData').textContent = JSON.stringify({success:false, error:e.message}, null, 2);
        showTestPanel('response');
      }
    }
    function showTestPanel(name) {
      ['request','response','result'].forEach(key => $('test' + key[0].toUpperCase() + key.slice(1) + 'Panel').classList.toggle('active', key === name));
    }
    function clearTestData(clearStatus = true) {
      clearTestTaskPoll();
      TEST_TASK_ID = '';
      localStorage.removeItem('selfieImageLastTestTaskId');
      setTestBusy(false);
      $('testImages').innerHTML = '';
      $('testRequestData').textContent = '';
      $('testResponseData').textContent = '';
      if (clearStatus) $('testStatus').textContent = '';
      showTestPanel('result');
    }

    async function refreshSelfie() {
      try {
        const res = await api('/api/selfie-reference');
        const data = res.data || {};
        if (data.has_image && data.image) {
          $('selfiePreview').src = data.image;
          $('selfiePreview').style.display = 'block';
        } else {
          $('selfiePreview').style.display = 'none';
        }
        $('selfieStatus').textContent = data.status || (data.has_image ? '当前已设置自拍参考图' : '当前还没有设置自拍参考图');
      } catch (e) { $('selfieStatus').textContent = e.message; }
    }
    async function refreshDailySelfie() {
      try {
        const res = await api('/api/selfie-profile/refresh', {method:'POST', body:'{}'});
        $('selfieStatus').textContent = (res.data && res.data.status) || '今日穿搭已刷新';
        showToast('今日穿搭已刷新', 'ok');
      } catch (e) {
        $('selfieStatus').textContent = e.message;
        showToast(e.message, 'bad');
      }
    }
    async function uploadSelfie() {
      const file = $('selfieFile').files[0];
      if (!file) { $('selfieStatus').textContent = '请选择图片'; return; }
      try {
        const image = await readFileDataUrl(file);
        await api('/api/selfie-reference', {method:'POST', body: JSON.stringify({image, mime_type:file.type, filename:file.name})});
        await refreshSelfie();
        showToast('参考图已更新', 'ok');
      } catch (e) { $('selfieStatus').textContent = e.message; }
    }
    async function clearSelfie() {
      try { await api('/api/selfie-reference/clear', {method:'POST', body:'{}'}); await refreshSelfie(); showToast('参考图已清除', 'ok'); }
      catch (e) { $('selfieStatus').textContent = e.message; }
    }

    function escapeHtml(text) {
      return String(text ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }
    function escapeJs(text) {
      return String(text ?? '')
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/\\/g, '\\\\')
        .replace(/'/g, "\\'")
        .replace(/\n/g, '\\n')
        .replace(/\r/g, '');
    }
    function setStatus(id, text) { $(id).textContent = text; }
    function setMultiStatus(text) {
      for (const id of ['baseStatus','channelStatus','imageStatus','auditStatus','configStatus']) if ($(id)) $(id).textContent = text;
    }
    async function enterApp() {
      $('loginStatus').textContent = '登录中...';
      AUTH_TOKEN = $('loginToken').value.trim();
      try {
        const res = await api('/api/config');
        CONFIG = res.data || {};
        localStorage.setItem('selfieImageToken', AUTH_TOKEN);
        localStorage.removeItem('aicatToken');
        fillForms();
        document.body.classList.add('authed');
        $('loginStatus').textContent = '';
        await checkHealth();
        await refreshSelfie();
        await loadRecords();
        await resumeImageTestTask();
      } catch (e) {
        document.body.classList.remove('authed');
        $('loginStatus').textContent = e.message || '登录失败';
      }
    }
    function logout() {
      AUTH_TOKEN = '';
      localStorage.removeItem('selfieImageToken');
      localStorage.removeItem('aicatToken');
      $('loginToken').value = '';
      document.body.classList.remove('authed');
      $('loginStatus').textContent = '已退出登录';
    }
    function setupAutoSave() {
      document.querySelectorAll('main.app-shell input, main.app-shell select, main.app-shell textarea').forEach(el => {
        if (el.type === 'file' || el.id === 'configText' || el.closest('#test') || el.closest('#monitor') || el.closest('#raw')) return;
        const eventName = el.tagName === 'SELECT' || el.type === 'checkbox' || el.type === 'number' ? 'change' : 'input';
        el.addEventListener(eventName, () => scheduleAutoSave());
      });
    }

    document.querySelectorAll('nav button').forEach(btn => {
      btn.onclick = () => {
        document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('section').forEach(s => s.classList.remove('active'));
        btn.classList.add('active');
        $(btn.dataset.tab).classList.add('active');
        if (btn.dataset.tab === 'monitor') loadRecords();
      };
    });
    $('loginBtn').onclick = enterApp;
    $('loginToken').onkeydown = event => { if (event.key === 'Enter') enterApp(); };
    $('logoutBtn').onclick = logout;
    $('reloadAll').onclick = async () => { await checkHealth(); await loadConfig(); await refreshSelfie(); await loadRecords(); };
    $('modalSave').onclick = saveChannelModal;
    $('modalProvider').onchange = modalProviderChanged;
    $('modalRefreshModels').onclick = () => refreshChannelModels();
    $('modalEnableAll').onclick = () => { removeAllEnabledModels(); scheduleAutoSave('已移除全部启用模型'); showToast('已移除全部启用模型', 'ok'); };
    $('cacheSearch').oninput = () => renderModalModels(currentModalChannel());
    $('manualAdd').onclick = () => {
      const value = $('manualModel').value.trim();
      if (!value) return;
      addEnabledModel(value);
      $('manualModel').value = '';
      scheduleAutoSave('启用模型已自动生效');
    };
    $('manualModel').onkeydown = event => { if (event.key === 'Enter') $('manualAdd').click(); };
    $('testImageBtn').onclick = runImageTest;
    $('uploadSelfie').onclick = uploadSelfie;
    $('testChannel').onchange = refreshTestModels;
    function mirrorValue(a, b) {
      const sync = () => { if ($(b).value !== $(a).value) $(b).value = $(a).value; };
      $(a).addEventListener('input', sync);
      $(a).addEventListener('change', sync);
    }
    mirrorValue('defaultAspect', 'selfieAspect');
    mirrorValue('selfieAspect', 'defaultAspect');
    $('monitorSource').oninput = monitorFilterChanged;
    ['monitorModel','monitorSuccess'].forEach(id => {
      $(id).onchange = monitorFilterChanged;
    });

    (async function init() {
      for (const id of ['defaultAspect','selfieAspect','testAspect']) setSelectOptions(id, ASPECTS, '自动');
      setupAutoSave();
      if (AUTH_TOKEN) await enterApp();
    })();
  </script>
</body>
</html>"""


class _ServerThread(threading.Thread):
    def __init__(self, app: Any, host: str, port: int):
        super().__init__(daemon=True)
        self.server = make_server(host, port, app, threaded=True)
        self.context = app.app_context()
        self.context.push()

    def run(self) -> None:
        self.server.serve_forever()

    def shutdown(self) -> None:
        self.server.shutdown()


class FlaskWebServer:
    def __init__(self, plugin: Any):
        self.plugin = plugin
        self.thread: Optional[_ServerThread] = None
        self.host = ""
        self.port = 0

    def start(self, host: str, port: int) -> None:
        if Flask is None or make_server is None:
            raise RuntimeError("Flask 未安装，请先安装 requirements.txt 中的 Flask/Werkzeug")
        if self.thread and self.host == host and self.port == port:
            return
        self.stop()
        app = self._create_app()
        self.thread = _ServerThread(app, host, port)
        self.host = host
        self.port = port
        self.thread.start()

    def stop(self) -> None:
        if not self.thread:
            return
        self.thread.shutdown()
        self.thread = None

    def _run_async(self, coro: Any, timeout: Optional[float] = None) -> Any:
        loop = getattr(self.plugin, "loop", None)
        if loop and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            return future.result(timeout)
        return asyncio.run(coro)

    def _create_app(self) -> Any:
        app = Flask("astrbot_plugin_selfie_image")
        app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024

        def ok(data: Any = None, **extra: Any) -> Any:
            payload = {"success": True, "data": data}
            payload.update(extra)
            return jsonify(payload)

        def fail(message: str, status: int = 400) -> Any:
            return jsonify({"success": False, "error": redact_sensitive_text(message)}), status

        @app.after_request
        def add_response_safety_headers(response: Any) -> Any:
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("Referrer-Policy", "no-referrer")
            response.headers.setdefault("X-Frame-Options", "DENY")
            if str(request.path or "").startswith("/api/"):
                response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                response.headers["Pragma"] = "no-cache"
                response.headers["Expires"] = "0"
            return response

        def json_object_payload() -> Any:
            payload = request.get_json(silent=True)
            if payload is None:
                raw_body = request.get_data(cache=True) or b""
                if raw_body.strip():
                    return None, fail("请求体必须是 JSON 对象")
                return {}, None
            if not isinstance(payload, dict):
                return None, fail("请求体必须是 JSON 对象")
            return payload, None

        def int_query_arg(name: str, default: int, minimum: int, maximum: int) -> tuple[Optional[int], Optional[Any]]:
            raw_value = str(request.args.get(name, "") or "").strip()
            if not raw_value:
                return default, None
            try:
                value = int(raw_value)
            except ValueError:
                return None, fail(f"{name} 必须是整数", 400)
            if value < minimum:
                return None, fail(f"{name} 不能小于 {minimum}", 400)
            return min(value, maximum), None

        def record_matches_query(record: Any, source: str, model: str, success: str, keyword: str) -> bool:
            if not isinstance(record, dict):
                return False
            if source:
                source_text = " ".join(
                    str(record.get(key) or "")
                    for key in ("source_label", "source", "group_id", "user_id")
                ).lower()
                if source not in source_text:
                    return False
            if model and model not in str(record.get("used_model") or "").lower():
                return False
            if success:
                expected = success in {"1", "true", "yes", "ok", "success", "succeeded", "成功"}
                if bool(record.get("success")) is not expected:
                    return False
            if keyword:
                text = json.dumps(record, ensure_ascii=False, default=str).lower()
                if keyword not in text:
                    return False
            return True

        def filtered_record_payload(records: list[Any]) -> Any:
            source = str(request.args.get("source") or "").strip().lower()
            model = str(request.args.get("model") or "").strip().lower()
            success = str(request.args.get("success") or "").strip().lower()
            keyword = str(request.args.get("q") or request.args.get("keyword") or "").strip().lower()
            if success and success not in {"1", "0", "true", "false", "yes", "no", "ok", "success", "succeeded", "failed", "失败", "成功"}:
                return None, None, fail("success 必须是 true 或 false", 400)

            offset, error_response = int_query_arg("offset", 0, 0, 10000)
            if error_response:
                return None, None, error_response
            default_limit = min(MAX_RECORD_PAGE_LIMIT, len(records))
            limit, error_response = int_query_arg("limit", default_limit, 1, MAX_RECORD_PAGE_LIMIT)
            if error_response:
                return None, None, error_response

            filtered = [
                record
                for record in records
                if record_matches_query(record, source, model, success, keyword)
            ]
            page = filtered[offset : offset + limit]
            meta = {
                "total": len(records),
                "filtered": len(filtered),
                "offset": offset,
                "limit": limit,
            }
            return page, meta, None

        def token_candidates_from_request() -> list[str]:
            tokens: list[str] = []
            auth = str(request.headers.get("Authorization") or "")
            if auth.lower().startswith("bearer "):
                tokens.append(auth[7:].strip())
            tokens.extend(
                str(request.headers.get(name) or "").strip()
                for name in ("X-Selfie-Image-Token", "X-AICat-Token", "X-Token")
            )
            return [token for token in tokens if token]

        def is_local_bind_host() -> bool:
            host = str(self.host or "").strip().lower()
            return host in {"localhost", "::1", "[::1]"} or host.startswith("127.")

        def check_auth() -> bool:
            configured = str(getattr(self.plugin.config, "web_token", "") or "").strip()
            if not configured:
                return is_local_bind_host()
            if not is_local_bind_host() and configured.lower() in WEAK_WEB_TOKENS:
                return False
            configured_bytes = configured.encode("utf-8")
            for token in token_candidates_from_request():
                if len(token) > MAX_WEB_TOKEN_LENGTH:
                    continue
                try:
                    if hmac.compare_digest(token.encode("utf-8"), configured_bytes):
                        return True
                except Exception:
                    continue
            return False

        @app.route("/", methods=["GET"])
        @app.route("/index.html", methods=["GET"])
        def index() -> Any:
            return INDEX_HTML

        @app.route("/api/health", methods=["GET"])
        def health() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return ok(
                {
                    "status": "ok",
                    "auth": bool(getattr(self.plugin.config, "web_token", "")),
                    "host": self.host,
                    "port": self.port,
                    "config_path": getattr(self.plugin, "config_path", ""),
                    "records_path": getattr(self.plugin, "records_path", ""),
                    "cache_dir": getattr(self.plugin, "generated_dir", ""),
                    "cache_size_mb": round(float(self.plugin._cache_size_bytes()) / 1024 / 1024, 2),
                    "cache_limit_mb": getattr(self.plugin.config, "image_cache_limit_mb", 100),
                }
            )

        @app.route("/api/config", methods=["GET", "POST"])
        def config_route() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            if request.method == "GET":
                return ok(self.plugin.get_config_for_web())
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            if "config" in payload:
                if not isinstance(payload.get("config"), dict):
                    return fail("config 必须是 JSON 对象")
                patch = payload["config"]
            else:
                patch = payload
            try:
                data = self.plugin.update_config_from_web(patch)
                return ok(data)
            except Exception as exc:
                return fail(str(exc), 500)

        @app.route("/api/selfie-reference", methods=["GET", "POST"])
        def selfie_reference() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            if request.method == "GET":
                return ok(self.plugin.get_selfie_reference_payload())
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                data = self.plugin.save_selfie_reference_from_web(payload)
                return ok(data, message="自拍参考图已保存")
            except Exception as exc:
                return fail(str(exc))

        @app.route("/api/selfie-reference/clear", methods=["POST"])
        def selfie_reference_clear() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            _, error_response = json_object_payload()
            if error_response:
                return error_response
            return ok(self.plugin.clear_selfie_reference_from_web(), message="自拍参考图已清除")

        @app.route("/api/selfie-profile/refresh", methods=["POST"])
        def selfie_profile_refresh() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            _, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                data = self._run_async(self.plugin.refresh_selfie_profile_from_web(), timeout=20)
                return ok(data, message="今日自拍设定已刷新")
            except Exception as exc:
                return fail(str(exc), 500)

        @app.route("/api/test-image-channel", methods=["POST"])
        def test_image_channel() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                data = self._run_async(self.plugin.web_test_image(payload), timeout=max(30, self.plugin.config.image_global_timeout + 30))
                return ok(redact_sensitive_data(data))
            except Exception as exc:
                return fail(str(exc), 500)

        @app.route("/api/test-image-channel/tasks", methods=["POST"])
        def test_image_channel_task_start() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                data = self.plugin.start_web_image_task(payload)
                return ok(redact_sensitive_data(data))
            except Exception as exc:
                return fail(str(exc), 500)

        @app.route("/api/test-image-channel/tasks/<task_id>", methods=["GET"])
        def test_image_channel_task_status(task_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            task_id_text = str(task_id or "").strip()
            if len(task_id_text) > MAX_WEB_TASK_ID_LENGTH or not WEB_TASK_ID_RE.fullmatch(task_id_text):
                return fail("非法任务 ID", 400)
            try:
                return ok(redact_sensitive_data(self.plugin.get_web_image_task(task_id_text)))
            except Exception as exc:
                return fail(str(exc), 404)

        @app.route("/api/refresh-image-models", methods=["POST"])
        def refresh_image_models() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                data = self._run_async(self.plugin.web_refresh_image_models(payload), timeout=30)
                return ok(data, count=len(data))
            except Exception as exc:
                return fail(str(exc), 500)

        @app.route("/api/records", methods=["GET"])
        def records() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            data = redact_sensitive_data(self.plugin.get_recent_records())
            page, meta, error_response = filtered_record_payload(data)
            if error_response:
                return error_response
            return ok(page, **meta)

        @app.route("/api/records/<record_id>", methods=["GET"])
        def record_detail(record_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            record_id_text = str(record_id or "").strip()
            if not record_id_text or len(record_id_text) > MAX_WEB_RECORD_ID_LENGTH:
                return fail("非法记录 ID", 400)
            try:
                return ok(redact_sensitive_data(self.plugin.get_record_for_web(record_id_text)))
            except Exception as exc:
                return fail(str(exc), 404)

        @app.route("/api/records/clear", methods=["POST"])
        def records_clear() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            _, error_response = json_object_payload()
            if error_response:
                return error_response
            return ok({"deleted": self.plugin.clear_recent_records()})

        @app.route("/api/cache-image", methods=["GET"])
        def cache_image() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            try:
                rel_path = str(request.args.get("path") or "")
                if len(rel_path) > MAX_CACHE_IMAGE_PATH_LENGTH:
                    return fail("图片路径过长", 400)
                info = self.plugin.get_cached_image_info(rel_path)
            except Exception as exc:
                return fail(str(exc), 400)
            if not info.get("exists"):
                return fail("图片已清理", 404)
            if info.get("is_image") is False:
                return fail("缓存文件不是有效图片", 400)
            return send_file(info["absolute_path"], mimetype=info.get("mime_type") or "image/png")

        return app
