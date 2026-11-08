/* 招采标讯标的物智能分析平台 · 前端（Vue 3 组合式 API，无需构建）
 * 后端：FastAPI（platform/app.py） ｜ Vue 运行时本地内置（platform/static/vue.global.prod.js）
 */
(function () {
  'use strict';
  const { createApp, ref, reactive, computed, onMounted } = Vue;

  /* ---------------- 全局状态与请求封装 ---------------- */
  const state = reactive({ token: sessionStorage.getItem('tok') || '' });

  async function api(path, opt) {
    const r = await fetch(path, Object.assign({
      headers: state.token ? { Authorization: 'Bearer ' + state.token } : {}
    }, opt || {}));
    if (r.status === 401) { state.token = ''; sessionStorage.removeItem('tok'); throw new Error('登录已过期，请重新登录'); }
    const j = await r.json().catch(() => ({ error: '响应解析失败' }));
    if (!r.ok) throw new Error(j.detail || j.error || ('HTTP ' + r.status));
    return j;
  }
  const fmt = (v) => (v === null || v === undefined || v === '') ? '—' : v;
  const num = (v) => { const n = Number(v); return isNaN(n) ? null : n; };

  /* ---------------- 通用子组件 ---------------- */
  const DataTable = {
    props: { rows: { type: Array, default: () => [] }, cols: Array, headers: Array, empty: { type: String, default: '（无结果）' } },
    template: `
      <p v-if="!rows.length" class="mut">{{ empty }}</p>
      <div v-else class="scroll"><table>
        <thead><tr><th v-for="(h,i) in (headers||cols)" :key="i">{{ h }}</th></tr></thead>
        <tbody><tr v-for="(r,ri) in rows" :key="ri">
          <td v-for="(c,ci) in cols" :key="ci">{{ fmt(r[c]) }}</td>
        </tr></tbody>
      </table></div>`,
    setup() { return { fmt }; }
  };

  const BarList = {
    props: { rows: Array, label: String, value: String, unit: { type: String, default: '' } },
    template: `
      <div v-if="!rows.length" class="mut">（无结果）</div>
      <div v-else>
        <div v-for="(r,i) in rows" :key="i" class="row" style="margin:5px 0">
          <span style="min-width:240px">{{ r[label] }}</span>
          <span class="bar"><i :style="{width: pct(r[value])}"></i></span>
          <span class="mut" style="min-width:84px;text-align:right">{{ fmt(r[value]) }}{{ unit }}</span>
        </div>
      </div>`,
    setup(props) {
      const max = computed(() => Math.max(1, ...props.rows.map(r => num(r[props.value]) || 0)));
      const pct = (v) => ((num(v) || 0) / max.value * 100).toFixed(1) + '%';
      return { pct, fmt };
    }
  };

  const Loading = { template: `<p class="mut"><span class="spin"></span> 查询中…</p>` };
  const ErrBox = { props: { msg: String }, template: `<p v-if="msg" class="warn err">{{ msg }}</p>` };

  /* ---------------- 概览 ---------------- */
  const Overview = {
    components: { DataTable, Loading, ErrBox },
    template: `
      <ErrBox :msg="err"/>
      <Loading v-if="!s && !err"/>
      <template v-if="s">
        <div class="card"><h3>数据总览</h3>
          <div class="kv"><div v-for="(v,k) in s.counts" :key="k"><b>{{ v.toLocaleString() }}</b>{{ k }}</div></div>
          <p class="mut">入库时间 {{ s.meta.built_at }} ｜ 耗时 {{ s.meta.elapsed_sec }} 秒（{{ s.meta.per_page_sec }} 秒/篇）—— 即赛题「处理速率」口径中的“写入数据库”环节。</p>
        </div>
        <div class="card"><h3>TOP 采购单位（按项目数）</h3>
          <DataTable :rows="s.top_purchasers" :cols="['purchaser','n_notices','total_amount']" :headers="['采购单位','项目数','总金额(元)']"/>
        </div>
        <div class="card"><h3>TOP 中标供应商（按中标金额）</h3>
          <DataTable :rows="s.top_suppliers" :cols="['supplier','win_notices','win_rate','won_amount','n_purchasers']"
                     :headers="['供应商','中标次数','中标率','中标金额(元)','合作采购单位数']"/>
        </div>
      </template>`,
    setup() {
      const s = ref(null), err = ref('');
      onMounted(async () => { try { s.value = await api('/api/stats'); } catch (e) { err.value = e.message; } });
      return { s, err };
    }
  };

  /* ---------------- 标的物全字段检索 ---------------- */
  const Search = {
    components: { DataTable, Loading, ErrBox },
    template: `
      <div class="card"><h3>标的物全字段检索</h3>
        <div class="row">
          <input v-model="q" @keyup.enter="run(0)" placeholder="关键字，如：服务器 / 深信服 / 海康威视" style="flex:1;min-width:260px">
          <select v-model="field">
            <option value="all">全字段</option><option value="item_name">标的名称</option>
            <option value="brand">品牌</option><option value="spec">规格型号</option>
            <option value="category">品目</option><option value="supplier_name">中标供应商</option>
          </select>
          <input v-model.number="limit" style="width:80px" title="返回条数">
          <button class="btn" :disabled="busy" @click="run(0)">检索</button>
        </div>
        <p class="mut">共 <b>{{ total }}</b> 条 ｜ 第 {{ offset + 1 }} - {{ Math.min(offset + limit, total) }} 条 ｜ 点击「公告ID」查看该公告全部明细</p>
      </div>
      <ErrBox :msg="err"/>
      <div class="card">
        <Loading v-if="busy"/>
        <div v-else class="scroll"><table>
          <thead><tr><th v-for="h in headers" :key="h">{{ h }}</th></tr></thead>
          <tbody><tr v-for="(r,i) in rows" :key="i">
            <td><span class="link" @click="detail(r.announcement_id)">{{ r.announcement_id }}</span></td>
            <td>{{ fmt(r.item_name) }}</td><td>{{ fmt(r.category) }}</td><td>{{ fmt(r.brand) }}</td>
            <td>{{ fmt(r.spec) }}</td><td>{{ fmt(r.quantity) }}</td><td>{{ fmt(r.unit) }}</td>
            <td>{{ fmt(r.unit_price) }}</td><td>{{ fmt(r.total_price) }}</td><td>{{ fmt(r.supplier_name) }}</td>
          </tr></tbody>
        </table></div>
        <div class="row" style="margin-top:10px">
          <button class="btn ghost" :disabled="offset<=0" @click="run(-limit)">上一页</button>
          <button class="btn ghost" :disabled="offset+limit>=total" @click="run(limit)">下一页</button>
        </div>
      </div>
      <div v-if="det" class="modal" @click.self="det=null">
        <div class="card">
          <div class="row"><h3 style="margin:0;flex:1">公告明细 · {{ det.notice.announcement_id }}</h3>
            <button class="btn ghost" @click="det=null">关闭</button></div>
          <p class="mut">{{ fmt(det.notice.project_name) }} ｜ 采购单位：{{ fmt(det.notice.purchaser) }} ｜ 金额：{{ fmt(det.notice.project_amount) }} 元</p>
          <DataTable :rows="det.items" :cols="['item_name','category','brand','spec','quantity','unit','unit_price','total_price','supplier_name']"
                     :headers="['标的名称','品目','品牌','规格型号','数量','单位','单价(元)','总价(元)','中标供应商']"/>
        </div>
      </div>`,
    setup() {
      const q = ref(''), field = ref('all'), limit = ref(50), offset = ref(0);
      const rows = ref([]), total = ref(0), busy = ref(false), err = ref(''), det = ref(null);
      const headers = ['公告ID', '标的名称', '品目', '品牌', '规格型号', '数量', '单位', '单价(元)', '总价(元)', '中标供应商'];
      async function run(delta) {
        offset.value = Math.max(0, offset.value + delta);
        busy.value = true; err.value = '';
        try {
          const j = await api(`/api/items?q=${encodeURIComponent(q.value)}&field=${field.value}&limit=${limit.value}&offset=${offset.value}`);
          rows.value = j.rows; total.value = j.total;
        } catch (e) { err.value = e.message; } finally { busy.value = false; }
      }
      async function detail(id) { try { det.value = await api('/api/notice/' + encodeURIComponent(id)); } catch (e) { err.value = e.message; } }
      onMounted(() => run(0));
      return { q, field, limit, offset, rows, total, busy, err, det, headers, run, detail, fmt, Math };
    }
  };

  /* ---------------- 场景通用外壳 ---------------- */
  function scenario(engine) {
    return {
      components: { DataTable, BarList, Loading, ErrBox },
      data() { return { a: '', b: '', res: null, busy: false, err: '' }; },
      methods: {
        async run() {
          this.busy = true; this.err = ''; this.res = null;
          try { this.res = await engine(this); }
          catch (e) { this.err = e.message; } finally { this.busy = false; }
        },
        fmt
      }
    };
  }

  const S1 = Object.assign(scenario(async (vm) => api('/api/scenario/s1?purchaser=' + encodeURIComponent(vm.a))), {
    template: `
      <div class="card"><h3>S1 指定采购单位 → 中标供应商 / 产品供应商</h3>
        <div class="row"><input v-model="a" @keyup.enter="run" placeholder="采购单位全称，如：济宁学院" style="flex:1;min-width:300px">
          <button class="btn" :disabled="busy||!a" @click="run">查询</button>
          <span class="mut">或按项目数自动选一个：<span class="link" @click="auto">自动选取</span></span></div>
      </div>
      <ErrBox :msg="err"/><Loading v-if="busy"/>
      <template v-if="res">
        <div class="card"><h4 style="margin-top:0">中标供应商（按合作次数）</h4>
          <p class="mut">输入：<b>{{ res.input }}</b> ｜ 历史项目 {{ fmt((res.summary||{}).n_notices) }} 个，总金额 {{ fmt((res.summary||{}).total_amount) }} 元</p>
          <DataTable :rows="res.winners" :cols="['supplier','coop_times','coop_amount','first_time','last_time']"
                     :headers="['中标供应商','合作次数','合作金额(元)','首次合作','最近合作']"/>
          <h4>产品供应商（品牌 + 声明函制造商）</h4>
          <DataTable :rows="res.product_suppliers" :cols="['product_supplier','src_kind','notices']"
                     :headers="['产品供应商','来源(brand=品牌 / declaration=声明函)','涉及项目数']"/>
        </div>
      </template>`,
    methods: {
      async auto() {
        try { const j = await api('/api/stats'); this.a = j.top_purchasers[0].purchaser; this.run(); }
        catch (e) { this.err = e.message; }
      }
    }
  });

  const S2 = Object.assign(scenario(async (vm) => api('/api/scenario/s2?purchaser=' + encodeURIComponent(vm.a))), {
    template: `
      <div class="card"><h3>S2 指定采购单位 → 高频投标主体 / 协同投标组合</h3>
        <div class="row"><input v-model="a" @keyup.enter="run" placeholder="采购单位全称" style="flex:1;min-width:300px">
          <button class="btn" :disabled="busy||!a" @click="run">查询</button></div>
      </div>
      <ErrBox :msg="err"/><Loading v-if="busy"/>
      <template v-if="res">
        <div class="card"><p class="mut">输入：<b>{{ res.input }}</b></p>
          <h4>TOP5 参与投标主体</h4>
          <DataTable :rows="res.top_bidders" :cols="['supplier','participate_times','win_times','bid_only_times','won_amount']"
                     :headers="['主体','参与投标次数','中标次数','仅投标(未中标)','中标金额(元)']"/>
          <h4>高频协同投标组合 TOP5</h4>
          <BarList :rows="res.top_pairs" label="supplier_a" value="co_times" unit=" 次"/>
          <DataTable :rows="res.top_pairs" :cols="['supplier_a','supplier_b','co_times']" :headers="['主体A','主体B','共同投标次数']"/>
        </div>
      </template>`
  });

  const S3 = Object.assign(scenario(async (vm) => api('/api/scenario/s3?supplier=' + encodeURIComponent(vm.a))), {
    template: `
      <div class="card"><h3>S3 指定中标供应商 → 共同竞标主体</h3>
        <div class="row"><input v-model="a" @keyup.enter="run" placeholder="中标供应商全称" style="flex:1;min-width:300px">
          <button class="btn" :disabled="busy||!a" @click="run">查询</button>
          <span class="mut">或取中标金额第一：<span class="link" @click="auto">自动选取</span></span></div>
      </div>
      <ErrBox :msg="err"/><Loading v-if="busy"/>
      <template v-if="res">
        <div class="card">
          <p class="mut">输入：<b>{{ res.input }}</b> ｜ 中标 {{ fmt((res.profile||{}).win_notices) }} 次 / 参与 {{ fmt((res.profile||{}).bid_notices) }} 次
            / 中标率 {{ fmt((res.profile||{}).win_rate) }} / 合作采购单位 {{ fmt((res.profile||{}).n_purchasers) }} 家</p>
          <h4>高频共同竞标主体 TOP5</h4>
          <BarList :rows="res.top_co_bidders" label="co_bidder" value="co_times" unit=" 次"/>
          <DataTable :rows="res.top_co_bidders" :cols="['co_bidder','co_times','co_project_amount']"
                     :headers="['共同竞标主体','共同投标次数','涉及项目金额(元)']"/>
          <h4>全部共同竞标主体（{{ (res.all_co_bidders||[]).length }} 家）</h4>
          <DataTable :rows="res.all_co_bidders" :cols="['co_bidder','co_times','co_project_amount']"
                     :headers="['共同竞标主体','共同投标次数','涉及项目金额(元)']"/>
        </div>
      </template>`,
    methods: {
      async auto() {
        try { const j = await api('/api/stats'); this.a = j.top_suppliers[0].supplier; this.run(); }
        catch (e) { this.err = e.message; }
      }
    }
  });

  const S4 = Object.assign(scenario(async (vm) =>
    api('/api/scenario/s4?suppliers=' + encodeURIComponent([vm.a, vm.b].filter(Boolean).join('|')))), {
    template: `
      <div class="card"><h3>S4 指定 ≥2 个中标供应商 → 均与其有合作关系的采购单位</h3>
        <div class="row"><input v-model="a" placeholder="中标供应商 A" style="flex:1;min-width:260px">
          <input v-model="b" placeholder="中标供应商 B" style="flex:1;min-width:260px">
          <button class="btn" :disabled="busy||!a||!b" @click="run">查询</button>
          <span class="mut">或取共同竞标最高的组合：<span class="link" @click="auto">自动选取</span></span></div>
      </div>
      <ErrBox :msg="err"/><Loading v-if="busy"/>
      <template v-if="res">
        <div class="card"><p class="mut">输入：<b>{{ (res.input||[]).join(' × ') }}</b> ｜
          以下 {{ (res.purchasers||[]).length }} 家采购单位与这些主体<b>均</b>有合作关系</p>
          <DataTable :rows="res.purchasers" :cols="['purchaser','n_suppliers','coop_times','coop_amount']"
                     :headers="['采购单位','涉及我方主体数','合作次数','合作金额(元)']"/>
        </div>
      </template>`,
    methods: {
      async auto() {
        try {
          const j = await api('/api/scenario/s3?supplier=' + encodeURIComponent('中国移动通信集团广东有限公司'));
          const c = (j.top_co_bidders || [])[0];
          if (c) { this.a = '中国移动通信集团广东有限公司'; this.b = c.co_bidder; this.run(); }
        } catch (e) { this.err = e.message; }
      }
    }
  });

  const S5 = Object.assign(scenario(async (vm) =>
    api('/api/scenario/s5?suppliers=' + encodeURIComponent([vm.a, vm.b].filter(Boolean).join('|')))), {
    template: `
      <div class="card"><h3>S5 指定 ≥2 个中标供应商 → 历史共同参与竞标的项目与结果</h3>
        <div class="row"><input v-model="a" placeholder="中标供应商 A" style="flex:1;min-width:260px">
          <input v-model="b" placeholder="中标供应商 B" style="flex:1;min-width:260px">
          <button class="btn" :disabled="busy||!a||!b" @click="run">查询</button></div>
      </div>
      <ErrBox :msg="err"/><Loading v-if="busy"/>
      <template v-if="res">
        <div class="card">
          <p class="mut">输入：<b>{{ (res.input||[]).join(' × ') }}</b> ｜ 共同参与项目 <b>{{ res.n_projects }}</b> 个，
            项目总金额 <b>{{ res.total_project_amount }}</b> 元</p>
          <div class="scroll"><table>
            <thead><tr><th>公告ID</th><th>项目名称</th><th>采购单位</th><th>公告时间</th><th>项目金额(元)</th><th>竞标结果</th></tr></thead>
            <tbody><tr v-for="(p,i) in res.projects" :key="i">
              <td>{{ p.notice_id }}</td><td>{{ fmt(p.project_name) }}</td><td>{{ fmt(p.purchaser) }}</td>
              <td>{{ fmt(p.publish_time) }}</td><td>{{ fmt(p.project_amount) }}</td>
              <td><span v-for="(r,j) in p.results" :key="j">{{ r.supplier }}：{{ Number(r.is_winner) ? '中标' : '参与' }}<br></span></td>
            </tr></tbody>
          </table></div>
        </div>
      </template>`
  });

  const About = {
    template: `
      <div class="card"><h3>平台说明</h3>
        <p><b>架构</b>：前端 Vue 3（组合式 API，运行时本地内置，无需 npm 构建）｜ 后端 FastAPI + uvicorn ｜ 数据库 SQLite。</p>
        <p><b>数据来源</b>：任务一（1,038 篇公告 + 970 个附件 → 6,206 条标的物明细）；任务二（3,239 条参与投标事实、3,402 个实体、1,672 条合作关系、6,562 条共同竞标关系）。</p>
        <p><b>全字段检索</b>：标的名称 / 品目 / 品牌 / 规格型号 / 数量 / 单位 / 单价 / 总价 / 中标供应商 / 包号；支持字段限定与分页。</p>
        <p><b>五大场景</b>：S1 采购单位合作画像（含产品供应商）｜S2 高频投标主体与协同组合｜S3 中标供应商共同竞标｜S4 多供应商共同采购单位（“均有合作”语义）｜S5 多供应商共同竞标项目与结果。</p>
        <p><b>自动化处理</b>：<code>POST /api/ingest</code> 重新入库并返回耗时，对应赛题“数据集导入后自动解析、实体抽取与结构化入库”。</p>
        <p><b>接口文档</b>：<a href="/docs" target="_blank">/docs</a>（Swagger UI）｜<a href="/redoc" target="_blank">/redoc</a>｜<a href="/openapi.json" target="_blank">/openapi.json</a></p>
      </div>`
  };

  /* ---------------- 根应用 ---------------- */
  const App = {
    components: { Overview, Search, S1, S2, S3, S4, S5, About },
    template: `
      <div v-cloak>
        <header>
          <h1>招采标讯标的物智能分析平台</h1>
          <small>赛题五 · 任务三（Vue 3 + FastAPI）</small>
          <span class="sp"></span>
          <small v-if="token">已登录：{{ user }}　<span class="link" style="color:#cfe0ff" @click="logout">退出</span></small>
        </header>

        <div v-if="!token" class="card" id="login">
          <h3>评审方登录</h3>
          <input v-model="u" placeholder="账号（默认 judge）">
          <input v-model="p" type="password" placeholder="密码（默认 ict2026）" @keyup.enter="login">
          <button class="btn" style="width:100%" :disabled="busy" @click="login">登录</button>
          <p v-if="err" class="err">{{ err }}</p>
          <p class="mut">账号配置见 app/platform/account.json；未登录访问业务接口返回 401。</p>
        </div>

        <template v-else>
          <nav>
            <button v-for="t in tabs" :key="t.k" :class="{on: tab===t.k}" @click="tab=t.k">{{ t.n }}</button>
          </nav>
          <main>
            <component :is="tab"/>
          </main>
        </template>
      </div>`,
    setup() {
      const token = ref(state.token), u = ref('judge'), p = ref('ict2026');
      const tab = ref('Overview'), err = ref(''), busy = ref(false), user = ref(sessionStorage.getItem('user') || 'judge');
      const tabs = [
        { k: 'Overview', n: '概览' }, { k: 'Search', n: '标的物全字段检索' },
        { k: 'S1', n: 'S1 采购单位合作画像' }, { k: 'S2', n: 'S2 高频投标/协同组合' },
        { k: 'S3', n: 'S3 中标供应商共同竞标' }, { k: 'S4', n: 'S4 多供应商共同采购单位' },
        { k: 'S5', n: 'S5 多供应商共同竞标项目' }, { k: 'About', n: '说明' }
      ];
      async function login() {
        busy.value = true; err.value = '';
        try {
          const r = await fetch(`/api/login?user=${encodeURIComponent(u.value)}&password=${encodeURIComponent(p.value)}`);
          const j = await r.json();
          if (!j.ok) throw new Error(j.detail || j.error || '登录失败');
          state.token = j.token; token.value = j.token; user.value = j.user;
          sessionStorage.setItem('tok', j.token); sessionStorage.setItem('user', j.user);
        } catch (e) { err.value = e.message; } finally { busy.value = false; }
      }
      function logout() {
        state.token = ''; token.value = ''; sessionStorage.removeItem('tok');
      }
      return { token, u, p, tab, tabs, err, busy, user, login, logout };
    }
  };

  function mount() { createApp(App).mount('#app'); }
  if (window.Vue) { mount(); } else {
    // 本地 Vue 运行时缺失时回退 CDN（保证离线/在线都能跑）
    const s = document.createElement('script');
    s.src = 'https://unpkg.com/vue@3.5.13/dist/vue.global.prod.js';
    s.onload = mount;
    s.onerror = () => { document.getElementById('app').innerHTML = '<p class="warn">Vue 运行时缺失：请确认 platform/static/vue.global.prod.js 存在，或联网后重试。</p>'; };
    document.head.appendChild(s);
  }
})();
