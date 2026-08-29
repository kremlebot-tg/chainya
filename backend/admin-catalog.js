'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const AXES = {
  floral: 'Цветочный', fruity: 'Фруктовый', driedfruit: 'Сухофрукты',
  honey: 'Медовый', nutty: 'Ореховый', roasted: 'Жареный',
  spicy: 'Пряный', woody: 'Древесный', herbal: 'Травяной',
};
const LANGUAGES = [
  ['ru', 'Русский', 'РУ'], ['en', 'English', 'EN'], ['zh', '中文', '中文'],
];
const HISTORY_ACTIONS = {
  create: 'Добавлен товар', update: 'Изменена карточка', reorder: 'Изменён порядок',
  image: 'Обновлено фото', image_add: 'Добавлено фото',
  image_primary: 'Выбрано главное фото', image_remove: 'Фото убрано из карточки',
  category_create: 'Добавлена категория', category_update: 'Изменена категория',
  category_delete: 'Удалена категория', category_reorder: 'Изменён порядок категорий',
};
const CYRILLIC_SLUG = {
  а:'a',б:'b',в:'v',г:'g',д:'d',е:'e',ё:'e',ж:'zh',з:'z',и:'i',й:'i',к:'k',л:'l',
  м:'m',н:'n',о:'o',п:'p',р:'r',с:'s',т:'t',у:'u',ф:'f',х:'h',ц:'c',ч:'ch',ш:'sh',
  щ:'sch',ъ:'',ы:'y',ь:'',э:'e',ю:'yu',я:'ya',
};
const GROUP_COPY = {
  tea: {
    noun: 'чай', title: 'Новый чай', unit: 'g', firstType: 'white',
    section: 'Название, описание и маркировка',
    note: 'Пищевые данные можно заполнить позже: они не блокируют публикацию, но остаются в напоминаниях.',
    complete: 'Тексты и маркировка заполнены на трёх языках.',
    fields: [
      ['name', 'Название', 'input'],
      ['orig', 'Происхождение / подзаголовок', 'input'],
      ['desc', 'Описание', 'textarea'],
      ['composition', 'Состав', 'textarea'],
      ['manufacturer', 'Изготовитель / упаковщик / импортёр', 'textarea'],
      ['shelf_life', 'Срок годности', 'input'],
      ['storage', 'Условия хранения', 'textarea'],
    ],
  },
  teaware: {
    noun: 'посуду', title: 'Новая посуда', unit: 'pc', firstType: 'teaware-teapots',
    section: 'Название, описание и характеристики',
    note: 'Для посуды используются материал, размеры, мастерская и уход — пищевые сроки и вкусовой профиль не показываются.',
    complete: 'Описание и характеристики посуды заполнены на трёх языках.',
    fields: [
      ['name', 'Название', 'input'],
      ['orig', 'Материал, объём / подзаголовок', 'input'],
      ['desc', 'Описание', 'textarea'],
      ['composition', 'Материал и техника изготовления', 'textarea'],
      ['manufacturer', 'Мастер / мастерская / изготовитель', 'textarea'],
      ['shelf_life', 'Размеры и объём', 'input'],
      ['storage', 'Уход и использование', 'textarea'],
    ],
  },
};

let catalog = null;
let selectedId = '';
let draft = null;
let dirty = false;
let groupFilter = 'all';
let pendingImages = [];
let pendingImageUrls = [];
let sabyReview = null;
let pendingSabyItem = null;
let editingCategoryId = '';

function toast(message) {
  const box = $('#toast');
  box.textContent = message;
  box.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { box.hidden = true; }, 5200);
}

function headers(json = true) {
  return {'X-Chainya-Admin': 'catalog', ...(json ? {'Content-Type': 'application/json'} : {})};
}

function clone(value) { return JSON.parse(JSON.stringify(value)); }

function visibleText(value) {
  return String(value || '')
    .normalize('NFKC')
    .replace(/[\s\u00a0\u2000-\u200d\u2060\u3164\ufeff]/g, '')
    .length > 0;
}

function typeInfo(id) { return catalog?.types?.find(type => type.id === id) || null; }
function typeName(id) { return typeInfo(id)?.name || id; }
function typeGroup(id) { return typeInfo(id)?.group === 'teaware' ? 'teaware' : 'tea'; }
function itemGroup(item) { return typeGroup(item?.type); }
function groupCopy(itemOrGroup) {
  const group = typeof itemOrGroup === 'string' ? itemOrGroup : itemGroup(itemOrGroup);
  return GROUP_COPY[group] || GROUP_COPY.tea;
}
function safeImage(url) {
  return /^\/img\/[A-Za-z0-9_-]+\.webp$/.test(url) ||
    /^\/catalog-media\/[a-f0-9]{32}\.webp$/.test(url)
    ? url : '/img/logo-mark.webp';
}
function isPlaceholderImage(item) {
  return item?.image?.kind === 'seed' && item.image.name === 'logo-mark';
}
function money(item) {
  return new Intl.NumberFormat('ru-RU').format(item.price) + ' ₽/' +
    (item.unit === 'pc' ? 'шт' : '10 г');
}

function missingByLanguage(item, language) {
  const text = item?.translations?.[language] || {};
  return groupCopy(item).fields
    .filter(([field]) => !visibleText(text[field]))
    .map(([field, label]) => ({field, label}));
}

function completionState(item) {
  const essentials = [];
  const names = LANGUAGES.map(([language]) => item?.translations?.[language]?.name);
  if (!names.some(visibleText)) essentials.push('Название хотя бы на одном языке');
  if (!(Number(item?.price) > 0)) essentials.push('Цена выше нуля');
  if (isPlaceholderImage(item) || item?.saby?.image_pending) essentials.push('Настоящее фото');
  const missing = Object.fromEntries(
    LANGUAGES.map(([language]) => [language, missingByLanguage(item, language)])
  );
  const ru = missing.ru;
  const translations = missing.en.length + missing.zh.length;
  const missingCount = ru.length + translations;
  if (essentials.length) return {level: 'need', label: 'Нужно дополнить', essentials, missing};
  if (ru.length) return {level: 'warn', label: `Дополнить · ${missingCount}`, essentials, missing};
  if (translations) return {level: 'translate', label: `Переводы · ${translations}`, essentials, missing};
  return {level: 'ready', label: 'Карточка готова', essentials, missing};
}

function missingPublicationFields(item) {
  const result = [];
  for (const [language, , short] of LANGUAGES) {
    missingByLanguage(item, language).forEach(({label}) => result.push(`${short}: ${label}`));
  }
  return result;
}

function isIncomplete(item) { return completionState(item).level !== 'ready'; }

function formatDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 'время неизвестно' : date.toLocaleString('ru-RU', {
    day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
  });
}

function updateState() {
  if (!catalog) return;
  const total = catalog.teas.length;
  const tea = catalog.teas.filter(item => itemGroup(item) === 'tea').length;
  const teaware = total - tea;
  $('#catalog-state').textContent = `Сохранён ${formatDate(catalog.updated_at)} · версия ${catalog.revision}`;
  $('#stat-total').textContent = total;
  $('#stat-published').textContent = catalog.teas.filter(item => item.published).length;
  $('#stat-stock').textContent = catalog.teas.filter(item => item.stock).length;
  $('#stat-hidden').textContent = catalog.teas.filter(item => !item.published).length;
  $('#stat-incomplete').textContent = catalog.teas.filter(isIncomplete).length;
  $('#group-all-count').textContent = total;
  $('#group-tea-count').textContent = tea;
  $('#group-teaware-count').textContent = teaware;
}

function filteredItems() {
  const query = $('#search').value.trim().toLocaleLowerCase('ru');
  const filter = $('#catalog-filter').value;
  return catalog.teas.filter(item => {
    const state = completionState(item);
    const groupOk = groupFilter === 'all' || itemGroup(item) === groupFilter;
    const filterOk = filter === 'all' ||
      (filter === 'published' && item.published) ||
      (filter === 'stock' && item.stock) ||
      (filter === 'out-of-stock' && !item.stock) ||
      (filter === 'incomplete' && state.level !== 'ready') ||
      (filter === 'complete' && state.level === 'ready') ||
      (filter === 'hidden' && !item.published);
    const haystack = `${item.name} ${item.orig} ${item.id} ${typeName(item.type)}`
      .toLocaleLowerCase('ru');
    return groupOk && filterOk && haystack.includes(query);
  });
}

function renderList() {
  const box = $('#items');
  const rows = filteredItems();
  box.replaceChildren();
  if (!rows.length) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.style.minHeight = '240px';
    empty.textContent = groupFilter === 'teaware'
      ? 'Посуда ещё не добавлена. Нажмите «Добавить» и выберите «Посуда».'
      : 'Ничего не найдено. Измените запрос или фильтр.';
    box.append(empty);
    return;
  }
  rows.forEach((item, visibleIndex) => {
    const state = completionState(item);
    const row = document.createElement('div');
    row.className = 'catalog-row' + (item.id === selectedId ? ' is-active' : '');
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'catalog-row__open';
    open.setAttribute('aria-label', `Редактировать: ${item.name || 'без названия'}`);
    open.onclick = () => selectItem(item.id);
    const image = document.createElement('img');
    image.src = safeImage(item.image_url);
    image.alt = '';
    image.loading = 'lazy';
    image.decoding = 'async';
    if (isPlaceholderImage(item)) image.className = 'photo-placeholder';
    const copy = document.createElement('span');
    const name = document.createElement('span');
    name.className = 'catalog-row__name';
    name.textContent = item.name || 'Без названия';
    const meta = document.createElement('span');
    meta.className = 'catalog-row__meta';
    meta.textContent = `${typeName(item.type)} · ${money(item)}`;
    const badges = document.createElement('span');
    badges.className = 'catalog-row__badges';
    const published = document.createElement('span');
    published.className = 'tag ' + (item.published ? 'on' : 'off');
    published.textContent = item.published ? 'На сайте' : 'Скрыт';
    const stock = document.createElement('span');
    stock.className = 'tag ' + (item.stock ? 'on' : 'off');
    stock.textContent = item.stock ? 'В наличии' : 'Нет';
    badges.append(published, stock);
    if (state.level !== 'ready') {
      const warning = document.createElement('span');
      warning.className = `tag ${state.level}`;
      warning.textContent = state.label;
      warning.title = [...state.essentials, ...missingPublicationFields(item)].join(', ');
      badges.append(warning);
    }
    copy.append(name, meta, badges);
    open.append(image, copy);
    row.append(open);
    // Стрелки нужны только у выбранной карточки. Так список из десятков товаров
    // остаётся спокойным, а случайно изменить порядок на телефоне сложнее.
    if (item.id === selectedId) {
      const order = document.createElement('span');
      order.className = 'row-order';
      [['↑', -1], ['↓', 1]].forEach(([label, direction]) => {
        const move = document.createElement('button');
        move.type = 'button';
        move.className = 'move';
        move.textContent = label;
        move.setAttribute('aria-label', `${direction < 0 ? 'Поднять' : 'Опустить'} ${item.name}`);
        move.disabled = visibleIndex + direction < 0 || visibleIndex + direction >= rows.length;
        move.onclick = () => moveItem(item.id, direction);
        order.append(move);
      });
      row.append(order);
    }
    box.append(row);
  });
}

function emptyTranslations() {
  return Object.fromEntries(LANGUAGES.map(([language]) => [language, {
    name: '', orig: '', desc: '', composition: '', manufacturer: '', shelf_life: '', storage: '',
  }]));
}

function blankItem(group = 'tea') {
  const copy = GROUP_COPY[group] || GROUP_COPY.tea;
  const firstType = catalog.types.find(type => type.group === group)?.id || copy.firstType;
  return {
    id: '', type: firstType, price: 0, unit: copy.unit, stock: true, published: false,
    img: 'logo-mark', image: {kind: 'seed', name: 'logo-mark'},
    images: [{kind: 'seed', name: 'logo-mark'}],
    image_url: '/img/logo-mark.webp', image_urls: ['/img/logo-mark.webp'],
    taste: Object.fromEntries(Object.keys(AXES).map(axis => [axis, 0])),
    translations: emptyTranslations(), name: '', orig: '', desc: '',
  };
}

function suggestedId(value) {
  const base = [...String(value).trim().toLocaleLowerCase('ru')]
    .map(char => CYRILLIC_SLUG[char] ?? char).join('')
    .normalize('NFKD').replace(/[\u0300-\u036f]/g, '')
    .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 70) || 'new-item';
  const used = new Set(catalog.teas.map(item => item.id));
  if (!used.has(base)) return base;
  for (let number = 2; number < 1000; number += 1) {
    const candidate = `${base.slice(0, 76 - String(number).length)}-${number}`;
    if (!used.has(candidate)) return candidate;
  }
  return `${base.slice(0, 65)}-${Date.now().toString(36)}`;
}

function suggestedCategoryId(value) {
  const base = [...String(value).trim().toLocaleLowerCase('ru')]
    .map(char => CYRILLIC_SLUG[char] ?? char).join('')
    .normalize('NFKD').replace(/[\u0300-\u036f]/g, '')
    .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 36) || 'new-category';
  const used = new Set(catalog.types.map(item => item.id));
  if (!used.has(base)) return base;
  for (let number = 2; number < 1000; number += 1) {
    const candidate = `${base.slice(0, 36 - String(number).length)}-${number}`;
    if (!used.has(candidate)) return candidate;
  }
  return `${base.slice(0, 28)}-${Date.now().toString(36)}`;
}

function confirmDiscard() {
  return !dirty || confirm('Отменить несохранённые изменения?');
}

function clearPendingImages() {
  pendingImageUrls.forEach(url => URL.revokeObjectURL(url));
  pendingImages = [];
  pendingImageUrls = [];
}

function revealEditorMobile() {
  if (innerWidth > 900) return;
  requestAnimationFrame(() => $('#editor').scrollIntoView({block: 'start'}));
}

function showAddDialog() {
  if (!confirmDiscard()) return;
  pendingSabyItem = null;
  $('#kind-title').textContent = 'Что добавляем?';
  $('#kind-dialog').showModal();
}

function showSabyKindDialog(item) {
  if (!confirmDiscard()) return;
  pendingSabyItem = item;
  $('#saby-dialog').close();
  $('#kind-title').textContent = 'К какой группе относится товар из СБИС?';
  $('#kind-dialog').showModal();
}

function chooseNewGroup(group) {
  if (pendingSabyItem) addSabyDraft(pendingSabyItem, group);
  else addItem(group);
}

function addItem(group) {
  selectedId = '';
  draft = blankItem(group);
  clearPendingImages();
  dirty = false;
  renderList();
  renderEditor();
  setDirty(true);
  $('#kind-dialog').close();
  revealEditorMobile();
}

function selectItem(id) {
  if (id === selectedId) return;
  if (!confirmDiscard()) return;
  selectedId = id;
  draft = clone(catalog.teas.find(item => item.id === id));
  clearPendingImages();
  dirty = false;
  renderList();
  renderEditor();
  revealEditorMobile();
}

function addSabyDraft(item, group = 'tea') {
  const next = blankItem(group);
  next.price = item.suggested_price;
  next.unit = item.unit;
  next.stock = item.suggested_stock === true;
  next.saby = {id: item.saby_id, external_id: item.external_id, image_pending: true};
  next.translations.ru.name = item.name;
  next.name = item.name;
  next.id = suggestedId(item.name);
  selectedId = '';
  draft = next;
  clearPendingImages();
  dirty = false;
  pendingSabyItem = null;
  renderList();
  renderEditor();
  setDirty(true);
  toast(item.suggested_stock === null
    ? 'Черновик создан. СБИС не вернул остаток — товар отмечен как недоступный.'
    : 'Черновик создан из СБИС. Проверьте тип товара, категорию, цену и добавьте фото.');
  revealEditorMobile();
}

function cloneItem() {
  if (!draft || !selectedId || !confirmDiscard()) return;
  const source = clone(draft);
  const next = blankItem(itemGroup(source));
  next.type = source.type;
  next.price = source.price;
  next.unit = source.unit;
  next.taste = clone(source.taste);
  next.stock = false;
  selectedId = '';
  draft = next;
  clearPendingImages();
  dirty = false;
  renderList();
  renderEditor();
  setDirty(true);
  toast('Создана скрытая заготовка. Фото и тексты нужно добавить заново.');
  revealEditorMobile();
}

function renderEmptyEditor(root) {
  const empty = document.createElement('div');
  empty.className = 'empty';
  empty.innerHTML = '<div class="empty__in"><div class="empty__mark" aria-hidden="true">茶</div><strong>Выберите товар</strong><p>Откройте карточку слева или создайте чай либо посуду.</p><button class="btn btn--primary" type="button">＋ Добавить новый товар</button></div>';
  $('button', empty).onclick = showAddDialog;
  root.append(empty);
}

function makeInput(label, name, value, type = 'text', wide = false) {
  const wrap = document.createElement('label');
  wrap.className = 'field' + (wide ? ' field--wide' : '');
  const title = document.createElement('span');
  title.textContent = label;
  const field = document.createElement('input');
  field.className = 'input';
  field.id = `catalog-field-${name.replace(/[^a-z0-9]+/gi, '-')}`;
  field.type = type;
  field.name = name;
  field.value = value ?? '';
  field.required = name === 'id' || name === 'price';
  if (type === 'text') field.maxLength = name.endsWith('.name') ? 160 : name.endsWith('.orig') ? 240 : 500;
  wrap.append(title, field);
  return wrap;
}

function makeTextarea(label, name, value) {
  const wrap = document.createElement('label');
  wrap.className = 'field field--wide';
  const title = document.createElement('span');
  title.textContent = label;
  const field = document.createElement('textarea');
  field.className = 'textarea';
  field.id = `catalog-field-${name.replace(/[^a-z0-9]+/gi, '-')}`;
  field.name = name;
  field.value = value ?? '';
  field.maxLength = 5000;
  wrap.append(title, field);
  return wrap;
}

function clearFormErrors() {
  const box = $('#form-errors');
  if (!box) return;
  $$('#catalog-form [aria-invalid="true"]').forEach(field => field.removeAttribute('aria-invalid'));
  box.hidden = true;
  box.replaceChildren();
}

function showFormErrors(form, errors) {
  const box = $('#form-errors');
  if (!box) return;
  box.replaceChildren();
  const title = document.createElement('strong');
  title.textContent = 'Проверьте карточку';
  const list = document.createElement('ul');
  errors.forEach(({message, name, language}) => {
    const field = form.elements.namedItem(name);
    field?.setAttribute('aria-invalid', 'true');
    const row = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = message;
    button.onclick = () => {
      if (language) switchLanguage(language);
      field?.focus();
    };
    row.append(button); list.append(row);
  });
  box.append(title, list);
  box.hidden = false;
  box.focus();
}

function setDirty(value = true) {
  dirty = value;
  const state = $('#save-state');
  if (state) state.textContent = value ? 'Есть несохранённые изменения' : 'Все изменения сохранены';
  const save = $('#save');
  if (save) save.disabled = !value;
}

function buildCompletion(item, node) {
  const state = completionState(item);
  node.className = 'completion' + (state.level === 'ready' ? ' is-ready' : '');
  node.replaceChildren();
  const strong = document.createElement('strong');
  strong.textContent = state.level === 'ready'
    ? 'Карточка заполнена'
    : state.essentials.length
      ? 'Карточку можно сохранить; важное ещё не заполнено'
      : 'Карточку можно опубликовать и дополнить позже';
  node.append(strong);
  const summary = document.createElement('p');
  summary.className = 'completion-summary';
  summary.textContent = state.level === 'ready'
    ? groupCopy(item).complete
    : state.essentials.length
      ? 'Сохранение доступно. Перед продажей проверьте название, цену и фотографию.'
      : 'Публикация не заблокирована: недостающие сведения останутся напоминанием только в панели.';
  node.append(summary);
  const chips = document.createElement('div');
  chips.className = 'completion-chips';
  for (const [language, , short] of LANGUAGES) {
    const count = state.missing[language].length;
    const chip = document.createElement('span');
    chip.className = 'completion-chip' + (count ? '' : ' is-ready');
    chip.textContent = count ? `${short}: дополнить ${count}` : `${short}: готово`;
    chips.append(chip);
  }
  node.append(chips);
  if (state.level === 'ready') {
    return;
  }
  const details = document.createElement('details');
  details.className = 'completion-details';
  const detailsSummary = document.createElement('summary');
  detailsSummary.textContent = 'Показать, что именно нужно дополнить';
  const list = document.createElement('div');
  list.className = 'completion-list';
  if (state.essentials.length) addCompletionLine(list, 'Важно', state.essentials.join(', '));
  for (const [language, , short] of LANGUAGES) {
    const missing = state.missing[language];
    if (missing.length) addCompletionLine(list, short, missing.map(row => row.label).join(', '));
  }
  details.append(detailsSummary, list);
  node.append(details);
}

function addCompletionLine(root, label, value) {
  const line = document.createElement('div');
  line.className = 'completion-line';
  const key = document.createElement('span');
  key.textContent = label;
  const detail = document.createElement('span');
  detail.textContent = value;
  line.append(key, detail);
  root.append(line);
}

function renderPendingQueue() {
  const root = $('#photo-pending');
  if (!root) return;
  root.replaceChildren();
  pendingImages.forEach((file, index) => {
    const row = document.createElement('div');
    row.className = 'photo-pending__row';
    const image = document.createElement('img');
    image.src = pendingImageUrls[index];
    image.alt = '';
    const name = document.createElement('span');
    name.textContent = `${index + 1}. ${file.name}`;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'photo-pending__remove';
    remove.textContent = '×';
    remove.setAttribute('aria-label', `Убрать выбранное фото ${index + 1}`);
    remove.onclick = () => removePendingImage(index);
    row.append(image, name, remove);
    root.append(row);
  });
}

function removePendingImage(index) {
  URL.revokeObjectURL(pendingImageUrls[index]);
  pendingImages.splice(index, 1);
  pendingImageUrls.splice(index, 1);
  const preview = $('#photo-preview');
  if (preview) preview.src = pendingImageUrls[0] || safeImage(draft.image_url);
  renderPendingQueue();
  setDirty(true);
}

function renderPhotoStack(item) {
  const stack = document.createElement('div');
  stack.className = 'photo-stack';
  const photo = document.createElement('div');
  photo.className = 'photo';
  const image = document.createElement('img');
  image.id = 'photo-preview';
  image.src = pendingImageUrls[0] || safeImage(item.image_url);
  image.alt = 'Фото товара';
  image.decoding = 'async';
  if (isPlaceholderImage(item) && !pendingImages.length) image.className = 'photo-placeholder';
  const label = document.createElement('label');
  label.append(document.createTextNode('Добавить фото'));
  const file = document.createElement('input');
  file.type = 'file';
  file.accept = 'image/jpeg,image/png,image/webp';
  file.multiple = true;
  file.onchange = chooseImages;
  label.append(file);
  photo.append(image, label);
  const help = document.createElement('span');
  help.className = 'photo-help';
  help.textContent = 'До 8 фото. Можно выбрать несколько сразу; порядок меняется стрелками.';
  const thumbs = document.createElement('div');
  thumbs.className = 'photo-thumbs';
  const urls = item.image_urls || [item.image_url];
  urls.forEach((url, index) => {
    const tile = document.createElement('div');
    tile.className = 'photo-thumb' + (index === 0 ? ' is-primary' : '');
    const pick = document.createElement('button');
    pick.type = 'button';
    pick.className = 'photo-thumb__pick';
    pick.title = index === 0 ? 'Главное фото' : 'Сделать главным';
    pick.disabled = index === 0 || !selectedId || dirty;
    const thumb = document.createElement('img');
    thumb.src = safeImage(url);
    thumb.alt = `Фото ${index + 1}`;
    if (isPlaceholderImage(item)) thumb.className = 'photo-placeholder';
    pick.append(thumb);
    pick.onclick = () => setPrimaryImage(index);
    tile.append(pick);
    if (urls.length > 1 && selectedId) {
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'photo-thumb__remove';
      remove.textContent = '×';
      remove.setAttribute('aria-label', `Убрать фото ${index + 1}`);
      remove.onclick = () => removeImage(index);
      tile.append(remove);
      const order = document.createElement('span');
      order.className = 'photo-thumb__order';
      [['←', -1], ['→', 1]].forEach(([symbol, direction]) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = symbol;
        button.disabled = dirty || index + direction < 0 || index + direction >= urls.length;
        button.setAttribute('aria-label', `${direction < 0 ? 'Сдвинуть раньше' : 'Сдвинуть позже'} фото ${index + 1}`);
        button.onclick = () => moveImage(index, direction);
        order.append(button);
      });
      tile.append(order);
    }
    thumbs.append(tile);
  });
  const pending = document.createElement('div');
  pending.className = 'photo-pending';
  pending.id = 'photo-pending';
  stack.append(photo, help, thumbs, pending);
  queueMicrotask(renderPendingQueue);
  return stack;
}

function renderEditor() {
  const root = $('#editor');
  root.replaceChildren();
  if (!draft) { renderEmptyEditor(root); return; }
  const copy = groupCopy(draft);
  const form = document.createElement('form');
  form.id = 'catalog-form';
  form.noValidate = true;
  form.addEventListener('submit', saveItem);

  if (innerWidth <= 900) {
    const back = document.createElement('button');
    back.type = 'button';
    back.className = 'mobile-back';
    back.textContent = '← К списку товаров';
    back.onclick = () => $('.sidebar').scrollIntoView({block: 'start'});
    form.append(back);
  }

  const head = document.createElement('div');
  head.className = 'editor-head';
  const photoStack = renderPhotoStack(draft);
  const title = document.createElement('div');
  title.className = 'title-block';
  const eyebrow = document.createElement('span');
  eyebrow.className = 'eyebrow';
  eyebrow.textContent = selectedId ? `Редактирование · ${itemGroup(draft) === 'teaware' ? 'посуда' : 'чай'}` : copy.title;
  const heading = document.createElement('h2');
  heading.id = 'editor-name';
  heading.textContent = draft.name || 'Без названия';
  const note = document.createElement('p');
  note.textContent = selectedId
    ? `Адрес карточки: /${itemGroup(draft) === 'teaware' ? 'teaware' : 'tea'}/${draft.id}`
    : 'Название создаст безопасную ссылку автоматически. Технический ID можно не открывать.';
  const saveState = document.createElement('span');
  saveState.className = 'save-state';
  saveState.id = 'save-state';
  saveState.textContent = dirty ? 'Есть несохранённые изменения' : 'Все изменения сохранены';
  const links = document.createElement('div');
  links.className = 'editor-links';
  const preview = document.createElement('button');
  preview.type = 'button';
  preview.className = 'editor-link';
  preview.textContent = 'Предпросмотр карточки';
  preview.onclick = showPreview;
  links.append(preview);
  if (selectedId && draft.published) {
    const open = document.createElement('a');
    open.className = 'editor-link';
    const section = itemGroup(draft) === 'teaware' ? 'teaware' : 'tea';
    open.href = `/${section}/${encodeURIComponent(draft.id)}`;
    open.target = '_blank';
    open.rel = 'noopener';
    open.textContent = 'Открыть на сайте ↗';
    links.append(open);
  }
  if (selectedId) {
    const duplicate = document.createElement('button');
    duplicate.type = 'button';
    duplicate.className = 'editor-link';
    duplicate.textContent = 'Создать похожий товар';
    duplicate.onclick = cloneItem;
    links.append(duplicate);
  }
  title.append(eyebrow, heading, note, saveState);
  if (draft.saby) {
    const link = document.createElement('span');
    link.className = 'saby-link' + (draft.saby.image_pending ? ' saby-photo-pending' : '');
    link.textContent = draft.saby.image_pending
      ? 'Связано со СБИС · фотографию можно добавить позже'
      : 'Связано со СБИС';
    title.append(link);
  }
  head.append(photoStack, title, links);
  form.append(head);

  const errors = document.createElement('div');
  errors.id = 'form-errors';
  errors.className = 'form-errors';
  errors.setAttribute('role', 'alert');
  errors.tabIndex = -1;
  errors.hidden = true;
  form.append(errors);

  if (draft.saby?.image_pending) {
    const warning = document.createElement('div');
    warning.className = 'saby-warning';
    warning.textContent = 'Черновик из СБИС. Проверьте тип товара, категорию, единицу и цену. Карточку можно сохранить и опубликовать сейчас, а фото и тексты добавить позже.';
    form.append(warning);
  }

  const completion = document.createElement('div');
  completion.id = 'completion';
  buildCompletion(draft, completion);

  const base = document.createElement('div');
  base.className = 'section';
  base.innerHTML = '<h3>Продажа и публикация</h3>';
  const baseGrid = document.createElement('div');
  baseGrid.className = 'grid three';
  const typeField = document.createElement('label');
  typeField.className = 'field';
  const typeTitle = document.createElement('span');
  typeTitle.textContent = 'Категория';
  const typeSelect = document.createElement('select');
  typeSelect.className = 'select';
  typeSelect.name = 'type';
  const groups = {tea: document.createElement('optgroup'), teaware: document.createElement('optgroup')};
  groups.tea.label = 'Чай';
  groups.teaware.label = 'Посуда';
  catalog.types.forEach(type => {
    const option = document.createElement('option');
    option.value = type.id;
    option.textContent = type.name;
    option.selected = type.id === draft.type;
    (groups[type.group] || groups.tea).append(option);
  });
  typeSelect.append(groups.tea, groups.teaware);
  typeField.append(typeTitle, typeSelect);
  const priceField = makeInput(draft.unit === 'pc' ? 'Цена за штуку, ₽' : 'Цена за 10 г, ₽', 'price', draft.price, 'number');
  priceField.id = 'price-field';
  const priceInput = $('input', priceField);
  priceInput.min = '0';
  priceInput.step = '1';
  const unitField = document.createElement('label');
  unitField.className = 'field';
  unitField.innerHTML = '<span>Единица продажи</span><select class="select" name="unit"><option value="g">Вес, цена за 10 г</option><option value="pc">Поштучно</option></select>';
  $('select', unitField).value = draft.unit;
  const checks = document.createElement('div');
  checks.className = 'checks field--wide';
  checks.innerHTML = `<label class="check"><input type="checkbox" name="stock" ${draft.stock ? 'checked' : ''}><span>В наличии<small>Покупатель может добавить товар в корзину</small></span></label><label class="check"><input type="checkbox" name="published" ${draft.published ? 'checked' : ''}><span>Показывать на сайте<small>${draft.saby?.image_pending ? 'Пока без фото будет показана нейтральная заглушка' : 'Скрытая карточка остаётся в панели'}</small></span></label>`;
  baseGrid.append(typeField, priceField, unitField, checks);
  base.append(baseGrid);
  const technical = document.createElement('details');
  technical.className = 'technical';
  const technicalSummary = document.createElement('summary');
  technicalSummary.textContent = 'Технические настройки ссылки';
  const technicalBody = document.createElement('div');
  technicalBody.className = 'technical__body';
  const idField = makeInput('ID для ссылки', 'id', draft.id);
  const idInput = $('input', idField);
  idInput.placeholder = 'Появится из названия';
  idInput.title = 'После первого сохранения ID нельзя изменить';
  if (selectedId) idInput.disabled = true;
  else idInput.dataset.auto = 'true';
  technicalBody.append(idField);
  technical.append(technicalSummary, technicalBody);
  base.append(technical);
  form.append(base);
  form.append(completion);

  const textSection = document.createElement('div');
  textSection.className = 'section';
  const textHeading = document.createElement('h3');
  textHeading.textContent = copy.section;
  const textNote = document.createElement('p');
  textNote.className = 'section-note';
  textNote.textContent = copy.note;
  const tabs = document.createElement('div');
  tabs.className = 'langs';
  tabs.setAttribute('role', 'tablist');
  for (const [language, label] of LANGUAGES) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'lang';
    button.dataset.lang = language;
    button.id = `catalog-lang-tab-${language}`;
    button.setAttribute('role', 'tab');
    button.setAttribute('aria-selected', String(language === 'ru'));
    button.setAttribute('aria-controls', `catalog-lang-panel-${language}`);
    button.tabIndex = language === 'ru' ? 0 : -1;
    const text = document.createElement('span');
    text.textContent = label;
    const status = document.createElement('span');
    status.className = 'lang__status';
    status.dataset.langStatus = language;
    button.append(text, status);
    button.onclick = () => switchLanguage(language);
    button.onkeydown = event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const current = LANGUAGES.findIndex(([code]) => code === language);
      const next = event.key === 'Home' ? 0
        : event.key === 'End' ? LANGUAGES.length - 1
          : (current + (event.key === 'ArrowRight' ? 1 : -1) + LANGUAGES.length) % LANGUAGES.length;
      switchLanguage(LANGUAGES[next][0], true);
    };
    tabs.append(button);
  }
  textSection.append(textHeading, textNote, tabs);
  for (const [language] of LANGUAGES) {
    const panel = document.createElement('div');
    panel.className = 'grid language-panel' + (language === 'ru' ? ' is-active' : '');
    panel.dataset.language = language;
    panel.id = `catalog-lang-panel-${language}`;
    panel.setAttribute('role', 'tabpanel');
    panel.setAttribute('aria-labelledby', `catalog-lang-tab-${language}`);
    panel.hidden = language !== 'ru';
    const translation = draft.translations[language] || {};
    copy.fields.slice(0, 3).forEach(([field, label, control]) => {
      panel.append(control === 'textarea'
        ? makeTextarea(label, `${language}.${field}`, translation[field])
        : makeInput(label, `${language}.${field}`, translation[field]));
    });
    const optional = document.createElement('details');
    optional.className = 'optional-fields';
    const optionalSummary = document.createElement('summary');
    const optionalTitle = document.createElement('span');
    optionalTitle.textContent = itemGroup(draft) === 'tea'
      ? 'Маркировка и хранение'
      : 'Характеристики и уход';
    const optionalStatus = document.createElement('small');
    optionalStatus.dataset.optionalStatus = language;
    optionalSummary.append(optionalTitle, optionalStatus);
    const optionalBody = document.createElement('div');
    optionalBody.className = 'optional-fields__body';
    copy.fields.slice(3).forEach(([field, label, control]) => {
      optionalBody.append(control === 'textarea'
        ? makeTextarea(label, `${language}.${field}`, translation[field])
        : makeInput(label, `${language}.${field}`, translation[field]));
    });
    optional.append(optionalSummary, optionalBody);
    panel.append(optional);
    textSection.append(panel);
  }
  form.append(textSection);

  if (itemGroup(draft) === 'tea') {
    const tasteSection = document.createElement('div');
    tasteSection.className = 'section';
    tasteSection.innerHTML = '<h3>Вкусовой профиль · от 0 до 5</h3>';
    const taste = document.createElement('div');
    taste.className = 'taste';
    Object.entries(AXES).forEach(([axis, label]) => {
      const field = document.createElement('label');
      const name = document.createElement('span');
      name.textContent = label;
      const output = document.createElement('output');
      output.textContent = draft.taste[axis] || 0;
      const range = document.createElement('input');
      range.type = 'range';
      range.min = '0';
      range.max = '5';
      range.step = '1';
      range.name = `taste.${axis}`;
      range.value = draft.taste[axis] || 0;
      range.oninput = () => { output.textContent = range.value; };
      field.append(name, output, range);
      taste.append(field);
    });
    tasteSection.append(taste);
    form.append(tasteSection);
  }

  const actions = document.createElement('div');
  actions.className = 'footer-actions';
  const hint = document.createElement('span');
  hint.className = 'hint';
  hint.id = 'publish-hint';
  hint.textContent = draft.published
    ? 'После сохранения изменения сразу появятся на сайте'
    : 'Карточка пока скрыта от покупателей';
  const save = document.createElement('button');
  save.type = 'submit';
  save.className = 'btn btn--primary';
  save.id = 'save';
  save.textContent = 'Сохранить товар';
  save.disabled = !dirty;
  actions.append(hint, save);
  form.append(actions);
  root.append(form);

  const nameFields = LANGUAGES.map(([language]) => form.elements.namedItem(`${language}.name`));
  const syncName = () => {
    const value = nameFields.map(field => field.value.trim()).find(Boolean) || '';
    heading.textContent = value || 'Без названия';
    if (!selectedId && idInput.dataset.auto === 'true') idInput.value = suggestedId(value);
  };
  nameFields.forEach(field => field.addEventListener('input', syncName));
  if (!selectedId) idInput.addEventListener('input', event => {
    if (event.isTrusted) idInput.dataset.auto = 'false';
  });
  typeSelect.addEventListener('change', () => changeCategory(form));
  form.elements.namedItem('unit').addEventListener('change', event => {
    $('#price-field > span').textContent = event.target.value === 'pc' ? 'Цена за штуку, ₽' : 'Цена за 10 г, ₽';
  });
  form.addEventListener('input', () => {
    clearFormErrors();
    setDirty(true);
    updateEditorFeedback(form);
  });
  syncName();
  updateEditorFeedback(form);
}

function switchLanguage(language, focus = false) {
  $$('.lang').forEach(button => {
    const active = button.dataset.lang === language;
    button.setAttribute('aria-selected', String(active));
    button.tabIndex = active ? 0 : -1;
    if (active && focus) button.focus();
  });
  $$('.language-panel').forEach(panel => {
    const active = panel.dataset.language === language;
    panel.classList.toggle('is-active', active);
    panel.hidden = !active;
  });
}

function formItem(form) {
  const data = new FormData(form);
  const translations = {};
  for (const [language] of LANGUAGES) {
    translations[language] = {};
    for (const field of ['name', 'orig', 'desc', 'composition', 'manufacturer', 'shelf_life', 'storage']) {
      translations[language][field] = String(data.get(`${language}.${field}`) || '').trim();
    }
  }
  const taste = clone(draft.taste || {});
  Object.keys(AXES).forEach(axis => {
    if (data.has(`taste.${axis}`)) taste[axis] = Number(data.get(`taste.${axis}`) || 0);
    else if (!Number.isInteger(taste[axis])) taste[axis] = 0;
  });
  return {
    ...draft,
    id: selectedId || String(data.get('id') || '').trim().toLowerCase(),
    type: String(data.get('type')),
    price: Number(data.get('price')),
    unit: String(data.get('unit')),
    stock: data.has('stock'),
    published: data.has('published'),
    taste,
    translations,
  };
}

function updateEditorFeedback(form) {
  const item = formItem(form);
  buildCompletion(item, $('#completion'));
  for (const [language] of LANGUAGES) {
    const count = missingByLanguage(item, language).length;
    const status = $(`[data-lang-status="${language}"]`);
    if (status) status.textContent = count ? String(count) : '✓';
    const optionalStatus = $(`[data-optional-status="${language}"]`);
    if (optionalStatus) {
      const text = item.translations?.[language] || {};
      const missing = groupCopy(item).fields.slice(3).filter(([field]) => !visibleText(text[field])).length;
      optionalStatus.textContent = missing ? `не заполнено: ${missing}` : 'заполнено';
    }
  }
  const hint = $('#publish-hint');
  if (hint) hint.textContent = item.published
    ? completionState(item).level === 'ready'
      ? 'Готовая карточка появится на сайте после сохранения'
      : 'Карточка появится на сайте, а напоминания останутся в панели'
    : 'Карточка пока скрыта от покупателей';
}

function changeCategory(form) {
  const item = formItem(form);
  const oldGroup = itemGroup(draft);
  const nextGroup = itemGroup(item);
  if (oldGroup !== nextGroup) item.unit = GROUP_COPY[nextGroup].unit;
  draft = item;
  renderEditor();
  setDirty(true);
  toast(nextGroup === 'teaware'
    ? 'Форма переключена на посуду: цена теперь поштучная, показаны характеристики посуды.'
    : 'Форма переключена на чай: цена указана за 10 г, доступен вкусовой профиль.');
}

function chooseImages(event) {
  const files = [...(event.target.files || [])];
  if (!files.length) return;
  const existing = (draft.image_urls || [draft.image_url]).length;
  const replacesPlaceholder = isPlaceholderImage(draft) || draft.saby?.image_pending;
  const maxNew = 8 - (replacesPlaceholder ? 0 : existing);
  if (files.length > maxNew) {
    event.target.value = '';
    toast(`Можно добавить ещё ${Math.max(0, maxNew)} фото. Всего — до 8.`);
    return;
  }
  for (const file of files) {
    if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) {
      event.target.value = '';
      toast('Поддерживаются JPG, PNG и WebP.');
      return;
    }
    if (file.size > 8 * 1024 * 1024) {
      event.target.value = '';
      toast('Каждое фото должно быть не больше 8 МБ.');
      return;
    }
  }
  clearPendingImages();
  pendingImages = files;
  pendingImageUrls = files.map(file => URL.createObjectURL(file));
  const preview = $('#photo-preview');
  preview.src = pendingImageUrls[0];
  preview.classList.remove('photo-placeholder');
  renderPendingQueue();
  setDirty(true);
  toast(`Выбрано фото: ${files.length}. Ниже можно проверить список до сохранения.`);
}

async function parseResponse(response) {
  let data = {};
  try { data = await response.json(); } catch (_error) { /* response without JSON */ }
  if (response.status === 401) {
    location.replace('/manage');
    throw new Error('Сессия завершена');
  }
  if (!response.ok) throw new Error(data.detail || 'Не удалось сохранить изменения');
  return data;
}

async function saveItem(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $('#save');
  const item = formItem(form);
  const errors = [];
  if (!Object.values(item.translations).some(translation => visibleText(translation.name))) {
    errors.push({message: 'Укажите название хотя бы на одном языке', name: 'ru.name', language: 'ru'});
  }
  if (!selectedId && !/^[a-z0-9][a-z0-9-]{0,79}$/.test(item.id)) {
    errors.push({message: 'Укажите адрес карточки латиницей, цифрами или дефисами', name: 'id'});
  }
  if (item.published && item.price <= 0) {
    errors.push({message: 'Для публикации укажите цену больше нуля', name: 'price'});
  }
  if (errors.length) {
    showFormErrors(form, errors);
    return;
  }
  clearFormErrors();
  button.disabled = true;
  button.textContent = 'Сохраняем карточку…';
  const creating = !selectedId;
  const queuedFiles = [...pendingImages];
  const queuedUrls = [...pendingImageUrls];
  let baseSaved = false;
  let uploaded = 0;
  try {
    const response = await fetch(
      creating ? '/api/admin/catalog/items' : `/api/admin/catalog/items/${encodeURIComponent(selectedId)}`,
      {method: creating ? 'POST' : 'PUT', headers: headers(), body: JSON.stringify({revision: catalog.revision, item})},
    );
    catalog = await parseResponse(response);
    selectedId = item.id;
    draft = clone(catalog.teas.find(current => current.id === selectedId));
    baseSaved = true;
    const replaceFirst = creating || draft.saby?.image_pending || isPlaceholderImage(draft);
    for (let index = 0; index < queuedFiles.length; index += 1) {
      button.textContent = `Загружаем фото ${index + 1} из ${queuedFiles.length}…`;
      const replace = replaceFirst && index === 0;
      const url = replace
        ? `/api/admin/catalog/items/${encodeURIComponent(selectedId)}/image?revision=${catalog.revision}`
        : `/api/admin/catalog/items/${encodeURIComponent(selectedId)}/images?revision=${catalog.revision}`;
      const upload = await fetch(url, {method: 'POST', headers: headers(false), body: queuedFiles[index]});
      catalog = await parseResponse(upload);
      draft = clone(catalog.teas.find(current => current.id === selectedId));
      uploaded += 1;
    }
    clearPendingImages();
    dirty = false;
    updateState();
    renderList();
    renderEditor();
    const state = completionState(draft);
    toast(state.level === 'ready'
      ? (creating ? 'Товар добавлен.' : 'Товар сохранён.')
      : `Товар сохранён. ${state.label}; публикация не заблокирована.`);
  } catch (error) {
    if (baseSaved) {
      queuedUrls.slice(0, uploaded).forEach(url => URL.revokeObjectURL(url));
      pendingImages = queuedFiles.slice(uploaded);
      pendingImageUrls = queuedUrls.slice(uploaded);
      updateState();
      renderList();
      renderEditor();
      setDirty(pendingImages.length > 0);
      toast(`Карточка сохранена, загружено фото: ${uploaded} из ${queuedFiles.length}. Оставшиеся можно повторить: ${error.message}`);
    } else {
      button.disabled = false;
      button.textContent = 'Сохранить товар';
      toast(error.message);
    }
  }
}

async function setPrimaryImage(index) {
  if (dirty) { toast('Сначала сохраните изменения карточки.'); return; }
  try {
    const response = await fetch(
      `/api/admin/catalog/items/${encodeURIComponent(selectedId)}/images/${index}/primary?revision=${catalog.revision}`,
      {method: 'PUT', headers: headers()},
    );
    catalog = await parseResponse(response);
    draft = clone(catalog.teas.find(item => item.id === selectedId));
    updateState(); renderList(); renderEditor();
    toast('Главное фото изменено.');
  } catch (error) { toast(error.message); }
}

async function moveImage(index, direction) {
  if (dirty) { toast('Сначала сохраните изменения карточки.'); return; }
  const target = index + direction;
  if (!selectedId || target < 0 || target >= draft.images.length) return;
  const item = clone(draft);
  [item.images[index], item.images[target]] = [item.images[target], item.images[index]];
  try {
    const response = await fetch(`/api/admin/catalog/items/${encodeURIComponent(selectedId)}`, {
      method: 'PUT', headers: headers(), body: JSON.stringify({revision: catalog.revision, item}),
    });
    catalog = await parseResponse(response);
    draft = clone(catalog.teas.find(row => row.id === selectedId));
    updateState(); renderList(); renderEditor();
    toast('Порядок фотографий сохранён.');
  } catch (error) { toast(error.message); }
}

async function removeImage(index) {
  if (dirty) { toast('Сначала сохраните изменения карточки.'); return; }
  if (!confirm('Убрать это фото из карточки?')) return;
  try {
    const response = await fetch(
      `/api/admin/catalog/items/${encodeURIComponent(selectedId)}/images/${index}?revision=${catalog.revision}`,
      {method: 'DELETE', headers: headers()},
    );
    catalog = await parseResponse(response);
    draft = clone(catalog.teas.find(item => item.id === selectedId));
    updateState(); renderList(); renderEditor();
    toast('Фото убрано из карточки.');
  } catch (error) { toast(error.message); }
}

async function moveItem(id, direction) {
  if (dirty && !confirmDiscard()) return;
  const visible = filteredItems().map(item => item.id);
  const visibleIndex = visible.indexOf(id);
  const targetId = visible[visibleIndex + direction];
  if (!targetId) return;
  const ids = catalog.teas.map(item => item.id);
  const from = ids.indexOf(id);
  const to = ids.indexOf(targetId);
  [ids[from], ids[to]] = [ids[to], ids[from]];
  try {
    const response = await fetch('/api/admin/catalog/order', {
      method: 'PUT', headers: headers(), body: JSON.stringify({revision: catalog.revision, ids}),
    });
    catalog = await parseResponse(response);
    updateState(); renderList();
    toast('Порядок внутри текущей подборки сохранён.');
  } catch (error) { toast(error.message); }
}

function showPreview() {
  const form = $('#catalog-form');
  const item = form ? formItem(form) : draft;
  const text = Object.values(item.translations).find(translation => visibleText(translation.name)) || {};
  const body = $('#preview-body');
  body.replaceChildren();
  const card = document.createElement('div');
  card.className = 'preview-card';
  const image = document.createElement('img');
  image.src = pendingImageUrls[0] || safeImage(item.image_url);
  image.alt = text.name || 'Товар';
  if (isPlaceholderImage(item) && !pendingImageUrls.length) image.className = 'photo-placeholder';
  const copy = document.createElement('div');
  const eyebrow = document.createElement('span');
  eyebrow.className = 'eyebrow';
  eyebrow.textContent = typeName(item.type);
  const heading = document.createElement('h3');
  heading.textContent = text.name || 'Без названия';
  const origin = document.createElement('p');
  origin.textContent = text.orig || 'Подзаголовок пока не заполнен';
  const description = document.createElement('p');
  description.textContent = text.desc || 'Описание можно добавить позже.';
  const price = document.createElement('span');
  price.className = 'preview-price';
  price.textContent = money(item);
  copy.append(eyebrow, heading, origin, description, price);
  card.append(image, copy);
  body.append(card);
  const state = completionState(item);
  if (state.level !== 'ready') {
    const warning = document.createElement('div');
    warning.className = 'preview-warning';
    warning.textContent = `${state.label}. Это только предпросмотр; незаполненные поля не блокируют публикацию.`;
    body.append(warning);
  }
  $('#preview-dialog').showModal();
}

function downloadCatalog() {
  if (!catalog) return;
  const snapshot = clone(catalog);
  snapshot.teas.forEach(item => { delete item.image_url; delete item.image_urls; });
  snapshot.types.forEach(item => { delete item.system; });
  const blob = new Blob([JSON.stringify(snapshot, null, 2) + '\n'], {type: 'application/json'});
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  const date = new Date().toISOString().slice(0, 10);
  link.href = url;
  link.download = `chainya-catalog-${date}-r${catalog.revision}.json`;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
  toast('Копия каталога скачана.');
}

function categoryProductCount(typeId) {
  return catalog.teas.filter(item => item.type === typeId).length;
}

function applyCategoryCatalog(next, message) {
  catalog = next;
  if (selectedId) {
    const current = catalog.teas.find(item => item.id === selectedId);
    draft = current ? clone(current) : null;
    if (!current) selectedId = '';
  }
  updateState();
  renderList();
  renderEditor();
  renderCategoryList();
  if (message) toast(message);
}

function renderCategoryList() {
  const root = $('#category-list');
  if (!catalog) return;
  root.replaceChildren();
  for (const [group, heading] of [['tea', 'Чай'], ['teaware', 'Посуда']]) {
    const categories = catalog.types.filter(item => item.group === group);
    const section = document.createElement('section');
    section.className = 'category-group';
    const title = document.createElement('h3');
    title.className = 'category-group__title';
    title.textContent = `${heading} · ${categories.length}`;
    section.append(title);
    categories.forEach((category, index) => {
      const count = categoryProductCount(category.id);
      const row = document.createElement('div');
      row.className = 'category-row';
      const copy = document.createElement('div');
      const name = document.createElement('span');
      name.className = 'category-row__name';
      name.textContent = category.names?.ru || category.name;
      const meta = document.createElement('span');
      meta.className = 'category-row__meta';
      const translations = [category.names?.en, category.names?.zh].filter(Boolean).join(' · ');
      meta.textContent = `${count} ${count === 1 ? 'товар' : count > 1 && count < 5 ? 'товара' : 'товаров'}${translations ? ` · ${translations}` : ''}`;
      copy.append(name, meta);
      const actions = document.createElement('div');
      actions.className = 'category-row__actions';
      const order = document.createElement('span');
      order.className = 'category-order';
      [['↑', -1], ['↓', 1]].forEach(([label, direction]) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'move';
        button.textContent = label;
        button.setAttribute('aria-label', `${direction < 0 ? 'Поднять' : 'Опустить'} категорию ${name.textContent}`);
        button.disabled = index + direction < 0 || index + direction >= categories.length;
        button.onclick = () => moveCategory(category.id, direction);
        order.append(button);
      });
      const edit = document.createElement('button');
      edit.type = 'button';
      edit.className = 'btn';
      edit.textContent = 'Изменить';
      edit.setAttribute('aria-label', `Изменить категорию ${name.textContent}`);
      edit.onclick = () => editCategory(category.id);
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'btn';
      remove.textContent = 'Удалить';
      remove.setAttribute('aria-label', `Удалить категорию ${name.textContent}`);
      remove.disabled = category.system || count > 0;
      remove.title = category.system
        ? 'Базовую категорию можно переименовать, но нельзя удалить'
        : count > 0 ? 'Сначала перенесите товары в другую категорию' : '';
      remove.onclick = () => deleteCategory(category.id);
      actions.append(order, edit, remove);
      row.append(copy, actions);
      section.append(row);
    });
    root.append(section);
  }
}

function showCategories() {
  if (dirty) {
    toast('Сначала сохраните или отмените изменения в открытой карточке товара.');
    return;
  }
  editingCategoryId = '';
  $('#category-form').hidden = true;
  $('#category-error').hidden = true;
  renderCategoryList();
  $('#categories-dialog').showModal();
}

function editCategory(typeId = '') {
  editingCategoryId = typeId;
  const category = typeId ? typeInfo(typeId) : null;
  const form = $('#category-form');
  form.reset();
  $('#category-form-title').textContent = category ? 'Изменить категорию' : 'Новая категория';
  form.elements.group.value = category?.group || 'tea';
  form.elements.group.disabled = Boolean(category && categoryProductCount(category.id) > 0);
  form.elements.group.title = form.elements.group.disabled
    ? 'Сначала перенесите товары в другую категорию' : '';
  form.elements.ru.value = category?.names?.ru || category?.name || '';
  form.elements.en.value = category?.names?.en || '';
  form.elements.zh.value = category?.names?.zh || '';
  form.elements.id.value = category?.id || '';
  form.elements.id.disabled = Boolean(category);
  form.elements.id.dataset.auto = category ? 'false' : 'true';
  $('#category-error').hidden = true;
  form.hidden = false;
  requestAnimationFrame(() => form.elements.ru.focus());
}

async function saveCategory(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const ru = form.elements.ru.value.trim();
  const categoryId = form.elements.id.value.trim() || suggestedCategoryId(ru);
  const item = {
    id: categoryId,
    group: form.elements.group.value,
    name: ru,
    names: {
      ru,
      en: form.elements.en.value.trim(),
      zh: form.elements.zh.value.trim(),
    },
  };
  const submit = form.querySelector('[type="submit"]');
  const error = $('#category-error');
  submit.disabled = true;
  error.hidden = true;
  try {
    const response = await fetch(
      editingCategoryId
        ? `/api/admin/catalog/types/${encodeURIComponent(editingCategoryId)}`
        : '/api/admin/catalog/types',
      {
        method: editingCategoryId ? 'PUT' : 'POST',
        headers: headers(),
        body: JSON.stringify({revision: catalog.revision, item}),
      },
    );
    const next = await parseResponse(response);
    editingCategoryId = item.id;
    form.hidden = true;
    applyCategoryCatalog(next, response.status === 201 ? 'Категория добавлена.' : 'Категория обновлена.');
  } catch (failure) {
    error.textContent = failure.message;
    error.hidden = false;
  } finally {
    submit.disabled = false;
  }
}

async function moveCategory(typeId, direction) {
  const group = typeInfo(typeId)?.group;
  const groupIds = catalog.types.filter(item => item.group === group).map(item => item.id);
  const position = groupIds.indexOf(typeId);
  const otherId = groupIds[position + direction];
  if (!otherId) return;
  const ids = catalog.types.map(item => item.id);
  const first = ids.indexOf(typeId);
  const second = ids.indexOf(otherId);
  [ids[first], ids[second]] = [ids[second], ids[first]];
  try {
    const response = await fetch('/api/admin/catalog/type-order', {
      method: 'PUT', headers: headers(), body: JSON.stringify({revision: catalog.revision, ids}),
    });
    applyCategoryCatalog(await parseResponse(response), 'Порядок категорий сохранён.');
  } catch (error) { toast(error.message); }
}

async function deleteCategory(typeId) {
  const category = typeInfo(typeId);
  if (!category || !confirm(`Удалить пустую категорию «${category.name}»?`)) return;
  try {
    const response = await fetch(
      `/api/admin/catalog/types/${encodeURIComponent(typeId)}?revision=${catalog.revision}`,
      {method: 'DELETE', headers: headers(false)},
    );
    applyCategoryCatalog(await parseResponse(response), 'Пустая категория удалена.');
  } catch (error) { toast(error.message); }
}

async function showHistory() {
  const dialog = $('#history-dialog');
  const list = $('#history-list');
  list.innerHTML = '<div class="history-empty">Загрузка…</div>';
  dialog.showModal();
  try {
    const response = await fetch('/api/admin/catalog/history?limit=50', {cache: 'no-store'});
    const data = await parseResponse(response);
    list.replaceChildren();
    if (!data.history.length) {
      list.innerHTML = '<div class="history-empty">Изменений пока нет</div>';
      return;
    }
    data.history.forEach(item => {
      const row = document.createElement('div');
      row.className = 'history-row';
      const copy = document.createElement('div');
      const title = document.createElement('strong');
      title.textContent = HISTORY_ACTIONS[item.action] || 'Изменён каталог';
      const detail = document.createElement('span');
      detail.textContent = item.item_name || item.item_id || `Версия ${item.revision}`;
      const time = document.createElement('time');
      time.dateTime = item.created_at;
      time.textContent = formatDate(item.created_at);
      copy.append(title, detail);
      row.append(copy, time);
      list.append(row);
    });
  } catch (error) { list.innerHTML = `<div class="history-empty"></div>`; $('.history-empty', list).textContent = error.message; }
}

function updateSabyStat(review, error = '') {
  const card = $('#stat-saby-card');
  const value = $('#stat-saby');
  const note = $('#stat-saby-note');
  card.classList.toggle('is-error', Boolean(error));
  if (error) { value.textContent = '—'; note.textContent = 'Не удалось получить данные'; return; }
  const selected = review.counts.saby_items;
  const base = review.counts.base_items ?? selected;
  const outside = review.counts.not_in_price_list || 0;
  value.textContent = base;
  note.textContent = outside ? `${selected} в прайс-листе сайта · ${outside} вне его` : `${selected} в прайс-листе сайта`;
}

async function fetchSabyReview() {
  const response = await fetch('/api/admin/saby/catalog-review', {cache: 'no-store'});
  sabyReview = await parseResponse(response);
  updateSabyStat(sabyReview);
  return sabyReview;
}

async function loadSabyStat() {
  try { await fetchSabyReview(); } catch (error) { updateSabyStat(null, error.message); }
}

async function showSaby() {
  const dialog = $('#saby-dialog');
  const summary = $('#saby-summary');
  const list = $('#saby-list');
  summary.textContent = 'Загрузка данных из СБИС…';
  list.replaceChildren();
  dialog.showModal();
  try {
    const review = await fetchSabyReview();
    const base = review.counts.base_items ?? review.counts.saby_items;
    const outside = review.counts.not_in_price_list || 0;
    summary.innerHTML = `<strong></strong>`;
    $('strong', summary).textContent = `В прайс-листе сайта: ${review.counts.saby_items} · в основном каталоге: ${base}`;
    summary.append(document.createTextNode(` · ${review.counts.linked} связаны · ${review.counts.new} новых · ${outside} вне прайс-листа. Только чтение.`));
    const rows = review.items.filter(item => item.status === 'new' || item.status === 'not_in_price_list');
    if (!rows.length) { list.innerHTML = '<div class="history-empty">Все доступные позиции СБИС уже связаны с сайтом</div>'; return; }
    rows.forEach(item => {
      const row = document.createElement('div');
      row.className = 'saby-row';
      const copy = document.createElement('div');
      const name = document.createElement('span');
      name.className = 'saby-row__name'; name.textContent = item.name;
      const meta = document.createElement('span');
      meta.className = 'saby-row__meta';
      const stockText = item.suggested_stock === null ? 'остаток не определён' : item.suggested_stock ? 'в наличии' : 'нет в наличии';
      meta.textContent = item.suggested_price === null ? 'Цена для сайта не определена' : `${new Intl.NumberFormat('ru-RU').format(item.suggested_price)} ₽/${item.unit === 'pc' ? 'шт' : '10 г'} · ${stockText}`;
      const note = document.createElement('span');
      note.className = 'saby-row__note'; note.textContent = item.note;
      const status = document.createElement('span');
      status.className = 'saby-status new';
      const outsidePrice = item.status === 'not_in_price_list';
      status.textContent = outsidePrice ? 'Вне прайс-листа сайта' : 'Новая позиция';
      copy.append(name, meta, note, status);
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'btn';
      button.textContent = outsidePrice ? 'Сначала добавить в прайс-лист СБИС' : 'Открыть скрытый черновик';
      button.disabled = !item.can_create_draft;
      button.onclick = () => showSabyKindDialog(item);
      row.append(copy, button); list.append(row);
    });
  } catch (error) { summary.textContent = `СБИС не ответил: ${error.message}`; }
}

async function loadCatalog(preserve = true) {
  try {
    const response = await fetch('/api/admin/catalog', {cache: 'no-store'});
    catalog = await parseResponse(response);
    updateState();
    if (preserve && selectedId && catalog.teas.some(item => item.id === selectedId)) {
      draft = clone(catalog.teas.find(item => item.id === selectedId));
    } else {
      selectedId = '';
      draft = null;
    }
    dirty = false;
    clearPendingImages();
    renderList();
    renderEditor();
  } catch (error) {
    toast(error.message);
    $('#items').textContent = 'Не удалось загрузить каталог';
  }
}

$('#search').addEventListener('input', renderList);
$('#catalog-filter').addEventListener('change', event => {
  $$('[data-stat-filter]').forEach(node => {
    node.setAttribute('aria-pressed', String(node.dataset.statFilter === event.target.value));
  });
  renderList();
});
$$('[data-stat-filter]').forEach(button => {
  button.addEventListener('click', () => {
    $('#catalog-filter').value = button.dataset.statFilter;
    $$('[data-stat-filter]').forEach(node => node.setAttribute('aria-pressed', String(node === button)));
    renderList();
    if (innerWidth <= 900) $('.sidebar').scrollIntoView({block: 'start', behavior: 'smooth'});
  });
});
$('#group-filter').addEventListener('click', event => {
  const button = event.target.closest('[data-group]');
  if (!button) return;
  groupFilter = button.dataset.group;
  $$('#group-filter button').forEach(node => node.setAttribute('aria-pressed', String(node === button)));
  renderList();
});
$('#add').onclick = showAddDialog;
$('#add-top').onclick = showAddDialog;
$$('[data-add-group]').forEach(button => { button.onclick = () => chooseNewGroup(button.dataset.addGroup); });
$('#close-kind').onclick = () => $('#kind-dialog').close();
$('#close-preview').onclick = () => $('#preview-dialog').close();
$('#show-saby').onclick = showSaby;
$('#stat-saby-card').onclick = showSaby;
$('#close-saby').onclick = () => $('#saby-dialog').close();
$('#export-catalog').onclick = downloadCatalog;
$('#show-history').onclick = showHistory;
$('#close-history').onclick = () => $('#history-dialog').close();
$('#manage-categories').onclick = showCategories;
$('#close-categories').onclick = () => $('#categories-dialog').close();
$('#add-category').onclick = () => editCategory();
$('#cancel-category').onclick = () => { editingCategoryId = ''; $('#category-form').hidden = true; };
$('#category-form').onsubmit = saveCategory;
$('#category-form').elements.ru.addEventListener('input', event => {
  const id = $('#category-form').elements.id;
  if (id.dataset.auto === 'true') id.value = suggestedCategoryId(event.target.value);
});
$('#category-form').elements.id.addEventListener('input', event => {
  event.target.dataset.auto = 'false';
});
for (const dialog of $$('dialog')) {
  dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
}
$('#reload').onclick = () => { if (confirmDiscard()) { loadCatalog(true); loadSabyStat(); } };
$('#logout').onclick = async () => {
  if (!confirmDiscard()) return;
  await fetch('/api/admin/session', {method: 'DELETE'});
  location.replace('/manage');
};
addEventListener('beforeunload', event => {
  if (dirty) { event.preventDefault(); event.returnValue = ''; }
});
addEventListener('keydown', event => {
  if (!(event.metaKey || event.ctrlKey) || event.key.toLocaleLowerCase('ru') !== 's') return;
  const form = $('#catalog-form');
  if (!form || !dirty) return;
  event.preventDefault();
  form.requestSubmit();
});

loadCatalog(false);
loadSabyStat();
