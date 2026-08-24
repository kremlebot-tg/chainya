'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const LANGUAGES = [['ru', 'Русский'], ['en', 'English'], ['zh', '中文']];
const ACTIONS = {partner_create: 'Добавлен партнёр', partner_update: 'Изменена карточка', partner_reorder: 'Изменён порядок'};
const CYRILLIC_SLUG = {а:'a',б:'b',в:'v',г:'g',д:'d',е:'e',ё:'e',ж:'zh',з:'z',и:'i',й:'i',к:'k',л:'l',м:'m',н:'n',о:'o',п:'p',р:'r',с:'s',т:'t',у:'u',ф:'f',х:'h',ц:'c',ч:'ch',ш:'sh',щ:'sch',ъ:'',ы:'y',ь:'',э:'e',ю:'yu',я:'ya'};

let documentState = null;
let selectedId = '';
let draft = null;
let baseline = '';
let activeLanguage = 'ru';
let saving = false;

function blankPartner() {
  return {id: '', published: false, logo: '', translations: Object.fromEntries(LANGUAGES.map(([code]) => [code, {name: '', type: ''}]))};
}

function clone(value) { return JSON.parse(JSON.stringify(value)); }
function snapshot(value) { return JSON.stringify(value); }
function isDirty() { return draft && snapshot(draft) !== baseline; }
function displayTranslation(partner, language = 'ru') {
  const order = [language, 'ru', 'en', 'zh'];
  return order.map(code => partner.translations?.[code]).find(value => value?.name) || {name: partner.id || 'Новый партнёр', type: ''};
}
function slug(value) {
  return value.toLocaleLowerCase('ru').split('').map(char => CYRILLIC_SLUG[char] ?? char).join('').normalize('NFKD').replace(/[\u0300-\u036f]/g, '').replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 80);
}
function completion(partner) {
  const missing = [];
  for (const [code, label] of LANGUAGES) {
    if (!partner.translations?.[code]?.name) missing.push(`название · ${label}`);
    if (!partner.translations?.[code]?.type) missing.push(`сфера · ${label}`);
  }
  return missing;
}
function toast(message) {
  const el = $('#toast'); el.textContent = message; el.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { el.hidden = true; }, 2800);
}
function fmtDate(value) {
  return new Intl.DateTimeFormat('ru-RU', {day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit'}).format(new Date(value));
}
function updateStats() {
  const partners = documentState?.partners || [];
  $('#stat-total').textContent = partners.length;
  $('#stat-published').textContent = partners.filter(item => item.published).length;
  $('#stat-incomplete').textContent = partners.filter(item => completion(item).length).length;
}
function confirmDiscard() {
  return !isDirty() || window.confirm('Есть несохранённые изменения. Перейти без сохранения?');
}
function renderList() {
  const box = $('#partner-list'), query = $('#search').value.trim().toLocaleLowerCase('ru');
  box.replaceChildren(); updateStats();
  const partners = documentState?.partners || [];
  const visible = partners.filter(item => {
    const text = Object.values(item.translations || {}).flatMap(value => [value.name, value.type]).join(' ').toLocaleLowerCase('ru');
    return !query || text.includes(query) || item.id.includes(query);
  });
  if (!visible.length) { const empty = document.createElement('div'); empty.className = 'list-empty'; empty.textContent = partners.length ? 'По этому запросу ничего не найдено.' : 'Партнёров пока нет. Добавьте первую запись.'; box.append(empty); return; }
  visible.forEach(item => {
    const index = partners.findIndex(partner => partner.id === item.id), copy = displayTranslation(item);
    const row = document.createElement('div'); row.className = 'partner-row' + (selectedId === item.id ? ' is-active' : '');
    const open = document.createElement('button'); open.type = 'button'; open.className = 'partner-row__open';
    open.setAttribute('aria-label', `Редактировать ${copy.name}`);
    const name = document.createElement('strong'); name.textContent = copy.name;
    const type = document.createElement('small'); type.textContent = copy.type || 'Сфера не указана';
    const tags = document.createElement('span'); tags.className = 'tags';
    const state = document.createElement('span'); state.className = `tag ${item.published ? 'on' : ''}`; state.textContent = item.published ? 'На сайте' : 'Скрыт'; tags.append(state);
    if (completion(item).length) { const warning = document.createElement('span'); warning.className = 'tag warn'; warning.textContent = 'Можно дополнить'; tags.append(warning); }
    const identity = document.createElement('span'); identity.className = 'partner-row__identity' + (item.logo ? '' : ' no-logo');
    if (item.logo) { const logo = document.createElement('span'); logo.className = 'partner-row__logo' + (item.id === 'relikta' ? ' is-relikta' : ''); const image = document.createElement('img'); image.src = item.logo; image.alt = ''; logo.append(image); identity.append(logo); }
    const text = document.createElement('span'); text.append(name, type, tags); identity.append(text);
    open.append(identity); open.onclick = () => selectPartner(item.id);
    row.append(open);
    if (selectedId === item.id) {
      const moves = document.createElement('span'); moves.className = 'moves';
      for (const [direction, symbol, label] of [[-1,'↑','Поднять выше'],[1,'↓','Опустить ниже']]) {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'move'; button.textContent = symbol; button.title = label; button.setAttribute('aria-label', `${label}: ${copy.name}`); button.disabled = saving || index + direction < 0 || index + direction >= partners.length; button.onclick = () => movePartner(item.id, direction); moves.append(button);
      }
      row.append(moves);
    }
    box.append(row);
  });
}
function createLanguagePanels(root) {
  root.replaceChildren();
  for (const [code, label] of LANGUAGES) {
    const panel = document.createElement('div'); panel.className = 'language-panel' + (code === activeLanguage ? ' is-active' : ''); panel.dataset.lang = code; panel.setAttribute('role', 'tabpanel');
    for (const [field, title, placeholder] of [['name','Название партнёра',label === 'Русский' ? 'Например, Реликта' : 'Название на этом языке'],['type','Короткая подпись','Например, винодельня или крупнейший автодилер']]) {
      const wrapper = document.createElement('label'); wrapper.className = 'field';
      const caption = document.createElement('span'); caption.textContent = title;
      const input = document.createElement('input'); input.className = 'input'; input.maxLength = field === 'name' ? 160 : 240; input.placeholder = placeholder; input.value = draft.translations[code][field] || ''; input.dataset.lang = code; input.dataset.field = field; input.oninput = () => { draft.translations[code][field] = input.value; if (!draft.id && field === 'name') $('#partner-id').value = slug(input.value); syncEditor(); };
      wrapper.append(caption, input); panel.append(wrapper);
    }
    root.append(panel);
  }
}
function renderEditor() {
  const editor = $('#editor'); editor.replaceChildren($('#editor-template').content.cloneNode(true));
  const isNew = !selectedId;
  $('#editor-eyebrow').textContent = isNew ? 'Новая запись · сначала скрыта' : 'Карточка партнёра';
  $('#partner-id').value = draft.id; $('#partner-id').disabled = !isNew;
  $('#published').checked = draft.published;
  createLanguagePanels($('#language-panels'));
  $$('.lang', editor).forEach(button => { button.setAttribute('aria-selected', String(button.dataset.lang === activeLanguage)); button.onclick = () => { activeLanguage = button.dataset.lang; $$('.lang', editor).forEach(item => item.setAttribute('aria-selected', String(item === button))); $$('.language-panel', editor).forEach(panel => panel.classList.toggle('is-active', panel.dataset.lang === activeLanguage)); syncPreview(); }; });
  $('#published').onchange = event => { draft.published = event.target.checked; syncEditor(); };
  $('#partner-id').oninput = event => { draft.id = event.target.value.trim().toLocaleLowerCase('ru'); syncEditor(); };
  $('#save').onclick = savePartner;
  syncEditor();
  if (window.innerWidth <= 900) editor.scrollIntoView({behavior:'smooth', block:'start'});
}
function syncPreview() {
  const copy = displayTranslation(draft, activeLanguage);
  const logo = $('#preview-logo'), image = $('img', logo);
  logo.hidden = !draft.logo; logo.classList.toggle('is-relikta', draft.id === 'relikta');
  if (draft.logo) image.src = draft.logo; else image.removeAttribute('src');
  $('#preview-name').textContent = copy.name || 'Название партнёра';
  $('#preview-type').textContent = copy.type || 'Сфера сотрудничества';
  $('#editor-title').textContent = displayTranslation(draft).name || 'Новый партнёр';
}
function syncEditor() {
  const missing = completion(draft), box = $('#completion');
  box.classList.toggle('is-ready', !missing.length);
  $('strong', box).textContent = missing.length ? 'Карточку можно дополнить' : 'Карточка заполнена';
  $('span', box).textContent = missing.length ? `Не заполнено: ${missing.join(', ')}. Это не блокирует сохранение или публикацию.` : 'Название и подпись готовы на трёх языках.';
  $('#save-note').textContent = isDirty() ? 'Есть несохранённые изменения' : 'Изменений нет';
  $('#save').disabled = saving || !isDirty();
  syncPreview();
}
function selectPartner(id) {
  if (id === selectedId && draft) return;
  if (!confirmDiscard()) return;
  const partner = documentState.partners.find(item => item.id === id); if (!partner) return;
  selectedId = id; draft = clone(partner); baseline = snapshot(draft); activeLanguage = 'ru'; renderList(); renderEditor();
}
function addPartner() {
  if (!confirmDiscard()) return;
  selectedId = ''; draft = blankPartner(); baseline = snapshot(draft); activeLanguage = 'ru'; renderList(); renderEditor();
  requestAnimationFrame(() => $('[data-lang="ru"][data-field="name"]')?.focus());
}
async function request(url, options = {}) {
  const response = await fetch(url, {cache:'no-store', ...options});
  if (response.status === 401) { location.replace('/manage'); throw new Error('Сессия завершена'); }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || 'Не удалось сохранить изменения');
  return data;
}
async function savePartner() {
  if (saving) return;
  const idInput = $('#partner-id').value.trim().toLocaleLowerCase('ru');
  if (!selectedId) draft.id = idInput || slug(displayTranslation(draft).name);
  if (!draft.id || !/^[a-z0-9][a-z0-9-]{0,79}$/.test(draft.id)) { toast('Укажите ID латиницей: буквы, цифры и дефисы'); $('#partner-id').focus(); return; }
  if (!Object.values(draft.translations).some(value => value.name.trim())) { toast('Укажите название хотя бы на одном языке'); return; }
  saving = true; syncEditor();
  try {
    const isNew = !selectedId;
    const url = isNew ? '/api/admin/site/partners' : `/api/admin/site/partners/${encodeURIComponent(selectedId)}`;
    documentState = await request(url, {method:isNew ? 'POST' : 'PUT', headers:{'Content-Type':'application/json','X-Chainya-Admin':'site'}, body:JSON.stringify({revision:documentState.revision,item:draft})});
    selectedId = draft.id; draft = clone(documentState.partners.find(item => item.id === selectedId)); baseline = snapshot(draft); toast(isNew ? 'Партнёр добавлен' : 'Изменения сохранены'); renderList(); renderEditor();
  } catch (error) { toast(error.message); }
  finally { saving = false; if (draft) syncEditor(); }
}
async function movePartner(id, direction) {
  if (saving || !confirmDiscard()) return;
  const ids = documentState.partners.map(item => item.id), index = ids.indexOf(id), next = index + direction; if (index < 0 || next < 0 || next >= ids.length) return;
  [ids[index], ids[next]] = [ids[next], ids[index]]; saving = true; renderList();
  try { documentState = await request('/api/admin/site/partner-order', {method:'PUT',headers:{'Content-Type':'application/json','X-Chainya-Admin':'site'},body:JSON.stringify({revision:documentState.revision,ids})}); toast('Порядок обновлён'); }
  catch (error) { toast(error.message); }
  finally { saving = false; if (selectedId) { draft = clone(documentState.partners.find(item => item.id === selectedId)); baseline = snapshot(draft); } renderList(); }
}
async function load() {
  try { documentState = await request('/api/admin/site/partners'); renderList(); if (documentState.partners.length) selectPartner(documentState.partners[0].id); }
  catch (error) { $('#partner-list').innerHTML = '<div class="list-empty"></div>'; $('.list-empty').textContent = error.message; toast(error.message); }
}
async function showHistory() {
  const dialog = $('#history-dialog'), list = $('#history-list'); list.innerHTML = '<div class="history-empty">Загрузка…</div>'; dialog.showModal();
  try { const data = await request('/api/admin/site/history?limit=50'); list.replaceChildren(); if (!data.history.length) { list.innerHTML = '<div class="history-empty">Изменений пока нет</div>'; return; } data.history.forEach(item => { const row = document.createElement('div'); row.className = 'history-row'; const copy = document.createElement('div'); const title = document.createElement('strong'); title.textContent = ACTIONS[item.action] || 'Изменён блок сайта'; const name = document.createElement('span'); name.textContent = item.item_name || (item.action === 'partner_reorder' ? 'Список партнёров' : item.item_id); copy.append(title, document.createElement('br'), name); const time = document.createElement('time'); time.dateTime = item.created_at; time.textContent = fmtDate(item.created_at); row.append(copy, time); list.append(row); }); }
  catch (error) { list.innerHTML = '<div class="history-empty"></div>'; $('.history-empty', list).textContent = error.message; }
}

$('#add-top').onclick = addPartner; $('#add-side').onclick = addPartner; $('#search').oninput = renderList;
$('#history-open').onclick = showHistory; $('#history-close').onclick = () => $('#history-dialog').close();
$('#logout').onclick = async () => { if (!confirmDiscard()) return; await fetch('/api/admin/session', {method:'DELETE'}); location.replace('/manage'); };
window.addEventListener('beforeunload', event => { if (isDirty()) { event.preventDefault(); event.returnValue = ''; } });
load();
