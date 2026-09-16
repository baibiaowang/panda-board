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
const context=vm.createContext({window:{ANNO_LIST:[],ANNO_META:{},ANNO_KLINE_SHARDS:{shards:16,files:{}},ANNO_TAXONOMY:[],addEventListener(){}},
  document,location:{search:''},URL,URLSearchParams,Date:TestDate,Map,Set,Promise,console,setTimeout:()=>0,clearTimeout(){}});
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
test('UI采用构建清单中的哈希资源',()=>{
  assert.match(source,/manifest\.files/);assert.match(source,/meta\.echarts_url/);
  assert.doesNotMatch(source,/el\.src\s*=\s*'data_kline_'/);
});
test('原始ECharts包可加载并提供图表API',()=>{
  const vendor={window:{},navigator:{userAgent:'Node.js'},console};vm.createContext(vendor);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/lib/echarts.min.js'),'utf8'),vendor);
  assert.equal(typeof vendor.window.echarts.init,'function');
  assert.equal(typeof vendor.window.echarts.version,'string');
  console.log('ECharts runtime version: '+vendor.window.echarts.version);
});
console.log(`${passed} frontend behavior tests passed (not browser rendering verification).`);
