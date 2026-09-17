/* Node-only behavior tests. A minimal DOM double is not a browser layout test. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../web/dashboard.js'),'utf8');
const elements=new Map();
const element=id=>{
  if(!elements.has(id))elements.set(id,{id,value:'',innerHTML:'',textContent:'',style:{},checked:false,
    querySelectorAll:()=>[],addEventListener(){},classList:{toggle(){}},remove(){}});
  return elements.get(id);
};
const document={getElementById:element,querySelectorAll:()=>[],head:{appendChild(){}},createElement:()=>element('script')};
class TestDate extends Date { static now(){return Date.parse('2026-09-15T00:00:00Z');} }
// 站点固定化（2026-09-16）后 dashboard.js 改为运行时 fetch data/*.json，
// 不再依赖 window.ANNO_* 内嵌全局变量。这里给一个最小 fetch 替身：
// 只要返回 {ok,status,json()} 形状，让 boot() 能跑完不抛即可。
// 同时把请求过的 URL 记下来，供「K线分片走清单文件名」那条用例断言。
const fetchCalls=[];
const fetchStub=url=>{
  fetchCalls.push(url);
  const body=url.includes('stocks')?[]:url.includes('manifest')?{shards:16,files:{}}:url.includes('taxonomy')?[]:{};
  return Promise.resolve({ok:true,status:200,json:()=>Promise.resolve(body)});
};
const context=vm.createContext({window:{addEventListener(){}},
  document,fetch:fetchStub,location:{search:''},URL,URLSearchParams,Date:TestDate,Map,Set,Promise,console,setTimeout:()=>0,clearTimeout(){}});
vm.runInContext(source,context);
let passed=0;
function test(name,fn){fn();passed++;console.log('PASS '+name);}
const run=code=>vm.runInContext(code,context);
const plain=value=>JSON.parse(JSON.stringify(value));
test('近3天包含今天且不多算一天',()=>assert.equal(run("cutoff(3,'2026-09-15')"),'2026-09-13'));
test('未知涨跌幅不显示成0%',()=>assert.equal(run('fmt(null)'),'—'));
test('数值0可正常显示',()=>assert.equal(run('fmt(0)'),'+0.00%'));
test('公告标题HTML转义',()=>assert.equal(run('esc("<img src=x onerror=alert(1)>")'),'&lt;img src=x onerror=alert(1)&gt;'));
test('原文链接拒绝javascript',()=>assert.equal(run('safeLink("javascript:alert(1)")'),''));
test('原文链接拒绝明文http',()=>assert.equal(run('safeLink("http://example.com")'),''));
test('窗口外旧公告不能标在第一根K线',()=>assert.deepEqual(plain(run("announcementPoints([{date:'2026-08-01',title:'旧公告'}],['2026-09-01','2026-09-02'])")),[]));
test('非交易日公告标记顺延但不伪称停牌',()=>{
  const p=plain(run("announcementPoints([{date:'2026-09-06',title:'周日公告'}],['2026-09-04','2026-09-07'])"));
  assert.equal(p[0].anchorDate,'2026-09-07');assert.equal(p[0].shifted,true);
});
test('类别和日期必须命中同一条公告',()=>{
  run("activeCat='并购重组';activeRange='3d'");
  const value=run("matchingAnns({code:'600000',name:'测试',announcements:[{date:'2026-09-01',category:'并购重组',title:'收购'},{date:'2026-09-15',category:'立案/处罚',title:'立案'}]})");
  assert.equal(value.length,0);
});
test('搜索可命中范围内非最新标题',()=>{
  run("activeCat='全部';activeRange=''");
  assert.equal(run("matchingAnns({code:'600000',name:'测试',announcements:[{date:'2026-09-01',category:'并购重组',title:'关键词收购'},{date:'2026-09-15',category:'立案/处罚',title:'立案'}]},'关键词').length"),1);
});
test('ST过滤对所有板块生效',()=>{
  run("all.push({code:'300001',name:'ST测试',board:'创业板',is_st:true,announcements:[{date:'2026-09-15',category:'并购重组',title:'测试'}]});activeBoard='创业板';showST=false");
  assert.equal(run('filtered().length'),0);
  run('showST=true');assert.equal(run('filtered().length'),1);
});
test('全部板块可选且不会排除创业板',()=>{run("activeBoard='全部板块'");assert.equal(run('filtered().length'),1);});
test('无匹配股票清空选中状态和旧图表',()=>{
  run("activeCode='300001';activeCat='不存在';renderList()");
  assert.equal(run('activeCode'),null);assert.match(element('chart').innerHTML,/无匹配股票/);
});
test('UI走数据层协议：fetch + 常量资源路径',()=>{
  assert.equal(run('DATA_DIR'),'data/');
  assert.equal(run('ECHARTS_URL'),'lib/echarts.min.js');
  assert.match(source,/fetch\(path/);
  assert.match(source,/manifest\.files/);
  // 旧协议已废弃：内嵌全局变量、清单里的 echarts_url 字段、哈希分片文件名
  assert.doesNotMatch(source,/meta\.echarts_url/);
  assert.doesNotMatch(source,/ANNO_KLINE_SHARDS/);
  assert.doesNotMatch(source,/el\.src\s*=\s*'data_kline_'/);
});
test('loadJSON 对同一路径只发一次请求',()=>{
  const first=run("loadJSON(DATA_DIR+'kline_9.json')");
  const second=run("loadJSON(DATA_DIR+'kline_9.json')");
  assert.equal(first,second);
});
test('K线分片按清单里的文件名取数据',()=>{
  run("manifest={shards:16,files:{'3':'kline_3.json'}}");
  run("loadKline('3')");
  assert.ok(fetchCalls.includes('data/kline_3.json'),'应请求 data/kline_3.json，实际：'+fetchCalls.join(','));
});
test('原始ECharts包可加载并提供图表API',()=>{
  const vendor={window:{},navigator:{userAgent:'Node.js'},console};vm.createContext(vendor);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/lib/echarts.min.js'),'utf8'),vendor);
  assert.equal(typeof vendor.window.echarts.init,'function');
  assert.equal(typeof vendor.window.echarts.version,'string');
  console.log('ECharts runtime version: '+vendor.window.echarts.version);
});
console.log(`${passed} frontend behavior tests passed (not browser rendering verification).`);
