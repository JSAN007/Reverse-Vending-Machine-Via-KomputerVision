// Local transaction management. All totals and mutations are validated by the server.
let transactionEditorState=null, transactionEditorBusy=false, transactionFieldId=0;

function transactionItems() {
  return [...$('transactionItems').children].map(row=>({
    label:row.querySelector('.item-brand').value.trim(),
    quantity:row.querySelector('.item-quantity').valueAsNumber,
    price_per_bottle:row.querySelector('.item-price').valueAsNumber,
  }));
}
function updateTransactionTotal() {
  let count=0,total=0;
  for (const item of transactionItems()) {
    if (Number.isFinite(item.quantity) && Number.isFinite(item.price_per_bottle)) {
      count+=item.quantity; total+=item.quantity*item.price_per_bottle;
    }
  }
  $('transactionQuantity').textContent=`${count} botol`;
  $('transactionTotal').textContent=money(total);
  const rows=[...$('transactionItems').children];
  for (const row of rows) row.querySelector('button').disabled=rows.length===1||transactionEditorBusy;
  $('addTransactionItem').disabled=rows.length>=50||transactionEditorBusy;
}
function addTransactionRow(item={label:'',quantity:1,price_per_bottle:settings?.price_per_bottle??100}) {
  const row=document.createElement('div'); row.className='transaction-item';
  for (const [name,caption,type,limits] of [
    ['label','Merek / jenis botol','text',{required:true,maxLength:120}],
    ['quantity','Jumlah','number',{required:true,min:1,max:1000,step:1}],
    ['price_per_bottle','Tarif (Rp)','number',{required:true,min:0,max:100000,step:1}],
  ]) {
    const label=document.createElement('label'), input=document.createElement('input');
    label.className='field'; label.textContent=caption; input.type=type;
    input.className=name==='label'?'item-brand':name==='quantity'?'item-quantity':'item-price';
    input.id=`transaction-field-${++transactionFieldId}`; label.htmlFor=input.id;
    Object.assign(input,limits); input.value=item[name]; input.addEventListener('input',updateTransactionTotal);
    label.append(input); row.append(label);
  }
  const remove=document.createElement('button'); remove.type='button'; remove.textContent='Hapus baris';
  remove.addEventListener('click',()=>{ row.remove(); updateTransactionTotal(); });
  row.append(remove); $('transactionItems').append(row); updateTransactionTotal();
  let customPrice=Boolean(item.label);
  row.querySelector('.item-price').addEventListener('input',()=>{ customPrice=true; });
  row.querySelector('.item-brand').addEventListener('input',()=>{
    if (!customPrice) { row.querySelector('.item-price').value=priceForBrand(settings,row.querySelector('.item-brand').value); updateTransactionTotal(); }
  });
}
function setTransactionBusy(busy) {
  transactionEditorBusy=busy;
  for (const field of $('transactionForm').elements) field.disabled=busy;
  if (!busy) updateTransactionTotal();
}
async function openTransactionEditor(id=null) {
  if (transactionEditorBusy) return;
  $('transactionNotice').textContent='';
  try {
    const data=id?await api(`/api/rvm/transactions/${id}`):null;
    if (data && data.status!=='completed') throw new Error('Selesaikan sesi aktif sebelum mengedit transaksi.');
    transactionEditorState={id:data?.id||null,version:data?.version,requestId:crypto.randomUUID()};
    $('transactionHeading').textContent=data?'Edit transaksi':'Tambah transaksi';
    $('transactionItems').replaceChildren(); $('transactionError').textContent='';
    const grouped=new Map();
    for (const deposit of data?.deposits||[]) {
      const key=JSON.stringify([deposit.label,deposit.amount_rupiah]);
      const item=grouped.get(key)||{label:deposit.label,quantity:0,price_per_bottle:deposit.amount_rupiah};
      item.quantity++; grouped.set(key,item);
    }
    for (const item of grouped.values()) addTransactionRow(item);
    if (!grouped.size) addTransactionRow();
    setTransactionBusy(false);
    $('transactionDialog').showModal();
    $('transactionItems').querySelector('input').focus();
  } catch (error) { $('transactionNotice').textContent=error.message; }
}
$('newTransaction').addEventListener('click',()=>openTransactionEditor());
$('addTransactionItem').addEventListener('click',()=>addTransactionRow());
$('closeTransaction').addEventListener('click',()=>{ if (!transactionEditorBusy) $('transactionDialog').close(); });
$('transactionDialog').addEventListener('cancel',event=>{ if (transactionEditorBusy) event.preventDefault(); });
$('transactionForm').addEventListener('submit',async event=>{
  event.preventDefault(); if (transactionEditorBusy||!transactionEditorState) return;
  const items=transactionItems();
  if (!items.length || items.some(item=>!item.label) || items.reduce((sum,item)=>sum+item.quantity,0)>1000) {
    $('transactionError').textContent='Isi merek pada setiap baris, maksimal 1.000 botol per transaksi.'; return;
  }
  setTransactionBusy(true); $('transactionError').textContent='';
  const state=transactionEditorState;
  let saved;
  try {
    saved=await api(state.id?`/api/rvm/transactions/${state.id}`:'/api/rvm/transactions',{
      method:state.id?'PUT':'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({items,...(state.id?{expected_version:state.version}:{request_id:state.requestId})}),
    });
  } catch (error) { $('transactionError').textContent=error.message; }
  finally { setTransactionBusy(false); }
  if (!saved) return;
  $('transactionDialog').close();
  if (session?.id===saved.id) applySession(saved);
  if (receiptSession?.id===saved.id) receiptSession=saved;
  offset=0;
  $('transactionNotice').textContent=`Transaksi ${saved.id.slice(0,8)} tersimpan: ${saved.bottles} botol, ${money(saved.total_rupiah)}.`;
  try { await refreshHistory(); }
  catch { $('transactionNotice').textContent+=' Data tersimpan; tekan Muat ulang untuk memperbarui daftar.'; }
});
async function removeTransaction(id) {
  $('transactionNotice').textContent='';
  try {
    const data=await api(`/api/rvm/transactions/${id}`);
    if (!confirm(`Hapus transaksi ${id.slice(0,8)}?\n${data.bottles} botol · ${money(data.total_rupiah)}\nTransaksi dan rinciannya akan dihapus dari daftar. Catatan perubahan tetap tersimpan untuk penelusuran.`)) return;
    await api(`/api/rvm/transactions/${id}?expected_version=${data.version}`,{method:'DELETE'});
    if (session?.id===id) applySession(null);
    if (receiptSession?.id===id) { receiptSession=null; $('receiptDialog').close(); }
    $('transactionNotice').textContent=`Transaksi ${id.slice(0,8)} berhasil dihapus.`;
    try { await refreshHistory(); }
    catch { $('transactionNotice').textContent+=' Tekan Muat ulang untuk memperbarui daftar.'; }
  } catch (error) { $('transactionNotice').textContent=error.message; }
}
