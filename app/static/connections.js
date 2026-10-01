'use strict';
let connections = [];
let editingConnection = null;
let displayedModels = [];
let displayedDate = null;
let displayedFolder = null;
let connectionBusy = false;
let connectionDirty = false;

function modelLabel(agent) {
  return agent.connection_id ? `${agent.source} · ${agent.connection_name} · ${agent.model_id || 'Модель не выбрана'}` : 'Модель не подключена';
}
async function loadConnections() { connections = await api('/connections'); }
function option(value, label) {
  const node = element('option', '', label);
  node.value = value;
  return node;
}
function installPicker(prefix) {
  const box = element('div', 'model-picker');
  // Static markup only; all external text uses textContent / option().
  box.innerHTML = `<label for="${prefix}-connection">Источник / подключение</label><select id="${prefix}-connection"></select>
    <a id="${prefix}-add-connection" href="#connections">Добавить подключение</a>
    <label for="${prefix}-model-search">Поиск модели по ID</label><input id="${prefix}-model-search" type="search" placeholder="Часть ID модели">
    <label for="${prefix}-model">Модель</label><select id="${prefix}-model"></select>
    <p id="${prefix}-model-hint" class="muted"></p>`;
  $(prefix + '-instruction').after(box);
  $(prefix + '-connection').onchange = () => {
    $(prefix + '-model-search').value = '';
    populateModels(prefix, null);
  };
  $(prefix + '-model-search').oninput = () => populateModels(prefix, $(prefix + '-model').value);
  $(prefix + '-add-connection').onclick = () => {
    if (state.busy) return false;
    if ($('create-dialog').open) $('create-dialog').close();
    openConnection(null);
  };
}
function populatePicker(prefix, connectionId, modelId) {
  const select = $(prefix + '-connection');
  select.replaceChildren(option('', 'Без подключения'));
  connections.forEach(item => select.append(option(item.id, `Яндекс AI Studio · ${item.name}`)));
  select.value = connectionId || '';
  $(prefix + '-add-connection').hidden = connections.length > 0;
  $(prefix + '-model-search').value = '';
  populateModels(prefix, modelId);
}
function populateModels(prefix, selected) {
  const connection = connections.find(item => item.id === Number($(prefix + '-connection').value));
  const select = $(prefix + '-model');
  const query = $(prefix + '-model-search').value.toLowerCase();
  const fresh = connection && connection.models_folder_id === connection.folder_id;
  const models = fresh ? connection.models : [];
  select.replaceChildren(option('', 'Без модели — выбрать позже'));
  for (const model of models) {
    if (model.id.toLowerCase().includes(query) || model.id === selected) select.append(option(model.id, `${model.id} — ${model.owned_by}`));
  }
  const missing = selected && !models.some(model => model.id === selected);
  if (missing) select.append(option(selected, `${selected} — отсутствует в актуальном списке (выбор сохранён)`));
  select.value = selected || '';
  select.disabled = !connection;
  $(prefix + '-model-search').disabled = !connection;
  $(prefix + '-model-hint').textContent = missing ? 'Модель отсутствует в актуальном списке. Автоматической замены не будет.' :
    !connection ? 'Можно создать агента без модели и назначить её позже.' :
    !fresh ? 'Получите список моделей в разделе «Подключения».' :
    `Моделей: ${models.length}. Выбор сохраняет настройку; поддержка чата и инструментов не определяется.`;
}
function renderConnectionList() {
  const list = $('connection-list');
  list.replaceChildren();
  if (!connections.length) list.append(element('p', 'empty', 'Подключений пока нет. Добавьте Яндекс AI Studio.'));
  for (const item of connections) {
    const button = element('button', 'connection-card');
    button.append(element('strong', '', item.name), element('span', 'muted', `Яндекс AI Studio · моделей: ${item.models.length}`));
    button.onclick = () => { if (!connectionBusy && (!connectionDirty || confirm('Открыть другое подключение? Несохранённые изменения будут потеряны.'))) openConnection(item); };
    list.append(button);
  }
}
function openConnection(item) {
  editingConnection = item?.id || null;
  connectionDirty = false;
  $('connection-form').hidden = false;
  $('connection-heading').textContent = item ? 'Настройки подключения' : 'Новое подключение';
  $('connection-name').value = item?.name || '';
  $('connection-folder').value = item?.folder_id || '';
  $('connection-key').value = '';
  $('key-status').textContent = item?.key_saved ? 'Ключ сохранён. Оставьте поле пустым, чтобы сохранить прежний ключ.' : 'Ключ не сохранён.';
  $('refresh-models').hidden = !item;
  $('delete-connection').hidden = !item;
  $('connection-next-step').hidden = !item;
  displayedModels = item?.models || [];
  displayedDate = item?.models_updated_at || null;
  displayedFolder = item?.models_folder_id || null;
  $('model-search').value = '';
  notice('connection-notice');
  renderModels();
}
function renderModels() {
  const query = $('model-search').value.toLowerCase();
  const models = displayedModels.filter(model => model.id.toLowerCase().includes(query));
  $('model-summary').textContent = `Моделей: ${displayedModels.length}. Показано: ${models.length}. Последнее успешное обновление: ${displayedDate ? new Date(displayedDate).toLocaleString('ru-RU') : 'ещё не было'}.`;
  notice('cache-warning', displayedDate && displayedFolder !== $('connection-folder').value.trim() ? 'Показан предыдущий список для другого Folder ID. Получите модели для нового каталога.' : '', true);
  const list = $('connection-models');
  list.replaceChildren();
  for (const model of models) {
    const row = element('div', 'model-row');
    row.append(element('code', '', model.id), element('span', 'muted', model.owned_by));
    list.append(row);
  }
  if (!models.length) list.append(element('p', 'muted', displayedDate ? 'Моделей по этому запросу нет.' : 'Нажмите «Получить модели».'));
}
function connectionBody() {
  return {name: $('connection-name').value.trim(), provider: 'yandex', folder_id: $('connection-folder').value.trim(), api_key: $('connection-key').value};
}
async function connectionAction(message, action) {
  if (connectionBusy || state.busy) return;
  connectionBusy = true;
  state.busy = true;
  const hash = location.hash;
  const controls = [...$('connection-form').querySelectorAll('button,input,select')];
  controls.forEach(node => node.disabled = true);
  notice('connection-notice', message);
  $('connection-form').setAttribute('aria-busy', 'true');
  try { await action(); }
  catch (error) { notice('connection-notice', error.message, true); }
  finally {
    controls.forEach(node => node.disabled = false);
    connectionBusy = false;
    state.busy = false;
    $('connection-form').setAttribute('aria-busy', 'false');
    if (hash !== location.hash) await route();
  }
}
$('add-connection').onclick = () => {
  if (!connectionBusy && (!connectionDirty || confirm('Начать новое подключение? Несохранённые изменения будут потеряны.'))) openConnection(null);
};
$('connection-form').addEventListener('input', event => {
  if (event.target.id !== 'model-search') connectionDirty = true;
  if (event.target.id === 'connection-folder') renderModels();
});
$('model-search').oninput = renderModels;
$('create-connected-agent').onclick = () => {
  if (connectionBusy || state.busy) return;
  openCreate();
  populatePicker('create', editingConnection, null);
};
$('get-models').onclick = () => {
  const body = connectionBody();
  connectionAction('Получение моделей…', async () => {
    const data = await api(editingConnection ? `/connections/${editingConnection}/preview` : '/connections/preview', 'POST', body);
    displayedModels = data.models;
    displayedDate = data.models_updated_at;
    displayedFolder = data.models_folder_id;
    renderModels();
    notice('connection-notice', 'Модели получены. Нажмите «Сохранить», чтобы сохранить подключение и список.');
    connectionDirty = true;
  });
};
$('connection-form').onsubmit = event => {
  event.preventDefault();
  const body = connectionBody();
  connectionAction('Сохранение…', async () => {
    const data = await api(editingConnection ? `/connections/${editingConnection}` : '/connections', editingConnection ? 'PUT' : 'POST', body);
    openConnection(data);
    notice('connection-notice', 'Подключение сохранено.');
    await loadConnections();
    renderConnectionList();
  });
};
$('refresh-models').onclick = () => {
  if (connectionDirty) { notice('connection-notice', 'Сначала сохраните изменения формы или нажмите «Получить модели» для проверки введённых данных.', true); return; }
  connectionAction('Обновление списка моделей…', async () => {
    const data = await api(`/connections/${editingConnection}/refresh`, 'POST');
    openConnection(data);
    notice('connection-notice', 'Список моделей обновлён и сохранён.');
    await loadConnections();
    renderConnectionList();
  });
};
$('delete-connection').onclick = () => {
  if (!confirm('Удалить подключение и его сохранённый ключ?')) return;
  connectionAction('Удаление…', async () => {
    await api(`/connections/${editingConnection}`, 'DELETE');
    openConnection(null);
    await loadConnections();
    renderConnectionList();
    notice('connection-notice', 'Подключение удалено.');
  });
};
installPicker('create');
installPicker('settings');
route();
