'use strict';
const RED = '#e6343a', GREEN = '#0aa858';
const all = Array.isArray(window.ANNO_LIST) ? window.ANNO_LIST : [];
const meta = window.ANNO_META || {};
const manifest = window.ANNO_KLINE_SHARDS || {};
const taxonomy = window.ANNO_TAXONOMY || [];
const byCode = new Map(all.map(s => [s.code, s]));
const colorByLabel = Object.fromEntries(taxonomy.map(t => [t.label, /^#[0-9a-f]{6}$/i.test(t.color || '') ? t.color : '#6b7280']));
let activeCat = all.some(s => (s.announcements || []).some(a => a.category === '并购重组')) ? '并购重组' : '全部';
let activeBoard = '主板', showST = false, activeRange = '3d', activeCode = null, chart = null;
let visible = [], generation = 0;
const scriptLoads = new Map();
const $ = id => document.getElementById(id);
const esc = v => String(v ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
const known = v => typeof v === 'number' && Number.isFinite(v);
const fmt = v => known(v) ? (v >= 0 ? '+' : '') + v.toFixed(2) + '%' : '—';
const cls = v => !known(v) ? '' : v >= 0 ? 'up' : 'down';
const fmtMv = v => known(v) && v > 0 ? (v >= 1e12 ? (v / 1e12).toFixed(2) + '万亿' : (v / 1e8).toFixed(1) + '亿') : '';
const todayCN = () => new Date(Date.now() + 8 * 3600000).toISOString().slice(0,10);
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
  const stockMatch = s.code.includes(query) || (s.name || '').toLowerCase().includes(query);
  return (s.announcements || []).filter(a => dateMatches(a.date) &&
    (activeCat === '全部' || a.category === activeCat) &&
    (!query || stockMatch || (a.title || '').toLowerCase().includes(query) || (a.category || '').toLowerCase().includes(query)));
}
function filtered() {
  const query = $('search').value.trim().toLowerCase();
  return all.flatMap(s => {
    if (activeBoard !== '全部板块' && (s.board || '未知板块') !== activeBoard) return [];
    if (!showST && s.is_st) return [];
    const anns = matchingAnns(s, query);
    return anns.length ? [{stock:s, anns}] : [];
  }).sort((a,b) => b.anns.at(-1).date.localeCompare(a.anns.at(-1).date) || a.stock.code.localeCompare(b.stock.code));
}
function setOptions(id, values, active) {
  $(id).innerHTML = values.map(([value,label]) => `<option value="${esc(value)}"${value === active ? ' selected' : ''}>${esc(label)}</option>`).join('');
}
function renderFilters() {
  const found = new Set(all.flatMap(s => (s.announcements || []).map(a => a.category)).filter(Boolean));
  const cats = ['全部', ...taxonomy.filter(t => found.has(t.label)).map(t => t.label), ...[...found].filter(x => !taxonomy.some(t => t.label === x))];
  $('cats').innerHTML = [...new Set(cats)].map(c => `<button type="button" class="cat${c === activeCat ? ' active' : ''}" data-cat="${esc(c)}">${esc(c)}</button>`).join('');
  $('cats').querySelectorAll('button').forEach(el => el.onclick = () => {activeCat=el.dataset.cat;renderFilters();renderList();});
  setOptions('boards', [...new Set(['全部板块','主板',...all.map(s => s.board || '未知板块')])].map(b => [b,b]), activeBoard);
  setOptions('dranges', [['','窗口内全部'],['3d','近3天'],['7d','近7天'],['15d','近15天'],['30d','近30天'],['30d+','30天之前']], activeRange);
}
function clearChart(message, retry) {
  if (chart) {chart.dispose();chart=null;}
  $('chart').innerHTML = `<div class="empty"><span>${esc(message)}</span>${retry ? '<button type="button" id="retry_chart" style="margin-left:12px;padding:6px 12px">重试</button>' : ''}</div>`;
  if (retry) $('retry_chart').onclick = retry;
}
function clearSelection() {
  activeCode=null;generation++;
  ['p_nm','p_cd','p_mv','p_px','p_chg'].forEach(id => $(id).textContent='');
  $('p_rsn').textContent='调整筛选条件后选择股票';
  $('p_anns').style.display='none';
  clearChart('无匹配股票');
}
function renderList() {
  visible=filtered();
  const list=$('list');
  if (!visible.length) {list.innerHTML='<div class="empty">无匹配股票</div>';clearSelection();return;}
  list.innerHTML=visible.map(({stock:s,anns}) => {
    const last=anns.at(-1);
    return `<div class="stock${activeCode === s.code ? ' active' : ''}" data-code="${esc(s.code)}" tabindex="0" role="button">
      <div class="row1"><span class="nm">${esc(s.name)}</span><span class="cd">${esc(s.code)}</span><span class="mv" title="首次取得的参考市值，不是实时市值">${fmtMv(s.market_cap)}</span><span class="badge">${esc(s.board)}${s.is_st ? ' · ST' : ''}</span></div>
      <div class="chgs"><span class="chip ${cls(s.chg)}">日 ${fmt(s.chg)}</span><span class="chip ${cls(s.chg_ann)}" title="当前展示窗口内首条公告日前一根收盘价至最新收盘价，与当前筛选条件无关">窗口首公告 ${fmt(s.chg_ann)}</span><span class="chip ${cls(s.chg5)}">5日 ${fmt(s.chg5)}</span></div>
      <div class="rsn"><span style="color:${colorByLabel[last.category] || '#6b7280'}">${esc(last.category)}</span> · ${esc(last.date)} · ${esc(last.title)}</div>
      <div class="rsn">行情日期 ${esc(s.price_date || '暂无')} · 匹配公告 ${anns.length} 条</div></div>`;
  }).join('');
  list.querySelectorAll('.stock').forEach(el => {el.onclick=()=>select(el.dataset.code);el.onkeydown=e=>{if(e.key==='Enter')select(el.dataset.code);};});
  select(visible.some(v=>v.stock.code===activeCode) ? activeCode : visible[0].stock.code);
}
function safeLink(url) {
  try {const u=new URL(url);return u.protocol==='https:' ? u.href : '';} catch (_) {return '';}
}
function loadScript(url, ready) {
  if (ready()) return Promise.resolve();
  // Only same-build relative assets from the generated manifest may execute.
  if (!/^[a-zA-Z0-9_./-]+\.js$/.test(url || '') || url.startsWith('/') || url.includes('..')) return Promise.reject(new Error('资源路径无效'));
  if (scriptLoads.has(url)) return scriptLoads.get(url);
  const task=new Promise((resolve,reject)=>{
    const el=document.createElement('script');let timer;
    const finish=err=>{clearTimeout(timer);el.onload=el.onerror=null;if(err){el.remove();reject(err);}else resolve();};
    el.onload=()=>finish(ready() ? null : new Error('资源内容无效'));
    el.onerror=()=>finish(new Error('资源加载失败'));
    timer=setTimeout(()=>finish(new Error('资源加载超时')),20000);
    el.src=url;document.head.appendChild(el);
  });
  scriptLoads.set(url,task);
  task.catch(()=>scriptLoads.delete(url));
  return task;
}
function select(code) {
  const found=visible.find(v=>v.stock.code===code);
  if (!found) return;
  activeCode=code;const token=++generation;const {stock:s,anns}=found;
  document.querySelectorAll('.stock').forEach(el=>el.classList.toggle('active',el.dataset.code===code));
  $('p_nm').textContent=s.name;$('p_cd').textContent=s.code;
  $('p_mv').textContent=fmtMv(s.market_cap) ? '参考市值 '+fmtMv(s.market_cap) : '';
  $('p_px').textContent=known(s.last_close) ? s.last_close.toFixed(2) : '—';
  $('p_px').style.color=known(s.chg) ? (s.chg>=0?RED:GREEN) : '';
  $('p_chg').textContent=fmt(s.chg);$('p_chg').style.color=$('p_px').style.color;
  const adjustment={qfq:'前复权',none:'不复权',unknown:'旧数据口径未核实'}[s.adjustment] || '口径未知';
  $('p_rsn').textContent=`匹配 ${anns.length} 条公告 · 日线 ${s.price_date || '暂无'} · ${s.price_source || '行情源未知'} / ${adjustment}`;
  $('p_anns').innerHTML=[...anns].reverse().map(a=>{const url=safeLink(a.url);return `<div class="ann-item"><span class="ann-date">${esc(a.date)}</span><span class="ann-title">[${esc(a.category)}] ${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(a.title)}</a>` : esc(a.title)}</span></div>`;}).join('');
  $('p_anns').style.display='block';
  clearChart('K线加载中…');
  const sh=String(Number(code)%(manifest.shards || 16));
  loadScript((manifest.files || {})[sh],()=>Object.prototype.hasOwnProperty.call(window,'ANNO_KLINE_SHARD_'+sh))
    .then(()=>{
      if(token!==generation)return;
      const k=(window['ANNO_KLINE_SHARD_'+sh] || {})[code] || [];
      if(!k.length){clearChart('该股票暂无可用K线；请检查行情日期与采集记录');return;}
      $('p_px').textContent=k.at(-1)[2].toFixed(2);
      return loadScript(meta.echarts_url,()=>!!window.echarts).then(()=>{if(token===generation)drawChart(s,k,anns);});
    }).catch(error=>{if(token===generation)clearChart(error.message+'，请刷新页面或重试',()=>select(code));});
}
function announcementPoints(anns,dates) {
  return anns.flatMap(a=>{
    // Older announcements must never be mislabelled as the first bar.
    if(a.date<dates[0] || a.date>dates.at(-1))return [];
    const i=dates.findIndex(d=>d>=a.date);
    return i<0 ? [] : [{...a,i,anchorDate:dates[i],shifted:dates[i]!==a.date}];
  });
}
function drawChart(s,k,anns) {
  clearChart('');$('chart').innerHTML='';chart=window.echarts.init($('chart'));
  const dates=k.map(r=>r[0]),closes=k.map(r=>r[2]),vols=k.map(r=>r[5]);
  const ohlc=k.map(r=>[r[1],r[2],r[4],r[3]]),points=announcementPoints(anns,dates);
  const ma=n=>closes.map((_,i)=>i<n-1?'-':+(closes.slice(i-n+1,i+1).reduce((a,b)=>a+b,0)/n).toFixed(3));
  chart.setOption({animation:false,axisPointer:{link:[{xAxisIndex:'all'}]},
    tooltip:{trigger:'axis',axisPointer:{type:'cross'},backgroundColor:'rgba(255,255,255,.97)',textStyle:{fontSize:12},formatter:ps=>{
      const first=Array.isArray(ps)?ps.find(p=>Number.isInteger(p.dataIndex)):null;
      if(!first || !k[first.dataIndex])return '';
      const i=first.dataIndex,r=k[i],chg=i>0?(r[2]/closes[i-1]-1)*100:null;
      let text=`<b>${esc(dates[i])}</b><br>开 ${r[1].toFixed(2)} 收 ${r[2].toFixed(2)} (${fmt(chg)})<br>高 ${r[3].toFixed(2)} 低 ${r[4].toFixed(2)}<br>成交量（源值）${vols[i].toLocaleString()}`;
      for(const p of points.filter(p=>p.i===i))text+=`<div style="max-width:360px;white-space:normal;margin-top:6px;color:#b45309">${esc(p.date)} · ${esc(p.title)}${p.shifted?'（无对应日线，标在后一根K线）':''}</div>`;
      return text;
    }},
    grid:[{left:65,right:20,top:22,height:'60%'},{left:65,right:20,top:'77%',height:'14%'}],
    xAxis:[{type:'category',data:dates},{type:'category',gridIndex:1,data:dates,axisLabel:{show:false}}],
    yAxis:[{scale:true,splitLine:{lineStyle:{color:'#eef0f5'}}},{gridIndex:1,splitNumber:2,axisLabel:{formatter:v=>v>=10000?(v/10000).toFixed(0)+'万':v},splitLine:{show:false}}],
    dataZoom:[{type:'inside',xAxisIndex:[0,1],startValue:Math.max(0,k.length-60),endValue:k.length-1},{type:'slider',xAxisIndex:[0,1],bottom:3,height:18}],
    series:[{name:'K线',type:'candlestick',data:ohlc,itemStyle:{color:RED,color0:GREEN,borderColor:RED,borderColor0:GREEN},
      markPoint:{symbol:'pin',symbolSize:30,label:{fontSize:10,formatter:p=>p.data.shifted?'顺延':'公告'},itemStyle:{color:'#f59e0b'},data:points.map(p=>({coord:[p.anchorDate,k[p.i][3]],name:p.title,shifted:p.shifted}))},
      markLine:{symbol:'none',silent:true,label:{show:false},lineStyle:{color:'#f59e0b',type:'dashed',width:1},data:[...new Set(points.map(p=>p.anchorDate))].map(d=>({xAxis:d}))}},
      ...[5,10,20].map((n,i)=>({name:'MA'+n,type:'line',data:ma(n),showSymbol:false,lineStyle:{width:1,color:['#f59e0b','#3b82f6','#a855f7'][i]}})),
      {name:'成交量',type:'bar',xAxisIndex:1,yAxisIndex:1,data:vols,itemStyle:{color:p=>k[p.dataIndex][2]>=k[p.dataIndex][1]?'rgba(230,52,58,.55)':'rgba(10,168,88,.55)'}}]});
  $('chart_hint').textContent=`K线共 ${k.length} 根，默认显示最近60根，可拖动缩放。橙色标记为匹配公告日期；“顺延”只表示该日无对应K线。日/5日为最近1/5个价格间隔涨跌；窗口首公告以前一根收盘价为基准，并非公告后可交易收益。缺失值为 —。`;
}
function renderQuality() {
  const cov=meta.coverage || {},run=meta.run || {};
  $('header_sub').textContent=`公告窗口 ${(cov.window || {}).start || '—'} ～ ${(cov.window || {}).end || '—'} · 更新 ${meta.generated_at || '未知'} · ${all.length} 只股票`;
  const messages=[];
  if(!window.ANNO_LIST || !window.ANNO_META || !window.ANNO_KLINE_SHARDS)messages.push('数据文件未完整加载，请刷新页面');
  if(meta.mock)messages.push('模拟数据演示，非真实行情');
  if(meta.generated_at && meta.generated_at.slice(0,10)<todayCN())messages.push('页面未在北京时间今天更新');
  messages.push(`来源 ${meta.source || '未知'}；逐日分页校验 ${cov.covered_days ?? 0}/${(cov.window || {}).days || '—'} 天；未抓取 ${(cov.missing_days || []).length} 天；不完整 ${(cov.incomplete_days || []).length} 天`);
  if(run.status && run.status!=='success')messages.push('最近采集状态：'+run.status);
  messages.push('分类为标题规则匹配；参考市值取首次成功值。分页校验不代表交易所全覆盖。');
  $('quality').textContent=messages.join(' · ');
}
renderQuality();renderFilters();
let searchTimer;
$('search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(renderList,150);});
$('boards').onchange=e=>{activeBoard=e.target.value;renderList();};
$('stswitch').onchange=e=>{showST=e.target.checked;renderList();};
$('dranges').onchange=e=>{activeRange=e.target.value;renderList();};
window.addEventListener('resize',()=>{if(chart)chart.resize();});
const requested=new URLSearchParams(location.search).get('stock');
if(byCode.has(requested)){activeCode=requested;activeBoard='全部板块';activeCat='全部';activeRange='';showST=true;$('stswitch').checked=true;renderFilters();}
renderList();
