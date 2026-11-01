const $ = id => document.getElementById(id);
const video = $('video'), canvas = $('overlay'), ctx = canvas.getContext('2d');
const capture = document.createElement('canvas'), captureCtx = capture.getContext('2d');
const money = value => new Intl.NumberFormat('id-ID',{style:'currency',currency:'IDR',maximumFractionDigits:0}).format(value);
const dateText = value => new Intl.DateTimeFormat('id-ID',{dateStyle:'medium',timeStyle:'short',timeZone:'Asia/Jakarta'}).format(new Date(value))+' WIB';
let stream=null, active=false, opening=false, timer, epoch=0, request=null;
let session=null, settings=null, working=false, initialized=false, receiptSession=null, offset=0, lastDetection=null;
const pageSize=10;
function brandPriceKey(label) {
  const key=label.normalize('NFKD').replace(/[\u0300-\u036f]/g,'').toLowerCase().replace(/[^a-z0-9]/g,'');
  const aliases={aqua:'aqua',nestlepurelife:'nestle pure life',purelife:'nestle pure life',leminerale:'le minerale'};
  return Object.hasOwn(aliases,key)?aliases[key]:key;
}
function priceForBrand(config,label) {
  const prices=config?.brand_prices||{}, key=brandPriceKey(label);
  return Object.hasOwn(prices,key)?prices[key]:config?.price_per_bottle??100;
}
function status(message) { $('statusText').textContent=message; }
function rejectionMessage(data) {
  if (data.rejection_reason==='low_confidence') {
    return `Botol tidak terdeteksi. Confidence ${((data.model_prediction?.confidence||0)*100).toFixed(1)}% di bawah batas ${Math.round(data.min_confidence*100)}%. Tunjukkan botol lebih jelas.`;
  }
  if (data.rejection_reason==='background') return 'Botol tidak terdeteksi. Model mengenali latar atau benda lain.';
  return 'Botol tidak terdeteksi. Belum ada hasil model yang cukup untuk membuka konfirmasi.';
}
async function api(path,options={}) {
  const response=await fetch(path,{cache:'no-store',...options}); let data;
  try { data=await response.json(); } catch { throw new Error(`Respons server tidak valid (HTTP ${response.status}).`); }
  if (!response.ok) throw new Error(typeof data.detail==='string'?data.detail:Array.isArray(data.detail)?data.detail.map(item=>item.msg.replace(/^Value error, /,'')).join(' '):'Data tidak valid. Periksa isian.');
  return data;
}
const post=(path,body)=>api(path,{method:'POST',...(body?{headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});
const url=(suffix='')=>`/api/rvm/sessions/${session.id}${suffix}`;
function applySession(next) {
  if (next && session?.id===next.id && next.version<session.version) return;
  if (next && !next.deposits) next.deposits=session?.id===next.id?session.deposits||[]:[];
  session=next; render();
}
function render() {
  const running=session?.status==='active', phase=running?session.phase:'idle';
  $('sessionRateLabel').textContent=running?'TARIF SESI INI':session?.status==='completed'?'TARIF TERCATAT':'TARIF BERIKUTNYA';
  $('sessionBottles').textContent=`${session?.bottles||0} botol`;
  $('sessionValue').textContent=money(session?.total_rupiah||0);
  $('sessionRate').textContent=settings?money(session?.config.price_per_bottle??settings.price_per_bottle):'—';
  const priceConfig=session?.status==='active'?session.config:settings;
  if (priceConfig) {
    const displayedPrices=['AQUA','Nestle Pure Life','Le Minerale'].map(label=>priceForBrand(priceConfig,label));
    $('sessionRate').textContent=new Set(displayedPrices).size>1?'Per merek':money(displayedPrices[0]);
  }
  if (running && session.candidate) $('sessionRate').textContent=money(session.candidate.price_per_bottle);
  if (session?.status==='completed' && session.deposits?.length) {
    const prices=new Set(session.deposits.map(item=>item.amount_rupiah));
    $('sessionRate').textContent=prices.size===1?money([...prices][0]):'Bervariasi';
  }
  $('sessionBadge').textContent=running?`Sesi ${session.id.slice(0,8)}`:session?'Sesi selesai':'Belum ada sesi';
  $('sessionBadge').classList.toggle('is-active',Boolean(running));
  $('beginSession').hidden=running; $('beginSession').disabled=working;
  $('beginSession').setAttribute('aria-describedby','rvmGuidance');
  $('confirmBottle').hidden=phase!=='ready'; $('nextBottle').hidden=phase!=='await_next';
  $('retryScan').hidden=phase!=='ready'; $('finishSession').hidden=!running;
  for (const id of ['confirmBottle','nextBottle','retryScan','finishSession']) $(id).disabled=working;
  if (session?.candidate && session.candidate.expires_at*1000<=Date.now()) $('confirmBottle').disabled=true;
  for (const field of $('settingsForm').elements) field.disabled=working||!initialized;
  for (const [id,state] of [['stepScan','scanning'],['stepConfirm','ready'],['stepNext','await_next']]) $(id).classList.toggle('current',phase===state);
  let message;
  if (!running) message='Tekan Mulai Setor untuk langsung scan. Berbagai merek botol bisa digabung dalam satu transaksi.';
  else if (phase==='ready') message=`Hasil scan: ${session.candidate.label} (${Math.round(session.candidate.confidence*100)}%) · ${money(session.candidate.price_per_bottle)} per botol. Cek hasilnya, lalu konfirmasi botol masuk. Pilih Scan ulang jika keliru.`;
  else if (phase==='await_next') message='Botol tercatat. Siapkan botol berikutnya, boleh berbeda merek, lalu tekan Botol berikutnya.';
  else if (active && lastDetection?.session?.id===session.id && !lastDetection.bottle_detected) message=rejectionMessage(lastDetection);
  else message=active?'Tunjukkan satu botol, lalu konfirmasi hasil scan. Merek berikutnya boleh berbeda.':'Sesi tersimpan. Aktifkan kamera untuk melanjutkan scan.';
  $('rvmGuidance').textContent=message; $('rvmGuidance').dataset.phase=phase;
  $('sessionDeposits').replaceChildren();
  const brands=new Map();
  for (const item of session?.deposits||[]) {
    const key=item.label.toLocaleLowerCase('id-ID'), group=brands.get(key)||{label:item.label,count:0,total:0};
    group.count++; group.total+=item.amount_rupiah; brands.set(key,group);
  }
  for (const item of brands.values()) {
    const row=document.createElement('div'), label=document.createElement('span'), value=document.createElement('strong');
    row.className='deposit-row'; label.textContent=`✓ ${item.label} × ${item.count}`; value.textContent=money(item.total);
    row.append(label,value); $('sessionDeposits').append(row);
  }
}
function stopCamera() {
  epoch++; active=false; clearTimeout(timer); request?.abort(); request=null;
  stream?.getTracks().forEach(track=>track.stop()); stream=null; video.srcObject=null; video.hidden=true;
  $('startBtn').disabled=opening; $('stopBtn').disabled=true; $('liveIndicator').hidden=true;
  $('placeholder').hidden=false;
}
function clearDetection() { lastDetection=null; canvas.width=canvas.height=0; $('count').textContent=$('average').textContent='—'; $('results').textContent='Belum ada hasil klasifikasi.'; }
async function startCamera() {
  if (active||opening) return;
  if (!navigator.mediaDevices?.getUserMedia) { status('Kamera memerlukan localhost atau HTTPS.'); return; }
  opening=true; $('startBtn').disabled=true; const currentEpoch=++epoch; let opened;
  try {
    opened=await navigator.mediaDevices.getUserMedia({video:{facingMode:'environment',width:{ideal:1280}},audio:false});
    if (currentEpoch!==epoch) { opened.getTracks().forEach(track=>track.stop()); return; }
    stream=opened; video.srcObject=stream; await video.play(); if (currentEpoch!==epoch) return;
    active=true; video.hidden=false; $('placeholder').hidden=true;
    $('stopBtn').disabled=false; $('liveIndicator').hidden=false; status('Kamera aktif. Siapkan satu botol.'); render(); detectLoop(currentEpoch);
  } catch (error) { opened?.getTracks().forEach(track=>track.stop()); stopCamera(); status(`Kamera gagal: ${error.message}. Periksa izin kamera.`); }
  finally { opening=false; $('startBtn').disabled=active; }
}
async function detectLoop(currentEpoch) {
  if (!active||epoch!==currentEpoch) return;
  const paused=working||(session?.status==='active' && session.phase!=='scanning');
  if (!paused && video.videoWidth && video.videoHeight) {
    const controller=new AbortController(); request=controller;
    try {
      const scale=Math.min(1,960/video.videoWidth); capture.width=Math.round(video.videoWidth*scale); capture.height=Math.round(video.videoHeight*scale);
      captureCtx.drawImage(video,0,0,capture.width,capture.height);
      const blob=await new Promise(resolve=>capture.toBlob(resolve,'image/jpeg',0.78)); if (!active||epoch!==currentEpoch) return;
      const id=session?.status==='active'?session.id:null, form=new FormData(); form.append('file',blob,'frame.jpg');
      const data=await api(id?`/api/rvm/sessions/${id}/scan`:'/predict',{method:'POST',body:form,signal:controller.signal});
      if (!active||epoch!==currentEpoch) return; display(data);
      if (data.session && session?.id===id) {
        const needsConfirmation=data.session.phase==='ready' && session.phase!=='ready';
        applySession(data.session); status(data.session.scan_accepted?`Hasil scan: ${data.prediction.label}. Silakan konfirmasi atau scan ulang.`:rejectionMessage(data));
        if (needsConfirmation) $('setoran').scrollIntoView({behavior:'smooth',block:'start'});
      } else status(data.bottle_detected?`Label model: ${data.prediction.label}. Ini hanya cek label.`:rejectionMessage(data));
    } catch (error) { if (error.name!=='AbortError' && epoch===currentEpoch) status(`Scan gagal: ${error.message}`); }
    finally { if (request===controller) request=null; }
  }
  if (active && epoch===currentEpoch) timer=setTimeout(()=>detectLoop(currentEpoch),700);
}
function display(data) {
  lastDetection=data;
  canvas.width=data.width; canvas.height=data.height; ctx.clearRect(0,0,canvas.width,canvas.height);
  ctx.lineWidth=Math.max(2,data.width/250); ctx.font=`bold ${Math.max(14,data.width/45)}px sans-serif`;
  for (const item of data.detections||[]) {
    const [x1,y1,x2,y2]=item.box; ctx.strokeStyle='#50846a'; ctx.strokeRect(x1,y1,x2-x1,y2-y1); ctx.fillStyle='#294d39';
    ctx.fillText(`${item.label} ${Math.round(item.confidence*100)}%`,x1+5,Math.max(18,y1-6));
  }
  $('count').textContent=data.bottle_detected?data.prediction.label:'Botol tidak terdeteksi';
  const score=data.model_prediction?.confidence;
  $('average').textContent=Number.isFinite(score)?`${(score*100).toFixed(1)}%`:'—';
  $('results').replaceChildren();
  if (!data.bottle_detected) { $('results').textContent=rejectionMessage(data); return; }
  for (const item of [data.prediction]) {
    const row=document.createElement('div'), label=document.createElement('span'), score=document.createElement('span'); row.className='result-row';
    label.textContent=item.label; score.textContent=`${(item.confidence*100).toFixed(1)}%`; row.append(label,score); $('results').append(row);
  }
}
async function mutate(action) {
  if (working) return; working=true; render(); let errorText='';
  try { await action(); }
  catch (error) { errorText=error.message; status(errorText); try { applySession(await api('/api/rvm/current')); } catch { /* Preserve last saved view offline. */ } }
  finally { working=false; render(); if (errorText) $('rvmGuidance').textContent=errorText; }
}
async function beginSession() {
  let started=false;
  await mutate(async()=>{
    if (!initialized) { await initialize(); if (!initialized) throw new Error('Server belum siap. Tekan Mulai Setor untuk mencoba lagi.'); }
    stopCamera(); clearDetection(); applySession(await post('/api/rvm/sessions'));
    started=true;
  });
  if (started) {
    $('scanStudio').scrollIntoView({behavior:'smooth',block:'start'});
    await startCamera();
  }
}
$('beginSession').addEventListener('click',beginSession);
$('confirmBottle').addEventListener('click',()=>{
  const candidate=session?.candidate?.id; if (!candidate) return;
  mutate(async()=>{ applySession(await post(url('/confirm'),{candidate_id:candidate})); status('Satu botol tercatat.'); await refreshHistory(); });
});
async function resetCycle() {
  let reset=false;
  await mutate(async()=>{ applySession(await post(url('/next'))); clearDetection(); reset=true; status('Siap scan botol berikutnya, boleh berbeda merek.'); });
  if (reset && !active) await startCamera();
}
$('nextBottle').addEventListener('click',resetCycle); $('retryScan').addEventListener('click',resetCycle);
$('finishSession').addEventListener('click',()=>mutate(async()=>{
  stopCamera(); const completed=await post(url('/finish')); applySession(completed); showReceipt(completed); offset=0; await refreshHistory(); status('Setoran selesai. Struk PDF siap diunduh.');
}));
function showReceipt(completed) {
  receiptSession=completed; $('receiptMeta').textContent=`${completed.bottles} botol · ${dateText(completed.finished_at)} · ${completed.id.slice(0,8)}`;
  $('receiptTotal').textContent=money(completed.total_rupiah); $('receiptBrands').replaceChildren(); const brands=new Map();
  for (const item of completed.deposits) brands.set(item.label,(brands.get(item.label)||0)+1);
  for (const [label,count] of brands) { const row=document.createElement('p'); row.textContent=`${label} × ${count}`; $('receiptBrands').append(row); }
  $('downloadReceipt').href=`/api/rvm/sessions/${completed.id}/receipt.pdf`; $('downloadReceipt').download=`EcoVision-${completed.id}.pdf`;
  $('receiptError').textContent=''; if (!$('receiptDialog').open) $('receiptDialog').showModal();
}
$('downloadReceipt').addEventListener('click',async event=>{
  event.preventDefault(); if (!receiptSession||$('downloadReceipt').getAttribute('aria-busy')==='true') return;
  $('downloadReceipt').setAttribute('aria-busy','true'); $('receiptError').textContent='';
  try {
    const response=await fetch(`/api/rvm/sessions/${receiptSession.id}/receipt.pdf`); if (!response.ok) throw new Error('Struk belum bisa diunduh. Coba lagi.');
    const blobUrl=URL.createObjectURL(await response.blob()), anchor=document.createElement('a'); anchor.href=blobUrl; anchor.download=`EcoVision-${receiptSession.id}.pdf`;
    document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(()=>URL.revokeObjectURL(blobUrl),30000);
  } catch (error) { $('receiptError').textContent=error.message; } finally { $('downloadReceipt').removeAttribute('aria-busy'); }
});
$('closeReceipt').addEventListener('click',()=>$('receiptDialog').close());
async function refreshHistory() {
  const [history,totals]=await Promise.all([api(`/api/rvm/history?limit=${pageSize}&offset=${offset}`),api('/api/rvm/summary')]);
  if (!history.items.length && offset>0) { offset=Math.max(0,offset-pageSize); return refreshHistory(); }
  $('lifetimeSummary').textContent=`${totals.bottles} botol diterima · ${money(totals.total_rupiah)} nilai setoran · ${totals.completed_sessions} sesi selesai`;
  $('historyBody').replaceChildren();
  if (!history.items.length) { const row=document.createElement('tr'), cell=document.createElement('td'); cell.colSpan=5; cell.textContent='Belum ada sesi selesai. Mulai langkah baik pertamamu.'; row.append(cell); $('historyBody').append(row); }
  for (const item of history.items) {
    const row=document.createElement('tr');
    for (const value of [dateText(item.finished_at),item.id.slice(0,8),String(item.bottles),money(item.total_rupiah)]) { const cell=document.createElement('td'); cell.textContent=value; row.append(cell); }
    const cell=document.createElement('td'), button=document.createElement('button'); button.className='table-button'; button.textContent='Lihat & PDF';
    button.addEventListener('click',async()=>{ button.disabled=true; try { showReceipt(await api(`/api/rvm/sessions/${item.id}`)); } catch (error) { status(error.message); } finally { button.disabled=false; } });
    const actions=document.createElement('div'); actions.className='row-actions'; actions.append(button);
    for (const [label,handler,danger] of [['Edit',()=>openTransactionEditor(item.id),false],['Hapus',()=>removeTransaction(item.id),true]]) {
      const action=document.createElement('button'); action.type='button'; action.className=`table-button${danger?' danger':''}`; action.textContent=label;
      action.addEventListener('click',async()=>{ action.disabled=true; try { await handler(); } finally { action.disabled=false; } }); actions.append(action);
    }
    cell.append(actions); row.append(cell); $('historyBody').append(row);
  }
  $('historyPrev').disabled=offset===0; $('historyNext').disabled=offset+pageSize>=history.total;
  $('historyPage').textContent=history.total?`${offset+1}–${Math.min(offset+pageSize,history.total)} dari ${history.total}`:'0 transaksi';
}
async function loadHistory(nextOffset) { offset=nextOffset; try { await refreshHistory(); } catch (error) { status(`Riwayat gagal dimuat: ${error.message}`); } }
$('refreshHistory').addEventListener('click',()=>loadHistory(offset)); $('historyPrev').addEventListener('click',()=>loadHistory(Math.max(0,offset-pageSize))); $('historyNext').addEventListener('click',()=>loadHistory(offset+pageSize));
function populateSettings() {
  $('priceAqua').value=priceForBrand(settings,'AQUA');
  $('priceNestle').value=priceForBrand(settings,'Nestle Pure Life');
  $('priceLeMinerale').value=priceForBrand(settings,'Le Minerale');
}
$('settingsForm').addEventListener('submit',async event=>{
  event.preventDefault(); $('saveSettings').disabled=true;
  const body={brand_prices:{
    aqua:Number($('priceAqua').value),'nestle pure life':Number($('priceNestle').value),'le minerale':Number($('priceLeMinerale').value),
  }};
  try {
    await api('/api/rvm/settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const saved=await api('/api/rvm/settings');
    if (Object.entries(body.brand_prices).some(([brand,price])=>saved.brand_prices?.[brand]!==price)) {
      throw new Error('Tarif per merek belum tersimpan. Jalankan ulang server aplikasi, lalu coba simpan lagi.');
    }
    settings=saved;
    populateSettings(); $('settingsMessage').textContent= session?.status==='active' ? 'Tarif tersimpan untuk sesi berikutnya. Sesi yang sedang berjalan tetap memakai tarif saat dimulai.' : 'Pengaturan tersimpan. Tarif berlaku untuk sesi baru.'; render();
  }
  catch (error) { $('settingsMessage').textContent=error.message; } finally { $('saveSettings').disabled=working||!initialized; }
});
$('startBtn').addEventListener('click',startCamera);
$('stopBtn').addEventListener('click',()=>mutate(async()=>{ stopCamera(); clearDetection(); if (session?.status==='active' && session.phase!=='await_next') applySession(await post(url('/next'))); status('Kamera dihentikan. Sesi tetap tersimpan.'); }));
$('expandCamera').hidden=!document.fullscreenEnabled;
$('expandCamera').addEventListener('click',async()=>{
  try { await $('viewer').requestFullscreen(); }
  catch { status('Layar penuh belum tersedia di browser ini. Area kamera tetap bisa digunakan.'); }
});
$('exitFullscreen').addEventListener('click',async()=>{
  try { if (document.fullscreenElement) await document.exitFullscreen(); }
  catch { status('Tekan Esc untuk keluar dari layar penuh.'); }
});
document.addEventListener('fullscreenchange',()=>{ $('exitFullscreen').hidden=document.fullscreenElement!==$('viewer'); });
const motionPreference=window.matchMedia('(prefers-reduced-motion: reduce)');
$('motionToggle').hidden=motionPreference.matches;
motionPreference.addEventListener('change',event=>{ $('motionToggle').hidden=event.matches; });
$('motionToggle').addEventListener('click',()=>{
  const paused=document.body.classList.toggle('motion-paused');
  $('motionToggle').setAttribute('aria-pressed',String(paused));
  $('motionToggle').querySelector('span').textContent=paused?'Lanjutkan animasi':'Jeda animasi';
});
window.addEventListener('beforeunload',stopCamera);
async function initialize() {
  try {
    const [config,current,health]=await Promise.all([api('/api/rvm/settings'),api('/api/rvm/current'),api('/health')]);
    if (health.pricing_version!==2) {
      $('modelStatus').textContent='Server Versi Lama';
      $('modelInfo').textContent='Buka aplikasi di http://127.0.0.1:8002/';
      $('settingsMessage').textContent='Server ini masih menghitung tarif lama. Buka http://127.0.0.1:8002/ untuk tarif per merek.';
      $('rvmGuidance').textContent='Server masih memakai perhitungan lama. Buka http://127.0.0.1:8002/ untuk melanjutkan.';
      $('beginSession').disabled=true;
      for (const field of $('settingsForm').elements) field.disabled=true;
      return;
    }
    settings=config; populateSettings(); initialized=true; applySession(current);
    if (!Object.hasOwn(config,'brand_prices')) $('settingsMessage').textContent='Server masih memakai versi lama. Jalankan ulang server agar tarif per merek bisa disimpan.';
    $('modelStatus').textContent=health.configured?'API Key Tersedia':'Belum Dikonfigurasi'; $('modelInfo').textContent=health.configured?'Koneksi diuji saat scan pertama':'Isi ROBOFLOW_API_KEY di .env';
    $('confidenceNote').textContent=`Hasil di bawah confidence ${Math.round(health.min_confidence*100)}% ditampilkan sebagai “Botol tidak terdeteksi” dan tidak bisa dikonfirmasi.`;
    await refreshHistory();
  } catch (error) { $('beginSession').disabled=false; $('modelStatus').textContent='Server Bermasalah'; $('modelInfo').textContent='Periksa FastAPI dan database'; $('rvmGuidance').textContent=`Gagal memuat: ${error.message}. Tekan Mulai Setor untuk mencoba lagi.`; status(error.message); }
}
initialize();
setInterval(async()=>{
  if (!initialized||working||request||document.hidden) return;
  try { const current=await api('/api/rvm/current'); if (current) applySession(current); else if (session?.status==='active') { applySession(await api(url())); stopCamera(); await refreshHistory(); } }
  catch { /* Preserve last saved view offline. */ }
},4000);
