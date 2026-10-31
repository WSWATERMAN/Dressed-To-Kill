const $ = (s) => document.querySelector(s);
let markets = [], selected = null, portfolio = null, pending = null, marketRefreshTimer = null, marketVisibleCount = 100;
const money = (n) => n == null ? '—' : new Intl.NumberFormat('en-US',{style:'currency',currency:'USD'}).format(n);
const pct = (n) => n == null ? '—' : `${(n*100).toFixed(0)}¢`;
const rate = (n) => n == null ? '—' : `${(n*100).toFixed(1)}%`;
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const date = (s) => { if (!s) return 'Not provided'; const d=new Date(s); return Number.isNaN(+d)?'Not provided':d.toLocaleString(); };

async function loadMarkets() {
  if(marketRefreshTimer){clearTimeout(marketRefreshTimer);marketRefreshTimer=null;}
  markets=[]; marketVisibleCount=100;
  $('#notice').textContent=''; $('#source').textContent='LOADING MARKET DATA…'; $('#market-list').innerHTML='<div class="empty">Loading market data…</div>';
  try {
    const res=await fetch(`/api/markets/progress?status=${encodeURIComponent($('#status').value)}&offset=0`);
    const data=await res.json(); if(!res.ok) throw new Error(data.error||'Market request failed');
    markets=data.markets;
    if(data.loading){
      $('#source').textContent='LOADING MARKET DATA…'; $('#market-count').textContent=markets.length||'…';
      $('#updated').textContent=markets.length?`Loaded ${markets.length} markets; continuing pagination…`:'Fetching demo market pages…';
      if(markets.length)renderMarkets();else $('#market-list').innerHTML='<div class="empty">Fetching demo market pages…</div>';
      marketRefreshTimer=setTimeout(pollMarketProgress,500); return;
    }
    $('#source').textContent=data.source==='mock'?'MOCK DATA':'KALSHI DEMO DATA';
    $('#updated').textContent=`Updated ${new Date().toLocaleTimeString()}`; $('#market-count').textContent=markets.length;
    renderMarkets(); if(selected) renderDetail(selected);
  } catch(e) { $('#source').textContent='MARKET DATA UNAVAILABLE'; $('#market-list').innerHTML='<div class="empty">Market data could not be loaded.<br><button class="button secondary" id="retry-markets" type="button">Retry</button></div>'; $('#retry-markets').onclick=loadMarkets; $('#notice').textContent=e.message; }
}
async function pollMarketProgress() {
  marketRefreshTimer=null;
  const status=encodeURIComponent($('#status').value);
  try {
    const res=await fetch(`/api/markets/progress?status=${status}&offset=${markets.length}`);
    const data=await res.json(); if(!res.ok)throw new Error(data.error||'Market request failed');
    if(data.markets.length){markets.push(...data.markets);renderMarkets();if(selected)renderDetail(selected);}
    $('#market-count').textContent=markets.length;
    if(data.loading){
      $('#source').textContent='LOADING MARKET DATA…';
      $('#updated').textContent=`Loaded ${markets.length} markets; continuing pagination…`;
      marketRefreshTimer=setTimeout(pollMarketProgress,750); return;
    }
    $('#source').textContent=data.source==='mock'?'MOCK DATA':'KALSHI DEMO DATA';
    $('#updated').textContent=`Loaded ${markets.length} markets · ${new Date().toLocaleTimeString()}`;
    renderMarkets(); if(selected)renderDetail(selected);
  } catch(e) {
    $('#source').textContent='MARKET DATA UNAVAILABLE';
    $('#market-list').innerHTML=`<div class="empty">${markets.length} markets loaded; remaining data unavailable.<br><button class="button secondary" id="retry-markets" type="button">Retry</button></div>`;
    $('#retry-markets').onclick=loadMarkets; $('#notice').textContent=e.message;
  }
}
function renderMarkets() {
  const q=$('#search').value.toLowerCase(); const rows=markets.filter(m=>`${m.title} ${m.ticker} ${m.category}`.toLowerCase().includes(q));
  const visible=rows.slice(0,marketVisibleCount);
  const emptyMessage=$('#source').textContent==='LOADING MARKET DATA…'?'No loaded markets match yet; more are being retrieved.':'No markets match this search.';
  $('#market-list').innerHTML=visible.length?visible.map(m=>`<article class="market-card ${selected?.ticker===m.ticker?'selected':''}" data-ticker="${esc(m.ticker)}"><div><span class="tag">${esc(m.category)} · ${esc(m.status)}</span><h3>${esc(m.title)}</h3><small>${esc(m.ticker)} · Closes ${esc(date(m.close_time))}</small></div><div class="prices"><div class="quote"><label>YES BID</label><b>${pct(m.yes_bid)}</b></div><div class="quote"><label>YES ASK</label><b>${pct(m.yes_ask)}</b></div></div></article>`).join(''):`<div class="empty">${emptyMessage}</div>`;
  if(rows.length>visible.length){$('#market-list').insertAdjacentHTML('beforeend',`<button class="button secondary show-more-markets" type="button">Show more (${rows.length-visible.length} remaining)</button>`);$('.show-more-markets').onclick=()=>{marketVisibleCount+=100;renderMarkets();};}
  document.querySelectorAll('.market-card').forEach(card=>card.onclick=()=>{selected=markets.find(m=>m.ticker===card.dataset.ticker);renderMarkets();renderDetail(selected);});
}
function renderDetail(m) {
 if(!m)return;
 const yesDefault=validContractPrice(m.yes_ask)?m.yes_ask:0.5;
 const noDefault=validContractPrice(m.no_ask)?m.no_ask:0.5;
 $('#detail').innerHTML=`<span class="tag">${esc(m.category)} · ${esc(m.source)} source</span><h2>${esc(m.title)}</h2><div class="metric-row"><span>Market ticker</span><b>${esc(m.ticker)}</b></div><div class="metric-row"><span>Status</span><b>${esc(m.status)}</b></div><div class="metric-row"><span>Closes · local time</span><b>${esc(date(m.close_time))}</b></div><div class="metric-row"><span>Volume · contracts</span><b>${m.volume==null?'—':esc(m.volume)}</b></div><div class="metric-row"><span>Open interest · contracts</span><b>${m.open_interest==null?'—':esc(m.open_interest)}</b></div><h4>Settlement rules</h4><p>${esc(m.rules)}</p><div class="trade"><h4>Practice a paper order</h4><div class="trade-controls"><select id="side"><option value="yes">Buy Yes</option><option value="no">Buy No</option></select><input id="quantity" type="number" min="1" max="1000" value="10" aria-label="Contracts"></div><div class="trade-controls" style="margin-top:7px"><input id="price" type="number" min="0.01" max="0.99" step="0.01" value="${yesDefault.toFixed(2)}" aria-label="Price in dollars"><span class="muted" style="align-self:center">USD per contract</span></div><label class="fee-select-label" for="fee-type">Fee estimate type</label><select id="fee-type" class="fee-select"><option value="taker">Taker · 7% formula</option><option value="maker">Maker · 1.75% formula</option></select><p id="fee-preview" class="fine">Estimated fee and total risk will appear here.</p><button class="button primary" id="review-order">Review paper order</button><p class="fine">Paper simulation only. Actual maker/taker fills and special market fee schedules may differ or be unavailable.</p></div>`;
 $('#review-order').onclick=reviewOrder;
 ['#side','#quantity','#price','#fee-type'].forEach(sel=>$(sel).addEventListener('input',updateFeePreview)); updateFeePreview();
 $('#side').onchange=()=>{const quote=$('#side').value==='yes'?m.yes_ask:m.no_ask;$('#price').value=(validContractPrice(quote)?quote:0.5).toFixed(2);updateFeePreview();};
}
function validContractPrice(value){let n=Number(value);if(!Number.isFinite(n))return false;if(Number.isInteger(n)&&n>1)n/=100;return n>=0.01&&n<=0.99;}
function reviewOrder() {
 const side=$('#side').value, quantity=Number($('#quantity').value), price=Number($('#price').value), fee_type=$('#fee-type').value;
 if(!Number.isInteger(quantity)||quantity<1||quantity>1000||!Number.isFinite(price)||price<=0||price>=1){$('#notice').textContent='Enter 1–1,000 contracts and a price between $0.01 and $0.99.';return;}
 const estimate=feeEstimate(selected.ticker,price,quantity,fee_type); const cost=Math.round(quantity*price*100)/100, fee=estimate.fee??0, total=cost+fee; if(total>portfolio.cash){$('#notice').textContent='Not enough simulated cash for this paper order.';return;}
 pending={ticker:selected.ticker,title:selected.title,side,quantity,price,fee_type,client_order_id:(globalThis.crypto?.randomUUID?globalThis.crypto.randomUUID():`${Date.now()}-${Math.random().toString(16).slice(2)}`)}; $('#order-summary').innerHTML=`<b>${esc(selected.title)}</b><br>${side.toUpperCase()} · ${quantity} contracts × ${money(price)}<br>Contract cost: ${money(cost)}<br>${esc(fee_type)} fee estimate: ${estimate.fee==null?'Unavailable':money(fee)}<br>Total maximum amount at risk: <b>${money(total)}</b><br><small>${esc(estimate.note)}</small><br>Settlement terms: ${esc(selected.rules)}`;
 $('#confirm').showModal();
}
$('#confirm-order').onclick=async(e)=>{e.preventDefault();if(!pending)return;const res=await fetch('/api/paper-orders',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(pending)});const d=await res.json();if(!res.ok){$('#confirm').close();$('#notice').textContent=d.error;return;}portfolio=d.portfolio;pending=null;$('#confirm').close();renderPortfolio();$('#notice').textContent='Paper order recorded in simulated account.';};
async function loadPortfolio(){try{const res=await fetch('/api/portfolio',{cache:'no-store'});portfolio=await res.json();if(!res.ok)throw new Error(portfolio.error||'Portfolio unavailable');renderPortfolio();}catch(e){$('#portfolio').innerHTML=`<div class="notice">${esc(e.message)}</div>`;}}
function renderPortfolio(){
 if(!portfolio)return;
 $('#cash').textContent=money(portfolio.cash);$('#order-count').textContent=portfolio.orders.length;
 const tracking=portfolio.tracking_enabled
  ?'<div class="tracking-status enabled">Settlement tracking is active. The app checks unresolved paper positions against Kalshi demo about every 5 minutes while it is running.</div>'
  :'<div class="tracking-status">Outcome tracking is paused in mock mode. Set <code>KALSHI_PROVIDER=kalshi_demo</code> to check Kalshi demo market settlements. Mock markets do not resolve.</div>';
 const valuationNote=portfolio.valuation_complete
  ?'<p class="valuation-note">Open-position values are estimates using the latest available side-specific bid. Unrealized P&amp;L includes the recorded opening cost and fee.</p>'
  :`<p class="valuation-note">Estimates are incomplete: ${portfolio.valuation_missing_count} open or review position(s) lack a usable quote. Aggregate open value, unrealized P&amp;L, and total value are unavailable.</p>`;
 const realizedClass=portfolio.realized_pnl>=0?'positive':'negative';
 const unrealizedClass=(portfolio.estimated_unrealized_pnl??0)>=0?'positive':'negative';
 const analytics=portfolio.analytics||{};
 const averageClass=(analytics.average_realized_pnl??0)>=0?'positive':'negative';
 const items=portfolio.orders.map(o=>{
  const label=o.outcome==='won'?'Won':o.outcome==='lost'?'Lost':o.outcome==='review_required'?'Review settlement':'Pending';
  const pnl=o.outcome==='pending'
   ?(o.estimated_unrealized_pnl==null?'Unrealized estimate unavailable':`${money(o.estimated_unrealized_pnl)} estimated unrealized P&amp;L`)
   :(o.realized_pnl==null?'Awaiting settlement':`${money(o.realized_pnl)} net realized P&amp;L`);
  const estimate=o.outcome==='pending'
   ?(o.estimated_current_value==null?'Current position value estimate unavailable':`Estimated current value ${money(o.estimated_current_value)} · Bid ${pct(o.estimated_mark_price)}`)
   :(o.outcome==='review_required'?'Valuation unavailable pending settlement review':'Position settled');
  return `<article class="portfolio-item"><div class="portfolio-main"><div><span class="outcome ${esc(o.outcome)}">${label}</span><h3>${esc(o.title)}</h3><small>${esc(o.ticker)} · ${esc(o.side.toUpperCase())} · ${esc(o.quantity)} contracts · Placed ${esc(date(o.created_at))}</small></div><div class="portfolio-numbers"><b>${pnl}</b><small>${estimate}<br>Cost ${money(o.contract_cost)} · Fee ${o.fee_status==='estimated'?money(o.fee):'unavailable'} · At risk ${money(o.max_loss)}</small></div></div><div class="position-foot"><span>Payout ${o.payout==null?'—':money(o.payout)} · Fee model: ${esc(o.fee_type)}</span><span>Market status: ${esc(o.market_status)}${o.result?` · result ${esc(o.result.toUpperCase())}`:''} · ${o.last_checked_at?`Checked ${esc(date(o.last_checked_at))}`:'Waiting for first demo check'}</span></div><p class="fee-note">${esc(o.fee_note||'Fee unavailable for legacy order.')}</p>${o.tracking_note?`<p class="tracking-note">${esc(o.tracking_note)}</p>`:''}</article>`;
 }).join('');
 $('#portfolio').innerHTML=`${tracking}<h3 class="portfolio-heading">Portfolio analytics</h3><p class="valuation-note">Metrics use saved paper positions. Settled Yes/No positions determine win rate and prediction accuracy; review-required positions remain open and are excluded.</p><div class="portfolio-summary portfolio-analytics"><div><small>WIN RATE</small><b>${rate(analytics.win_rate)}</b></div><div><small>SETTLED POSITIONS</small><b>${analytics.settled_positions??0}</b></div><div><small>OPEN POSITIONS</small><b>${analytics.open_positions??0}</b></div><div><small>AVERAGE REALIZED P&amp;L</small><b class="${averageClass}">${money(analytics.average_realized_pnl)}</b></div><div><small>TOTAL REALIZED P&amp;L</small><b class="${realizedClass}">${money(analytics.total_realized_pnl??portfolio.realized_pnl)}</b></div><div><small>PREDICTION ACCURACY</small><b>${rate(analytics.prediction_accuracy)}</b></div></div><div class="export-actions" aria-label="Download portfolio CSV files"><a href="/api/portfolio/export?type=positions" download>Export positions CSV</a><a href="/api/portfolio/export?type=settlements" download>Export settlements CSV</a><a href="/api/portfolio/export?type=realized-pnl" download>Export realized P&amp;L CSV</a></div><h3 class="portfolio-heading">Portfolio value</h3><div class="portfolio-summary"><div><small>ESTIMATED TOTAL VALUE</small><b>${money(portfolio.estimated_total_value)}</b></div><div><small>ESTIMATED OPEN POSITION VALUE</small><b>${money(portfolio.estimated_open_position_value)}</b></div><div><small>ESTIMATED UNREALIZED P&amp;L</small><b class="${unrealizedClass}">${money(portfolio.estimated_unrealized_pnl)}</b></div><div><small>REALIZED P&amp;L</small><b class="${realizedClass}">${money(portfolio.realized_pnl)}</b></div><div><small>SIMULATED CASH</small><b>${money(portfolio.cash)}</b></div><div><small>AWAITING SETTLEMENT</small><b>${portfolio.pending_count}</b></div></div>${valuationNote}<h3 class="portfolio-heading">Paper positions <span>${portfolio.orders.length}</span></h3>${items||'<div class="empty">No paper orders yet. Select a market and review a simulated order to start.</div>'}`;
}
function feeEstimate(ticker,price,quantity,type){if(/^(INX|NASDAQ100)/i.test(ticker))return{fee:null,note:'Special product schedule is not modeled.'};const rate=type==='maker'?0.0175:0.07;const raw=rate*quantity*price*(1-price);return{fee:Math.ceil((raw-1e-10)*100)/100,note:`Estimated ${type} fee; actual fill and market schedule may differ.`};}
function updateFeePreview(){if(!selected||!$('#fee-preview'))return;const q=Number($('#quantity').value),p=Number($('#price').value),t=$('#fee-type').value;if(!Number.isInteger(q)||q<1||q>1000||!Number.isFinite(p)||p<=0||p>=1){$('#fee-preview').textContent='Enter a valid quantity and price to estimate fees.';return;}const est=feeEstimate(selected.ticker,p,q,t),cost=Math.round(q*p*100)/100;$('#fee-preview').textContent=`Contract cost ${money(cost)} · Fee ${est.fee==null?'unavailable':money(est.fee)} · Total risk ${money(cost+(est.fee||0))}. ${est.note}`;}
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active',t===b));document.querySelectorAll('.view').forEach(v=>v.classList.add('hidden'));$(`#${b.dataset.view}-view`).classList.remove('hidden');});
$('#search').oninput=()=>{marketVisibleCount=100;renderMarkets();}; $('#status').onchange=loadMarkets;
loadPortfolio(); loadMarkets();
setInterval(loadPortfolio, 60_000);
