"""Embedded Flask Web UI for Selfie Image."""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import os
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


WEB_TASK_ID_RE = re.compile(r"^(?:web|web-studio)-\d{8,}-\d+$")
MAX_WEB_TASK_ID_LENGTH = 64
MAX_CACHE_IMAGE_PATH_LENGTH = 512
MAX_WEB_RECORD_ID_LENGTH = 128
MAX_RECORD_PAGE_LIMIT = 100
_LOGO_SRC_PLACEHOLDER = "__SELFIE_LOGO_SRC__"


def _bundled_logo_data_url() -> str:
    """Inline logo so Flask and AstrBot iframe both show the same brand mark."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
        if not raw:
            return ""
        return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    except Exception:
        return ""


def render_index_html(html: Optional[str] = None) -> str:
    text = INDEX_HTML if html is None else str(html)
    logo = _bundled_logo_data_url()
    if logo:
        return text.replace(_LOGO_SRC_PLACEHOLDER, logo)
    # Hide broken image slot when logo file is missing.
    return text.replace(f'src="{_LOGO_SRC_PLACEHOLDER}"', 'src="" style="display:none"')


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Selfie Image 管理面板</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f8fb;
      --panel: #ffffff;
      --panel-soft: rgba(60, 150, 202, 0.08);
      --line: rgba(27, 28, 29, 0.1);
      --line-strong: rgba(27, 28, 29, 0.14);
      --muted: rgba(27, 28, 29, 0.56);
      --text: #1b1c1d;
      --text-secondary: rgba(27, 28, 29, 0.68);
      --primary: #3c96ca;
      --primary-strong: #2f86bd;
      --primary-weak: rgba(60, 150, 202, 0.12);
      --danger: #f44336;
      --ok: #00c853;
      --shadow: 0 10px 30px rgba(27, 28, 29, 0.06);
      --radius-lg: 16px;
      --radius-md: 12px;
      --radius-sm: 10px;
      font-family: "SF Pro Display", "SF Pro Text", -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; background: var(--bg); color: var(--text); }
    header.app-shell {
      display: none;
      max-width: 1280px;
      margin: 0 auto;
      padding: 20px 16px 8px;
      align-items: flex-end;
      justify-content: space-between;
      gap: 16px;
      background: transparent;
      color: var(--text);
    }
    .header-brand { display: flex; align-items: center; gap: 14px; min-width: 0; }
    .header-logo {
      width: 48px; height: 48px; border-radius: 14px; object-fit: cover;
      border: 1px solid var(--line); background: var(--panel); box-shadow: var(--shadow); flex: 0 0 auto;
    }
    .header-eyebrow {
      margin: 0 0 4px; color: var(--primary); font-size: 12px; font-weight: 700;
      letter-spacing: 0.08em; text-transform: uppercase;
    }
    h1 { font-size: 1.45rem; margin: 0; font-weight: 700; line-height: 1.2; color: var(--text); }
    .header-sub { margin: 4px 0 0; color: var(--text-secondary); font-size: 13px; line-height: 1.45; }
    h2 { font-size: 1.05rem; margin: 0 0 12px; font-weight: 700; }
    h3 { font-size: 14px; margin: 16px 0 8px; font-weight: 650; }
    main.app-shell {
      display: none;
      max-width: 1280px;
      margin: 0 auto;
      padding: 8px 16px 28px;
      gap: 14px;
    }
    body.authed header.app-shell { display: flex; }
    body.authed main.app-shell { display: grid; }
    .login-page { min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 18px; }
    body.authed .login-page { display: none; }
    .login-box {
      width: min(420px, 100%); background: var(--panel); border: 1px solid var(--line);
      border-radius: var(--radius-lg); padding: 22px; box-shadow: var(--shadow);
    }
    .login-box h1 { color: var(--text); margin-bottom: 8px; font-size: 1.25rem; }
    nav.page-nav {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
      padding: 10px;
      margin: 0 0 4px;
      border: 1px solid var(--line);
      border-radius: 16px;
      background:
        linear-gradient(180deg, rgba(255,255,255,0.92), rgba(246,248,251,0.96)),
        var(--panel);
      box-shadow: var(--shadow);
    }
    nav.page-nav button,
    nav.page-nav button.secondary,
    nav.page-nav button.ok,
    nav.page-nav button.danger {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 100%;
      min-height: 44px;
      margin: 0;
      border: 1px solid transparent;
      border-radius: 12px;
      padding: 10px 8px;
      background: transparent;
      color: var(--text-secondary);
      font-size: 14px;
      font-weight: 650;
      line-height: 1.2;
      letter-spacing: 0;
      white-space: nowrap;
      text-align: center;
      box-shadow: none;
      transition: background .15s ease, color .15s ease, border-color .15s ease, box-shadow .15s ease;
    }
    nav.page-nav button:hover {
      background: var(--panel-soft);
      color: var(--text);
      border-color: color-mix(in srgb, var(--primary) 12%, transparent);
    }
    nav.page-nav button.active,
    nav.page-nav button.active:hover {
      background: linear-gradient(180deg, #eef7fc, #e3f2fa);
      color: var(--primary-strong);
      border-color: color-mix(in srgb, var(--primary) 28%, transparent);
      box-shadow: 0 1px 0 rgba(255,255,255,0.8) inset, 0 6px 14px rgba(60,150,202,0.12);
    }
    section {
      display: none; background: var(--panel); border: 1px solid var(--line);
      border-radius: var(--radius-lg); padding: 20px; box-shadow: var(--shadow);
      position: relative;
      overflow: hidden;
    }
    section.active { display: block; }
    section.active::before {
      content: "";
      position: absolute; left: 0; top: 0; bottom: 0; width: 4px;
      background: linear-gradient(180deg, var(--primary), color-mix(in srgb, var(--primary) 30%, #fff));
      border-radius: 4px 0 0 4px;
    }
    section > .between:first-child,
    section > h2:first-child {
      padding-left: 4px;
    }
    label { display: block; font-size: 12px; font-weight: 650; color: var(--text-secondary); margin: 9px 0 5px; }
    input, select, textarea, button { font: inherit; }
    input, select, textarea {
      width: 100%; border: 1px solid var(--line-strong); border-radius: var(--radius-sm);
      padding: 9px 11px; background: #fff; color: var(--text);
    }
    input:focus, select:focus, textarea:focus {
      outline: none; border-color: color-mix(in srgb, var(--primary) 55%, var(--line-strong));
      box-shadow: 0 0 0 3px var(--primary-weak);
    }
    textarea { min-height: 92px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 13px; resize: vertical; }
    button {
      cursor: pointer; border-radius: var(--radius-sm); border: 1px solid var(--primary);
      background: var(--primary); color: #fff; padding: 8px 14px; font-weight: 600;
    }
    button:hover { background: var(--primary-strong); border-color: var(--primary-strong); }
    button.secondary { background: #fff; border-color: var(--line-strong); color: var(--text); }
    button.secondary:hover { background: var(--panel-soft); border-color: var(--line-strong); }
    button.danger { background: var(--danger); border-color: var(--danger); color: #fff; }
    button.ok { background: var(--ok); border-color: var(--ok); color: #fff; }
    button:disabled { opacity: .55; cursor: not-allowed; }
    .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .grid3 { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
    .grid4 { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
    .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
    .status, pre {
      white-space: pre-wrap; background: #f8fafc; border: 1px solid var(--line); border-radius: var(--radius-sm);
      padding: 10px; min-height: 24px; max-width: 100%; overflow-wrap: anywhere; word-break: break-word;
    }
    .muted { color: var(--muted); font-size: 13px; line-height: 1.45; }
    .card { border: 1px solid var(--line); border-radius: var(--radius-md); padding: 12px; background: #fff; margin-bottom: 12px; box-shadow: var(--shadow); }
    .soft { background: var(--panel-soft); }
    .between { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
    .channel-row { display: grid; grid-template-columns: minmax(160px, 1fr) 130px 90px auto; gap: 10px; align-items: center; }
    .pill {
      display: inline-flex; align-items: center; gap: 5px; border-radius: 999px; padding: 4px 10px;
      background: var(--panel-soft); color: var(--primary-strong); font-size: 12px; font-weight: 600;
      border: 1px solid color-mix(in srgb, var(--primary) 18%, transparent);
    }
    .pill.green { background: #e7f7ed; color: #116735; border-color: transparent; }
    .pill.gray { background: #f1f3f5; color: #57606a; border-color: transparent; }
    .modal-mask { position: fixed; inset: 0; display: none; align-items: center; justify-content: center; background: rgba(27,28,29,.42); padding: 14px; z-index: 50; }
    .modal-mask.show { display: flex; }
    .modal {
      width: min(900px, 100%); max-height: 92vh; display: flex; flex-direction: column; overflow: hidden;
      background: #fff; border-radius: var(--radius-lg); border: 1px solid var(--line); padding: 16px; box-shadow: 0 18px 60px rgba(27,28,29,.16);
    }
    .modal-body { overflow: auto; padding-right: 4px; }
    .modal-footer { position: sticky; bottom: 0; margin-top: 14px; padding-top: 12px; background: linear-gradient(180deg, rgba(255,255,255,0), #fff 24px); }
    .toast-wrap { position: fixed; right: 16px; top: 16px; z-index: 100; display: grid; gap: 8px; pointer-events: none; }
    .toast { min-width: 220px; max-width: min(360px, calc(100vw - 32px)); padding: 10px 12px; border-radius: 12px; background: rgba(27,28,29,.94); color: #fff; box-shadow: 0 14px 32px rgba(0,0,0,.16); }
    .toast.ok { background: rgba(0,200,83,.94); }
    .toast.bad { background: rgba(244,67,54,.94); }
    .detail-title { display: flex; align-items: center; gap: 8px; margin: 16px 0 8px; }
    .detail-title h3 { margin: 0; }
    .copy-btn { width: 28px; height: 28px; display: inline-flex; align-items: center; justify-content: center; padding: 0; background: #fff; border-color: var(--line); color: #344054; }
    .copy-btn:hover { border-color: var(--primary); color: var(--primary); background: var(--primary-weak); }
    .copy-btn svg { width: 15px; height: 15px; display: block; }
    .tabs-inline { display: flex; gap: 8px; margin: 10px 0 14px; flex-wrap: wrap; }
    .tabs-inline button {
      background: #fff; border-color: var(--line-strong); color: var(--text);
      box-shadow: none;
    }
    .tabs-inline button:hover { background: var(--panel-soft); border-color: var(--line-strong); color: var(--text); }
    .tabs-inline button.active,
    .tabs-inline button.active:hover {
      background: linear-gradient(180deg, #eef7fc, #e3f2fa);
      border-color: color-mix(in srgb, var(--primary) 35%, var(--line-strong));
      color: var(--primary-strong);
      box-shadow: 0 1px 0 rgba(255,255,255,0.8) inset;
    }
    .channel-pane { display: none; }
    .channel-pane.active { display: block; }
    .model-panel { border: 1px solid var(--line); border-radius: var(--radius-md); padding: 12px; background: #fff; min-height: 120px; }
    .model-list { display: grid; gap: 7px; margin-top: 8px; }
    .model-list.collapsed { max-height: 240px; overflow-y: auto; border: 1px dashed var(--line); padding: 4px; border-radius: 6px; }
    .model-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; align-items: center; border: 1px solid var(--line); border-radius: var(--radius-sm); padding: 8px 10px; background: #f8fafc; }
    .model-row.with-provider { grid-template-columns: minmax(0, 1fr) minmax(150px, 190px) auto; }
    .model-row .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .model-row .actions { margin-top: 0; }
    .model-provider { min-width: 150px; padding: 5px 8px; font-size: 12px; }
    .mini { padding: 5px 8px; font-size: 12px; }
    .table { width: 100%; border-collapse: collapse; font-size: 13px; }
    .table th, .table td { text-align: left; border-bottom: 1px solid var(--line); padding: 8px; vertical-align: top; }
    .table th { background: #f8fafc; font-weight: 650; }
    .preview { max-width: 260px; border: 1px solid var(--line); border-radius: var(--radius-md); display: none; margin-top: 10px; box-shadow: var(--shadow); }
    .images { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 10px; margin-top: 12px; }
    .images img { width: 100%; border: 1px solid var(--line); border-radius: var(--radius-md); background: #fff; box-shadow: var(--shadow); }
    .test-panel { display: none; margin-top: 12px; }
    .test-panel.active { display: block; }
    .studio-slots { display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:10px; margin-top:10px; }
    .studio-slot { border:1px solid var(--line); border-radius:var(--radius-md); background:#fff; padding:10px; min-height:170px; display:flex; flex-direction:column; gap:8px; box-shadow:var(--shadow); }
    .studio-slot .slot-label { font-size:12px; font-weight:650; color:var(--muted); }
    .studio-slot img { width:100%; height:110px; object-fit:cover; border-radius:8px; border:1px solid var(--line); background:#f8fafc; display:none; }
    .studio-slot.empty img { display:none; }
    .studio-slot.has-image img { display:block; }
    .studio-slot .slot-actions { display:flex; gap:6px; flex-wrap:wrap; }
    .studio-slot .slot-actions button { padding:4px 8px; font-size:12px; }
    .studio-results { display:grid; grid-template-columns:repeat(auto-fill,minmax(140px,1fr)); gap:10px; margin-top:10px; }
    .studio-results .card { border:1px solid var(--line); border-radius:var(--radius-md); padding:8px; background:#fff; }
    .studio-results img { width:100%; border-radius:8px; border:1px solid var(--line); }
    .studio-chiprow { display:flex; flex-wrap:wrap; gap:8px; margin:8px 0; }
    .studio-chiprow button { padding:5px 10px; font-size:12px; }
    .checkline { display: flex; align-items: center; gap: 8px; min-height: 38px; }
    .checkline input { width: auto; }
    .topline { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .topline input { max-width: 280px; }
    body.dashboard-embedded { background: var(--bg); }
    @media (max-width: 1100px) {
      .grid4 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .channel-row { grid-template-columns: minmax(160px, 1fr) 120px 90px; }
      .channel-row .actions { grid-column: 1 / -1; }
      nav.page-nav { grid-template-columns: repeat(4, minmax(0, 1fr)); }
    }
    @media (max-width: 760px) {
      body.authed header.app-shell { display: block; }
      .grid, .grid3, .grid4, .channel-row { grid-template-columns: 1fr; }
      main.app-shell { padding: 8px 10px 20px; }
      header.app-shell { padding: 14px 10px 6px; }
      header .topline { margin-top: 10px; justify-content: flex-start; }
      nav.page-nav {
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 8px;
        overflow: visible;
      }
      nav.page-nav button {
        font-size: 13px;
        min-height: 42px;
        padding: 10px 6px;
        white-space: normal;
      }
      .modal { max-height: 96vh; padding: 12px; }
      .model-row, .model-row.with-provider { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div id="loginPage" class="login-page">
    <div class="login-box">
      <p class="header-eyebrow">Selfie Image</p>
      <h1>管理登录</h1>
      <p class="muted" id="loginHint">独立打开时需要管理口令。在 AstrBot 插件页里打开会自动登录，不用再输。</p>
      <label>管理口令</label>
      <input id="loginToken" type="password" placeholder="管理口令" autocomplete="current-password">
      <div class="actions">
        <button id="loginBtn" class="ok">进入</button>
      </div>
      <div id="loginStatus" class="status"></div>
    </div>
  </div>

  <header class="app-shell">
    <div class="header-brand">
      <img class="header-logo" src="__SELFIE_LOGO_SRC__" alt="" onerror="this.style.display='none'">
      <div>
        <p class="header-eyebrow">Plugin Page</p>
        <h1>生图 · 自拍 管理</h1>
        <p class="header-sub">渠道、试画、形象与记录，内嵌页与独立页同一套界面</p>
      </div>
    </div>
    <div class="topline">
      <button id="reloadAll" class="secondary">刷新</button>
      <button id="logoutBtn" class="secondary">退出</button>
    </div>
  </header>
  <main class="app-shell">
    <nav class="page-nav" aria-label="功能分区">
      <button data-tab="base" class="active" type="button">基础</button>
      <button data-tab="channels" type="button">渠道</button>
      <button data-tab="monitor" type="button">记录</button>
      <button data-tab="studio" type="button">画布</button>
      <button data-tab="test" type="button">试画</button>
      <button data-tab="image" type="button">出图</button>
      <button data-tab="selfie" type="button">形象</button>
      <button data-tab="audit" type="button">审核</button>
      <button data-tab="raw" type="button">高级</button>
    </nav>

    <section id="base" class="active">
      <div class="between">
        <h2>基础</h2>
        <span id="healthPill" class="pill">未连接</span>
      </div>
      <label>连接状态</label><div id="health" class="status">还没连上</div>
      <div class="grid">
        <div><label>图片缓存上限（MB）</label><input id="cacheLimitMB" type="number" min="10" max="102400"></div>
        <div><label>说明</label><div class="status">请求图和生成图存在同一缓存目录；超过上限会自动删最旧的，直到回到上限以下。</div></div>
      </div>
      <h3>谁能用</h3>
      <div class="grid">
        <div><label>可用名单</label><textarea id="usableUsers" placeholder="空着=所有人都能用"></textarea></div>
        <div><label>黑名单</label><textarea id="blockedUsers"></textarea></div>
        <div><label>白名单用户</label><textarea id="whitelistUsers"></textarea></div>
        <div><label>白名单群</label><textarea id="whitelistGroups"></textarea></div>
      </div>
      <div id="baseStatus" class="status" style="margin-top:12px"></div>
    </section>

    <section id="channels">
      <div class="between">
        <h2>渠道</h2>
        <div class="actions" style="margin-top:0">
          <button class="secondary" type="button" onclick="addChannel()">加生图渠道</button>
          <button class="secondary" type="button" onclick="addAuditChannel()">加审核渠道</button>
          <button class="secondary" type="button" onclick="addVideoChannel()">加视频渠道</button>
        </div>
      </div>
      <p class="muted">模型协议可按模型名自动识别，也可在已启用模型旁手动切换。新建/编辑渠道点「保存渠道」才写入；列表里启停、删除、复制和优先级会自动保存。启用但没选模型时会自动关掉该渠道。</p>
      <div class="tabs-inline" role="tablist" aria-label="渠道分类">
        <button id="channelTabImage" class="active" type="button" onclick="switchChannelPane('image')">生图渠道</button>
        <button id="channelTabAudit" type="button" onclick="switchChannelPane('audit')">审核渠道</button>
        <button id="channelTabVideo" type="button" onclick="switchChannelPane('video')">视频渠道</button>
      </div>
      <div id="channelPaneImage" class="channel-pane active"><div id="channelList"></div></div>
      <div id="channelPaneAudit" class="channel-pane"><div id="auditChannelList"></div></div>
      <div id="channelPaneVideo" class="channel-pane"><div id="videoChannelList"></div></div>
      <h3>生图模型优先级</h3>
      <p class="muted">未设置优先级时，按渠道与已启用模型的配置顺序依次尝试。开启「随机」后每次生图从全部已启用模型中随机排序（失败仍会换下一个）；关闭随机后恢复下方优先级列表，列表不会被清空。</p>
      <div class="grid">
        <div><label>模型选择模式</label>
          <div class="actions" style="margin-top:8px">
            <label class="check"><input id="randomImageModel" type="checkbox" onchange="onRandomImageModelChange()"> 随机（启用时忽略优先级）</label>
          </div>
        </div>
        <div><label>选择已启用模型</label><select id="priorityPicker"></select></div>
        <div><label>操作</label><div class="actions" style="margin-top:0"><button id="addPriorityBtn" class="secondary" type="button" onclick="addPriority()">加入优先级</button><button id="clearPriorityBtn" class="secondary" type="button" onclick="clearPriority()">清空优先级</button></div></div>
      </div>
      <textarea id="priorityList" style="display:none"></textarea>
      <div id="priorityRows" class="model-list"></div>
      <div id="channelStatus" class="status" style="margin-top:12px"></div>
    </section>

    <section id="monitor">
      <div class="between">
        <h2>出图记录</h2>
        <div class="actions" style="margin-top:0">
          <button class="secondary" onclick="loadRecords()">刷新</button>
          <button class="danger" onclick="clearRecords()">清空</button>
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


    <section id="studio">
      <div class="between">
        <h2>画布</h2>
        <span class="status">多参考编排 · 合影模板 · 仅图片</span>
      </div>
      <div class="grid">
        <div>
          <label>画布会话</label>
          <select id="studioSessionSelect"></select>
        </div>
        <div>
          <label>标题</label>
          <input id="studioTitle" placeholder="合影画布">
        </div>
        <div class="actions" style="align-items:end">
          <button class="ok" id="studioCreateBtn" type="button">新建合影画布</button>
          <button class="secondary" id="studioReloadBtn" type="button">刷新</button>
          <button class="danger" id="studioDeleteBtn" type="button">删除</button>
        </div>
      </div>
      <div class="grid">
        <div><label>模式</label>
          <select id="studioMode">
            <option value="group">合影</option>
            <option value="selfie">自拍</option>
            <option value="i2i">图生图</option>
            <option value="t2i">文生图</option>
          </select>
        </div>
        <div><label>画面比例</label><select id="studioAspect"></select></div>
        <div><label>清晰度</label><select id="studioResolution"><option>1K</option><option>2K</option><option>4K</option></select></div>
        <div><label>张数</label><input id="studioCount" type="number" min="1" max="4" value="1"></div>
      </div>
      <div class="grid">
        <label class="checkline"><input id="studioUsePersona" type="checkbox" checked> 缺形象槽时用当前形象</label>
        <div><label>渠道策略</label>
          <select id="studioPolicy">
            <option value="priority">按优先级/启用序</option>
            <option value="random">随机启用模型</option>
          </select>
        </div>
      </div>
      <label>提示词</label>
      <textarea id="studioPrompt" rows="3" placeholder="自然并肩合影…"></textarea>
      <div class="studio-chiprow" id="studioPromptChips"></div>
      <div class="between" style="margin-top:8px">
        <h3>参考槽位</h3>
        <button class="secondary" id="studioAddSlotBtn" type="button">加槽位</button>
      </div>
      <div class="studio-slots" id="studioSlots"></div>
      <div class="actions">
        <button class="ok" id="studioSaveBtn" type="button">保存设置</button>
        <button class="ok" id="studioRunBtn" type="button">开始生成</button>
      </div>
      <div id="studioStatus" class="status">打开后点「新建合影画布」，或选已有会话。</div>
      <h3>结果</h3>
      <div class="studio-results" id="studioResults"></div>
      <input id="studioFilePick" type="file" accept="image/*" style="display:none">
    </section>

    <section id="test">
      <h2>试画</h2>
      <div class="grid">
        <div><label>用哪个渠道</label><select id="testChannel"></select></div>
        <div><label>模型</label><select id="testModel"></select></div>
        <div><label>画面比例</label><select id="testAspect"></select></div>
        <div><label>清晰度</label><select id="testResolution"><option>1K</option><option>2K</option><option>4K</option></select></div>
      </div>
      <label>想画什么</label><textarea id="testPrompt">一只可爱的白色猫咪，坐在樱花树下，柔和光线，精致插画风格</textarea>
      <div class="grid">
        <label class="checkline"><input id="promptEnhance" type="checkbox"> 润色提示词</label>
        <label class="checkline"><input id="useSelfie" type="checkbox"> 带上当前形象参考</label>
        <div><label>额外参考图</label><input id="testRefs" type="file" accept="image/png,image/jpeg,image/webp,image/gif,image/bmp,image/avif,image/heic,image/heif,image/tiff,image/svg+xml" multiple></div>
      </div>
      <div class="actions">
        <button class="ok" id="testImageBtn">开始试画</button>
        <button class="secondary" onclick="showTestPanel('request')">看请求</button>
        <button class="secondary" onclick="showTestPanel('response')">看响应</button>
        <button class="secondary" onclick="showTestPanel('result')">看结果</button>
        <button class="danger" onclick="clearTestData()">清空</button>
      </div>
      <div id="testStatus" class="status"></div>
      <div id="testRequestPanel" class="test-panel"><h3>请求数据</h3><pre id="testRequestData"></pre></div>
      <div id="testResponsePanel" class="test-panel"><h3>响应数据</h3><pre id="testResponseData"></pre></div>
      <div id="testResultPanel" class="test-panel active"><h3>生成结果</h3><div id="testImages" class="images"></div></div>
    </section>

    <section id="image">
      <h2>出图习惯</h2>
      <div class="grid4">
        <div><label>默认比例</label><select id="defaultAspect"></select></div>
        <div><label>默认清晰度</label><select id="defaultResolution"><option>1K</option><option>2K</option><option>4K</option></select></div>
        <div><label>同时画几张上限</label><input id="maxConcurrent" type="number" min="1" max="20"></div>
        <div><label>整次最长等待（秒）</label><input id="globalTimeout" type="number" min="10" max="900"></div>
        <div><label>参考图最大 MB</label><input id="maxImageSize" type="number" min="1" max="100"></div>
        <div><label>一次最多连画几轮</label><input id="maxBatchCount" type="number" min="1" max="8"></div>
        <div><label>每人冷却（秒）</label><input id="rateLimitSeconds" type="number" min="0"></div>
        <div><label>每日基础次数</label><input id="dailyLimitCount" type="number" min="1"></div>
      </div>
      <div class="grid">
        <label class="checkline"><input id="enableLLMTool" type="checkbox"> 对话里也能叫我出图/自拍</label>
        <label class="checkline"><input id="showGenerationInfo" type="checkbox"> 回复里带上耗时和张数</label>
        <label class="checkline"><input id="showModelInfo" type="checkbox"> 回复里带上用了哪个模型</label>
        <label class="checkline"><input id="enableDailyLimit" type="checkbox"> 开启每日次数上限</label>
      </div>
      <div id="imageStatus" class="status"></div>
    </section>

    <section id="selfie">
      <h2>形象</h2>
      <div class="grid">
        <div><label>显示名</label><input id="selfieBotName"></div>
        <div><label>默认自拍比例</label><select id="selfieAspect"></select></div>
      </div>
      <label>人设（语气与氛围）</label><textarea id="selfiePersonality"></textarea>
      <h3>形象参考图</h3>
      <input id="selfieFile" type="file" accept="image/png,image/jpeg,image/webp,image/gif,image/bmp,image/avif,image/heic,image/heif,image/tiff,image/svg+xml">
      <img id="selfiePreview" class="preview" alt="selfie reference">
      <div class="actions">
        <button id="uploadSelfie">上传并保存</button>
        <button class="secondary" onclick="refreshSelfie()">刷新预览</button>
        <button class="ok" onclick="refreshDailySelfie()">换今日穿搭</button>
        <button class="danger" onclick="clearSelfie()">清除参考图</button>
      </div>
      <div id="selfieStatus" class="status"></div>
    </section>

    <section id="audit">
      <h2>审核</h2>
      <div class="grid">
        <label class="checkline"><input id="enablePromptAudit" type="checkbox"> 出图前审文字</label>
        <label class="checkline"><input id="enableOutputAudit" type="checkbox"> 出图后审成品</label>
        <div><label>文字审核模型</label><select id="promptAuditModel"></select></div>
        <div><label>成品审核模型</label><select id="outputAuditModel"></select></div>
        <div><label>识图模型</label><select id="ocrModel"></select></div>
      </div>
      <label>文字屏蔽词</label><textarea id="blockedWords"></textarea>
      <label>文字审核说明</label><textarea id="promptAuditTemplate"></textarea>
      <label>成品审核说明</label><textarea id="outputAuditTemplate"></textarea>
      <div id="auditStatus" class="status">屏蔽词、文字审核、成品审核会在指令、对话工具和试画里一起生效。成品审核请选能看图的模型。</div>
    </section>

    <section id="raw">
      <h2>高级 · 配置原文</h2>
      <p class="muted">这里改的是插件自己的配置，不包含 AstrBot 启动用的网页地址和口令。</p>
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
        <div style="display:none" id="modalProviderWrap"><label>类型</label><select id="modalProvider"></select></div>
        <div><label>Base URL</label><input id="modalBaseUrl"></div>
        <div><label>API Key（可多行，每行一把；鉴权失败或限流会自动换下一把）</label><textarea id="modalApiKey" rows="3" placeholder="sk-xxx&#10;sk-yyy"></textarea></div>
        <div><label>代理 URL</label><input id="modalProxy" placeholder="http://127.0.0.1:7890"></div>
        <div><label>默认模型</label><input id="modalModel"></div>
        <div><label>超时（秒）</label><input id="modalTimeout" type="number" min="10" max="900"></div>
      </div>
      <p class="muted" id="modalProviderHint" style="margin-top:8px">渠道默认协议会按模型名自动识别；也可在下方已启用模型旁手动切换协议。</p>
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

  <!-- Telegram-forwarder style: load bridge BEFORE app script so embed boot sees AstrBotPluginPage. -->
  <script src="/api/plugin/page/bridge-sdk.js"></script>
  <script>
    const $ = id => document.getElementById(id);
    const ASPECTS = ['自动','1:1','2:3','3:2','3:4','4:3','4:3','4:5','5:4','9:16','16:9','21:9'].filter((v,i,a)=>a.indexOf(v)===i);
    const PROVIDERS = ['openai','gemini','gemini_openai','z_image_gitee','jimeng2api','grok','agnes','novelai'];
    const AUDIT_PROVIDERS = ['openai','gemini','gemini_openai'];
    // Video model-family protocols (like image provider types). Auto by model name; manual override.
    const VIDEO_PROVIDERS = ['openai_video','sora','veo','seedance','agnes','kling','cogvideo','video_chat','video_sync'];
    const VIDEO_PROVIDER_LABELS = {
      openai_video: '通用 OpenAI 视频（/videos/generations）',
      sora: 'Sora',
      veo: 'Veo（Google）',
      seedance: 'Seedance / 即梦视频',
      agnes: 'Agnes 视频',
      kling: '可灵 Kling',
      cogvideo: 'CogVideo / 智谱',
      video_chat: '对话回链（chat/completions）',
      video_sync: '同步长等（高级）'
    };
    const MONITOR_PAGE_SIZE = 20;
    // AstrBot plugin iframe is sandboxed without allow-same-origin.
    // Bare storage access throws and aborts the whole script before login bindings.
    function safeStorageGet(key, fallback = '') {
      try { return window.localStorage?.getItem(key) ?? fallback; } catch (_) { return fallback; }
    }
    function safeStorageSet(key, value) {
      try { window.localStorage?.setItem(key, value); } catch (_) {}
    }
    function safeStorageRemove(key) {
      try { window.localStorage?.removeItem(key); } catch (_) {}
    }
    let CONFIG = {};    let STUDIO = { sessions: [], current: null, prompts: [], pollTimer: null, uploadSlotId: '' };

    let RECORDS = [];
    let RECORD_META = {total: 0, filtered: 0, offset: 0, limit: MONITOR_PAGE_SIZE};
    let MONITOR_PAGE = 1;
    let AUTH_TOKEN = safeStorageGet('selfieImageToken') || safeStorageGet('aicatToken') || '';
    let IS_FILLING = false;
    let AUTO_SAVE_TIMER = null;
    let MONITOR_LOAD_TIMER = null;
    let ACTIVE_CHANNEL_PANE = 'image';
    let EDITING_CHANNEL_INDEX = -1;
    let EDITING_CHANNEL_KIND = 'image';
    let EDITING_CHANNEL_IS_NEW = false;
    let CHANNEL_DRAFT = null;
    let CHANNEL_MODAL_DIRTY = false;
    let SUPPRESS_AUTOSAVE = false;
    let CURRENT_RECORD = null;
    let TEST_TASK_POLL_TIMER = null;
    let TEST_TASK_ID = '';
    let DASHBOARD_BRIDGE = null;

    function isEmbeddedFrame() {
      try {
        return window.parent && window.parent !== window;
      } catch (_) {
        return true;
      }
    }
    function isDashboardPage() {
      // Prefer live bridge; iframe/path are fallbacks because AstrBot may inject
      // bridge-sdk after inline scripts unless we preload it ourselves.
      return Boolean(window.AstrBotPluginPage || DASHBOARD_BRIDGE)
        || isEmbeddedFrame()
        || String(window.location.pathname || '').startsWith('/api/plugin/page/content/');
    }
    function dashboardBridge() {
      return window.AstrBotPluginPage || DASHBOARD_BRIDGE || null;
    }
    async function waitForDashboardBridge(timeout = 12000) {
      const existing = dashboardBridge();
      if (existing) return existing;
      const started = Date.now();
      while (Date.now() - started < timeout) {
        if (window.AstrBotPluginPage) {
          DASHBOARD_BRIDGE = window.AstrBotPluginPage;
          return DASHBOARD_BRIDGE;
        }
        await new Promise(resolve => setTimeout(resolve, 25));
      }
      throw new Error('AstrBot 内嵌管理页 bridge 未就绪');
    }
    function bridgeEndpoint(path) {
      // Match telegram forwarder api.js:
      // "/api/config" -> "config"
      // parent then calls /api/v1/plugins/extensions/<plugin>/config
      const value = String(path || '').trim();
      const noQuery = value.split('?', 1)[0];
      if (noQuery.startsWith('/api/')) return noQuery.slice('/api/'.length);
      return noQuery.replace(/^\/+/, '');
    }
    function bridgePayload(payload) {
      if (payload && typeof payload === 'object' && payload.ok === false) {
        throw new Error(payload.message || payload.error || 'Dashboard Page API request failed.');
      }
      if (payload && typeof payload === 'object' && payload.status === 'error') {
        throw new Error(payload.message || payload.error || '请求失败');
      }
      if (payload && typeof payload === 'object' && payload.success === false) {
        throw new Error(payload.error || payload.message || '请求失败');
      }
      // Parent may unwrap once. Accept either full envelope or bare data.
      if (payload && typeof payload === 'object' && payload.ok === true && 'data' in payload) {
        return { success: true, data: payload.data || {}, message: payload.message || '' };
      }
      if (payload && typeof payload === 'object' && payload.success === true && 'data' in payload) {
        return payload;
      }
      return { success: true, data: payload || {} };
    }
    if ($('loginToken')) $('loginToken').value = AUTH_TOKEN;

    function headers() {
      const result = {'Content-Type':'application/json'};
      if (AUTH_TOKEN) result['X-Selfie-Image-Token'] = AUTH_TOKEN;
      return result;
    }
    function apiErrorMessage(status, data) {
      return String(data?.error || ("HTTP " + status));
    }
    async function api(path, options = {}) {
      if (isDashboardPage()) {
        const bridge = await waitForDashboardBridge();
        if (typeof bridge.ready === 'function') await bridge.ready();
        const method = String(options.method || 'GET').toUpperCase();
        const endpoint = bridgeEndpoint(path);
        let body = null;
        if (options.body) {
          try { body = typeof options.body === 'string' ? JSON.parse(options.body) : options.body; }
          catch (_) { body = {}; }
        } else if (method === 'GET' && path.includes('?')) {
          body = Object.fromEntries(new URLSearchParams(path.split('?', 2)[1]));
        }
        try {
          const raw = method === 'GET'
            ? await bridge.apiGet(endpoint, body || {})
            : await bridge.apiPost(endpoint, body || {});
          return bridgePayload(raw);
        } catch (err) {
          throw new Error(err?.message || '内嵌管理页请求失败');
        }
      }
      const opts = Object.assign({}, options, {headers: Object.assign({}, headers(), options.headers || {})});
      let res;
      try {
        res = await fetch(path, opts);
      } catch (_) {
        throw new Error("网络请求失败，请检查 Web 服务连接");
      }
      let data;
      try {
        data = await res.json();
      } catch (_) {
        throw new Error("接口返回了无效响应（HTTP " + res.status + "）");
      }
      if (!res.ok || data.success === false) {
        if (res.status === 401) document.body.classList.remove("authed");
        throw new Error(apiErrorMessage(res.status, data));
      }
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
      ACTIVE_CHANNEL_PANE = (kind === 'audit' || kind === 'video') ? kind : 'image';
      const map = { image: 'channelTabImage', audit: 'channelTabAudit', video: 'channelTabVideo' };
      const panes = { image: 'channelPaneImage', audit: 'channelPaneAudit', video: 'channelPaneVideo' };
      for (const key of Object.keys(map)) {
        const tab = $(map[key]);
        const pane = $(panes[key]);
        if (tab) tab.classList.toggle('active', ACTIVE_CHANNEL_PANE === key);
        if (pane) pane.classList.toggle('active', ACTIVE_CHANNEL_PANE === key);
      }
    }
    function ensureConfig() {
      delete CONFIG.web;
      delete CONFIG.webHost;
      delete CONFIG.webPort;
      delete CONFIG.webToken;
      delete CONFIG.webEnable;
      CONFIG.bot_name ??= '啊呜';
      CONFIG.personality ??= '可爱猫娘助手，说话带“喵”等语气词，活泼俏皮会撒娇';
      CONFIG.permission ??= {};
      CONFIG.image ??= {};
      CONFIG.video ??= {};
      CONFIG.image_channels ??= [];
      CONFIG.audit_channels ??= [];
      CONFIG.video_channels ??= [];
      CONFIG.enabled_image_model_priority ??= [];
      CONFIG.enabled_video_model_priority ??= [];
      CONFIG.random_image_model ??= false;
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
      const vid = CONFIG.video;
      vid.enable ??= true;
      vid.default_duration ??= 5;
      vid.max_concurrent_tasks ??= 1;
      vid.global_timeout ??= 300;
    }

    function fillForms() {
      IS_FILLING = true;
      try {
        ensureConfig();
        normalizeChannels();
        normalizeAuditChannels();
        normalizeVideoChannels();
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
        if ($('randomImageModel')) $('randomImageModel').checked = !!CONFIG.random_image_model;
        updatePriorityControlsState();
        renderAllChannelLists();
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
      CONFIG.random_image_model = !!( $('randomImageModel') && $('randomImageModel').checked );
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
        nai: 'novelai',
        novel_ai: 'novelai',
        novelai: 'novelai',
        nai2api: 'novelai',
        bestnai: 'novelai',
        ppnai: 'novelai',
        xai: 'grok',
        x_ai: 'grok'
      };
      const normalized = aliases[raw] || raw;
      return PROVIDERS.includes(normalized) ? normalized : '';
    }
    function normalizeVideoProviderType(value) {
      const rawText = String(value || '').trim();
      const raw = rawText.toLowerCase().replace(/-/g, '_');
      if (!raw) return '';
      if (rawText.includes('对话')) return 'video_chat';
      if (rawText.includes('同步') && !rawText.includes('异步')) return 'video_sync';
      const aliases = {
        openai_video: 'openai_video',
        openai_compatible: 'openai_video',
        openai_videos: 'openai_video',
        async: 'openai_video',
        async_task: 'openai_video',
        openai_video: 'openai_video',
        video: 'openai_video',
        videos: 'openai_video',
        poll: 'openai_video',
        sora: 'sora', sora2: 'sora', openai_sora: 'sora',
        veo: 'veo', veo2: 'veo', veo3: 'veo', google_veo: 'veo', gemini_veo: 'veo',
        seedance: 'seedance', doubao_seedance: 'seedance', jimeng_video: 'seedance',
        agnes: 'agnes', agnes_video: 'agnes', agnes_ai: 'agnes',
        kling: 'kling', kuaishou_kling: 'kling',
        cogvideo: 'cogvideo', cogvideox: 'cogvideo', zhipu_video: 'cogvideo',
        sync: 'video_sync', openai_sync: 'video_sync', video_sync: 'video_sync',
        chat: 'video_chat', openai_chat: 'video_chat', chat_completions: 'video_chat', video_chat: 'video_chat'
      };
      let normalized = aliases[raw] || raw;
      if (VIDEO_PROVIDERS.includes(normalized)) return normalized;
      if (raw.includes('sora')) return 'sora';
      if (raw.includes('veo')) return 'veo';
      if (raw.includes('seedance')) return 'seedance';
      if (raw.includes('agnes')) return 'agnes';
      if (raw.includes('kling') || rawText.includes('可灵')) return 'kling';
      if (raw.includes('cogvideo') || raw.includes('zhipu')) return 'cogvideo';
      return '';
    }
    function inferProviderTypeFromModel(model) {
      const compact = String(model || '').trim().toLowerCase().replace(/[\s_]+/g, '-');
      if (!compact) return '';
      if (compact.includes('agnes')) return 'agnes';
      if (compact.includes('z-image') || compact.startsWith('zimage')) return 'z_image_gitee';
      if (compact.includes('jimeng') || compact.includes('seedream') || compact.includes('doubao-seedream')) return 'jimeng2api';
      if (compact.includes('grok') || compact.includes('xai') || compact.includes('x-ai')) return 'grok';
      if (compact.includes('gpt-image') || compact.includes('dall-e') || compact.includes('dalle')) return 'openai';
      if (compact.includes('nai-diffusion') || compact.startsWith('nai-') || compact.includes('novelai')) return 'novelai';
      if (compact.includes('gemini') || compact.includes('nano-banana')) return 'gemini';
      return '';
    }
    function inferVideoProviderTypeFromModel(model) {
      const compact = String(model || '').trim().toLowerCase().replace(/[\s_]+/g, '-');
      if (!compact) return '';
      if (compact.includes('sora')) return 'sora';
      if (compact.includes('veo')) return 'veo';
      if (compact.includes('seedance') || compact.includes('doubao-seedance')) return 'seedance';
      if (compact.includes('agnes')) return 'agnes';
      if (compact.includes('kling') || compact.includes('可灵')) return 'kling';
      if (compact.includes('cogvideo') || compact.includes('cog-videox')) return 'cogvideo';
      if ((['gpt-4o','gpt4o','claude','deepseek-chat'].some(k => compact.includes(k))) && compact.includes('video')) return 'video_chat';
      if (compact.endsWith('-chat') || compact.startsWith('chat-')) return 'video_chat';
      if (['luma','runway','minimax','hailuo','vidu','wanx','wan2','gen-3','gen3'].some(k => compact.includes(k))) return 'openai_video';
      if (compact.includes('sync') && compact.includes('video')) return 'video_sync';
      return '';
    }
    function resolveModelProviderType(model, defaultProviderType, manualProviderType = '') {
      return normalizeProviderType(manualProviderType)
        || inferProviderTypeFromModel(model)
        || normalizeProviderType(defaultProviderType)
        || 'openai';
    }
    function resolveVideoModelProviderType(model, defaultProviderType, manualProviderType = '') {
      return normalizeVideoProviderType(manualProviderType)
        || inferVideoProviderTypeFromModel(model)
        || normalizeVideoProviderType(defaultProviderType)
        || 'openai_video';
    }
    function collectModelProviderTypes(ch, enabled) {
      const enabledSet = new Set(enabled || []);
      const out = {};
      const sources = [ch.model_provider_types, ch.modelProviderTypes, ch.provider_types, ch.providerTypes];
      const isVideo = EDITING_CHANNEL_KIND === 'video' || !!normalizeVideoProviderType(ch.provider_type);
      for (const source of sources) {
        if (!source || typeof source !== 'object' || Array.isArray(source)) continue;
        for (const [model, provider] of Object.entries(source)) {
          const name = String(model || '').trim();
          const resolved = isVideo ? normalizeVideoProviderType(provider) : normalizeProviderType(provider);
          if (name && enabledSet.has(name) && resolved) out[name] = resolved;
        }
      }
      for (const item of ch.enabled_models || ch.enabledModels || []) {
        if (!item || typeof item !== 'object') continue;
        const name = modelId(item);
        const raw = item.provider_type || item.providerType || item.api_type || item.apiType;
        const resolved = isVideo ? normalizeVideoProviderType(raw) : normalizeProviderType(raw);
        if (name && enabledSet.has(name) && resolved) out[name] = resolved;
      }
      return out;
    }
    function compactModelProviderTypes(ch) {
      const enabledSet = new Set(ch.enabled_models || []);
      const out = {};
      const isVideo = !!normalizeVideoProviderType(ch.provider_type) || EDITING_CHANNEL_KIND === 'video';
      for (const [model, provider] of Object.entries(ch.model_provider_types || {})) {
        const resolved = isVideo ? normalizeVideoProviderType(provider) : normalizeProviderType(provider);
        if (enabledSet.has(model) && resolved) out[model] = resolved;
      }
      ch.model_provider_types = out;
      return ch;
    }
    function normalizeChannel(ch) {
      ch = ch && typeof ch === 'object' ? ch : {};
      const enabled = uniq((ch.enabled_models || ch.enabledModels || (ch.model ? [ch.model] : [])).map(modelId));
      const cache = uniq((ch.models_cache || ch.modelsCache || ch.available_models || ch.availableModels || []).map(modelId));
      const rawType = ch.provider_type || ch.providerType || ch.api_type || ch.apiType;
      const videoType = normalizeVideoProviderType(rawType);
      const providerType = videoType || normalizeProviderType(rawType) || 'openai';
      const normalized = {
        name: String(ch.name || ch.id || 'new-channel').trim(),
        provider_type: providerType,
        base_url: String(ch.base_url || ch.baseUrl || '').trim(),
        api_key: String(ch.api_key || ch.apiKey || '').trim(),
        model: String(ch.model || enabled[0] || '').trim(),
        timeout: Number(ch.timeout || (videoType ? 300 : 280)),
        enabled: ch.enabled !== false,
        enabled_models: enabled,
        model_provider_types: collectModelProviderTypes(Object.assign({}, ch, {provider_type: providerType}), enabled),
        models_cache: cache,
        proxy: String(ch.proxy || '').trim(),
        extra: ch.extra && typeof ch.extra === 'object' ? ch.extra : {}
      };
      if (videoType || String(providerType).startsWith('video_')) {
        return compactModelProviderTypes(normalized);
      }
      return compactModelProviderTypes(applyProviderDefaults(normalized));
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
    function normalizeVideoChannels() {
      CONFIG.video_channels = (CONFIG.video_channels || []).map(ch => {
        ch = normalizeChannel(ch);
        ch.provider_type = normalizeVideoProviderType(ch.provider_type) || 'openai_video';
        if (Number(ch.timeout || 0) < 60) ch.timeout = 300;
        const enabledSet = new Set(ch.enabled_models || []);
        const out = {};
        for (const [model, provider] of Object.entries(ch.model_provider_types || {})) {
          const resolved = normalizeVideoProviderType(provider);
          if (enabledSet.has(model) && resolved) out[model] = resolved;
        }
        ch.model_provider_types = out;
        return ch;
      });
    }
    function channelListFor(kind) {
      if (kind === 'audit') return CONFIG.audit_channels;
      if (kind === 'video') return CONFIG.video_channels;
      return CONFIG.image_channels;
    }
    function setChannelListFor(kind, list) {
      if (kind === 'audit') CONFIG.audit_channels = list;
      else if (kind === 'video') CONFIG.video_channels = list;
      else CONFIG.image_channels = list;
    }
    function setChannelEnabledModels(ch, list) {
      ch.enabled_models = uniq(list);
      ch.model = ch.enabled_models[0] || '';
      compactModelProviderTypes(ch);
      softDisableChannelIfNoModels(ch);
    }
    function softDisableChannelIfNoModels(ch) {
      const models = (ch.enabled_models && ch.enabled_models.length) ? ch.enabled_models : (ch.model ? [ch.model] : []);
      if ((!models || !models.length) && ch.enabled !== false) {
        ch.enabled = false;
        return true;
      }
      return false;
    }
    function isChannelModalOpen() {
      return !!(
        $('channelModal')
        && $('channelModal').classList.contains('show')
        && (EDITING_CHANNEL_IS_NEW || EDITING_CHANNEL_INDEX >= 0)
      );
    }
    function channelFactory(kind = 'image') {
      if (kind === 'audit') return newAuditChannel;
      if (kind === 'video') return newVideoChannel;
      return newChannel;
    }
    function newChannel() {
      return normalizeChannel({name:'new-channel', provider_type:'openai', base_url:'https://api.openai.com', api_key:'', model:'', enabled_models:[], timeout:280, enabled:false, models_cache:[]});
    }
    function newAuditChannel() {
      return normalizeChannel({name:'audit-channel', provider_type:'openai', base_url:'https://api.openai.com', api_key:'', model:'', enabled_models:[], timeout:280, enabled:false, models_cache:[]});
    }
    function newVideoChannel() {
      return normalizeChannel({name:'video-channel', provider_type:'openai_video', base_url:'https://api.openai.com/v1', api_key:'', model:'', enabled_models:[], timeout:300, enabled:false, models_cache:[]});
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
      switchChannelPane('image');
      openChannelModal(-1, 'image', { isNew: true });
    }
    function addAuditChannel() {
      ensureConfig();
      normalizeAuditChannels();
      switchChannelPane('audit');
      openChannelModal(-1, 'audit', { isNew: true });
    }
    function addVideoChannel() {
      ensureConfig();
      normalizeVideoChannels();
      switchChannelPane('video');
      openChannelModal(-1, 'video', { isNew: true });
    }
    function removeChannel(index, kind = 'image') {
      if (!confirm('确认删除这个渠道？')) return;
      channelListFor(kind).splice(index, 1);
      renderAllChannelLists();
      refreshModelSelectors();
      scheduleChannelListAutoSave();
    }
    function duplicateChannel(index, kind = 'image') {
      const list = channelListFor(kind);
      const factory = kind === 'audit' ? newAuditChannel : (kind === 'video' ? newVideoChannel : newChannel);
      const copy = JSON.parse(JSON.stringify(list[index] || factory()));
      copy.name = (copy.name || 'channel') + '-copy';
      list.splice(index + 1, 0, normalizeChannel(copy));
      renderAllChannelLists();
      refreshModelSelectors();
      scheduleChannelListAutoSave();
    }
    function renderAllChannelLists() {
      renderChannels();
      renderAuditChannels();
      renderVideoChannels();
    }
    function renderChannelCards(boxId, list, kind, emptyText) {
      const box = $(boxId);
      if (!box) return;
      box.innerHTML = '';
      if (!list.length) {
        box.innerHTML = `<div class="card soft muted">${emptyText}</div>`;
        return;
      }
      list.forEach((ch, i) => {
        const card = document.createElement('div');
        card.className = 'card';
        const enabledCount = Number((ch.enabled_models || []).length);
        const cacheCount = Number((ch.models_cache || []).length);
        card.innerHTML = `
          <div class="channel-row">
            <div>
              <b>${escapeHtml(ch.name || '未命名')}</b>
              <div class="actions" style="margin-top:6px">
                <span class="pill gray">缓存 ${cacheCount}</span>
                <span class="pill green">启用模型 ${enabledCount}</span>
                ${ch.enabled === false ? '<span class="pill gray">已停用</span>' : '<span class="pill">使用中</span>'}
              </div>
            </div>
            <div class="muted" style="font-size:12px">${kind === 'video' ? 'OpenAI 兼容视频' : '协议随模型'}</div>
            <label class="checkline"><input type="checkbox" ${ch.enabled !== false ? 'checked' : ''}>启用</label>
            <div class="actions" style="margin-top:0">
              <button class="secondary" type="button" data-act="edit">编辑</button>
              <button class="secondary" type="button" data-act="dup">复制</button>
              <button class="danger" type="button" data-act="del">删除</button>
            </div>
          </div>
        `;
        card.querySelector('input[type="checkbox"]').onchange = event => {
          const target = list[i];
          target.enabled = event.target.checked;
          if (target.enabled && softDisableChannelIfNoModels(target)) {
            event.target.checked = false;
            setStatus('channelStatus', `${target.name || '渠道'} 还没有启用模型，已保持关闭`);
          }
          refreshModelSelectors();
          renderAllChannelLists();
          scheduleChannelListAutoSave();
        };
        card.querySelector('[data-act="edit"]').onclick = () => openChannelModal(i, kind);
        card.querySelector('[data-act="dup"]').onclick = () => duplicateChannel(i, kind);
        card.querySelector('[data-act="del"]').onclick = () => removeChannel(i, kind);
        box.appendChild(card);
      });
    }
    function renderChannels() {
      normalizeChannels();
      renderChannelCards('channelList', CONFIG.image_channels || [], 'image', '还没有生图渠道。');
    }
    function renderAuditChannels() {
      normalizeAuditChannels();
      renderChannelCards('auditChannelList', CONFIG.audit_channels || [], 'audit', '还没有审核渠道。');
    }
    function renderVideoChannels() {
      normalizeVideoChannels();
      renderChannelCards('videoChannelList', CONFIG.video_channels || [], 'video', '还没有视频渠道。');
    }
    function collectChannels() {
      normalizeChannels();
      normalizeAuditChannels();
      normalizeVideoChannels();
      for (const ch of [...(CONFIG.image_channels || []), ...(CONFIG.audit_channels || []), ...(CONFIG.video_channels || [])]) {
        softDisableChannelIfNoModels(ch);
      }
    }
    function removeAllEnabledModels() {
      const ch = currentModalChannel();
      setChannelEnabledModels(ch, []);
      $('modalModel').value = '';
      $('modalEnabled').checked = false;
      ch.enabled = false;
      CHANNEL_MODAL_DIRTY = true;
      renderModalModels(ch);
      refreshModelSelectors();
    }
    function collectModalChannel() {
      const factory = channelFactory(EDITING_CHANNEL_KIND);
      let source;
      if (EDITING_CHANNEL_IS_NEW) {
        source = normalizeChannel(CHANNEL_DRAFT || factory());
      } else {
        const list = channelListFor(EDITING_CHANNEL_KIND);
        source = normalizeChannel(list[EDITING_CHANNEL_INDEX] || factory());
      }
      const enabled = Array.from(document.querySelectorAll('#enabledModels .model-row .name')).map(el => el.textContent || '');
      const ch = normalizeChannel(Object.assign({}, source, {
        name: $('modalChannelName').value.trim(),
        provider_type: EDITING_CHANNEL_KIND === 'video'
          ? (normalizeVideoProviderType($('modalProvider').value || source.provider_type) || 'openai_video')
          : ($('modalProvider').value || source.provider_type || 'openai'),
        base_url: $('modalBaseUrl').value.trim(),
        api_key: $('modalApiKey').value.trim(),
        proxy: $('modalProxy').value.trim(),
        model: $('modalModel').value.trim(),
        timeout: Number($('modalTimeout').value || (EDITING_CHANNEL_KIND === 'video' ? 300 : 280)),
        enabled: $('modalEnabled').checked,
        enabled_models: enabled,
        model_provider_types: collectModalProviderTypes(enabled),
        models_cache: source.models_cache || []
      }));
      softDisableChannelIfNoModels(ch);
      if ($('modalEnabled')) $('modalEnabled').checked = ch.enabled !== false;
      return ch;
    }
    function collectModalProviderTypes(enabled) {
      const enabledSet = new Set(enabled || []);
      const out = {};
      document.querySelectorAll('#enabledModels .model-row').forEach(row => {
        const model = String(row.querySelector('.name')?.textContent || '').trim();
        const raw = row.querySelector('.model-provider')?.value || '';
        const provider = EDITING_CHANNEL_KIND === 'video'
          ? normalizeVideoProviderType(raw)
          : normalizeProviderType(raw);
        if (model && enabledSet.has(model) && provider) out[model] = provider;
      });
      return out;
    }
    function storeModalChannel(ch) {
      if (EDITING_CHANNEL_IS_NEW) {
        CHANNEL_DRAFT = ch;
        return;
      }
      if (EDITING_CHANNEL_INDEX >= 0) {
        channelListFor(EDITING_CHANNEL_KIND)[EDITING_CHANNEL_INDEX] = ch;
      }
    }
    function openChannelModal(index, kind = 'image', options = {}) {
      normalizeChannels();
      normalizeAuditChannels();
      normalizeVideoChannels();
      const isNew = !!options.isNew || index < 0;
      EDITING_CHANNEL_IS_NEW = isNew;
      EDITING_CHANNEL_KIND = (kind === 'audit' || kind === 'video') ? kind : 'image';
      EDITING_CHANNEL_INDEX = isNew ? -1 : index;
      CHANNEL_MODAL_DIRTY = false;
      const factory = channelFactory(EDITING_CHANNEL_KIND);
      let ch;
      if (isNew) {
        ch = normalizeChannel(factory());
        if (EDITING_CHANNEL_KIND === 'video') ch.provider_type = normalizeVideoProviderType(ch.provider_type) || 'openai_video';
        else applyProviderDefaults(ch);
        CHANNEL_DRAFT = ch;
      } else {
        const list = channelListFor(EDITING_CHANNEL_KIND);
        ch = normalizeChannel(list[index] || factory());
        if (EDITING_CHANNEL_KIND === 'video') ch.provider_type = normalizeVideoProviderType(ch.provider_type) || 'openai_video';
        else applyProviderDefaults(ch);
        list[index] = ch;
        CHANNEL_DRAFT = null;
      }
      const titles = {
        image: isNew ? '新建生图渠道' : '编辑生图渠道',
        audit: isNew ? '新建审核渠道' : '编辑审核渠道',
        video: isNew ? '新建视频渠道' : '编辑视频渠道',
      };
      $('channelModalTitle').textContent = titles[EDITING_CHANNEL_KIND] || (isNew ? '新建渠道' : '编辑渠道');
      const providerChoices = EDITING_CHANNEL_KIND === 'video'
        ? VIDEO_PROVIDERS
        : (EDITING_CHANNEL_KIND === 'audit' ? AUDIT_PROVIDERS : PROVIDERS);
      const defaultProvider = EDITING_CHANNEL_KIND === 'video'
        ? (normalizeVideoProviderType(ch.provider_type) || 'openai_video')
        : (ch.provider_type || 'openai');
      setSelectOptions('modalProvider', providerChoices, defaultProvider);
      // label video options more readable
      if (EDITING_CHANNEL_KIND === 'video') {
        const sel = $('modalProvider');
        Array.from(sel.options || []).forEach(opt => {
          opt.textContent = VIDEO_PROVIDER_LABELS[opt.value] || opt.value;
        });
      }
      // 生图/审核：渠道级类型隐藏（模型旁自动/手动）；视频：显示协议下拉
      if ($('modalProviderWrap')) {
        $('modalProviderWrap').style.display = EDITING_CHANNEL_KIND === 'video' ? '' : 'none';
      }
      $('modalChannelName').value = ch.name || '';
      $('modalProvider').value = defaultProvider;
      $('modalBaseUrl').value = ch.base_url || '';
      $('modalApiKey').value = ch.api_key || '';
      $('modalProxy').value = ch.proxy || '';
      $('modalModel').value = ch.model || '';
      $('modalTimeout').value = ch.timeout || (EDITING_CHANNEL_KIND === 'video' ? 300 : 280);
      $('modalEnabled').checked = ch.enabled !== false;
      $('cacheSearch').value = '';
      $('manualModel').value = '';
      $('modalStatus').textContent = isNew ? '填写后点「保存渠道」才会加入列表；关闭则不添加。' : '';
      if ($('modalProviderHint')) {
        $('modalProviderHint').textContent = EDITING_CHANNEL_KIND === 'video'
          ? '视频协议可选：异步轮询 / 同步等待 / 对话回链。模型旁可「自动」识别或手动指定；与生图渠道用法一致。'
          : '渠道默认协议会按模型名自动识别；也可在下方已启用模型旁手动切换协议。';
      }
      renderModalModels(ch);
      $('channelModal').classList.add('show');
    }
    function modalProviderChanged() {
      const ch = currentModalChannel();
      if (EDITING_CHANNEL_KIND === 'video') {
        ch.provider_type = normalizeVideoProviderType($('modalProvider').value) || 'openai_video';
      } else {
        ch.provider_type = $('modalProvider').value || 'openai';
        applyProviderDefaults(ch);
        $('modalBaseUrl').value = ch.base_url || '';
        $('modalModel').value = ch.model || '';
      }
      CHANNEL_MODAL_DIRTY = true;
      storeModalChannel(ch);
      renderModalModels(ch);
      refreshModelSelectors();
    }
    function closeChannelModal() {
      $('channelModal').classList.remove('show');
      EDITING_CHANNEL_INDEX = -1;
      EDITING_CHANNEL_KIND = 'image';
      EDITING_CHANNEL_IS_NEW = false;
      CHANNEL_DRAFT = null;
      CHANNEL_MODAL_DIRTY = false;
    }
    function renderModalModels(ch) {
      const factory = channelFactory(EDITING_CHANNEL_KIND);
      if (!ch) {
        if (EDITING_CHANNEL_IS_NEW) ch = CHANNEL_DRAFT || factory();
        else ch = channelListFor(EDITING_CHANNEL_KIND)[EDITING_CHANNEL_INDEX] || factory();
      }
      ch = normalizeChannel(ch);
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
        return `<div class="model-row" data-model="${escapeHtml(item)}"><div class="name">${escapeHtml(item)}</div><div class="actions"><button class="secondary mini" type="button" onclick="${active ? `removeEnabledModel('${escapeJs(item)}')` : `addEnabledModel('${escapeJs(item)}')`}">${active ? '取消' : '启用'}</button></div></div>`;
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
      const isVideo = EDITING_CHANNEL_KIND === 'video';
      const manual = isVideo
        ? normalizeVideoProviderType((ch.model_provider_types || {})[model] || '')
        : normalizeProviderType((ch.model_provider_types || {})[model] || '');
      let choices = isVideo
        ? VIDEO_PROVIDERS.slice()
        : (EDITING_CHANNEL_KIND === 'audit' ? AUDIT_PROVIDERS.slice() : PROVIDERS.slice());
      if (manual && !choices.includes(manual)) choices.unshift(manual);
      const auto = isVideo
        ? resolveVideoModelProviderType(model, ch.provider_type, '')
        : resolveModelProviderType(model, ch.provider_type, '');
      const autoLabel = isVideo ? (VIDEO_PROVIDER_LABELS[auto] || auto) : auto;
      const options = [`<option value="" ${manual ? '' : 'selected'}>自动：${escapeHtml(autoLabel)}</option>`]
        .concat(choices.map(provider => {
          const label = isVideo ? (VIDEO_PROVIDER_LABELS[provider] || provider) : provider;
          return `<option value="${escapeHtml(provider)}" ${manual === provider ? 'selected' : ''}>${escapeHtml(label)}</option>`;
        }));
      return `<select class="model-provider" title="模型类型：自动按模型名识别，也可手动指定" onchange="setModelProviderType(${index}, this.value)">${options.join('')}</select>`;
    }
    function currentModalChannel() {
      const ch = collectModalChannel();
      storeModalChannel(ch);
      return ch;
    }
    function setModelProviderType(index, provider) {
      const ch = currentModalChannel();
      const model = (ch.enabled_models || [])[index] || '';
      if (!model) return;
      ch.model_provider_types ||= {};
      const resolved = EDITING_CHANNEL_KIND === 'video'
        ? normalizeVideoProviderType(provider)
        : normalizeProviderType(provider);
      if (resolved) ch.model_provider_types[model] = resolved;
      else delete ch.model_provider_types[model];
      compactModelProviderTypes(ch);
      storeModalChannel(ch);
      CHANNEL_MODAL_DIRTY = true;
      renderModalModels(ch);
      refreshModelSelectors();
    }
    function addEnabledModel(name) {
      const ch = currentModalChannel();
      setChannelEnabledModels(ch, (ch.enabled_models || []).concat([name]));
      storeModalChannel(ch);
      CHANNEL_MODAL_DIRTY = true;
      renderModalModels(ch);
      refreshModelSelectors();
    }
    function removeEnabledModel(name) {
      const ch = currentModalChannel();
      setChannelEnabledModels(ch, (ch.enabled_models || []).filter(item => item !== name));
      if ($('modalEnabled')) $('modalEnabled').checked = ch.enabled !== false;
      storeModalChannel(ch);
      CHANNEL_MODAL_DIRTY = true;
      renderModalModels(ch);
      refreshModelSelectors();
    }
    function moveEnabledModel(index, delta) {
      const ch = currentModalChannel();
      const next = index + delta;
      if (next < 0 || next >= ch.enabled_models.length) return;
      const list = ch.enabled_models.slice();
      const item = list.splice(index, 1)[0];
      list.splice(next, 0, item);
      setChannelEnabledModels(ch, list);
      storeModalChannel(ch);
      CHANNEL_MODAL_DIRTY = true;
      renderModalModels(ch);
      refreshModelSelectors();
    }
    async function refreshChannelModels(index = EDITING_CHANNEL_INDEX) {
      const ch = (EDITING_CHANNEL_IS_NEW || index === EDITING_CHANNEL_INDEX)
        ? currentModalChannel()
        : normalizeChannel(channelListFor(EDITING_CHANNEL_KIND)[index]);
      $('modalStatus').textContent = `正在刷新 ${ch.name || '渠道'} 模型...`;
      try {
        const res = await api('/api/refresh-image-models', {method:'POST', body: JSON.stringify({channel: ch})});
        ch.models_cache = res.data || [];
        if (EDITING_CHANNEL_IS_NEW || index === EDITING_CHANNEL_INDEX) {
          storeModalChannel(ch);
          CHANNEL_MODAL_DIRTY = true;
          renderModalModels(ch);
        } else {
          channelListFor(EDITING_CHANNEL_KIND)[index] = ch;
          renderAllChannelLists();
        }
        refreshModelSelectors();
        $('modalStatus').textContent = `刷新成功：${ch.models_cache.length} 个模型（点「保存渠道」后生效）`;
      } catch (e) { $('modalStatus').textContent = e.message; }
    }
    async function saveChannelModal() {
      const ch = currentModalChannel();
      if (!ch.name) {
        $('modalStatus').textContent = '渠道名不能为空';
        return;
      }
      if (!ch.model && ch.enabled_models.length) ch.model = ch.enabled_models[0];
      softDisableChannelIfNoModels(ch);
      const list = channelListFor(EDITING_CHANNEL_KIND);
      if (EDITING_CHANNEL_IS_NEW) {
        list.push(ch);
      } else if (EDITING_CHANNEL_INDEX >= 0) {
        list[EDITING_CHANNEL_INDEX] = ch;
      } else {
        $('modalStatus').textContent = '没有可保存的渠道';
        return;
      }
      renderAllChannelLists();
      refreshModelSelectors();
      $('modalStatus').textContent = '保存中...';
      const ok = await persistConfig(false, EDITING_CHANNEL_IS_NEW ? '渠道已添加' : '渠道已保存', { toast: true });
      $('modalStatus').textContent = ok
        ? (ch.enabled === false && !(ch.enabled_models || []).length
          ? (EDITING_CHANNEL_IS_NEW ? '已添加（无启用模型，渠道已自动关闭）' : '已保存（无启用模型，渠道已自动关闭）')
          : (EDITING_CHANNEL_IS_NEW ? '已添加' : '已保存'))
        : '保存失败，请检查上方提示';
      if (ok) {
        CHANNEL_MODAL_DIRTY = false;
        closeChannelModal();
      } else if (EDITING_CHANNEL_IS_NEW) {
        // 保存失败时撤回刚 push 的草稿，避免列表脏数据
        const pos = list.lastIndexOf(ch);
        if (pos >= 0) list.splice(pos, 1);
        CHANNEL_DRAFT = ch;
        renderAllChannelLists();
        refreshModelSelectors();
      }
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
      // Channel connectivity tests are more stable on an explicit square size.
      if ($('testAspect') && (!$('testAspect').value || $('testAspect').value === '自动')) {
        $('testAspect').value = '1:1';
      }
    }
    function addPriority() {
      if (isRandomImageModel()) return;
      const value = $('priorityPicker').value;
      if (!value) return;
      const current = textList('priorityList');
      if (!current.includes(value)) current.push(value);
      $('priorityList').value = current.join('\n');
      renderPriorityRows();
      scheduleChannelListAutoSave();
    }
    function clearPriority() {
      if (isRandomImageModel()) return;
      $('priorityList').value = '';
      renderPriorityRows();
      scheduleChannelListAutoSave();
    }
    function setPriorityItems(items) {
      $('priorityList').value = uniq(items).join('\n');
      renderPriorityRows();
      scheduleChannelListAutoSave();
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
      if (isRandomImageModel()) return;
      const items = textList('priorityList');
      const next = index + delta;
      if (next < 0 || next >= items.length) return;
      const item = items.splice(index, 1)[0];
      items.splice(next, 0, item);
      setPriorityItems(items);
    }
    function removePriority(index) {
      if (isRandomImageModel()) return;
      const items = textList('priorityList');
      items.splice(index, 1);
      setPriorityItems(items);
    }
    function isRandomImageModel() {
      return !!( $('randomImageModel') && $('randomImageModel').checked );
    }
    function onRandomImageModelChange() {
      // Keep priorityList as-is; only toggle runtime selection mode.
      CONFIG.random_image_model = isRandomImageModel();
      updatePriorityControlsState();
      renderPriorityRows();
      scheduleChannelListAutoSave();
    }
    function updatePriorityControlsState() {
      const random = isRandomImageModel();
      const picker = $('priorityPicker');
      const addBtn = $('addPriorityBtn');
      const clearBtn = $('clearPriorityBtn');
      if (picker) picker.disabled = random;
      if (addBtn) addBtn.disabled = random;
      if (clearBtn) clearBtn.disabled = random;
      const box = $('priorityRows');
      if (box) box.style.opacity = random ? '0.55' : '1';
    }
    function renderPriorityRows() {
      const items = textList('priorityList');
      const box = $('priorityRows');
      const random = isRandomImageModel();
      updatePriorityControlsState();
      if (random) {
        box.innerHTML = '<div class="muted">已开启随机：每次生图从全部已启用模型中随机排序；下方优先级列表仍保留，关闭随机后立即生效。</div>'
          + (items.length ? items.map((item, i) => `
        <div class="model-row">
          <div class="name">${escapeHtml(item)} <span class="muted">（暂停）</span></div>
        </div>`).join('') : '');
        return;
      }
      box.innerHTML = items.map((item, i) => `
        <div class="model-row">
          <div class="name">${escapeHtml(item)}</div>
          <div class="actions">
            <button class="secondary mini" type="button" onclick="movePriority(${i}, -1)">上移</button>
            <button class="secondary mini" type="button" onclick="movePriority(${i}, 1)">下移</button>
            <button class="danger mini" type="button" onclick="removePriority(${i})">移除</button>
          </div>
        </div>`).join('') || '<div class="muted">未设置优先级时按渠道与已启用模型顺序尝试。</div>';
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
    async function persistConfig(renderAfterSave = false, okText = '配置已保存', options = {}) {
      const toastOnOk = options.toast === true;
      const toastOnError = options.toastError !== false;
      const quiet = options.quiet === true;
      try {
        collectForms();
        const res = await api('/api/config', {method:'POST', body: JSON.stringify({config: CONFIG})});
        CONFIG = res.data || CONFIG;
        ensureConfig();
        $('configText').value = JSON.stringify(CONFIG, null, 2);
        if (renderAfterSave) fillForms();
        else if (!quiet) {
          // keep lists in sync with server soft-disable without full form thrash
          renderAllChannelLists();
          refreshModelSelectors();
        }
        if (okText) setMultiStatus(okText);
        if (toastOnOk && okText) showToast(okText, 'ok');
        return true;
      } catch (e) {
        setMultiStatus(e.message);
        if (toastOnError) showToast(e.message, 'bad');
        return false;
      }
    }
    async function saveAll() {
      await persistConfig(true, '配置已保存', { toast: true });
    }
    function scheduleAutoSave(okText = '', options = {}) {
      if (IS_FILLING || SUPPRESS_AUTOSAVE || !document.body.classList.contains('authed')) return;
      // 渠道弹窗：只本地草稿，必须点「保存渠道」
      if (isChannelModalOpen() && options.allowWhileModal !== true) return;
      clearTimeout(AUTO_SAVE_TIMER);
      const reason = options.reason || 'form';
      // 输入框自动保存不刷「正在保存…」，避免每敲一字全页状态抖动
      if (!options.silentStatus) setMultiStatus('正在保存…');
      AUTO_SAVE_TIMER = setTimeout(() => {
        persistConfig(false, okText || '', {
          toast: false,
          toastError: true,
          quiet: reason === 'form',
        });
      }, options.delay != null ? options.delay : (reason === 'form' ? 900 : 650));
    }
    /** 主表单字段变更：仅配置页，不负责渠道弹窗/试画/记录。 */
    function scheduleFormAutoSave() {
      scheduleAutoSave('', { reason: 'form', silentStatus: true, toast: false });
    }
    /** 渠道列表结构化变更（删/复制/启停、优先级）：静默落盘。 */
    function scheduleChannelListAutoSave() {
      if (isChannelModalOpen()) return;
      scheduleAutoSave('', { reason: 'channel-list', silentStatus: true, toast: false, delay: 500 });
    }
    async function saveJsonConfig() {
      try {
        CONFIG = JSON.parse($('configText').value || '{}');
        ensureConfig();
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
        let objectUrl = '';
        if (isDashboardPage()) {
          const res = await api('/api/cache-image-preview?path=' + encodeURIComponent(path));
          const data = res.data || {};
          if (!data.data_url) throw new Error('图片已清理');
          objectUrl = data.data_url;
          img.src = objectUrl;
          return;
        }
        const res = await fetch(cacheImageUrl(path), {headers: headers()});
        if (!res.ok) throw new Error('图片已清理');
        const blob = await res.blob();
        objectUrl = URL.createObjectURL(blob);
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
      $('testImageBtn').textContent = busy ? '正在画…' : '开始试画';
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
    async function pollImageTestTask(taskId, failStreak = 0) {
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
          const seconds = Number(task.running_seconds || 0);
          const hint = seconds >= 20
            ? ' 有的中转要 15–60 秒，先等等；超过设定超时才会判失败。'
            : '';
          $('testStatus').textContent = `任务 ${task.task_id || TEST_TASK_ID} ${label}，已用 ${seconds}s。${hint}关掉页面也不会停。`;
          TEST_TASK_POLL_TIMER = setTimeout(() => pollImageTestTask(TEST_TASK_ID, 0), 2000);
          return;
        }
        setTestBusy(false);
        safeStorageRemove('selfieImageLastTestTaskId');
        renderImageTestResult(task.result || {success:false, error: task.error || '任务未返回结果'});
        try { await loadRecords(); } catch (_) {}
      } catch (e) {
        if (TEST_TASK_ID !== taskId) return;
        // Transient bridge/network errors should not freeze a still-running generation.
        const nextFail = failStreak + 1;
        if (nextFail < 8) {
          setTestBusy(true);
          $('testStatus').textContent = `轮询暂时失败（${nextFail}/8）：${e.message || e}。正在重试...`;
          TEST_TASK_POLL_TIMER = setTimeout(() => pollImageTestTask(TEST_TASK_ID, nextFail), 2000);
          return;
        }
        setTestBusy(false);
        $('testStatus').textContent = e.message;
        $('testResponseData').textContent = JSON.stringify({success:false, error:e.message}, null, 2);
        showTestPanel('response');
      }
    }
    async function resumeImageTestTask() {
      const taskId = safeStorageGet('selfieImageLastTestTaskId') || '';
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
          safeStorageRemove('selfieImageLastTestTaskId');
          $('testResponseData').textContent = JSON.stringify(task, null, 2);
          if (task.request_data) $('testRequestData').textContent = JSON.stringify(task.request_data, null, 2);
          renderImageTestResult(task.result || {success:false, error: task.error || '任务未返回结果'});
          try { await loadRecords(); } catch (_) {}
        }
      } catch (_) {
        safeStorageRemove('selfieImageLastTestTaskId');
      }
    }
    async function runImageTest() {
      collectForms();
      clearTestData(false);
      setTestBusy(true);
      $('testStatus').textContent = '正在提交试画…';
      try {
        if (!$('testChannel').value) throw new Error('还没有可用渠道，先去「渠道」里启用');
        if (!$('testModel').value) throw new Error('这个渠道还没有可用模型，先启用模型');
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
        safeStorageSet('selfieImageLastTestTaskId', TEST_TASK_ID);
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
      safeStorageRemove('selfieImageLastTestTaskId');
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
        $('selfieStatus').textContent = data.status || (data.has_image ? '已设置形象参考图' : '还没有形象参考图');
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
    async function enterApp(silent = false) {
      if (!silent && $('loginStatus')) $('loginStatus').textContent = '登录中...';
      if (isDashboardPage()) {
        AUTH_TOKEN = '';
        document.body.classList.add('dashboard-embedded');
        if ($('loginPage')) $('loginPage').style.display = 'none';
        if ($('logoutBtn')) $('logoutBtn').style.display = 'none';
      } else {
        AUTH_TOKEN = ($('loginToken')?.value || '').trim();
      }
      try {
        const res = await api('/api/config');
        CONFIG = res.data || {};
        if (!isDashboardPage()) {
          if (AUTH_TOKEN) safeStorageSet('selfieImageToken', AUTH_TOKEN);
          else safeStorageRemove('selfieImageToken');
          safeStorageRemove('aicatToken');
        }
        fillForms();
        document.body.classList.add('authed');
        if (isDashboardPage()) {
          document.body.classList.add('dashboard-embedded');
          if ($('loginPage')) $('loginPage').style.display = 'none';
          if ($('logoutBtn')) $('logoutBtn').style.display = 'none';
        }
        if ($('loginStatus')) $('loginStatus').textContent = '';
        await checkHealth();
        await refreshSelfie();
        await loadRecords(false);
        await resumeImageTestTask();
      } catch (e) {
        document.body.classList.remove('authed');
        const message = e.message || '登录失败';
        if (isDashboardPage()) {
          // Keep a visible error instead of falling back to token login.
          if ($('loginPage')) {
            $('loginPage').style.display = '';
            const box = $('loginPage').querySelector('.login-box');
            if (box) {
              box.innerHTML = `<h1>内嵌管理页加载失败</h1><p class="muted">已走 AstrBot Dashboard 登录态，不需要 Web Token。</p><div class="status">${escapeHtml(message)}</div>`;
            }
          }
          if ($('loginStatus')) $('loginStatus').textContent = message;
          return;
        }
        if (!silent || AUTH_TOKEN) {
          if ($('loginStatus')) $('loginStatus').textContent = message;
        }
      }
    }
    function logout() {
      if (isDashboardPage()) {
        showToast('AstrBot 内嵌管理页无需退出 Web Token', 'ok');
        return;
      }
      AUTH_TOKEN = '';
      safeStorageRemove('selfieImageToken');
      safeStorageRemove('aicatToken');
      if ($('loginToken')) $('loginToken').value = '';
      document.body.classList.remove('authed');
      if ($('loginStatus')) $('loginStatus').textContent = '已退出登录';
    }
    function setupAutoSave() {
      // 仅配置型页面字段自动保存；渠道弹窗/试画/记录/高级 JSON 排除。
      // 渠道新增/编辑：点「保存渠道」；列表启停/删/复制/优先级：scheduleChannelListAutoSave。
      const allowSections = new Set(['base', 'image', 'selfie', 'audit']);
      document.querySelectorAll('main.app-shell input, main.app-shell select, main.app-shell textarea').forEach(el => {
        if (el.type === 'file' || el.id === 'configText') return;
        if (el.closest('#channelModal') || el.closest('#recordModal')) return;
        if (el.closest('#test') || el.closest('#monitor') || el.closest('#raw')) return;
        // 渠道页里除 priority 隐藏域外，列表操作走专用 schedule，不绑通用 input 自动保存
        if (el.closest('#channels') && el.id !== 'priorityList') return;
        const section = el.closest('section');
        if (section && section.id && !allowSections.has(section.id) && section.id !== 'channels') return;
        const eventName = el.tagName === 'SELECT' || el.type === 'checkbox' || el.type === 'number' ? 'change' : 'input';
        el.addEventListener(eventName, () => scheduleFormAutoSave());
      });
    }


    function studioCacheUrl(path) {
      return cacheImageUrl(path);
    }
    async function studioApi(path, opts = {}) {
      return api(path, opts);
    }
    function studioStopPoll() {
      if (STUDIO.pollTimer) { clearInterval(STUDIO.pollTimer); STUDIO.pollTimer = null; }
    }
    function fillStudioAspect() {
      const aspects = ['自动','1:1','2:3','3:2','3:4','4:3','4:5','5:4','9:16','16:9','21:9'];
      setSelectOptions('studioAspect', aspects, (STUDIO.current && STUDIO.current.graph && STUDIO.current.graph.aspect_ratio) || '自动');
    }
    function renderStudioPromptChips() {
      const wrap = $('studioPromptChips');
      if (!wrap) return;
      wrap.innerHTML = '';
      for (const item of (STUDIO.prompts || [])) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'secondary';
        btn.textContent = item.title || item.id;
        btn.onclick = () => { $('studioPrompt').value = item.prompt || ''; };
        wrap.appendChild(btn);
      }
    }
    function renderStudioSessionSelect() {
      const el = $('studioSessionSelect');
      if (!el) return;
      const cur = STUDIO.current && STUDIO.current.id;
      el.innerHTML = '';
      const blank = document.createElement('option');
      blank.value = '';
      blank.textContent = '（未选择）';
      el.appendChild(blank);
      for (const s of STUDIO.sessions || []) {
        const opt = document.createElement('option');
        opt.value = s.id;
        opt.textContent = `${s.title || s.id} · ${s.updated_at || ''}`;
        if (s.id === cur) opt.selected = true;
        el.appendChild(opt);
      }
    }
    function renderStudioSlots() {
      const wrap = $('studioSlots');
      if (!wrap) return;
      wrap.innerHTML = '';
      const session = STUDIO.current;
      if (!session) { wrap.innerHTML = '<div class="status">还没有画布会话</div>'; return; }
      for (const slot of session.slots || []) {
        const card = document.createElement('div');
        const has = !!(slot.image_path);
        card.className = 'studio-slot ' + (has ? 'has-image' : 'empty');
        card.innerHTML = `
          <div class="slot-label">${slot.label || slot.role || '槽位'} · ${slot.role || ''}</div>
          <img alt="" ${has ? `src="${studioCacheUrl(slot.image_path)}"` : ''}>
          <div class="slot-actions">
            <button type="button" class="secondary" data-act="upload">上传</button>
            <button type="button" class="secondary" data-act="clear" ${has ? '' : 'disabled'}>清空</button>
          </div>`;
        card.querySelector('[data-act="upload"]').onclick = () => {
          STUDIO.uploadSlotId = slot.id;
          $('studioFilePick').click();
        };
        card.querySelector('[data-act="clear"]').onclick = async () => {
          const res = await studioApi(`/api/studio/sessions/${encodeURIComponent(session.id)}/slots/${encodeURIComponent(slot.id)}`, {
            method: 'POST', body: JSON.stringify({ clear: true })
          });
          if (!res.success) { showToast(res.message || '清空失败', 'bad'); return; }
          STUDIO.current = res.data; renderStudio();
        };
        wrap.appendChild(card);
      }
    }
    function renderStudioResults() {
      const wrap = $('studioResults');
      if (!wrap) return;
      wrap.innerHTML = '';
      const session = STUDIO.current;
      if (!session) return;
      for (const item of session.results || []) {
        const card = document.createElement('div');
        card.className = 'card';
        const path = item.image_path || '';
        card.innerHTML = `
          <img src="${studioCacheUrl(path)}" alt="">
          <div class="actions" style="margin-top:6px">
            <button type="button" class="secondary mini" data-act="peer">作同框</button>
            <button type="button" class="secondary mini" data-act="id">作形象</button>
          </div>
          <div class="status" style="font-size:11px">${item.created_at || ''}</div>`;
        const peer = (session.slots || []).find(s => s.role === 'peer') || (session.slots || [])[1];
        const identity = (session.slots || []).find(s => s.role === 'identity') || (session.slots || [])[0];
        card.querySelector('[data-act="peer"]').onclick = async () => {
          if (!peer) return showToast('没有同框槽', 'bad');
          const res = await studioApi(`/api/studio/sessions/${encodeURIComponent(session.id)}/promote`, {
            method:'POST', body: JSON.stringify({ result_id: item.id, slot_id: peer.id })
          });
          if (!res.success) return showToast(res.message || '失败', 'bad');
          STUDIO.current = res.data; renderStudio(); showToast('已放进同框槽', 'ok');
        };
        card.querySelector('[data-act="id"]').onclick = async () => {
          if (!identity) return showToast('没有形象槽', 'bad');
          const res = await studioApi(`/api/studio/sessions/${encodeURIComponent(session.id)}/promote`, {
            method:'POST', body: JSON.stringify({ result_id: item.id, slot_id: identity.id })
          });
          if (!res.success) return showToast(res.message || '失败', 'bad');
          STUDIO.current = res.data; renderStudio(); showToast('已放进形象槽', 'ok');
        };
        wrap.appendChild(card);
      }
      const last = session.last_run;
      if (last && last.status === 'failed' && last.error) {
        const err = document.createElement('div');
        err.className = 'status';
        err.textContent = '上次失败：' + last.error;
        wrap.prepend(err);
      }
    }
    function renderStudio() {
      renderStudioSessionSelect();
      fillStudioAspect();
      renderStudioPromptChips();
      const s = STUDIO.current;
      if (!s) {
        $('studioTitle').value = '';
        $('studioPrompt').value = '';
        $('studioStatus').textContent = '打开后点「新建合影画布」，或选已有会话。';
        renderStudioSlots();
        renderStudioResults();
        return;
      }
      const g = s.graph || {};
      $('studioTitle').value = s.title || '';
      $('studioPrompt').value = g.prompt || '';
      $('studioMode').value = g.mode || 'group';
      $('studioResolution').value = g.resolution || '1K';
      $('studioCount').value = g.count || 1;
      $('studioUsePersona').checked = g.use_persona_identity !== false;
      $('studioPolicy').value = g.channel_policy || 'priority';
      setSelectOptions('studioAspect', ['自动','1:1','2:3','3:2','3:4','4:3','4:5','5:4','9:16','16:9','21:9'], g.aspect_ratio || '自动');
      const lr = s.last_run;
      if (lr && lr.status === 'running') $('studioStatus').textContent = '生成中… ' + (lr.task_id || '');
      else if (lr && lr.status === 'succeeded') $('studioStatus').textContent = '完成 · ' + (lr.used_model || '') + ' · ' + ((lr.result_paths||[]).length) + ' 张';
      else if (lr && lr.status === 'failed') $('studioStatus').textContent = '失败：' + (lr.error || '');
      else $('studioStatus').textContent = '已加载：' + (s.title || s.id);
      renderStudioSlots();
      renderStudioResults();
    }
    async function loadStudioList(selectId) {
      const res = await studioApi('/api/studio/sessions');
      if (!res.success) { showToast(res.message || '画布列表失败', 'bad'); return; }
      STUDIO.sessions = (res.data && res.data.sessions) || [];
      STUDIO.prompts = (res.data && res.data.builtin_prompts) || [];
      if (selectId) {
        const detail = await studioApi('/api/studio/sessions/' + encodeURIComponent(selectId));
        if (detail.success) STUDIO.current = detail.data;
      } else if (STUDIO.current && STUDIO.current.id) {
        const detail = await studioApi('/api/studio/sessions/' + encodeURIComponent(STUDIO.current.id));
        if (detail.success) STUDIO.current = detail.data;
      }
      renderStudio();
    }
    async function studioCreate() {
      const res = await studioApi('/api/studio/sessions', {
        method:'POST',
        body: JSON.stringify({ title: $('studioTitle').value || '合影画布', use_group_template: true })
      });
      if (!res.success) return showToast(res.message || '创建失败', 'bad');
      STUDIO.current = res.data;
      await loadStudioList(res.data.id);
      showToast('已新建合影画布', 'ok');
    }
    async function studioSave() {
      if (!STUDIO.current) return showToast('请先新建或选择画布', 'bad');
      const body = {
        title: $('studioTitle').value.trim(),
        graph: {
          prompt: $('studioPrompt').value,
          mode: $('studioMode').value,
          aspect_ratio: $('studioAspect').value,
          resolution: $('studioResolution').value,
          count: Number($('studioCount').value || 1),
          use_persona_identity: $('studioUsePersona').checked,
          channel_policy: $('studioPolicy').value,
        }
      };
      const res = await studioApi('/api/studio/sessions/' + encodeURIComponent(STUDIO.current.id), {
        method:'POST', body: JSON.stringify(body)
      });
      if (!res.success) return showToast(res.message || '保存失败', 'bad');
      STUDIO.current = res.data;
      await loadStudioList(STUDIO.current.id);
      showToast('画布已保存', 'ok');
    }
    async function studioDelete() {
      if (!STUDIO.current) return;
      const res = await studioApi('/api/studio/sessions/' + encodeURIComponent(STUDIO.current.id) + '/delete', {
        method:'POST', body: JSON.stringify({})
      });
      if (!res.success) return showToast(res.message || '删除失败', 'bad');
      STUDIO.current = null;
      await loadStudioList();
      showToast('已删除', 'ok');
    }
    async function studioRun() {
      if (!STUDIO.current) return showToast('请先新建或选择画布', 'bad');
      await studioSave();
      if (!STUDIO.current) return;
      $('studioStatus').textContent = '已提交生成…';
      const res = await studioApi('/api/studio/sessions/' + encodeURIComponent(STUDIO.current.id) + '/run', {
        method:'POST', body: JSON.stringify({})
      });
      if (!res.success) {
        $('studioStatus').textContent = res.message || '启动失败';
        return showToast(res.message || '启动失败', 'bad');
      }
      const taskId = res.data && res.data.task_id;
      showToast('开始生成', 'ok');
      studioStopPoll();
      STUDIO.pollTimer = setInterval(async () => {
        try {
          const st = await studioApi('/api/studio/tasks/' + encodeURIComponent(taskId));
          if (!st.success) return;
          const task = st.data || {};
          if (task.status === 'queued' || task.status === 'running') {
            $('studioStatus').textContent = '生成中… ' + (task.running_seconds != null ? task.running_seconds + 's' : '');
            return;
          }
          studioStopPoll();
          await loadStudioList(STUDIO.current.id);
          if (task.success) showToast('画布生成完成', 'ok');
          else showToast(task.error || '生成失败', 'bad');
        } catch (e) {}
      }, 1500);
    }
    async function studioUploadFile(file) {
      if (!STUDIO.current || !STUDIO.uploadSlotId || !file) return;
      const dataUrl = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = reject;
        reader.readAsDataURL(file);
      });
      const res = await studioApi(`/api/studio/sessions/${encodeURIComponent(STUDIO.current.id)}/slots/${encodeURIComponent(STUDIO.uploadSlotId)}`, {
        method:'POST', body: JSON.stringify({ image: dataUrl, source: 'upload' })
      });
      STUDIO.uploadSlotId = '';
      if (!res.success) return showToast(res.message || '上传失败', 'bad');
      STUDIO.current = res.data;
      renderStudio();
      showToast('参考图已放入槽位', 'ok');
    }

    document.querySelectorAll('nav.page-nav button').forEach(btn => {
      btn.onclick = () => {
        document.querySelectorAll('nav.page-nav button').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('section').forEach(s => s.classList.remove('active'));
        btn.classList.add('active');
        $(btn.dataset.tab).classList.add('active');
        if (btn.dataset.tab === 'monitor') loadRecords();
        if (btn.dataset.tab === 'studio') loadStudioList(STUDIO.current && STUDIO.current.id);
      };
    });
    $('loginBtn').onclick = enterApp;
    $('loginToken').onkeydown = event => { if (event.key === 'Enter') enterApp(); };
    $('logoutBtn').onclick = logout;
    $('reloadAll').onclick = async () => { await checkHealth(); await loadConfig(); await refreshSelfie(); await loadRecords(); };
    $('modalSave').onclick = saveChannelModal;
    $('modalProvider').onchange = modalProviderChanged;
    $('modalRefreshModels').onclick = () => refreshChannelModels();
    $('modalEnableAll').onclick = () => { removeAllEnabledModels(); };
    $('cacheSearch').oninput = () => renderModalModels(currentModalChannel());
    $('manualAdd').onclick = () => {
      const value = $('manualModel').value.trim();
      if (!value) return;
      addEnabledModel(value);
      $('manualModel').value = '';
    };
    $('manualModel').onkeydown = event => { if (event.key === 'Enter') $('manualAdd').click(); };
    // Modal field edits stay local until「保存渠道」
    ['modalChannelName','modalBaseUrl','modalApiKey','modalProxy','modalModel','modalTimeout','modalEnabled'].forEach(id => {
      const el = $(id);
      if (!el) return;
      const eventName = el.type === 'checkbox' || el.type === 'number' ? 'change' : 'input';
      el.addEventListener(eventName, () => { if (isChannelModalOpen()) CHANNEL_MODAL_DIRTY = true; });
    });
    $('testImageBtn').onclick = runImageTest;
    if ($('studioCreateBtn')) $('studioCreateBtn').onclick = studioCreate;
    if ($('studioReloadBtn')) $('studioReloadBtn').onclick = () => loadStudioList(STUDIO.current && STUDIO.current.id);
    if ($('studioDeleteBtn')) $('studioDeleteBtn').onclick = studioDelete;
    if ($('studioSaveBtn')) $('studioSaveBtn').onclick = studioSave;
    if ($('studioRunBtn')) $('studioRunBtn').onclick = studioRun;
    if ($('studioAddSlotBtn')) $('studioAddSlotBtn').onclick = async () => {
      if (!STUDIO.current) return showToast('请先新建画布', 'bad');
      const res = await studioApi('/api/studio/sessions/' + encodeURIComponent(STUDIO.current.id) + '/slots', {method:'POST', body: JSON.stringify({role:'extra', label:'额外参考'})});
      if (!res.success) return showToast(res.message || '失败', 'bad');
      STUDIO.current = res.data; renderStudio();
    };
    if ($('studioSessionSelect')) $('studioSessionSelect').onchange = async () => {
      const id = $('studioSessionSelect').value;
      if (!id) { STUDIO.current = null; renderStudio(); return; }
      const res = await studioApi('/api/studio/sessions/' + encodeURIComponent(id));
      if (!res.success) return showToast(res.message || '加载失败', 'bad');
      STUDIO.current = res.data; renderStudio();
    };
    if ($('studioFilePick')) $('studioFilePick').onchange = async (ev) => {
      const file = ev.target.files && ev.target.files[0];
      ev.target.value = '';
      if (file) await studioUploadFile(file);
    };

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
      // AstrBot injects bridge-sdk at </body> if missing; we also preload it above.
      // Still wait when running inside the plugin iframe so boot never races token login.
      const embedded = isEmbeddedFrame()
        || String(window.location.pathname || '').startsWith('/api/plugin/page/content/')
        || Boolean(window.AstrBotPluginPage);
      if (embedded) {
        if ($('loginPage')) $('loginPage').style.display = 'none';
        if ($('loginStatus')) $('loginStatus').textContent = '正在连上 AstrBot 管理通道…';
        if ($('loginHint')) $('loginHint').textContent = '插件页会沿用后台登录，不必再输管理口令。';
        try {
          DASHBOARD_BRIDGE = await waitForDashboardBridge(12000);
          if (typeof DASHBOARD_BRIDGE.ready === 'function') await DASHBOARD_BRIDGE.ready();
        } catch (e) {
          if ($('loginPage')) $('loginPage').style.display = '';
          if ($('loginStatus')) $('loginStatus').textContent = e.message || '内嵌 bridge 未就绪';
          const box = $('loginPage') && $('loginPage').querySelector('.login-box');
          if (box) {
            box.innerHTML = `<h1>内嵌管理页加载失败</h1><p class="muted">已检测为 AstrBot 内嵌入口，不需要 Web Token。请重载插件或刷新 Dashboard。</p><div class="status">${escapeHtml(e.message || 'bridge 未就绪')}</div>`;
          }
          return;
        }
        await enterApp(true);
        return;
      }
      await enterApp(!AUTH_TOKEN);
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

        def check_auth() -> bool:
            configured = str(getattr(self.plugin.config, "web_token", "") or "").strip()
            if not configured:
                return True
            configured_bytes = configured.encode("utf-8")
            for token in token_candidates_from_request():
                try:
                    if hmac.compare_digest(token.encode("utf-8"), configured_bytes):
                        return True
                except Exception:
                    continue
            return False

        @app.route("/", methods=["GET"])
        @app.route("/index.html", methods=["GET"])
        def index() -> Any:
            return render_index_html()

        @app.route("/api/health", methods=["GET"])
        def health() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return ok(
                {
                    "status": "ok",
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


        @app.route("/api/studio/sessions", methods=["GET", "POST"])
        def studio_sessions() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            if request.method == "GET":
                return ok(self.plugin.studio_list())
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                return ok(self.plugin.studio_create(payload or {}))
            except Exception as exc:
                return fail(str(exc))

        @app.route("/api/studio/sessions/<session_id>", methods=["GET", "POST"])
        def studio_session_detail(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            if request.method == "GET":
                try:
                    return ok(self.plugin.studio_get(session_id))
                except Exception as exc:
                    return fail(str(exc), 404)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                return ok(self.plugin.studio_update(session_id, payload or {}))
            except Exception as exc:
                return fail(str(exc))

        @app.route("/api/studio/sessions/<session_id>/delete", methods=["POST"])
        def studio_session_delete(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            _, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                return ok(self.plugin.studio_delete(session_id))
            except Exception as exc:
                return fail(str(exc), 404)

        @app.route("/api/studio/sessions/<session_id>/slots/<slot_id>", methods=["POST"])
        def studio_session_slot(session_id: str, slot_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                return ok(self.plugin.studio_set_slot(session_id, slot_id, payload or {}))
            except Exception as exc:
                return fail(str(exc))

        @app.route("/api/studio/sessions/<session_id>/slots", methods=["POST"])
        def studio_session_add_slot(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                return ok(self.plugin.studio_add_slot(session_id, payload or {}))
            except Exception as exc:
                return fail(str(exc))

        @app.route("/api/studio/sessions/<session_id>/reorder", methods=["POST"])
        def studio_session_reorder(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                return ok(self.plugin.studio_reorder(session_id, payload or {}))
            except Exception as exc:
                return fail(str(exc))

        @app.route("/api/studio/sessions/<session_id>/promote", methods=["POST"])
        def studio_session_promote(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                return ok(self.plugin.studio_promote(session_id, payload or {}))
            except Exception as exc:
                return fail(str(exc))

        @app.route("/api/studio/sessions/<session_id>/run", methods=["POST"])
        def studio_session_run(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                return ok(redact_sensitive_data(self.plugin.start_studio_run(session_id, payload or {})))
            except Exception as exc:
                return fail(str(exc), 500)

        @app.route("/api/studio/tasks/<task_id>", methods=["GET"])
        def studio_task_status(task_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            task_id_text = str(task_id or "").strip()
            if len(task_id_text) > MAX_WEB_TASK_ID_LENGTH or not WEB_TASK_ID_RE.fullmatch(task_id_text):
                return fail("非法任务 ID", 400)
            try:
                return ok(redact_sensitive_data(self.plugin.get_web_image_task(task_id_text)))
            except Exception as exc:
                return fail(str(exc), 404)

        return app
