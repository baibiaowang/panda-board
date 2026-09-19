/* Node-only behavior tests. A minimal DOM double is not a browser layout test.
 *
 * 数据层 v2（2026-09-19）：首屏只拉 meta.json + home.json；切板块/时间按需拉
 * list-<key>.json；点股票才拉 stock/<前2位>/<code>.json（K 线并入单股文件）。
 * 条目是 13 元数组 [c,n,b,st,cat,cap,p,ch,ch5,cha,d,lu,a]，a 内公告是三元、不含 URL。
 */
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
// 按 v2 契约回形状（items 留空，避免 boot() 的渲染污染单测状态），
// 同时记录请求过的 URL，供「首屏只拉两个文件」那条用例断言。
const fetchCalls=[];
const fetchStub=url=>{
  fetchCalls.push(url);
  let body={};
  if(url.endsWith('meta.json')) body={v:2,generated_at:'2026-09-15T09:00:00',source:'mock',boards:['主板','创业板','科创板','北交所'],
    counts:{stocks:0},coverage:{window:{start:'2026-09-13',end:'2026-09-15',days:3},covered_days:3,missing_days:[],incomplete_days:[]},
    taxonomy:[{id:'merger',label:'并购重组',color:'#2563eb'},{id:'penalty',label:'立案/处罚',color:'#e6343a'}]};
  else if(url.endsWith('home.json')) body={v:2,key:'home',range_days:3,count:0,items:[]};
  else if(url.includes('list-')) body={v:2,key:'main',count:0,items:[]};
  else if(url.includes('stock/')) body={v:2,c:'600000',n:'测试',b:'主板',st:0,k:[],a:[]};
  return Promise.resolve({ok:true,status:200,json:()=>Promise.resolve(body)});
};
const context=vm.createContext({window:{addEventListener(){}},
  document,fetch:fetchStub,location:{search:''},URL,URLSearchParams,Date:TestDate,Map,Set,Promise,console,setTimeout:()=>0,clearTimeout(){}});
vm.runInContext(source,context);
const run=code=>vm.runInContext(code,context);
const plain=value=>JSON.parse(JSON.stringify(value));
let passed=0;
function test(name,fn){fn();passed++;console.log('PASS '+name);}
// 13 元条目：[c,n,b,st,cat,cap,p,ch,ch5,cha,d,lu,a]
const item=(code,name,board,isST,catID,anns)=>[code,name,board,isST?1:0,catID,1e9,10,1,2,3,'2026-09-15','',
  anns.map(a=>[a.date,a.title,a.cat])];
const ann=(date,title,cat)=>({date,title,cat});
const arg=value=>JSON.stringify(value);

(async () => {
  // boot() 的 fetch 是微任务链，先让它跑完，meta/taxonomy/labelOf 才是真的。
  await new Promise(resolve=>setImmediate(resolve));

  test('首屏只拉 meta.json + home.json 两个文件',()=>{
    assert.deepEqual(fetchCalls.slice(),['data/meta.json','data/home.json']);
    assert.ok(!fetchCalls.some(u=>/stocks\.json|kline_\d+\.json|kline_manifest/.test(u)),
      '首屏不应再拉 v1 数据文件，实际：'+fetchCalls.join(','));
    assert.equal(run('loadedKey'),'home');
    assert.equal(run('activeBoard'),'主板');
    assert.equal(run('activeRange'),'3d');
    assert.equal(run('showST'),false);
  });
  test('taxonomy 从 meta.json 内联加载',()=>{
    assert.equal(run("labelOf['merger']"),'并购重组');
    assert.equal(run("colorOf['penalty']"),'#e6343a');
  });
  test('近3天包含今天且不多算一天',()=>assert.equal(run("cutoff(3,'2026-09-15')"),'2026-09-13'));
  test('未知涨跌幅不显示成0%',()=>assert.equal(run('fmt(null)'),'—'));
  test('数值0可正常显示',()=>assert.equal(run('fmt(0)'),'+0.00%'));
  test('公告标题HTML转义',()=>assert.equal(run('esc("<img src=x onerror=alert(1)>")'),'&lt;img src=x onerror=alert(1)&gt;'));
  test('原文链接拒绝javascript',()=>assert.equal(run('safeLink("javascript:alert(1)")'),''));
  test('原文链接拒绝明文http',()=>assert.equal(run('safeLink("http://example.com")'),''));
  test('窗口外旧公告不能标在第一根K线',()=>assert.deepEqual(
    plain(run("announcementPoints([['2026-08-01','旧公告','merger','']],['2026-09-01','2026-09-02'])")),[]));
  test('非交易日公告标在前一根K线上',()=>{
    // 口径：全项目统一用「公告日之前最近的一根」。09-06（周日）→ 09-04（周五）。
    const p=plain(run("announcementPoints([['2026-09-06','周日公告','merger','']],['2026-09-04','2026-09-07'])"));
    assert.equal(p[0].anchorDate,'2026-09-04');assert.equal(p[0].shifted,true);
  });
  test('四元公告（带URL）也能标注',()=>{
    const p=plain(run("announcementPoints([['2026-09-15','收购','merger','https://example.com/a.pdf']],['2026-09-15'])"));
    assert.equal(p[0].i,0);assert.equal(p[0].title,'收购');
  });
  test('类别和日期必须命中同一条公告',()=>{
    run("activeCat='并购重组';activeRange='3d'");
    const v=plain(run(`matchingAnns(${arg(item('600000','测试','主板',false,'merger',
      [ann('2026-09-01','收购','merger'),ann('2026-09-15','立案','penalty')]))})`));
    assert.equal(v.length,0);
  });
  test('搜索可命中窗口内非最新标题',()=>{
    run("activeCat='全部';activeRange=''");
    const v=plain(run(`matchingAnns(${arg(item('600000','测试','主板',false,'merger',
      [ann('2026-09-01','关键词收购','merger'),ann('2026-09-15','立案','penalty')]))},'关键词')`));
    assert.equal(v.length,1);
  });
  test('ST过滤对所有板块生效',()=>{
    run(`items=${arg([item('300001','ST测试','创业板',true,'merger',[ann('2026-09-15','测试','merger')])])}`);
    run("activeBoard='创业板';showST=false;activeCat='全部';activeRange='3d'");
    assert.equal(run('filtered().length'),0);
    run('showST=true');assert.equal(run('filtered().length'),1);
  });
  test('全部板块可选且不会排除创业板',()=>{run("activeBoard='全部板块'");assert.equal(run('filtered().length'),1);});
  test('无匹配股票清空选中状态和旧图表',()=>{
    run("activeCode='300001';activeCat='不存在';renderList()");
    assert.equal(run('activeCode'),null);assert.match(element('chart').innerHTML,/无匹配股票/);
  });
  test('档位按板块/时间/ST按需切换',()=>{
    run("activeBoard='主板';activeRange='3d';showST=false");assert.equal(run('needKey()'),'home');
    run("activeRange='7d'");assert.equal(run('needKey()'),'main');
    run("activeRange='3d';showST=true");assert.equal(run('needKey()'),'main');
    run("activeBoard='创业板';showST=false");assert.equal(run('needKey()'),'gem');
    run("activeBoard='科创板'");assert.equal(run('needKey()'),'star');
    run("activeBoard='北交所'");assert.equal(run('needKey()'),'bse');
    run("activeBoard='全部板块'");assert.equal(run('needKey()'),'all');
  });
  test('单股详情走二级目录',()=>assert.equal(run("stockPath('600000')"),'data/stock/60/600000.json'));
  test('公告面板用四元公告渲染出原文链接',()=>{
    run("activeCat='全部'");
    run("renderAnnPanel([['2026-09-15','收购公告','merger','https://example.com/a.pdf']])");
    const html=element('p_anns').innerHTML;
    assert.match(html,/href="https:\/\/example\.com\/a\.pdf"/);
    assert.match(html,/收购公告/);
  });
  test('档位三元公告没有链接时不渲染死链',()=>{
    run("renderAnnPanel([['2026-09-15','无链接公告','merger']])");
    const html=element('p_anns').innerHTML;
    assert.doesNotMatch(html,/<a /);
    assert.match(html,/无链接公告/);
  });
  test('UI走数据层 v2 协议：fetch + 档位按需加载',()=>{
    assert.equal(run('DATA_DIR'),'data/');
    assert.equal(run('ECHARTS_URL'),'lib/echarts.min.js');
    assert.match(source,/fetch\(path/);
    assert.match(source,/stockPath/);
    // 旧协议已废弃：内嵌全局变量、K线分片、清单文件、哈希资源名
    assert.doesNotMatch(source,/ANNO_KLINE_SHARDS/);
    assert.doesNotMatch(source,/meta\.echarts_url/);
    assert.doesNotMatch(source,/loadKline/);
    // ★ 面板与 K 线标注必须用单股详情里的全窗口公告（detail.a）：
    //   用档位数组（s[A]）会让原文链接全丢、标注范围缩到当前档位。
    assert.match(source,/windowAnns/);
    assert.doesNotMatch(source,/allAnns/);
  });
  test('loadJSON 对同一路径只发一次请求',()=>{
    const first=run("loadJSON(DATA_DIR+'meta.json')");
    const second=run("loadJSON(DATA_DIR+'meta.json')");
    assert.equal(first,second);
  });
  test('原始ECharts包可加载并提供图表API',()=>{
    const vendor={window:{},navigator:{userAgent:'Node.js'},console};vm.createContext(vendor);
    vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/lib/echarts.min.js'),'utf8'),vendor);
    assert.equal(typeof vendor.window.echarts.init,'function');
    assert.equal(typeof vendor.window.echarts.version,'string');
    console.log('ECharts runtime version: '+vendor.window.echarts.version);
  });
  console.log(`${passed} frontend behavior tests passed (not browser rendering verification).`);
})();
