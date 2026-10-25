/* Read-only public research UI. No orders, credentials or external assets. */
'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number = value => typeof value === 'number' && Number.isFinite(value);
const fmt = (value, digits = 1) => number(value) ? value.toLocaleString('zh-CN', {minimumFractionDigits:digits, maximumFractionDigits:digits}) : '—';
const pct = value => number(value) ? `${fmt(value * 100)}%` : '—';
const money = value => number(value) ? `${value >= 0 ? '+' : '−'}$${fmt(Math.abs(value), 2)}` : '—';
const time = value => number(value) ? new Date(value * 1000).toLocaleString('zh-CN', {hour12:false}) : '—';
const duration = value => !number(value) ? '—' : value >= 3600 ? `${fmt(value / 3600)}小时` : value >= 60 ? `${fmt(value / 60)}分钟` : `${fmt(value)}秒`;
const tone = value => !number(value) ? '' : value >= 0 ? 'positive-value' : 'negative-value';
const labels = {training:'训练中',insufficient_training:'训练不足',insufficient_test:'验证不足',low_coverage:'覆盖不足',incomplete_shadow:'执行估算不完整',unmarked_inventory:'库存待估值',positive_shadow_sample:'样本外正值',negative_shadow_sample:'样本外负值'};
let state = {live:null, manifest:null, report:null};
let tab = 'ranking';
let descending = true;
let busy = false;

function badge(status) {
  const style = status === 'positive_shadow_sample' ? 'positive' : status === 'negative_shadow_sample' ? 'negative' : 'warning';
  return `<span class="badge ${style}">${esc(labels[status] || status)}</span>`;
}

function marketCell(symbol, inventory = false) {
  return `<div class="symbol-cell"><span class="token">${esc(symbol.slice(0, 2))}</span><div><div class="market-name">${esc(symbol)}</div><div class="market-sub">PERPETUAL${inventory ? ' · 待退出库存' : ''}</div></div></div>`;
}

function setTab(next) {
  tab = next;
  document.querySelectorAll('[data-tab]').forEach(button => button.classList.toggle('active', button.dataset.tab === tab));
  for (const name of ['ranking','live','method']) $(`${name}-section`).hidden = name !== tab;
}
document.querySelectorAll('[data-tab]').forEach(button => button.addEventListener('click', () => setTab(button.dataset.tab)));

function render() {
  const {live,manifest,report} = state;
  const rows = report?.rows || [];
  const args = report?.arguments || manifest?.arguments || {};
  const latency = manifest?.arguments?.latency_ms ?? 350;
  const dataAge = live ? Date.now()/1000 - live.updated_ts : Infinity;
  const connected = live?.state === 'collecting' && dataAge < 30;
  $('connection').classList.toggle('stale', !connected);
  $('connection').textContent = connected ? '公共行情采集中' : live?.state === 'stopped' ? '采集已停止 · 查看历史' : live ? '数据未更新' : '等待采集';
  $('market-count').textContent = fmt(live?.markets?.length || rows.length || 0, 0);
  $('probe-count').textContent = fmt(live?.counts?.usable, 0);
  $('probe-note').textContent = live?.counts ? `总探测 ${fmt(live.counts.probes,0)} · ${pct(live.counts.probes ? live.counts.usable/live.counts.probes : null)} 可用` : '等待公共行情数据';
  $('positive-count').textContent = report ? fmt(rows.filter(r => r.status === 'positive_shadow_sample').length,0) : '—';
  const marked = rows.filter(r => number(r.shadow_net_including_open_usd));
  const total = marked.reduce((sum,r) => sum+r.shadow_net_including_open_usd,0);
  $('shadow-total').textContent = report && marked.length ? money(total) : '—';
  $('shadow-total').className = `kpi-value ${tone(total)}`;
  $('run-label').textContent = manifest ? `${manifest.run_id} · 本机时间 ${new Date().toLocaleTimeString('zh-CN',{hour12:false})}` : '等待采集目录';
  $('report-time').textContent = report ? `分析更新 ${time(report.generated_ts)}` : '尚无分析结果';
  $('analysis-subtitle').textContent = report ? `训练截止 ${time(report.train_end_ts)} · 后续数据独立验证 · 影子完整成交假设` : `先采集 ${fmt(args.train_hours || 24,0)} 小时训练数据；实时盘口可立即查看。`;
  $('survival-heading').textContent = `${fmt(latency,0)}ms 存续`;
  $('live-subtitle').textContent = `每腿目标买入金额 $${fmt(args.notional || 300,0)} · 扣采集费率，不含未知资金费与实盘冲击`;
  renderRanking();
  const liveRows = [...(live?.markets || [])].sort((a,b) => a.symbol.localeCompare(b.symbol));
  $('live-body').innerHTML = liveRows.map(r => `<tr><td>${marketCell(r.symbol)}</td><td>${fmt(r.premium_bps,2)} bps</td><td class="${tone(r.quotes?.buy_rh?.net_bps)}">${fmt(r.quotes?.buy_rh?.net_bps,2)} bps</td><td class="${tone(r.quotes?.sell_rh?.net_bps)}">${fmt(r.quotes?.sell_rh?.net_bps,2)} bps</td><td>${fmt(r.elapsed_ms)}ms</td><td><span class="badge ${r.usable && connected ? 'positive' : 'warning'}">${live?.state === 'stopped' ? '历史快照' : !connected ? '未更新' : r.usable ? '可观测' : '等待有效盘口'}</span></td></tr>`).join('');
  $('live-empty').hidden = liveRows.length > 0;
  const params = [['采集与分析窗口',`${fmt(args.train_hours ?? 24)}小时训练 / 最少${fmt(args.min_test_hours ?? 24)}小时样本外验证`],
    ['目标买入金额',`$${fmt(args.notional || 300,0)} 等值`],['探测间隔 / 延迟',`${fmt(manifest?.arguments?.interval_ms ?? 5000,0)}ms / ${fmt(latency,0)}ms`],
    ['额外执行成本假设',`每次跨市场 ${fmt(args.reserve_bps ?? 2)}bps；往返两次计入`],
    ['持仓成本假设',`${fmt(args.holding_cost_bps_hour ?? 0)}bps/小时（实际资金费未采集）`],
    ['往返目标 / 最长持有',`${fmt(args.cycle_target_bps ?? 2)}bps / ${duration(args.max_hold_seconds ?? 3600)}`],
    ['合约与换算',`规格仍需人工核验；RH报价换算 ${fmt(manifest?.arguments?.rh_quote_usd ?? 1,4)}，LIGHTER ${fmt(manifest?.arguments?.lighter_quote_usd ?? 1,4)}`],
    ['实际成交率','未知。盘口延迟存续率不能用于替代。']];
  $('parameters').innerHTML = params.map(([k,v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
}

function renderRanking() {
  const text = $('search').value.trim().toUpperCase();
  const filter = $('filter').value;
  const rows = (state.report?.rows || []).filter(r => r.symbol.toUpperCase().includes(text) && (filter === 'all' || filter === r.status || filter === 'inventory' && r.open_inventory || filter === 'insufficient' && ['training','insufficient_training','insufficient_test','low_coverage','incomplete_shadow','unmarked_inventory'].includes(r.status)));
  rows.sort((a,b) => (descending ? -1 : 1) * ((a.shadow_net_including_open_usd ?? -1e30) - (b.shadow_net_including_open_usd ?? -1e30)));
  $('ranking-count').textContent = rows.length;
  $('ranking-body').innerHTML = rows.map(r => `<tr>
    <td>${marketCell(r.symbol,r.open_inventory)}</td><td>${badge(r.status)}</td>
    <td><div class="progress"><span>${pct(r.depth_coverage)}</span><span class="progress-track"><span style="width:${Math.max(0,Math.min(100,r.depth_coverage*100))}%"></span></span></div><div class="sub-value">验证 ${fmt(r.test_hours)}h</div></td>
    <td>${pct(r.buy_rh_survival_lower_bound)}<div class="sub-value">买 RH / 卖 RH ${pct(r.sell_rh_survival_lower_bound)}</div></td>
    <td>${fmt(r.shadow_cycles,0)}<div class="sub-value">强退 ${fmt(r.forced_exits,0)} 次</div></td>
    <td class="${tone(r.shadow_net_including_open_usd)}">${money(r.shadow_net_including_open_usd)}<div class="sub-value">已平仓 ${money(r.shadow_closed_usd)}</div></td>
    <td>${duration(r.hold_p95_seconds)}</td><td>${pct(r.best_hour_positive_profit_share)}</td>
    <td><button class="detail-button" data-symbol="${esc(r.symbol)}" aria-label="查看 ${esc(r.symbol)} 详情">↗</button></td></tr>`).join('');
  $('ranking-empty').hidden = rows.length > 0;
  $('ranking-body').querySelectorAll('[data-symbol]').forEach(button => button.addEventListener('click', () => showDetail(button.dataset.symbol)));
}
$('search').addEventListener('input',renderRanking);
$('filter').addEventListener('change',renderRanking);
$('sort-net').addEventListener('click',() => {descending=!descending;$('sort-net').textContent=`影子净结果 ${descending?'↓':'↑'}`;renderRanking();});

function chart(series, key, baseline = null) {
  const points = series.filter(p => number(p[key]) && number(p.ts));
  if (!points.length) return '<div class="detail-description">尚无足够数据绘图。</div>';
  let values = points.map(p=>p[key]);
  if(number(baseline)) values.push(baseline);
  let low=Math.min(...values),high=Math.max(...values);
  const pad=Math.max((high-low)*.12,.1);low-=pad;high+=pad;
  const width=760,height=180,left=48,right=18,top=14,bottom=29;
  const start=points[0].ts,end=Math.max(start+1,points[points.length-1].ts);
  const px=t=>left+(t-start)/(end-start)*(width-left-right),py=y=>top+(high-y)/(high-low)*(height-top-bottom);
  const lines=[0,.5,1].map(r=>{const value=low+(high-low)*r;return `<line x1="${left}" y1="${py(value)}" x2="${width-right}" y2="${py(value)}" stroke="#e6eddf"/><text x="${left-7}" y="${py(value)+3}" text-anchor="end" font-size="9" fill="#92a187">${fmt(value,2)}</text>`;}).join('');
  const paths=[];let segment=[];
  series.forEach(p=>{if(!number(p[key]) || !number(p.ts)){if(segment.length) paths.push(segment);segment=[];}else segment.push(`${px(p.ts)},${py(p[key])}`);});if(segment.length) paths.push(segment);
  return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="历史序列曲线">${lines}${number(baseline)?`<line x1="${left}" y1="${py(baseline)}" x2="${width-right}" y2="${py(baseline)}" stroke="#b4bc92" stroke-dasharray="4,4"/>`:''}${paths.map(path=>`<polyline points="${path.join(' ')}" fill="none" stroke="#507d5b" stroke-width="1.8"/>`).join('')}<text x="${left}" y="${height-8}" font-size="9" fill="#92a187">${esc(time(start))}</text><text x="${width-right}" y="${height-8}" text-anchor="end" font-size="9" fill="#92a187">${esc(time(end))}</text></svg>`;
}

async function showDetail(symbol) {
  $('detail-symbol').textContent=symbol;
  $('detail-content').textContent='读取分析记录…';
  if (!$('detail').open) $('detail').showModal();
  try {
    const response=await fetch(`/api/detail?symbol=${encodeURIComponent(symbol)}`);
    if(!response.ok) throw new Error('该市场尚无完整分析结果');
    const detail=await response.json();
    const row=state.report.rows.find(r=>r.symbol===symbol);
    let cumulative=row.shadow_closed_usd-detail.cycles.reduce((sum,c)=>sum+c.net_usd,0);
    const equity=detail.cycles.map(c=>({ts:c.closed_ts,value:(cumulative+=c.net_usd)}));
    $('detail-content').innerHTML=`<div class="detail-cards"><div><label>影子净结果（含库存）</label><strong class="${tone(row.shadow_net_including_open_usd)}">${money(row.shadow_net_including_open_usd)}</strong></div><div><label>完整往返 / 胜率</label><strong>${fmt(row.shadow_cycles,0)} / ${pct(row.shadow_win_rate)}</strong></div><div><label>采样最大回撤</label><strong>${money(row.sampled_max_drawdown_usd)}</strong></div></div>
      <div class="detail-warning">双腿按延迟后的可见均价完整成交是假设，未模拟排队、单腿失败与冲击。未观测资金费；图中结果不能当作实盘利润。</div>
      <div class="chart-label"><span>样本外基差中枢 · bps</span><span>虚线：冻结训练中线</span></div><div class="chart">${chart(detail.series,'premium_bps',detail.calibration.center_bps)}</div>
      <div class="chart-label"><span>影子已平仓结果累计 · USD 等值</span><span>不含尚未退出库存</span></div><div class="chart">${chart(equity,'value',0)}</div>
      <div class="detail-description">冻结买 RH 阈值 ${fmt(detail.calibration.hurdles_bps?.buy_rh,2)}bps；卖 RH 阈值 ${fmt(detail.calibration.hurdles_bps?.sell_rh,2)}bps。观测中断 ${fmt(row.observation_gaps,0)} 次；无法估算的决策 ${fmt(row.censored_decisions,0)} 次。${row.open_inventory?'当前仍有影子库存待退出。':''}</div>
      <div class="cycle-list"><table><thead><tr><th>退出时间</th><th>方向</th><th>持有时间</th><th>影子净结果</th><th>退出方式</th></tr></thead><tbody>${detail.cycles.slice(-100).reverse().map(c=>`<tr><td>${esc(time(c.closed_ts))}</td><td>${c.direction==='buy_rh'?'买 RH → 反向退出':'卖 RH → 反向退出'}</td><td>${duration(c.hold_seconds)}</td><td class="${tone(c.net_usd)}">${money(c.net_usd)}</td><td>${c.forced?'超时退出':'目标退出'}</td></tr>`).join('')}</tbody></table></div>`;
  }catch(error){$('detail-content').textContent=error.message;}
}
$('close-detail').addEventListener('click',()=> $('detail').close());
$('detail').addEventListener('click',event=>{if(event.target===$('detail')){const r=$('detail').getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)$('detail').close();}});

$('export').addEventListener('click',()=>{
  const rows=state.report?.rows;if(!rows?.length){setTab('live');return;}
  const keys=Object.keys(rows[0]);
  const cell=value=>{let text=String(value??'');if(/^[=+@\-]/.test(text))text="'"+text;return '"'+text.replace(/"/g,'""')+'"';};
  const csv='\ufeff'+[keys.map(cell).join(','),...rows.map(row=>keys.map(key=>cell(row[key])).join(','))].join('\r\n');
  const url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download='market-screening.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
});

async function refresh(){
  if(busy)return;busy=true;
  try{const response=await fetch('/api/status',{cache:'no-store'});if(!response.ok)throw new Error(`读取失败 HTTP ${response.status}`);state=await response.json();$('error-banner').hidden=true;render();}
  catch(error){$('error-banner').textContent=`无法更新数据：${error.message}。请检查网页服务与采集进程。`;$('error-banner').hidden=false;$('connection').textContent='连接中断';$('connection').classList.add('stale');}
  finally{busy=false;}
}
$('refresh').addEventListener('click',refresh);
refresh();setInterval(refresh,5000);
