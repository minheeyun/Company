/* VFIA 영향 그래프 탭: Cytoscape.js(파일에 내장)로 변경 영향·함수 의존·모듈 의존 그래프를 그린다. */
(function () {
  'use strict';
  var D = JSON.parse(document.getElementById('ig-data').textContent);
  var NODES = D.nodes || {};
  var CHG = {};
  D.changes.forEach(function (c) { CHG[c.id] = c; });
  var COST = D.step_cost || { call: 1, shared: 0.5, signal: 0.5, observe: 0.5, change: 0 };
  var KIND_LABEL = { change: '변경', function: '함수', external: '외부 함수(본문 없음)', variable: '변수',
    register: '레지스터(MMIO)', signal: '신호', observation: '관찰 대상(기능 출력)', module: '모듈', cluster: '묶음' };
  var EDGE_LABEL = { call: '호출·반환', shared: '공유 변수', signal: '신호', observe: '관찰', change: '변경 지점', module: '모듈 의존' };
  var STATUS_LABEL = { added: '추가(변경 후에만 있음)', removed: '삭제(변경 전에만 있음)', same: '전후 모두 있음',
    changed: '호출·공유 수가 바뀜' };
  var RISK_LABEL = { 2: '높음 — 동작 차이가 확인된 기능의 출력까지 가는 근거 경로 위',
    1: '보통 — 정적 영향 후보 기능의 출력까지 가는 근거 경로 위' };
  var CTRL_TEXT = { impact: '제어 조건(변경 영향이 조건문을 거쳐 전달)', program: '제어 조건(조건문 안의 호출·접근)',
    module: '제어 조건(조건부 호출 포함)' };
  var KIND_ORDER = { change: 0, function: 1, external: 2, variable: 3, register: 3, signal: 4, observation: 5, cluster: 6, module: 1 };

  var st = {
    type: 'impact', change: D.changes.length ? D.changes[0].id : null, steps: { impact: null, program: 2 },
    view: 'compare', pathOnly: false, vars: true, gran: 'file', layout: { impact: 'layered', program: 'layered', module: 'cose' },
    cluster: true, th: D.cluster_threshold || 150, expanded: {}, path: null, sel: null, minw: null
  };
  var cy = null, lastModel = null, inited = false;
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  };

  // ------------------------------------------------------------------ 탭·이동
  function showTab(name) {
    var g = name === 'graph';
    var wasHidden = $('tab-graph').hidden;
    $('tab-report').hidden = g;
    $('tab-graph').hidden = !g;
    document.body.classList.toggle('ig-wide', g);
    $('tabbtn-report').classList.toggle('on', !g);
    $('tabbtn-graph').classList.toggle('on', g);
    if (g) {
      if (wasHidden) { document.querySelector('.tabs').scrollIntoView({ block: 'start' }); }
      if (!inited) { init(); } else if (cy) { cy.resize(); }
    }
  }
  function flash(el) {
    el.classList.add('flash');
    setTimeout(function () { el.classList.remove('flash'); }, 1800);
  }
  function goCard(id) {
    if (location.hash === '#' + id) { route(); } else { location.hash = id; }
  }
  function route() {
    var h = decodeURIComponent(location.hash.replace(/^#/, ''));
    if (h === 'graph' || h.indexOf('graph:') === 0) {
      var parts = h.split(':');
      if (parts[1] && (CHG[parts[1]] || parts[1] === '*')) { st.change = parts[1]; st.path = null; st.sel = null; }
      if (parts[2] && /^(impact|program|module)$/.test(parts[2])) { st.type = parts[2]; }
      showTab('graph');
      if (inited) { syncControls(); render(true); }
      return;
    }
    showTab('report');
    if (h) {
      var el = document.getElementById(h);
      if (el) {
        var d = el.closest('details');
        while (d) { d.open = true; d = d.parentElement && d.parentElement.closest('details'); }
        el.scrollIntoView({ block: 'start' });
        flash(el);
      }
    }
  }
  window.addEventListener('hashchange', route);

  // ------------------------------------------------------------------ 색
  function cssVar(v, fb) {
    var x = getComputedStyle(document.documentElement).getPropertyValue(v).trim();
    return x || fb;
  }
  function palette() {
    var dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    return {
      fg: cssVar('--fg', '#1d1d1b'), card: cssVar('--card', '#fff'), bd: cssVar('--bd', '#ccc'), mut: cssVar('--mut', '#777'),
      edge: dark ? '#8d8c86' : '#9a9a95', ctrl: dark ? '#b48cff' : '#7b3fbf', add: dark ? '#5fd38a' : '#2e8b57',
      rem: dark ? '#ff7a70' : '#c62828', chgFill: dark ? '#5c2323' : '#ffd6d6', chg: dark ? '#ff8a80' : '#c62828',
      r2: dark ? '#ff9e4a' : '#e65100', r1: dark ? '#e8c35a' : '#f2a900', hl: dark ? '#64b5f6' : '#1565c0',
      cl: dark ? '#2b2b28' : '#efeeea', out: dark ? '#23303d' : '#e8f1fb'
    };
  }

  // ------------------------------------------------------------------ 모델 만들기
  function centersOf(cid) {
    if (cid === '*') {
      var s = {};
      D.changes.forEach(function (c) { c.centers.forEach(function (x) { s[x] = 1; }); });
      return Object.keys(s);
    }
    return (CHG[cid] || {}).centers || [];
  }
  function sideOk(side) {
    if (st.view === 'compare') return true;
    return st.view === 'after' ? (side & 2) : (side & 1);
  }
  function textWidth(s) {
    var w = 0;
    for (var i = 0; i < s.length; i++) { w += s.charCodeAt(i) > 0x2e80 ? 12 : 6.6; }
    return w;
  }
  function sizeOf(label) {
    var lines = String(label).split('\n');
    var w = 0;
    lines.forEach(function (l) { w = Math.max(w, textWidth(l)); });
    return { w: Math.max(36, Math.min(230, w + 18)), h: 16 + 14 * Math.min(4, Math.ceil(Math.max(1, w / 212)) * lines.length) };
  }
  function mkNode(id, extra) {
    var n;
    if (id.indexOf('chg:') === 0) {
      var c = CHG[id.slice(4)] || {};
      n = { id: id, label: c.id + '\n' + (c.type || ''), k: 'change', m: c.file || '(변경)', s: 'same', risk: 0, changed: true };
    } else {
      var i = NODES[id] || { k: 'function', l: id.split(':').pop() };
      n = { id: id, label: i.l, k: i.k, m: i.m || i.f || '(외부)', s: i.s || 'same', risk: 0, changed: false };
    }
    for (var k in extra) { if (Object.prototype.hasOwnProperty.call(extra, k)) n[k] = extra[k]; }
    return n;
  }
  function dijkstra(adj, sources, limit) {
    var dist = {}, pq = [];
    sources.forEach(function (s) { pq.push([0, s]); });
    while (pq.length) {
      var bi = 0;
      for (var i = 1; i < pq.length; i++) { if (pq[i][0] < pq[bi][0]) bi = i; }
      var cur = pq[bi]; pq[bi] = pq[pq.length - 1]; pq.pop();
      if (dist[cur[1]] !== undefined || cur[0] > limit + 1e-9) continue;
      dist[cur[1]] = cur[0];
      (adj[cur[1]] || []).forEach(function (x) { if (dist[x[0]] === undefined) pq.push([cur[0] + x[1], x[0]]); });
    }
    return dist;
  }

  function impactModel() {
    var cid = st.change === '*' || !CHG[st.change] ? (D.changes[0] || {}).id : st.change;
    var im = D.impact[cid];
    if (!im) return { nodes: [], edges: [], maxSteps: 1, steps: 1 };
    var maxD = 0;
    im.n.forEach(function (r) { if (r[1] != null) maxD = Math.max(maxD, r[1]); });
    var maxSteps = Math.max(1, Math.ceil(maxD));
    var steps = st.steps.impact == null ? maxSteps : Math.min(st.steps.impact, maxSteps);
    var centers = {};
    (CHG[cid].centers || []).forEach(function (x) { centers[x] = 1; });
    var nodes = [], ids = {};
    im.n.forEach(function (r) {
      var id = r[0], dist = r[1], onp = r[2], side = r[3], risk = r[4] || 0;
      if (!sideOk(side)) return;
      if (st.pathOnly && !onp && id.indexOf('chg:') !== 0) return;
      if (dist != null && dist > steps + 1e-9) return;
      var n = mkNode(id, { dist: dist == null ? maxD : dist, onp: onp, risk: onp ? risk : 0, changed: !!centers[id] || id.indexOf('chg:') === 0 });
      nodes.push(n); ids[id] = 1;
    });
    var edges = [];
    im.e.forEach(function (r) {
      if (!ids[r[0]] || !ids[r[1]] || !sideOk(r[5])) return;
      if (st.pathOnly && !r[4]) return;
      edges.push({ s: r[0], t: r[1], k: r[2], ctrl: r[3], onp: r[4], cnt: r[6], lab: r[7], status: r[8] });
    });
    return { kind: 'impact', cid: cid, nodes: nodes, edges: edges, maxSteps: maxSteps, steps: steps, paths: im.paths,
      note: im.truncated ? '노드가 많아 출력 경로 밖의 먼 노드 일부를 생략함(전체 ' + im.total_nodes + '개)' : '' };
  }

  function programModel() {
    var P = D.program, centers = centersOf(st.change);
    var maxSteps = Math.max(1, Math.floor(P.max_steps || 4));
    var steps = Math.min(st.steps.program || 2, maxSteps);
    var adj = {}, E = [];
    P.e.forEach(function (r) {
      if (!sideOk(r[4])) return;
      var e = { s: r[0], t: r[1], k: r[2], ctrl: r[3], side: r[4], cnt: r[5], lab: r[6], status: r[7] };
      E.push(e);
      var w = COST[e.k] == null ? 1 : COST[e.k];
      (adj[e.s] = adj[e.s] || []).push([e.t, w]);
      (adj[e.t] = adj[e.t] || []).push([e.s, w]);
    });
    var dist = dijkstra(adj, centers, steps);
    var cset = {};
    centers.forEach(function (x) { cset[x] = 1; });
    var nodes = [], ids = {};
    Object.keys(dist).forEach(function (id) {
      var k = (NODES[id] || {}).k;
      if (!st.vars && (k === 'variable' || k === 'register')) return;
      nodes.push(mkNode(id, { dist: dist[id], risk: D.risk[id] || 0, changed: !!cset[id] }));
      ids[id] = 1;
    });
    var edges = [];
    E.forEach(function (e) { if (ids[e.s] && ids[e.t]) edges.push(e); });
    if (!st.vars) {   // 함수 → 변수 → 함수를 함수 사이 점선 하나로
      var wr = {}, rd = {};
      E.forEach(function (e) {
        var ks = (NODES[e.s] || {}).k, kt = (NODES[e.t] || {}).k;
        if (e.k !== 'shared') return;
        if ((kt === 'variable' || kt === 'register') && ids[e.s]) (wr[e.t] = wr[e.t] || []).push(e);
        if ((ks === 'variable' || ks === 'register') && ids[e.t]) (rd[e.s] = rd[e.s] || []).push(e);
      });
      var agg = {};
      Object.keys(wr).forEach(function (v) {
        (rd[v] || []).forEach(function (r) {
          wr[v].forEach(function (w) {
            if (w.s === r.t) return;
            var key = w.s + '\u0001' + r.t;
            var a = agg[key] = agg[key] || { s: w.s, t: r.t, k: 'shared', ctrl: 0, cnt: 0, labs: {}, sts: {} };
            a.ctrl = a.ctrl || w.ctrl || r.ctrl; a.cnt += 1;
            a.labs[(NODES[v] || {}).l || v] = 1;
            a.sts[w.status === 'same' && r.status === 'same' ? 'same' : (w.status === 'removed' || r.status === 'removed' ? 'removed' : 'added')] = 1;
          });
        });
      });
      Object.keys(agg).forEach(function (key) {
        var a = agg[key], labs = Object.keys(a.labs);
        var status = a.sts.same ? 'same' : (a.sts.added && !a.sts.removed ? 'added' : (a.sts.removed && !a.sts.added ? 'removed' : 'same'));
        edges.push({ s: a.s, t: a.t, k: 'shared', ctrl: a.ctrl, cnt: a.cnt, lab: labs.slice(0, 3).join(', ') + (labs.length > 3 ? ' …' : ''), status: status });
      });
    }
    return { kind: 'program', nodes: nodes, edges: edges, maxSteps: maxSteps, steps: steps,
      note: P.truncated ? '함수 그래프는 변경 대상에서 가까운 ' + P.n.length + '개 노드만 담음(도달 ' + P.reachable + '개)' : '' };
  }

  function moduleModel() {
    var mg = D.modules[st.gran] || { n: [], e: [] };
    var nodes = [], ids = {};
    mg.n.forEach(function (r) {
      var m = r[0], fo = r[1], fn = r[2], changed = r[3], risk = r[4], status = r[5];
      if (st.view === 'after' && !fn && !changed) return;
      if (st.view === 'before' && !fo) return;
      var lab = m + '\n함수 ' + (st.view === 'before' ? fo : (st.view === 'after' || fo === fn ? fn : fo + '→' + fn));
      nodes.push({ id: 'mod:' + m, label: lab, k: 'module', m: m, s: status, risk: risk, changed: !!changed, fo: fo, fn: fn, dist: 0 });
      ids['mod:' + m] = 1;
    });
    var edges = [];
    mg.e.forEach(function (r) {
      var s = 'mod:' + r[0], t = 'mod:' + r[1];
      if (!ids[s] || !ids[t]) return;
      var co = r[2], cn = r[3], so = r[4], sn = r[5];
      if (st.view === 'after' && cn + sn === 0) return;
      if (st.view === 'before' && co + so === 0) return;
      var part = function (name, a, b) {
        if (st.view === 'before') return a ? name + ' ' + a : '';
        if (st.view === 'after' || a === b) return b ? name + ' ' + b : '';
        return name + ' ' + a + '→' + b;
      };
      var lab = [part('호출', co, cn), part('공유 변수', so, sn)].filter(Boolean).join(' · ');
      var calls = st.view === 'before' ? co : Math.max(cn, st.view === 'compare' ? co : 0);
      edges.push({ s: s, t: t, k: calls > 0 ? 'call' : 'shared', ctrl: r[6] > 0 ? 1 : 0, cnt: Math.max(co + so, cn + sn), lab: lab,
        status: r[7], vars: r[8], co: co, cn: cn, so: so, sn: sn });
    });
    // 연결이 너무 많으면 약한 연결(호출+공유 변수 수가 기준 미만)을 숨긴다(기준은 화면에서 조절)
    var minw = st.minw;
    if (minw == null) {
      minw = 1;
      var ws = edges.map(function (e) { return e.cnt; }).sort(function (a, b) { return b - a; });
      if (ws.length > 300) minw = Math.max(1, ws[299]);
    }
    var allE = edges.length;
    edges = edges.filter(function (e) { return e.cnt >= minw; });
    var hiddenE = allE - edges.length;
    // 파일 단위일 때 폴더를 상자로 묶어 배치
    var folders = {};
    if (st.gran === 'file') {
      nodes.forEach(function (n) {
        var f = n.m.indexOf('/') >= 0 ? n.m.slice(0, n.m.lastIndexOf('/')) : '.';
        n.parent = 'fold:' + f; folders[f] = (folders[f] || 0) + 1;
      });
      if (Object.keys(folders).length < 2) nodes.forEach(function (n) { delete n.parent; });
    }
    // 단계별 배치용 거리: 변경이 있는 모듈에서
    var adj = {};
    edges.forEach(function (e) { (adj[e.s] = adj[e.s] || []).push([e.t, 1]); (adj[e.t] = adj[e.t] || []).push([e.s, 1]); });
    var dist = dijkstra(adj, nodes.filter(function (n) { return n.changed; }).map(function (n) { return n.id; }), 1e9);
    nodes.forEach(function (n) { n.dist = dist[n.id] == null ? 0 : dist[n.id]; });
    return { kind: 'module', nodes: nodes, edges: edges, maxSteps: 1, steps: 1, minw: minw,
      folders: Object.keys(folders).length >= 2 ? Object.keys(folders) : [],
      note: hiddenE ? '약한 연결 ' + hiddenE + '개 숨김(연결 수 ' + minw + ' 미만, 기준은 \'최소 연결 수\'에서 조절)' : '' };
  }

  // ------------------------------------------------------------------ 묶음(파일별)
  function pathSet() {
    var s = {};
    if (st.path && st.type !== 'module') {
      var p = findPath(st.path);
      if (p) p.nodes.forEach(function (x) { s[x] = 1; });
    }
    return s;
  }
  function clusterModel(model) {
    model.clusters = 0; model.hidden = 0;
    if (model.kind === 'module' || !st.cluster || model.nodes.length <= st.th) return model;
    var ps = pathSet();
    // 1순위(항상 따로): 변경·변경 대상·출력·강조한 경로·펼친 묶음
    var tierA = function (n) {
      return n.k === 'change' || n.changed || n.k === 'observation' || n.k === 'signal' || ps[n.id] || st.expanded[n.m];
    };
    // 2순위: 출력까지 가는 경로 위(위험 후보), 3순위: 변경 바로 옆(1단계). 기준 개수 안에 들어갈 때만 따로 표시
    var tierB = function (n) { return (model.kind === 'impact' && n.onp) || n.risk > 0; };
    var tierC = function (n) { return n.dist != null && n.dist <= 1; };
    var cntA = 0, cntB = 0, cntC = 0;
    model.nodes.forEach(function (n) {
      if (tierA(n)) cntA++; else if (tierB(n)) cntB++; else if (tierC(n)) cntC++;
    });
    var useB = cntA + cntB <= st.th, useC = useB && cntA + cntB + cntC <= st.th;
    var essential = function (n) { return tierA(n) || (useB && tierB(n)) || (useC && tierC(n)); };
    model.clusterNote = useB ? '' : '출력 경로 위 노드도 많아 파일별로 묶음(경로를 강조하면 그 경로의 노드는 따로 표시)';
    var groups = {}, keep = [];
    model.nodes.forEach(function (n) {
      if (essential(n)) { keep.push(n); return; }
      (groups[n.m] = groups[n.m] || []).push(n);
    });
    var map = {};
    Object.keys(groups).forEach(function (m) {
      var g = groups[m];
      if (g.length < 2) { keep.push(g[0]); return; }
      var cid = 'cl:' + m, dmin = Infinity, kinds = {};
      g.forEach(function (n) { map[n.id] = cid; dmin = Math.min(dmin, n.dist == null ? 0 : n.dist); kinds[n.k] = (kinds[n.k] || 0) + 1; });
      var desc = Object.keys(kinds).map(function (k) { return (KIND_LABEL[k] || k) + ' ' + kinds[k]; }).join(', ');
      var crisk = 0;
      g.forEach(function (n) { crisk = Math.max(crisk, n.risk || 0); });
      keep.push({ id: cid, label: m.split('/').slice(-2).join('/') + '\n' + g.length + '개 묶음', k: 'cluster', m: m, s: 'same', risk: crisk,
        changed: false, dist: dmin, members: g.map(function (n) { return n.id; }), desc: desc });
      model.clusters += 1; model.hidden += g.length;
    });
    var out = [], agg = {};
    model.edges.forEach(function (e) {
      var s = map[e.s] || e.s, t = map[e.t] || e.t;
      if (s === t) return;
      if (s === e.s && t === e.t) { out.push(e); return; }
      var key = s + '\u0001' + t + '\u0001' + e.k;
      var a = agg[key];
      if (!a) { a = agg[key] = { s: s, t: t, k: e.k, ctrl: 0, onp: 0, cnt: 0, lab: '', sts: {} }; out.push(a); }
      a.ctrl = a.ctrl || e.ctrl; a.onp = a.onp || e.onp; a.cnt += e.cnt || 1; a.sts[e.status || 'same'] = 1;
    });
    out.forEach(function (a) {
      if (!a.sts) return;
      var ks = Object.keys(a.sts);
      a.status = ks.length === 1 ? ks[0] : 'same';
      a.lab = a.cnt + '개 연결';
      delete a.sts;
    });
    model.edges = out;
    model.nodes = keep;
    return model;
  }

  // ------------------------------------------------------------------ 배치
  function layeredPositions(nodes, edges, sizes) {
    var cols = {}, colKeys = [];
    nodes.forEach(function (n) {
      var c = Math.round((n.dist || 0) * 2);
      if (!cols[c]) { cols[c] = []; colKeys.push(c); }
      cols[c].push(n);
    });
    colKeys.sort(function (a, b) { return a - b; });
    var nb = {};
    edges.forEach(function (e) { (nb[e.s] = nb[e.s] || []).push(e.t); (nb[e.t] = nb[e.t] || []).push(e.s); });
    colKeys.forEach(function (c) {
      cols[c].sort(function (a, b) {
        return (KIND_ORDER[a.k] || 9) - (KIND_ORDER[b.k] || 9) || String(a.label).localeCompare(String(b.label));
      });
    });
    var idx = {};
    var setIdx = function (c) { cols[c].forEach(function (n, i) { idx[n.id] = i / Math.max(1, cols[c].length - 1); }); };
    colKeys.forEach(setIdx);
    for (var pass = 0; pass < 4; pass++) {      // 무게중심법으로 교차 줄이기(앞→뒤, 뒤→앞)
      var order = pass % 2 === 0 ? colKeys : colKeys.slice().reverse();
      order.forEach(function (c) {
        var bc = {};
        cols[c].forEach(function (n) {
          var xs = (nb[n.id] || []).filter(function (m) { return idx[m] !== undefined; }).map(function (m) { return idx[m]; });
          bc[n.id] = xs.length ? xs.reduce(function (a, b) { return a + b; }, 0) / xs.length : idx[n.id];
        });
        cols[c].sort(function (a, b) { return bc[a.id] - bc[b.id]; });
        setIdx(c);
      });
    }
    // 열 안에서는 노드 높이만큼 쌓고(겹치지 않게), 한 열이 너무 길면 옆으로 나눠 놓는다
    var pos = {}, x = 0, GAP = 22, PER = 22;
    colKeys.forEach(function (c) {
      var list = cols[c], sub = Math.ceil(list.length / PER), maxW = 0;
      list.forEach(function (n) { maxW = Math.max(maxW, (sizes[n.id] || {}).w || 120); });
      var colW = maxW + 48;
      for (var k = 0; k < sub; k++) {
        var part = list.slice(k * PER, (k + 1) * PER), total = 0;
        part.forEach(function (n) { total += ((sizes[n.id] || {}).hh || 40) + GAP; });
        var y = -total / 2;
        part.forEach(function (n) {
          var hh = (sizes[n.id] || {}).hh || 40;
          pos[n.id] = { x: x + k * colW * 0.75, y: y + hh / 2 };
          y += hh + GAP;
        });
      }
      x += colW + (sub - 1) * colW * 0.75;
    });
    return pos;
  }

  // ------------------------------------------------------------------ 그리기
  function styleSheet(P, big) {
    return [
      { selector: 'node', style: {
        'label': 'data(label)', 'text-wrap': 'wrap', 'text-max-width': 210, 'font-size': 11, 'color': P.fg,
        'text-valign': 'center', 'text-halign': 'center', 'background-color': P.card, 'border-width': 1.2,
        'border-color': P.mut, 'shape': 'round-rectangle', 'width': 'data(w)', 'height': 'data(h)',
        'min-zoomed-font-size': big ? 6 : 4, 'font-family': '-apple-system,"Segoe UI","Malgun Gothic","Apple SD Gothic Neo",sans-serif' } },
      { selector: 'node[k = "variable"]', style: { 'shape': 'ellipse' } },
      { selector: 'node[k = "register"]', style: { 'shape': 'hexagon' } },
      { selector: 'node[k = "signal"]', style: { 'shape': 'tag', 'background-color': P.out } },
      { selector: 'node[k = "observation"]', style: { 'shape': 'diamond', 'background-color': P.out, 'height': 'data(hd)', 'width': 'data(wd)' } },
      { selector: 'node[k = "external"]', style: { 'shape': 'cut-rectangle', 'background-color': P.out } },
      { selector: 'node[k = "cluster"]', style: { 'background-color': P.cl, 'border-style': 'dashed', 'border-width': 1.5, 'color': P.mut } },
      { selector: 'node[k = "module"]', style: { 'width': 'data(w)', 'height': 'data(h)' } },
      { selector: 'node[s = "added"]', style: { 'border-style': 'dashed', 'border-color': P.add, 'border-width': 2.5 } },
      { selector: 'node[s = "removed"]', style: { 'border-style': 'dashed', 'border-color': P.rem, 'border-width': 2.5, 'opacity': 0.75 } },
      { selector: 'node[risk = 1]', style: { 'border-color': P.r1, 'border-width': 3.5 } },
      { selector: 'node[risk = 2]', style: { 'border-color': P.r2, 'border-width': 4.5 } },
      { selector: 'node[changed = 1]', style: { 'background-color': P.chgFill, 'border-color': P.chg, 'border-width': 3.5 } },
      { selector: 'node[k = "change"]', style: { 'background-color': P.chgFill, 'border-color': P.chg, 'border-width': 4, 'font-weight': 'bold', 'shape': 'round-octagon' } },
      { selector: 'edge', style: {
        'width': 1.6, 'line-color': P.edge, 'target-arrow-color': P.edge, 'target-arrow-shape': 'triangle', 'arrow-scale': 0.9,
        'curve-style': big ? 'straight' : 'bezier', 'label': big ? '' : 'data(label)', 'font-size': 9, 'color': P.mut,
        'text-background-color': P.card, 'text-background-opacity': 0.85, 'text-background-padding': 1,
        'min-zoomed-font-size': 9, 'text-rotation': 'autorotate' } },
      { selector: 'edge[k = "shared"], edge[k = "signal"]', style: { 'line-style': 'dashed', 'line-dash-pattern': [6, 4] } },
      { selector: 'edge[k = "observe"]', style: { 'line-style': 'dotted' } },
      { selector: 'edge[onp = 1]', style: { 'width': 2.4 } },
      { selector: 'edge[ctrl = 1]', style: { 'line-color': P.ctrl, 'target-arrow-color': P.ctrl, 'width': 2.8 } },
      { selector: 'edge[k = "change"]', style: { 'line-color': P.chg, 'target-arrow-color': P.chg, 'width': 3 } },
      { selector: 'edge[mod = 1]', style: { 'width': 'data(mw)' } },
      { selector: 'edge[status = "added"]', style: { 'line-color': P.add, 'target-arrow-color': P.add, 'width': 3 } },
      { selector: 'edge[status = "changed"]', style: { 'line-color': P.add, 'target-arrow-color': P.add, 'line-opacity': 0.7 } },
      { selector: 'edge[status = "removed"]', style: { 'line-color': P.rem, 'target-arrow-color': P.rem, 'width': 3 } },
      { selector: 'node[k = "folder"]', style: { 'shape': 'round-rectangle', 'background-color': P.cl, 'background-opacity': 0.45,
        'border-style': 'dashed', 'border-color': P.bd, 'text-valign': 'top', 'text-halign': 'center', 'font-size': 12,
        'font-weight': 'bold', 'color': P.mut, 'padding': 14 } },
      { selector: '.faded', style: { 'opacity': 0.12 } },
      { selector: '.dim', style: { 'opacity': 0.18 } },
      { selector: 'node.hl', style: { 'border-color': P.hl, 'border-width': 5, 'opacity': 1 } },
      { selector: 'edge.hl', style: { 'line-color': P.hl, 'target-arrow-color': P.hl, 'width': 4, 'opacity': 1, 'z-index': 10 } },
      { selector: ':selected', style: { 'overlay-color': P.hl, 'overlay-opacity': 0.18, 'overlay-padding': 6 } }
    ];
  }

  function buildModel() {
    var m = st.type === 'impact' ? impactModel() : (st.type === 'program' ? programModel() : moduleModel());
    return clusterModel(m);
  }

  function render(fit) {
    if (!inited) return;
    var t0 = performance.now();
    var model = buildModel();
    lastModel = model;
    var P = palette();
    var big = model.nodes.length > 300 || model.edges.length > 700;
    var maxCnt = 1;
    model.edges.forEach(function (e) { maxCnt = Math.max(maxCnt, e.cnt || 1); });
    var els = [];
    model.nodes.forEach(function (n) {
      var sz = sizeOf(n.label);
      if (n.k === 'module') {
        var f = Math.max(n.fn || 0, n.fo || 0);
        sz = { w: Math.max(sz.w, 40 + Math.sqrt(f) * 14), h: Math.max(sz.h, 30 + Math.sqrt(f) * 6) };
      }
      els.push({ group: 'nodes', data: { id: n.id, label: n.label, k: n.k, risk: n.risk || 0, changed: n.changed ? 1 : 0,
        s: st.view === 'compare' ? (n.s || 'same') : 'same', dist: n.dist, m: n.m, w: sz.w, h: sz.h, wd: sz.w + 26, hd: sz.h + 18 } });
    });
    (model.folders || []).forEach(function (f) {
      els.push({ group: 'nodes', data: { id: 'fold:' + f, label: f + '/', k: 'folder', m: f, w: 10, h: 10, risk: 0, changed: 0, s: 'same' } });
    });
    model.nodes.forEach(function (n, i) { if (n.parent) els[i].data.parent = n.parent; });
    model.edges.forEach(function (e, i) {
      els.push({ group: 'edges', data: { id: 'e' + i, source: e.s, target: e.t, k: e.k, mod: model.kind === 'module' ? 1 : 0,
        ek: model.kind === 'module' ? 'module' : e.k, ctrl: e.ctrl ? 1 : 0, onp: e.onp ? 1 : 0, label: e.lab || '', cnt: e.cnt || 1,
        status: st.view === 'compare' ? (e.status || 'same') : 'same',
        mw: 1.2 + 6 * Math.sqrt((e.cnt || 1) / maxCnt) } });
    });
    var vp = cy && fit === false ? { zoom: cy.zoom(), pan: cy.pan() } : null;
    if (cy) { cy.destroy(); }
    var container = $('ig-cy');
    var empty = container.querySelector('.ig-empty');
    if (empty) empty.remove();
    cy = cytoscape({
      container: container, elements: els, style: styleSheet(P, big), minZoom: 0.03, maxZoom: 4,
      textureOnViewport: big, hideEdgesOnViewport: model.edges.length > 1200, motionBlur: false, pixelRatio: big ? 1 : 'auto',
      boxSelectionEnabled: false, layout: { name: 'preset' }
    });
    var lay = st.layout[st.type];
    if (lay === 'cose' && model.nodes.length > 400) { lay = 'layered'; }
    if (lay === 'layered') {
      var sizes = {};
      els.forEach(function (x) {
        if (x.group === 'nodes') {
          var dia = x.data.k === 'observation';
          sizes[x.data.id] = { w: dia ? x.data.wd : x.data.w, hh: dia ? x.data.hd : x.data.h };
        }
      });
      var pos = layeredPositions(model.nodes, model.edges, sizes);
      cy.layout({ name: 'preset', positions: function (n) { return pos[n.id()]; }, fit: fit !== false, padding: 30 }).run();
    } else if (lay === 'concentric') {
      var maxD = 0;
      model.nodes.forEach(function (n) { maxD = Math.max(maxD, n.dist || 0); });
      cy.layout({ name: 'concentric', concentric: function (n) { return 2 * maxD - Math.round((n.data('dist') || 0) * 2); },
        levelWidth: function () { return 1; }, minNodeSpacing: 12, animate: false, fit: fit !== false, padding: 30 }).run();
    } else {
      var dense = model.edges.length > model.nodes.length * 4;
      cy.layout({ name: 'cose', animate: false, randomize: false, nodeDimensionsIncludeLabels: true,
        nodeRepulsion: function () { return dense ? 300000 : 1000000; }, idealEdgeLength: function () { return dense ? 70 : 180; },
        edgeElasticity: function () { return dense ? 200 : 60; }, nodeOverlap: 80, componentSpacing: 140,
        gravity: dense ? 1.2 : 0.3, nestingFactor: 1.2,
        numIter: model.nodes.length > 200 ? 600 : 2000, fit: fit !== false, padding: 30 }).run();
    }
    if (!model.nodes.length) {
      var d = document.createElement('div');
      d.className = 'ig-empty';
      d.textContent = st.type === 'impact' ? '이 변경은 표시할 영향 요소가 없음(전방 슬라이스가 비었거나 보기 조건에 맞는 요소 없음)' : '표시할 노드 없음';
      container.appendChild(d);
    }
    if (fit !== false && model.kind !== 'module') { readableZoom(model); }
    if (vp) { cy.viewport(vp); }
    bindEvents();
    applyPath(false);
    var ms = Math.round(performance.now() - t0);
    $('ig-status').textContent = '노드 ' + model.nodes.length + '개 · 연결 ' + model.edges.length + '개' +
      (model.clusters ? ' · 묶음 ' + model.clusters + '개(노드 ' + model.hidden + '개 포함, 누르면 펼침)' : '') +
      (model.note ? ' · ' + model.note : '') + (model.clusterNote ? ' · ' + model.clusterNote : '') + ' · 그리기 ' + ms + 'ms' +
      (cy.zoom() <= 0.62 + 1e-6 && model.kind !== 'module' ? ' · 변경 지점부터 표시(전체는 \'맞춤\')' : '');
    $('ig-ctrl-text').textContent = CTRL_TEXT[st.type];
    if (model.kind === 'module') $('ig-minw').value = model.minw;
    syncSteps(model);
    if (st.sel && cy.getElementById(st.sel).length) { showNode(cy.getElementById(st.sel)); } else { st.sel = null; showOverview(); }
  }

  // 전체 맞춤 배율이 너무 작으면 글자가 읽히는 배율로 두고 변경 지점(시작점)부터 보여 준다
  function readableZoom(model) {
    var MIN = 0.62;
    if (cy.zoom() >= MIN || !cy.nodes().length) return;
    var start = cy.nodes('[k = "change"]');
    if (!start.length) start = cy.nodes('[changed = 1]');
    if (!start.length) return;
    var bb = start.boundingBox(), c = $('ig-cy');
    cy.zoom(MIN);
    cy.pan({ x: 40 - bb.x1 * MIN, y: c.clientHeight / 2 - ((bb.y1 + bb.y2) / 2) * MIN });
    $('ig-status').dataset.hint = '1';
  }

  function bindEvents() {
    cy.on('tap', 'node', function (ev) {
      var n = ev.target;
      if (n.data('k') === 'cluster') {
        st.expanded[n.data('m')] = true;
        st.sel = null;
        render(false);
        return;
      }
      st.sel = n.id();
      focus(n);
      showNode(n);
    });
    cy.on('dbltap', 'node', function (ev) { navigateFrom(ev.target); });
    cy.on('tap', 'edge', function (ev) { showEdge(ev.target); });
    cy.on('tap', function (ev) { if (ev.target === cy) { st.sel = null; cy.elements().removeClass('dim'); showOverview(); } });
  }
  // 선택한 노드와 이웃만 진하게(경로 강조 중이면 그대로 둠)
  function focus(n) {
    cy.elements().removeClass('dim');
    if (st.path) return;
    var keep = n.closedNeighborhood().union(n.ancestors()).union(n.closedNeighborhood().ancestors());
    cy.elements().not(keep).addClass('dim');
  }

  // ------------------------------------------------------------------ 경로 강조
  function allPaths() {
    var out = [];
    D.changes.forEach(function (c) {
      ((D.impact[c.id] || {}).paths || []).forEach(function (p, i) { out.push({ cid: c.id, i: i, p: p }); });
    });
    return out;
  }
  function findPath(ref) {
    var im = D.impact[ref.cid];
    return im && im.paths ? im.paths[ref.i] : null;
  }
  function applyPath(fit) {
    if (!cy) return;
    cy.elements().removeClass('faded hl');
    if (!st.path) return;
    var p = findPath(st.path);
    if (!p) return;
    var owner = {};
    cy.nodes().forEach(function (n) {
      owner[n.id()] = n.id();
      (n.data('k') === 'cluster' ? (lastModel.nodes.filter(function (x) { return x.id === n.id(); })[0] || {}).members || [] : [])
        .forEach(function (m) { owner[m] = n.id(); });
    });
    var seq = p.nodes.map(function (x) { return owner[x]; }).filter(Boolean);
    var hl = cy.collection();
    seq.forEach(function (id, i) {
      hl = hl.union(cy.getElementById(id));
      if (i > 0 && seq[i - 1] !== id) {
        hl = hl.union(cy.edges().filter(function (e) {
          return (e.source().id() === seq[i - 1] && e.target().id() === id) || (e.source().id() === id && e.target().id() === seq[i - 1]);
        }));
      }
    });
    if (!hl.length) return;
    cy.elements().addClass('faded');
    hl.removeClass('faded').addClass('hl');
    if (fit) {
      cy.fit(hl, 60);
      if (cy.zoom() > 1.1) { cy.zoom({ level: 1.1, position: hl.boundingBox ? { x: (hl.boundingBox().x1 + hl.boundingBox().x2) / 2, y: (hl.boundingBox().y1 + hl.boundingBox().y2) / 2 } : undefined }); }
    }
  }

  // ------------------------------------------------------------------ 오른쪽 패널
  function badgeOf(status) { return '<span class="badge">' + esc(status) + '</span>'; }
  function pathItem(ref, idx) {
    var p = ref.p;
    var on = st.path && st.path.cid === ref.cid && st.path.i === ref.i;
    var names = p.nodes.map(function (x) {
      return x.indexOf('chg:') === 0 ? x.slice(4) : ((NODES[x] || {}).l || x.split(':').pop());
    });
    return '<li class="ig-path' + (on ? ' on' : '') + '" data-cid="' + esc(ref.cid) + '" data-i="' + ref.i + '">' +
      '<b>' + esc(ref.cid) + ' → ' + esc(p.feature) + '</b> <span class="mut">(' + (p.side === 'old' ? '변경 전' : '변경 후') +
      ', ' + esc(p.status) + ')</span><br><span style="font-size:12px">' + esc(names.join(' → ')) + '</span><br>' +
      '<button data-act="hl">경로 강조</button> <button data-act="card" data-card="' + esc(p.card) + '">근거 상세로 이동</button></li>';
  }
  function bindSide() {
    var side = $('ig-side');
    Array.prototype.forEach.call(side.querySelectorAll('[data-act]'), function (b) {
      b.addEventListener('click', function (ev) {
        ev.stopPropagation();
        var act = b.getAttribute('data-act');
        if (act === 'card') { goCard(b.getAttribute('data-card')); return; }
        if (act === 'hl') {
          var li = b.closest('.ig-path');
          var ref = { cid: li.getAttribute('data-cid'), i: +li.getAttribute('data-i') };
          if (st.type === 'impact' && ref.cid !== st.change) { st.change = ref.cid; syncControls(); }
          st.path = (st.path && st.path.cid === ref.cid && st.path.i === ref.i) ? null : ref;
          if (lastModel && lastModel.clusters) { render(false); }
          applyPath(true);
          if (st.sel && cy.getElementById(st.sel).length) showNode(cy.getElementById(st.sel)); else showOverview();
          return;
        }
        if (act === 'graph') { st.change = b.getAttribute('data-cid'); st.type = 'impact'; st.path = null; syncControls(); render(true); return; }
        if (act === 'expand') { st.expanded[b.getAttribute('data-m')] = true; st.sel = null; render(false); return; }
        if (act === 'center') { st.change = b.getAttribute('data-cid'); st.type = 'program'; syncControls(); render(true); }
      });
    });
  }
  function showOverview() {
    var h = '';
    if (st.type === 'module') {
      h = '<h4>모듈 의존 그래프</h4><p class="mut">노드 크기 = 함수 수, 선 굵기 = 호출 수 + 공유 변수 수. 호출이 있으면 실선, 공유 변수만 있으면 점선. ' +
        '변경이 있는 모듈은 빨강, 위험 후보 함수가 있는 모듈은 주황 테두리.</p>';
    } else {
      var cid = st.change === '*' ? null : st.change;
      if (cid && CHG[cid]) {
        var c = CHG[cid];
        h += '<h4>' + esc(c.id) + ' · ' + esc(c.type) + '</h4><div>' + esc(c.summary) + '</div><div class="mut">' + esc(c.file || '') +
          (c.line ? ':' + c.line : '') + '</div><div style="margin:6px 0"><button data-act="card" data-card="' + esc(c.id) +
          '">변경 카드로 이동</button></div>';
        if (c.features.length) {
          h += '<div>기능 판정: ' + c.features.map(function (f) { return esc(f.feature) + ' ' + badgeOf(f.status); }).join(' ') + '</div>';
        }
      } else {
        h += '<h4>전체 변경</h4><p class="mut">모든 변경 대상(빨강)에서 N단계 안의 함수·변수·신호.</p>';
      }
      var refs = allPaths().filter(function (r) { return !cid || r.cid === cid; });
      h += '<h4 style="margin-top:12px">근거 경로(' + refs.length + ')</h4>';
      h += refs.length ? '<ul style="list-style:none;padding:0">' + refs.slice(0, 60).map(pathItem).join('') + '</ul>'
        : '<p class="mut">기능 출력(관찰 대상)까지 가는 근거 경로가 없음.</p>';
    }
    $('ig-side').innerHTML = h;
    bindSide();
  }
  function relatedChanges(id) {
    return D.changes.filter(function (c) {
      return (D.impact[c.id] || { n: [] }).n.some(function (r) { return r[0] === id; });
    }).map(function (c) { return c.id; });
  }
  function showNode(n) {
    var id = n.id(), k = n.data('k'), h = '';
    if (k === 'change') {
      var c = CHG[id.slice(4)];
      h = '<h4>' + esc(c.id) + ' · ' + esc(c.type) + '</h4><div>' + esc(c.summary) + '</div>' +
        '<div class="mut">' + esc(c.unit || '') + ' · ' + esc(c.file || '') + (c.line ? ':' + c.line : '') + '</div>' +
        (c.code_old ? '<div class="mut">변경 전</div><pre>' + esc(c.code_old) + '</pre>' : '') +
        (c.code_new ? '<div class="mut">변경 후</div><pre>' + esc(c.code_new) + '</pre>' : '') +
        '<button data-act="card" data-card="' + esc(c.id) + '">변경 카드로 이동</button> ' +
        '<button data-act="center" data-cid="' + esc(c.id) + '">함수 의존 그래프에서 보기</button>' +
        (c.features.length ? '<h4 style="margin-top:10px">기능 판정</h4><ul>' + c.features.map(function (f) {
          return '<li>' + esc(f.feature) + ' ' + badgeOf(f.status) + ' <button data-act="card" data-card="' + esc(c.id + '-' + f.feature) + '">근거 상세</button></li>';
        }).join('') + '</ul>' : '<p class="mut">기능 출력까지 가는 경로 없음</p>');
    } else if (k === 'cluster') {
      var mem = (lastModel.nodes.filter(function (x) { return x.id === id; })[0] || {}).members || [];
      h = '<h4>묶음: ' + esc(n.data('m')) + '</h4><div class="mut">' + mem.length + '개 노드(핵심 노드가 아닌 요소를 파일별로 묶음)</div>' +
        '<button data-act="expand" data-m="' + esc(n.data('m')) + '">펼치기</button><ul>' + mem.slice(0, 40).map(function (x) {
          return '<li>' + esc((NODES[x] || {}).l || x) + ' <span class="mut">' + esc(KIND_LABEL[(NODES[x] || {}).k] || '') + '</span></li>';
        }).join('') + (mem.length > 40 ? '<li>…</li>' : '') + '</ul>';
    } else if (k === 'module') {
      var m = n.data('m');
      var mm = (lastModel.nodes.filter(function (x) { return x.id === id; })[0] || {});
      var ins = n.incomers('edge'), outs = n.outgoers('edge');
      h = '<h4>모듈 ' + esc(m) + '</h4><div class="kv"><div>함수(전 → 후)</div><div>' + mm.fo + ' → ' + mm.fn + '</div>' +
        '<div>변경 포함</div><div>' + (mm.changed ? '예' : '아니오') + '</div><div>위험 후보</div><div>' + (RISK_LABEL[mm.risk] || '없음') + '</div>' +
        '<div>나가는 의존</div><div>' + outs.length + '</div><div>들어오는 의존</div><div>' + ins.length + '</div></div>';
      var list = outs.union(ins).map(function (e) { return e; }).slice(0, 30);
      if (list.length) {
        h += '<ul>' + list.map(function (e) {
          return '<li>' + esc(e.source().data('m')) + ' → ' + esc(e.target().data('m')) + ': ' + esc(e.data('label')) + '</li>';
        }).join('') + '</ul>';
      }
    } else {
      var i = NODES[id] || {};
      h = '<h4>' + esc(i.l || id) + '</h4><div class="mut">' + esc(KIND_LABEL[i.k] || i.k) + (i.f ? ' · ' + esc(i.f) + (i.ln ? ':' + i.ln : '') : '') + '</div>';
      h += '<div class="kv" style="margin-top:6px">';
      if (st.view === 'compare' && i.s && i.s !== 'same') h += '<div>전후 비교</div><div>' + esc(STATUS_LABEL[i.s]) + '</div>';
      var r = n.data('risk');
      if (r) h += '<div>위험 후보</div><div>' + esc(RISK_LABEL[r]) + '</div>';
      if (n.data('changed')) h += '<div>변경</div><div>변경이 직접 일어난 요소</div>';
      if (n.data('dist') != null) h += '<div>단계</div><div>' + n.data('dist') + '</div>';
      Object.keys(i.x || {}).forEach(function (kk) { h += '<div>' + esc(kk) + '</div><div>' + esc(i.x[kk]) + '</div>'; });
      h += '</div>';
      var refs = allPaths().filter(function (r) { return r.p.nodes.indexOf(id) >= 0; });
      var cur = st.change;
      refs.sort(function (a, b) { return (a.cid === cur ? 0 : 1) - (b.cid === cur ? 0 : 1); });
      var nCur = refs.filter(function (r) { return r.cid === cur; }).length;
      h += '<h4 style="margin-top:10px">이 요소를 지나는 근거 경로(' + refs.length + ')</h4>' +
        (cur && cur !== '*' && refs.length > nCur ? '<div class="mut">' + esc(cur) + ' 경로 ' + nCur + '개를 먼저, 다른 변경의 경로 ' + (refs.length - nCur) + '개를 뒤에 표시</div>' : '');
      h += refs.length ? '<ul style="list-style:none;padding:0">' + refs.slice(0, 30).map(pathItem).join('') + '</ul>'
        : '<p class="mut">기능 출력까지 가는 근거 경로에는 없음(영향 범위에만 포함되었거나 주변 요소)</p>';
      var rel = relatedChanges(id);
      if (rel.length) {
        h += '<h4>이 요소에 영향이 닿는 변경</h4><div>' + rel.map(function (cid) {
          return '<button data-act="graph" data-cid="' + esc(cid) + '">' + esc(cid) + ' 영향 그래프</button> ' +
            '<button data-act="card" data-card="' + esc(cid) + '">' + esc(cid) + ' 카드</button>';
        }).join('<br>') + '</div>';
      }
    }
    $('ig-side').innerHTML = h;
    bindSide();
  }
  function showEdge(e) {
    var d = e.data();
    var h = '<h4>' + esc(EDGE_LABEL[d.ek] || d.k) + '</h4><div>' + esc(nameOf(e.source())) + ' → ' + esc(nameOf(e.target())) + '</div>' +
      '<div class="kv" style="margin-top:6px"><div>설명</div><div>' + esc(d.label || '-') + '</div><div>근거 수</div><div>' + d.cnt + '</div>' +
      '<div>제어 조건</div><div>' + (d.ctrl ? '예 — ' + esc(CTRL_TEXT[st.type]) : '아니오') + '</div>' +
      (st.view === 'compare' ? '<div>전후 비교</div><div>' + esc(STATUS_LABEL[d.status] || d.status) + '</div>' : '') + '</div>';
    if (st.type === 'module') {
      var m = (lastModel.edges[+d.id.slice(1)] || {});
      if (m.vars) h += '<div class="mut" style="margin-top:6px">공유 변수: ' + esc(m.vars) + '</div>';
    }
    $('ig-side').innerHTML = h;
  }
  function nameOf(n) {
    if (n.data('k') === 'change') return n.id().slice(4);
    if (n.data('k') === 'module' || n.data('k') === 'cluster') return n.data('m');
    return (NODES[n.id()] || {}).l || n.id();
  }
  function navigateFrom(n) {
    var id = n.id(), k = n.data('k');
    if (k === 'change') { goCard(id.slice(4)); return; }
    if (k === 'cluster') return;
    var refs = allPaths().filter(function (r) {
      return r.p.nodes.indexOf(id) >= 0 && (st.type !== 'impact' || r.cid === st.change);
    });
    if (refs.length) { goCard(refs[0].p.card); return; }
    var rel = relatedChanges(id);
    if (rel.length) goCard(rel[0]);
  }

  // ------------------------------------------------------------------ 내보내기(지금 보이는 그래프)
  function dq(s) { return '"' + String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\n/g, '\\n') + '"'; }
  function exportName(ext) {
    var parts = ['vfia', st.type];
    if (st.type !== 'module') parts.push(st.change === '*' ? 'all' : st.change);
    if (st.type === 'module') parts.push(st.gran);
    if (st.type !== 'module' && lastModel) parts.push(lastModel.steps + 'step');
    parts.push(st.view);
    return parts.join('_') + '.' + ext;
  }
  function toDot() {
    var P = palette(), L = ['digraph ' + dq(exportName('dot').replace(/\.dot$/, '')) + ' {',
      '  graph [rankdir=LR, overlap=false, fontname="Malgun Gothic"];', '  node [fontname="Malgun Gothic", fontsize=10, style=filled, fillcolor="#ffffff"];',
      '  edge [fontname="Malgun Gothic", fontsize=9];'];
    var shapes = { change: 'octagon', function: 'box', external: 'box', variable: 'ellipse', register: 'hexagon', signal: 'cds',
      observation: 'diamond', cluster: 'folder', module: 'folder' };
    cy.nodes().forEach(function (n) {
      var d = n.data(), a = { label: d.label, shape: shapes[d.k] || 'box', color: '#555555', vfia_kind: d.k, vfia_risk: d.risk, vfia_status: d.s };
      if (d.changed || d.k === 'change') { a.fillcolor = '#ffd6d6'; a.color = '#c62828'; a.penwidth = 2.5; }
      else if (d.risk) { a.color = d.risk >= 2 ? '#e65100' : '#f2a900'; a.penwidth = 2.5; }
      if (d.s === 'added') { a.style = 'filled,dashed'; a.color = '#2e8b57'; }
      if (d.s === 'removed') { a.style = 'filled,dashed'; a.color = '#c62828'; }
      if (d.k === 'cluster') { a.style = 'filled,dashed'; a.fillcolor = '#efeeea'; }
      L.push('  ' + dq(n.id()) + ' [' + Object.keys(a).map(function (k) { return k + '=' + dq(a[k]); }).join(', ') + '];');
    });
    cy.edges().forEach(function (e) {
      var d = e.data(), color = d.ctrl ? '#7b3fbf' : '#7a7a75';
      if (d.status === 'added' || d.status === 'changed') color = '#2e8b57';
      if (d.status === 'removed') color = '#c62828';
      if (d.k === 'change') color = '#c62828';
      var a = { label: d.label, color: color, style: (d.k === 'shared' || d.k === 'signal') ? 'dashed' : (d.k === 'observe' ? 'dotted' : 'solid'),
        vfia_kind: d.ek, vfia_control: d.ctrl ? 'true' : 'false', vfia_status: d.status, vfia_count: d.cnt };
      L.push('  ' + dq(d.source) + ' -> ' + dq(d.target) + ' [' + Object.keys(a).map(function (k) { return k + '=' + dq(a[k]); }).join(', ') + '];');
    });
    L.push('}');
    return L.join('\n') + '\n';
  }
  function xesc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function toGraphML() {
    var nk = ['label', 'kind', 'module', 'risk', 'changed', 'status', 'step'], ek = ['kind', 'label', 'control', 'status', 'count'];
    var L = ['<?xml version="1.0" encoding="UTF-8"?>', '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">'];
    nk.forEach(function (k) { L.push('  <key id="n_' + k + '" for="node" attr.name="' + k + '" attr.type="string"/>'); });
    ek.forEach(function (k) { L.push('  <key id="e_' + k + '" for="edge" attr.name="' + k + '" attr.type="string"/>'); });
    L.push('  <graph id="' + xesc(exportName('graphml')) + '" edgedefault="directed">');
    cy.nodes().forEach(function (n) {
      var d = n.data(), v = { label: d.label, kind: d.k, module: d.m, risk: d.risk, changed: d.changed ? 'true' : 'false', status: d.s, step: d.dist };
      L.push('    <node id="' + xesc(n.id()) + '">' + nk.map(function (k) { return '<data key="n_' + k + '">' + xesc(v[k]) + '</data>'; }).join('') + '</node>');
    });
    cy.edges().forEach(function (e, i) {
      var d = e.data(), v = { kind: d.ek, label: d.label, control: d.ctrl ? 'true' : 'false', status: d.status, count: d.cnt };
      L.push('    <edge id="e' + i + '" source="' + xesc(d.source) + '" target="' + xesc(d.target) + '">' +
        ek.map(function (k) { return '<data key="e_' + k + '">' + xesc(v[k]) + '</data>'; }).join('') + '</edge>');
    });
    L.push('  </graph>', '</graphml>');
    return L.join('\n') + '\n';
  }
  function download(name, blob) {
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1500);
  }

  // ------------------------------------------------------------------ 컨트롤
  function syncSteps(model) {
    var show = st.type !== 'module';
    $('ig-steps-l').style.display = show ? '' : 'none';
    if (!show) return;
    var r = $('ig-steps');
    r.max = model.maxSteps;
    r.value = model.steps;
    $('ig-steps-v').textContent = model.steps + ' / ' + model.maxSteps;
  }
  function syncControls() {
    if (st.type === 'impact' && (st.change === '*' || !CHG[st.change])) st.change = (D.changes[0] || {}).id;
    $('ig-type').value = st.type;
    var sel = $('ig-change');
    var allOpt = sel.querySelector('option[value="*"]');
    if (allOpt) allOpt.disabled = st.type === 'impact';
    sel.value = st.change;
    $('ig-change-l').style.display = st.type === 'module' ? 'none' : '';
    $('ig-pathonly-l').style.display = st.type === 'impact' ? '' : 'none';
    $('ig-vars-l').style.display = st.type === 'program' ? '' : 'none';
    $('ig-gran-l').style.display = st.type === 'module' ? '' : 'none';
    $('ig-minw-l').style.display = st.type === 'module' ? '' : 'none';
    $('ig-layout').value = st.layout[st.type];
    $('ig-view').value = st.view;
    $('ig-cluster').checked = st.cluster;
    $('ig-th').value = st.th;
  }
  function init() {
    inited = true;
    var sel = $('ig-change');
    sel.innerHTML = '<option value="*">전체 변경(함수 의존 그래프용)</option>' + D.changes.map(function (c) {
      return '<option value="' + esc(c.id) + '">' + esc(c.id + ' ' + (c.type || '') + ' · ' + (c.unit || '')) + '</option>';
    }).join('');
    if (typeof cytoscape !== 'function') {
      $('ig-cy').innerHTML = '<div class="ig-empty">그래프 라이브러리를 불러오지 못함</div>';
      return;
    }
    $('ig-type').addEventListener('change', function () { st.type = this.value; st.sel = null; syncControls(); render(true); });
    sel.addEventListener('change', function () { st.change = this.value; st.path = null; st.sel = null; st.expanded = {}; syncControls(); render(true); });
    $('ig-steps').addEventListener('input', function () { st.steps[st.type] = +this.value; render(true); });
    $('ig-view').addEventListener('change', function () { st.view = this.value; render(true); });
    $('ig-pathonly').addEventListener('change', function () { st.pathOnly = this.checked; render(true); });
    $('ig-vars').addEventListener('change', function () { st.vars = this.checked; render(true); });
    $('ig-gran').addEventListener('change', function () { st.gran = this.value; st.minw = null; render(true); });
    $('ig-minw').addEventListener('change', function () { st.minw = Math.max(1, +this.value || 1); render(true); });
    $('ig-layout').addEventListener('change', function () { st.layout[st.type] = this.value; render(true); });
    $('ig-cluster').addEventListener('change', function () { st.cluster = this.checked; render(true); });
    $('ig-th').addEventListener('change', function () { st.th = Math.max(20, +this.value || 150); render(true); });
    $('ig-zin').addEventListener('click', function () { zoomBy(1.3); });
    $('ig-zout').addEventListener('click', function () { zoomBy(1 / 1.3); });
    $('ig-fit').addEventListener('click', function () { if (cy) cy.fit(undefined, 30); });
    $('ig-expand').addEventListener('click', function () {
      if (!lastModel) return;
      lastModel.nodes.forEach(function (n) { if (n.k === 'cluster') st.expanded[n.m] = true; });
      st.cluster = false; syncControls(); render(true);
    });
    $('ig-dot').addEventListener('click', function () { if (cy) download(exportName('dot'), new Blob([toDot()], { type: 'text/vnd.graphviz' })); });
    $('ig-gml').addEventListener('click', function () { if (cy) download(exportName('graphml'), new Blob([toGraphML()], { type: 'application/xml' })); });
    $('ig-png').addEventListener('click', function () {
      if (cy) download(exportName('png'), cy.png({ output: 'blob', full: true, scale: 2, bg: palette().card }));
    });
    window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () { render(false); });
    syncControls();
    render(true);
  }
  function zoomBy(f) {
    if (!cy) return;
    var c = $('ig-cy');
    cy.zoom({ level: Math.max(cy.minZoom(), Math.min(cy.maxZoom(), cy.zoom() * f)), renderedPosition: { x: c.clientWidth / 2, y: c.clientHeight / 2 } });
  }

  // 시험·자동화용(브라우저 콘솔에서 상태 확인)
  window.vfiaGraph = { state: st, render: render, model: function () { return lastModel; }, cy: function () { return cy; },
    toDot: function () { return toDot(); }, toGraphML: function () { return toGraphML(); }, go: goCard };

  $('tabbtn-report').addEventListener('click', function (ev) { ev.preventDefault(); history.pushState(null, '', '#report'); showTab('report'); });
  $('tabbtn-graph').addEventListener('click', function (ev) {
    ev.preventDefault();
    history.pushState(null, '', '#graph' + (st.change ? ':' + st.change : ''));
    showTab('graph');
  });
  route();
})();
