'use strict';
/* 数据层 v2（2026-09-19 改造）
   首屏只拉 meta.json + home.json（主板 + 非ST + 近3天，用户 90% 的用法）；
   切板块/时间范围时按需拉 list-<key>.json；点股票才拉 stock/<前2位>/<code>.json。
   K 线已并入单股文件，不再是 16 个分片（点一只股票从 ~1.8MB 降到 ~7KB）。

   条目是 13 元数组（省掉 key 名，全市场约省 750KB）：
     [c, n, b, st, cat, cap, p, ch, ch5, cha, d, lu, a]
     a = [[date, title, category_id], ...]  —— 不含 URL（URL 只在单股详情里）
   单股文件：{v,c,n,b,st,cat,cap,p,ch,ch5,cha,d,src,adj,k:[[date,o,c,h,l,v]],a:[[date,title,cat,url]]}
*/
const RED = '#e6343a', GREEN = '#0aa858';
const DATA_DIR = 'data/';
const ECHARTS_URL = 'lib/echarts.min.js';

// 13 元条目索引
const C = 0, N = 1, B = 2, ST = 3, CAT = 4, CAP = 5, P = 6, CH = 7, CH5 = 8, CHA = 9, D = 10, LU = 11, A = 12;

let meta = {}, taxonomy = [], items = [], loadedKey = null;
let labelOf = {}, colorOf = {};
let activeCat = '全部', activeBoard = '主板', showST = false, activeRange = '3d', activeCode = null, chart = null;
let visible = [], generation = 0;

const $ = id => document.getElementById(id);
const esc = v => String(v ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
const known = v => typeof v === 'number' && Number.isFinite(v);
const fmt = v => known(v) ? (v >= 0 ? '+' : '') + v.toFixed(2) + '%' : '—';
const cls = v => !known(v) ? '' : v >= 0 ? 'up' : 'down';
const fmtMv = v => known(v) && v > 0 ? (v >= 1e12 ? (v / 1e12).toFixed(2) + '万亿' : (v / 1e8).toFixed(1) + '亿') : '';
const todayCN = () => new Date(Date.now() + 8 * 3600000).toISOString().slice(0,10);

const jsonLoads = new Map();
function loadJSON(path) {
  if (jsonLoads.has(path)) return jsonLoads.get(path);
  const task = fetch(path, {cache: 'no-cache'}).then(res => {
    if (!res.ok) throw new Error('加载 ' + path + ' 失败（HTTP ' + res.status + '）');
    return res.json();
  });
  jsonLoads.set(path, task);
  task.catch(() => jsonLoads.delete(path));
  return task;
}

function cutoff(n, end = todayCN()) {
  return new Date(Date.parse(end + 'T00:00:00Z') - (n-1)*86400000).toISOString().slice(0,10);
}
function dateMatches(d) {
  if (!d || d > todayCN()) return false;
  if (!activeRange) return true;
  if (activeRange === '30d+') return d < cutoff(30);
  return d >= cutoff(Number(activeRange.slice(0,-1)));
}
function matchingAnns(s, query = '') {
  const code = String(s[C] ?? ''), name = String(s[N] ?? '').toLowerCase();
  const stockMatch = code.includes(query) || name.includes(query);
  return (s[A] || []).filter(a => dateMatches(a[0]) &&
    (activeCat === '全部' || labelOf[a[2]] === activeCat) &&
    (!query || stockMatch || String(a[1] ?? '').toLowerCase().includes(query) ||
      String(labelOf[a[2]] ?? '').toLowerCase().includes(query)));
}
function filtered() {
  const query = $('search').value.trim().toLowerCase();
  return items.flatMap(s => {
    if (activeBoard !== '全部板块' && (s[B] || '未知板块') !== activeBoard) return [];
    if (!showST && s[ST]) return [];
    const anns = matchingAnns(s, query);
    return anns.length ? [{stock:s, anns}] : [];
  }).sort((a,b) => b.anns.at(-1)[0].localeCompare(a.anns.at(-1)[0]) ||
    String(a.stock[C]).localeCompare(String(b.stock[C])));
}
function setOptions(id, values, active) {
  $(id).innerHTML = values.map(([value,label]) => `<option value="${esc(value)}"${value === active ? ' selected' : ''}>${esc(label)}</option>`).join('');
}
function renderFilters() {
  const found = new Set(items.flatMap(s => (s[A] || []).map(a => labelOf[a[2]])).filter(Boolean));
  const cats = ['全部', ...taxonomy.filter(t => found.has(t.label)).map(t => t.label),
    ...[...found].filter(x => !taxonomy.some(t => t.label === x))];
  $('cats').innerHTML = [...new Set(cats)].map(c => `<button type="button" class="cat${c === activeCat ? ' active' : ''}" data-cat="${esc(c)}">${esc(c)}</button>`).join('');
  $('cats').querySelectorAll('button').forEach(el => el.onclick = () => {activeCat = el.dataset.cat; renderFilters(); renderList();});
  setOptions('boards', [...new Set(['全部板块', ...(meta.boards || ['主板','创业板','科创板','北交所'])])].map(b => [b,b]), activeBoard);
  setOptions('dranges', [['','窗口内全部'],['3d','近3天'],['7d','近7天'],['15d','近15天'],['30d','近30天'],['30d+','30天之前']], activeRange);
  $('stswitch').checked = showST;
}
function clearChart(message, retry) {
  if (chart) {chart.dispose(); chart = null;}
  $('chart').innerHTML = `<div class="empty"><span>${esc(message)}</span>${retry ? '<button type="button" id="retry_chart" style="margin-left:12px;padding:6px 12px">重试</button>' : ''}</div>`;
  if (retry) $('retry_chart').onclick = retry;
}
function clearSelection() {
  activeCode = null; generation++;
  ['p_nm','p_cd','p_mv','p_px','p_chg'].forEach(id => $(id).textContent = '');
  $('p_rsn').textContent = '调整筛选条件后选择股票';
  $('p_anns').style.display = 'none';
  clearChart('无匹配股票');
}
function renderList() {
  visible = filtered();
  const list = $('list');
  if (!visible.length) {list.innerHTML = '<div class="empty">无匹配股票</div>'; clearSelection(); return;}
  list.innerHTML = visible.map(({stock:s, anns}) => {
    const last = anns.at(-1);
    const catLabel = labelOf[last[2]] || last[2] || '';
    return `<div class="stock${String(activeCode) === String(s[C]) ? ' active' : ''}" data-code="${esc(s[C])}" tabindex="0" role="button">
      <div class="row1"><span class="nm">${esc(s[N])}</span><span class="cd">${esc(s[C])}</span><span class="mv" title="首次取得的参考市值，不是实时市值">${fmtMv(s[CAP])}</span><span class="badge">${esc(s[B])}${s[ST] ? ' · ST' : ''}</span></div>
      <div class="chgs"><span class="chip ${cls(s[CH])}">日 ${fmt(s[CH])}</span><span class="chip ${cls(s[CHA])}" title="当前展示窗口内首条公告日前一根收盘价至最新收盘价，与当前筛选条件无关">窗口首公告 ${fmt(s[CHA])}</span><span class="chip ${cls(s[CH5])}">5日 ${fmt(s[CH5])}</span></div>
      <div class="rsn"><span style="color:${colorOf[last[2]] || '#6b7280'}">${esc(catLabel)}</span> · ${esc(last[0])} · ${esc(last[1])}</div>
      <div class="rsn">行情日期 ${esc(s[D] || '暂无')} · 匹配公告 ${anns.length} 条</div></div>`;
  }).join('');
  list.querySelectorAll('.stock').forEach(el => {el.onclick = () => select(el.dataset.code); el.onkeydown = e => {if (e.key === 'Enter') select(el.dataset.code);};});
  select(visible.some(v => String(v.stock[C]) === String(activeCode)) ? activeCode : visible[0].stock[C]);
}
function safeLink(url) {
  try {const u = new URL(url); return u.protocol === 'https:' ? u.href : '';} catch (_) {return '';}
}
const scriptLoads = new Map();
function loadScript(url, ready) {
  if (ready()) return Promise.resolve();
  // Only same-build relative assets may execute.
  if (!/^[a-zA-Z0-9_./-]+\.js$/.test(url || '') || url.startsWith('/') || url.includes('..')) return Promise.reject(new Error('资源路径无效'));
  if (scriptLoads.has(url)) return scriptLoads.get(url);
  const task = new Promise((resolve,reject) => {
    const el = document.createElement('script'); let timer;
    const finish = err => {clearTimeout(timer); el.onload = el.onerror = null; if (err) {el.remove(); reject(err);} else resolve();};
    el.onload = () => finish(ready() ? null : new Error('资源内容无效'));
    el.onerror = () => finish(new Error('资源加载失败'));
    timer = setTimeout(() => finish(new Error('资源加载超时')), 20000);
    el.src = url; document.head.appendChild(el);
  });
  scriptLoads.set(url, task);
  task.catch(() => scriptLoads.delete(url));
  return task;
}
function stockPath(code) {
  const c = String(code);
  return DATA_DIR + 'stock/' + c.slice(0,2) + '/' + encodeURIComponent(c) + '.json';
}
function renderAnnPanel(anns, note = '') {
  // anns 元素：[date, title, category_id, url?]。
  // ★ 只有单股详情（detail.a）带 url；档位数组（s[A]）的公告是三元、没有 url。
  //   所以面板最终必须以 detail.a 渲染，否则原文链接会全部落空。
  if (!anns.length && !note) { $('p_anns').innerHTML = ''; $('p_anns').style.display = 'none'; return; }
  $('p_anns').innerHTML = [...anns].reverse().map(a => {
    const url = safeLink(a[3]);
    const cat = labelOf[a[2]] || a[2] || '';
    const hit = activeCat !== '全部' && cat === activeCat;
    return `<div class="ann-item${hit ? ' ann-hit' : ''}"><span class="ann-date">${esc(a[0])}</span><span class="ann-title">[${esc(cat)}] ${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(a[1])}</a>` : esc(a[1])}</span></div>`;
  }).join('') + (note ? `<div class="ann-item"><span class="ann-title" style="color:#b45309">${esc(note)}</span></div>` : '');
  $('p_anns').style.display = 'block';
}
function select(code) {
  const found = visible.find(v => String(v.stock[C]) === String(code));
  if (!found) return;
  activeCode = code;
  const token = ++generation;
  const {stock:s, anns} = found;
  document.querySelectorAll('.stock').forEach(el => el.classList.toggle('active', el.dataset.code === String(code)));
  $('p_nm').textContent = s[N]; $('p_cd').textContent = s[C];
  $('p_mv').textContent = fmtMv(s[CAP]) ? '参考市值 ' + fmtMv(s[CAP]) : '';
  $('p_px').textContent = known(s[P]) ? s[P].toFixed(2) : '—';
  $('p_px').style.color = known(s[CH]) ? (s[CH] >= 0 ? RED : GREEN) : '';
  $('p_chg').textContent = fmt(s[CH]); $('p_chg').style.color = $('p_px').style.color;
  $('p_rsn').textContent = `匹配 ${anns.length} 条 · 日线 ${s[D] || '暂无'}`;
  // 先用当前档位的公告给即时反馈（无原文链接），详情到达后再覆盖成全窗口带链接的版本。
  renderAnnPanel((s[A] || []).filter(a => a[0] && a[0] <= todayCN()), '公告原文加载中…');
  clearChart('K线加载中…');
  loadJSON(stockPath(s[C]))
    .then(detail => {
      if (token !== generation) return;
      const k = (detail || {}).k || [];
      // ★ 公告面板与 K 线标注一律用单股详情里的全窗口公告（detail.a）：
      //   档位数组（s[A]）只含"当前档位"的公告 —— 首屏走 home 档就只剩近 3 天，
      //   标注会凭空少一大截。detail.a 是 [date,title,category_id,url] 四元，多一个原文链接。
      const windowAnns = ((detail || {}).a || []).filter(a => a[0] && a[0] <= todayCN());
      const adj = {qfq:'前复权', none:'不复权', unknown:'旧数据口径未核实'}[(detail || {}).adj] || '口径未知';
      $('p_rsn').textContent = `匹配 ${anns.length} 条 / 窗口内共 ${windowAnns.length} 条公告 · 日线 ${(detail || {}).d || '暂无'} · ${(detail || {}).src || '行情源未知'} / ${adj}`;
      renderAnnPanel(windowAnns);
      if (!k.length) {clearChart('该股票暂无可用K线；请检查行情日期与采集记录'); return;}
      $('p_px').textContent = k.at(-1)[2].toFixed(2);
      return loadScript(ECHARTS_URL, () => !!window.echarts).then(() => {if (token === generation) drawChart(detail, k, windowAnns);});
    })
    .catch(error => {if (token === generation) clearChart(error.message + '，请刷新页面或重试', () => select(code));});
}
function announcementPoints(anns, dates) {
  return anns.flatMap(a => {
    // 公告日无对应K线（周末/停牌/超出右端）时，标在它【之前】的最后一根上。
    // 口径与项目其他部分一致：基准是"公告前一根收盘价"（见 app/board.py _chg_stats 的 base 子查询）。
    let i = -1;
    for (let j = dates.length-1; j >= 0; j--) {if (dates[j] <= a[0]) {i = j; break;}}
    return i < 0 ? [] : [{date:a[0], title:a[1], cat:a[2], i, anchorDate:dates[i], shifted:dates[i] !== a[0]}];
  });
}
function drawChart(detail, k, anns) {
  clearChart(''); $('chart').innerHTML = ''; chart = window.echarts.init($('chart'));
  const dates = k.map(r => r[0]), closes = k.map(r => r[2]), vols = k.map(r => r[5]);
  const ohlc = k.map(r => [r[1], r[2], r[4], r[3]]), points = announcementPoints(anns, dates);
  const ma = n => closes.map((_, i) => i < n-1 ? '-' : +(closes.slice(i-n+1, i+1).reduce((a,b) => a+b, 0) / n).toFixed(3));
  chart.setOption({animation:false, axisPointer:{link:[{xAxisIndex:'all'}]},
    tooltip:{trigger:'axis', axisPointer:{type:'cross'}, backgroundColor:'rgba(255,255,255,.97)', textStyle:{fontSize:12}, formatter:ps => {
      const first = Array.isArray(ps) ? ps.find(p => Number.isInteger(p.dataIndex)) : null;
      if (!first || !k[first.dataIndex]) return '';
      const i = first.dataIndex, r = k[i], chg = i > 0 ? (r[2]/closes[i-1]-1)*100 : null;
      let text = `<b>${esc(dates[i])}</b><br>开 ${r[1].toFixed(2)} 收 ${r[2].toFixed(2)} (${fmt(chg)})<br>高 ${r[3].toFixed(2)} 低 ${r[4].toFixed(2)}<br>成交量（源值）${vols[i].toLocaleString()}`;
      for (const p of points.filter(p => p.i === i)) text += `<div style="max-width:360px;white-space:normal;margin-top:6px;color:#b45309">${esc(p.date)} · [${esc(labelOf[p.cat] || p.cat || '')}] ${esc(p.title)}${p.shifted ? '（无对应日线，标在前一根K线）' : ''}</div>`;
      return text;
    }},
    grid:[{left:65,right:20,top:22,height:'60%'},{left:65,right:20,top:'77%',height:'14%'}],
    xAxis:[{type:'category',data:dates},{type:'category',gridIndex:1,data:dates,axisLabel:{show:false}}],
    yAxis:[{scale:true,splitLine:{lineStyle:{color:'#eef0f5'}}},{gridIndex:1,splitNumber:2,axisLabel:{formatter:v => v >= 10000 ? (v/10000).toFixed(0) + '万' : v},splitLine:{show:false}}],
    dataZoom:[{type:'inside',xAxisIndex:[0,1],startValue:Math.max(0,k.length-60),endValue:k.length-1},{type:'slider',xAxisIndex:[0,1],bottom:3,height:18}],
    series:[{name:'K线',type:'candlestick',data:ohlc,itemStyle:{color:RED,color0:GREEN,borderColor:RED,borderColor0:GREEN},
      markPoint:{symbol:'pin',symbolSize:30,label:{fontSize:10,formatter:p => p.data.shifted ? '前一根' : '公告'},itemStyle:{color:'#f59e0b'},data:points.map(p => ({coord:[p.anchorDate,k[p.i][3]],name:p.title,shifted:p.shifted}))},
      markLine:{symbol:'none',silent:true,label:{show:false},lineStyle:{color:'#f59e0b',type:'dashed',width:1},data:[...new Set(points.map(p => p.anchorDate))].map(d => ({xAxis:d}))}},
      ...[5,10,20].map((n,i) => ({name:'MA'+n,type:'line',data:ma(n),showSymbol:false,lineStyle:{width:1,color:['#f59e0b','#3b82f6','#a855f7'][i]}})),
      {name:'成交量',type:'bar',xAxisIndex:1,yAxisIndex:1,data:vols,itemStyle:{color:p => k[p.dataIndex][2] >= k[p.dataIndex][1] ? 'rgba(230,52,58,.55)' : 'rgba(10,168,88,.55)'}}]});
  $('chart_hint').textContent = `K线共 ${k.length} 根，默认显示最近60根，可拖动缩放。橙色标记为该股窗口内全部公告（不受分类筛选影响）；“前一根”只表示该日无对应K线，标记落在它之前最近的一根上。日/5日为最近1/5个价格间隔涨跌；窗口首公告以前一根收盘价为基准，并非公告后可交易收益。缺失值为 —。`;
}
function renderQuality() {
  const cov = meta.coverage || {}, run = meta.run || {};
  const counts = meta.counts || {};
  $('header_sub').textContent = `公告窗口 ${(cov.window || {}).start || '—'} ～ ${(cov.window || {}).end || '—'} · 更新 ${meta.generated_at || '未知'} · ${counts.stocks || items.length} 只股票`;
  const messages = [];
  if (!items.length || !meta.generated_at) messages.push('数据文件未完整加载，请刷新页面');
  if (meta.mock) messages.push('模拟数据演示，非真实行情');
  if (meta.generated_at && meta.generated_at.slice(0,10) < todayCN()) messages.push('页面未在北京时间今天更新');
  messages.push(`来源 ${meta.source || '未知'}；逐日分页校验 ${cov.covered_days ?? 0}/${(cov.window || {}).days || '—'} 天；未抓取 ${(cov.missing_days || []).length} 天；不完整 ${(cov.incomplete_days || []).length} 天`);
  if (run.status && run.status !== 'success') messages.push('最近采集状态：' + run.status);
  messages.push('分类为标题规则匹配；参考市值取首次成功值。分页校验不代表交易所全覆盖。');
  $('quality').textContent = messages.join(' · ');
}
// 档位：home = 主板 + 非ST + 近3天（首屏）；其余按需拉 list-<key>.json
function needKey() {
  if (activeBoard === '主板') return (activeRange === '3d' && !showST) ? 'home' : 'main';
  if (activeBoard === '创业板') return 'gem';
  if (activeBoard === '科创板') return 'star';
  if (activeBoard === '北交所') return 'bse';
  return 'all';
}
async function ensureKey() {
  const key = needKey();
  if (key === loadedKey) return;
  const file = key === 'home' ? 'home.json' : 'list-' + key + '.json';
  const obj = await loadJSON(DATA_DIR + file);
  items = Array.isArray(obj.items) ? obj.items : [];
  loadedKey = key;
}
function bindEvents() {
  let searchTimer;
  $('search').addEventListener('input', () => {clearTimeout(searchTimer); searchTimer = setTimeout(renderList, 150);});
  $('boards').onchange = async e => {activeBoard = e.target.value; await ensureKey(); renderFilters(); renderList();};
  $('stswitch').onchange = async e => {showST = e.target.checked; await ensureKey(); renderList();};
  $('dranges').onchange = async e => {activeRange = e.target.value; await ensureKey(); renderList();};
  window.addEventListener('resize', () => {if (chart) chart.resize();});
}
async function boot() {
  try {
    // 首屏固定默认条件：主板 + 近3天 + 非ST（home.json 就是按这个预切好的）。
    const [metaJson, home] = await Promise.all([
      loadJSON(DATA_DIR + 'meta.json'),
      loadJSON(DATA_DIR + 'home.json'),
    ]);
    meta = metaJson || {};
    taxonomy = Array.isArray(meta.taxonomy) ? meta.taxonomy : [];
    labelOf = Object.fromEntries(taxonomy.map(t => [t.id, t.label || t.id]));
    colorOf = Object.fromEntries(taxonomy.map(t => [t.id, /^#[0-9a-f]{6}$/i.test(t.color || '') ? t.color : '#6b7280']));
    items = Array.isArray(home.items) ? home.items : [];
    loadedKey = 'home';
    activeBoard = '主板'; showST = false; activeRange = '3d';
    activeCat = items.some(s => (s[A] || []).some(a => labelOf[a[2]] === '并购重组')) ? '并购重组' : '全部';
    const requested = new URLSearchParams(location.search).get('stock');
    if (requested && items.some(s => String(s[C]) === requested)) {
      activeCode = requested; activeBoard = '全部板块'; activeCat = '全部'; activeRange = ''; showST = true;
    }
    renderQuality();
    renderFilters();
    bindEvents();
    renderList();
  } catch (error) {
    $('header_sub').textContent = '数据加载失败';
    $('quality').textContent = '数据加载失败：' + error.message;
    $('list').innerHTML = `<div class="empty">数据加载失败<br>${esc(error.message)}</div>`;
  }
}
boot();
window.addEventListener('error', e => {console.error(e.error || e.message); const s = $('quality'); if (s) s.textContent = '前端脚本异常：' + (e.message || '未知错误');});
window.addEventListener('unhandledrejection', e => {console.error(e.reason); const s = $('quality'); if (s) s.textContent = '前端加载异常：' + (e.reason?.message || e.reason || '未知错误');});
