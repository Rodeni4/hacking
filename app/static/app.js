'use strict';
const $ = id => document.getElementById(id);
const state = {agent: null, conversations: [], conversation: null, epoch: 0, busy: false, drafts: new Map()};
let activeGeneration = null;
let retryMessageId = null;
for (const prefix of ['create', 'settings']) {
  const tools = element('fieldset', 'agent-tools');
  tools.append(element('legend', '', 'Инструменты агента'));
  for (const [name, title] of [['web_search', 'Веб-поиск и чтение страниц'], ['code_interpreter', 'Интерпретатор Python и файлы его контейнера']]) {
    const label = element('label', 'tool-option');
    const checkbox = element('input');
    checkbox.type = 'checkbox';
    checkbox.id = `${prefix}-${name}`;
    label.append(checkbox, document.createTextNode(title));
    tools.append(label);
  }
  tools.append(element('p', 'muted', 'Чекбоксы разрешают две группы инструментов Яндекса. Поиск и чтение страниц включаются вместе; Python может просматривать файлы своего контейнера. Отдельных переключателей внутренних функций в публичном API нет. Доступа к файлам вашего компьютера нет.'));
  tools.append(element('p', 'muted', 'Модель сама выбирает, когда использовать включённые инструменты. Работа инструментов оплачивается по тарифам Яндекса.'));
  $(prefix + '-instruction').after(tools);
}
const requestPanel = element('details', 'request-panel');
requestPanel.hidden = true;
requestPanel.setAttribute('aria-label', 'Состояние запроса');
requestPanel.innerHTML = '<summary class="request-toggle"><span id="request-title" role="status"></span><span class="request-expand">Развернуть ▾</span><span class="request-collapse">Свернуть ▴</span></summary><p id="request-description"></p><p id="request-action"></p><details><summary>Подробности запроса</summary><pre id="request-details"></pre></details><div class="request-actions"><button id="request-settings" type="button">Настройки агента</button><a href="#connections">Подключения</a><button id="request-check" type="button">Проверить статус</button></div>';
$('chat-notice').before(requestPanel);
$('request-settings').onclick = () => setTab('settings');
$('request-check').onclick = () => { if (!state.busy && state.conversation) selectConversation(state.conversation); };
function requestStatus(title, description, action = '', details = '', kind = '') {
  requestPanel.hidden = false;
  requestPanel.className = 'request-panel ' + kind;
  $('request-title').textContent = title;
  $('request-description').textContent = description;
  $('request-action').textContent = action;
  $('request-details').textContent = details;
}
function showGeneration(generation) {
  const diagnostic = generation.diagnostics || {};
  const elapsed = generation.elapsed_seconds == null ? 'не зафиксировано' : `${Math.floor(generation.elapsed_seconds)} с`;
  const details = `Запрос №${generation.id}\nМодель: ${generation.model_id}\nВремя: ${elapsed}\nЛимит ожидания: ${generation.timeout_seconds || 120} с\nHTTP: ${diagnostic.http_status || 'код ответа не зафиксирован'}\nПричина: ${diagnostic.code || 'нет подробной диагностики'}`;
  if (generation.status === 'running') {
    requestStatus(`Ожидаем результат · ${elapsed}`, 'Сообщение сохранено. Сервер выполняет запрос к Яндекс AI Studio.',
      'Ответ появится целиком. Пока неизвестно, на каком этапе обработки он находится у провайдера. Можно остановить ожидание.', details, 'pending');
  } else if (generation.status === 'completed') {
    requestStatus('Ответ получен и сохранён', `Запрос завершён. Время: ${elapsed}.`, generation.finish_reason === 'length' ? 'Достигнут лимит длины ответа. Попросите модель продолжить.' : '', details, 'success');
    requestPanel.hidden = generation.finish_reason !== 'length';
  } else {
    requestStatus(generation.status === 'cancelled' ? 'Ожидание остановлено' : 'Ответ не получен', generation.error || 'Запрос завершился без ответа.',
      diagnostic.action || 'Сообщение сохранено. Повторите запрос или проверьте модель и подключение. Для старого запроса точная причина и этап сбоя не записаны.', details, 'error');
  }
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function notice(id, message = '', error = false) {
  if (id === 'chat-notice' && !error && (activeGeneration !== null || !requestPanel.hidden)) message = '';
  const node = $(id);
  node.textContent = message;
  node.hidden = !message;
  node.classList.toggle('error', error);
}
async function api(path, method = 'GET', body) {
  let response;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 35000);
  try {
    response = await fetch('/api' + path, {method, signal: controller.signal, headers: {'Content-Type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)});
  } catch { throw new Error('Локальный сервер не отвечает. Проверьте окно запуска приложения. Состояние запроса к модели пока неизвестно.'); }
  finally { clearTimeout(timer); }
  if (!response.ok) {
    if (response.status === 405 && method === 'DELETE' && /^\/conversations\/\d+$/.test(path)) {
      throw new Error('Запущена старая версия сервера без удаления чатов. Остановите сервер через Ctrl+C в окне запуска и снова запустите «Запустить.bat». Затем обновите страницу через Ctrl+F5.');
    }
    const data = await response.json().catch(() => ({}));
    throw new Error(typeof data.detail === 'string' ? data.detail : 'Не удалось выполнить операцию. Повторите попытку.');
  }
  return response.status === 204 ? null : response.json();
}
function readAgent(prefix) {
  return {name: $(prefix + '-name').value.trim(), description: $(prefix + '-description').value, instruction: $(prefix + '-instruction').value,
    connection_id: Number($(prefix + '-connection').value) || null, model_id: $(prefix + '-model').value || null,
    web_search: $(prefix + '-web_search').checked, code_interpreter: $(prefix + '-code_interpreter').checked};
}
function setTab(name) {
  for (const tab of ['chat', 'settings']) {
    $(tab + '-tab').setAttribute('aria-selected', String(tab === name));
    $(tab + '-panel').hidden = tab !== name;
  }
}
function renderAgent() {
  $('agent-name').textContent = state.agent.name;
  $('agent-description').textContent = state.agent.description || 'Добавьте описание в настройках агента.';
  $('agent-model').textContent = modelLabel(state.agent);
  $('model-status').textContent = state.agent.model_id ? 'Модель выбрана · доступ проверяется при запросе' : 'Модель не подключена';
  $('retry-response').hidden = !retryMessageId || !state.agent.model_id || activeGeneration !== null;
  notice('agent-model-warning', state.agent.model_missing ? 'Выбранная модель отсутствует в актуальном списке. Прежний выбор сохранён; выберите другую модель вручную при необходимости.' : '', true);
}
function rememberDraft() {
  if (state.conversation) state.drafts.set(state.conversation, $('message-input').value);
}
function renderConversations() {
  const list = $('conversation-list');
  list.replaceChildren();
  if (!state.conversations.length) list.append(element('p', 'muted', 'Разговоров пока нет. Создайте первый.'));
  for (const item of state.conversations) {
    const button = element('button', 'conversation', item.title);
    button.title = item.title;
    button.setAttribute('aria-current', String(item.id === state.conversation));
    button.disabled = state.busy;
    button.onclick = () => selectConversation(item.id);
    const row = element('div', 'conversation-row');
    const remove = element('button', 'small danger delete-chat');
    remove.type = 'button';
    remove.title = 'Удалить чат';
    remove.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18M9 6V4h6v2M5 6l1 14h12l1-14M10 10v6M14 10v6"/></svg>';
    remove.setAttribute('aria-label', `Удалить чат «${item.title}»`);
    remove.disabled = state.busy;
    remove.onclick = async () => {
      if (state.busy || !confirm(`Удалить чат «${item.title}» и все его сообщения? Это действие нельзя отменить.`)) return;
      const agentId = state.agent.id;
      let deleted = false;
      await write(remove, 'chat-notice', async () => {
        await api(`/conversations/${item.id}`, 'DELETE');
        state.conversations = state.conversations.filter(chat => chat.id !== item.id);
        state.drafts.delete(item.id);
        if (state.conversation === item.id) {
          state.conversation = null;
          $('message-input').value = '';
          deleted = true;
        }
      });
      if (deleted && state.agent?.id === agentId) await selectConversation(state.conversations[0]?.id || null);
    };
    row.append(button, remove);
    list.append(row);
  }
}
function emptyMessages(title, description) {
  const box = element('div', 'empty');
  box.append(element('h2', '', title), element('p', '', description));
  $('messages').replaceChildren(box);
}
function addMessage(message) {
  if ($('messages').querySelector(`[data-message-id="${message.id}"]`)) return;
  const node = element('article', message.role === 'assistant' ? 'message assistant-message' : 'message');
  node.dataset.messageId = message.id;
  node.append(element('div', 'message-meta', (message.role === 'assistant' ? 'Агент' : 'Вы') + ' · ' + new Date(message.created_at).toLocaleString('ru-RU')), element('div', 'message-body', message.content));
  $('messages').append(node);
  $('messages').scrollTop = $('messages').scrollHeight;
}
function chatEnabled(enabled) {
  $('message-input').disabled = !enabled;
  $('send-message').disabled = !enabled;
}
async function selectConversation(id) {
  if (state.busy) return;
  rememberDraft();
  const epoch = ++state.epoch;
  state.conversation = id;
  $('message-input').value = state.drafts.get(id) || '';
  notice('chat-notice');
  retryMessageId = null;
  requestPanel.hidden = true;
  $('retry-response').hidden = true;
  renderConversations();
  chatEnabled(false);
  if (!id) { emptyMessages('Начните новый разговор', 'Нажмите «＋ Новый» в списке разговоров.'); return; }
  emptyMessages('Загрузка истории…', '');
  try {
    const [messages, generation] = await Promise.all([api(`/conversations/${id}/messages`), api(`/conversations/${id}/generation`)]);
    if (epoch !== state.epoch) return;
    $('messages').replaceChildren();
    if (!messages.length) emptyMessages('Разговор пока пуст', state.agent.model_id ? 'Напишите сообщение выбранной модели.' : 'Выберите модель в настройках, чтобы получать ответы.');
    messages.forEach(addMessage);
    chatEnabled(true);
    const last = messages[messages.length - 1];
    retryMessageId = last?.role === 'user' ? last.id : null;
    $('retry-response').hidden = !retryMessageId || !state.agent.model_id;
    if (generation && (!retryMessageId || generation.user_message_id === retryMessageId)) showGeneration(generation);
    else if (retryMessageId) requestStatus('Сообщение сохранено · ответ не запрошен', 'Для последнего сообщения нет сохранённого запроса к модели.', 'Нажмите «Получить ответ» или выберите модель в настройках.');
    if (generation?.status === 'running') {
      await write($('send-message'), 'chat-notice', () => watchGeneration(generation));
    } else if (generation?.error && generation.user_message_id === retryMessageId) {
      notice('chat-notice', generation.error, true);
    }
  } catch (error) {
    if (epoch !== state.epoch) return;
    emptyMessages('Не удалось загрузить историю', 'Выберите разговор ещё раз, чтобы повторить попытку.');
    notice('chat-notice', error.message, true);
  }
}
async function route() {
  if (state.busy) return;
  rememberDraft();
  const epoch = ++state.epoch;
  notice('global-error');
  const match = location.hash.match(/^#agent\/(\d+)$/);
  const connectionsPage = location.hash === '#connections';
  $('home').hidden = !!match || connectionsPage;
  $('connections-page').hidden = !connectionsPage;
  $('agent-page').hidden = true;
  state.conversation = null;
  try {
    await loadConnections();
    if (epoch !== state.epoch) return;
    if (connectionsPage) { renderConnectionList(); return; }
    if (!match) {
      state.agent = null;
      $('agent-grid').replaceChildren();
      $('empty-agents').hidden = true;
      const agents = await api('/agents');
      if (epoch !== state.epoch) return;
      $('empty-agents').hidden = agents.length > 0;
      for (const agent of agents) {
        const card = element('a', 'agent-card');
        card.href = '#agent/' + agent.id;
        card.append(element('div', 'avatar', Array.from(agent.name)[0].toUpperCase()), element('h2', '', agent.name), element('p', 'muted', agent.description || 'Описание пока не добавлено'), element('div', 'card-footer', modelLabel(agent)));
        if (agent.model_missing) card.append(element('p', 'warning-text', 'Модель отсутствует в актуальном списке. Выбор сохранён.'));
        card.append(element('span', '', 'Открыть чат →'));
        $('agent-grid').append(card);
      }
    } else {
      const [agent, conversations] = await Promise.all([api('/agents/' + match[1]), api(`/agents/${match[1]}/conversations`)]);
      if (epoch !== state.epoch) return;
      state.agent = agent;
      state.conversations = conversations;
      renderAgent();
      for (const field of ['name', 'description', 'instruction']) $('settings-' + field).value = agent[field];
      for (const field of ['web_search', 'code_interpreter']) $('settings-' + field).checked = !!agent[field];
      populatePicker('settings', agent.connection_id, agent.model_id);
      notice('settings-notice');
      $('agent-page').hidden = false;
      setTab('chat');
      await selectConversation(conversations[0]?.id || null);
    }
  } catch (error) { if (epoch === state.epoch) notice('global-error', error.message, true); }
}
// Serialize writes, retaining form contents on any save error.
async function write(button, noticeId, action) {
  if (state.busy) return;
  state.busy = true;
  const hash = location.hash;
  button.disabled = true;
  renderConversations();
  notice(noticeId);
  try { await action(); }
  catch (error) { notice(noticeId, error.message, true); }
  finally {
    state.busy = false;
    button.disabled = false;
    renderConversations();
    if (hash !== location.hash) await route();
  }
}
function openCreate() { notice('create-notice'); populatePicker('create', Number($('create-connection').value) || null, $('create-model').value); $('create-dialog').showModal(); $('create-name').focus(); }
$('add-agent').onclick = openCreate;
$('add-first').onclick = openCreate;
for (const id of ['close-dialog', 'cancel-create']) $(id).onclick = () => { if (!state.busy) $('create-dialog').close(); };
$('create-dialog').addEventListener('cancel', event => { if (state.busy) event.preventDefault(); });
$('create-form').onsubmit = event => {
  event.preventDefault();
  write(event.submitter, 'create-notice', async () => {
    const agent = await api('/agents', 'POST', readAgent('create'));
    $('create-form').reset();
    $('create-dialog').close();
    location.hash = '#agent/' + agent.id;
  });
};
$('settings-form').onsubmit = event => {
  event.preventDefault();
  write(event.submitter, 'settings-notice', async () => {
    state.agent = await api('/agents/' + state.agent.id, 'PUT', readAgent('settings'));
    renderAgent();
    notice('settings-notice', 'Изменения сохранены.');
  });
};
$('delete-agent').onclick = event => {
  if (state.busy || !confirm(`Удалить агента «${state.agent.name}»? Все его разговоры и сообщения также будут удалены. Это действие нельзя отменить.`)) return;
  write(event.currentTarget, 'settings-notice', async () => {
    await api('/agents/' + state.agent.id, 'DELETE');
    for (const item of state.conversations) state.drafts.delete(item.id);
    state.conversation = null;
    location.hash = '';
  });
};
$('new-conversation').onclick = async event => {
  let created;
  const agentId = state.agent.id;
  await write(event.currentTarget, 'chat-notice', async () => {
    created = await api(`/agents/${agentId}/conversations`, 'POST');
    state.conversations.unshift(created);
  });
  if (created && state.agent?.id === agentId) await selectConversation(created.id);
};
$('message-form').onsubmit = event => {
  event.preventDefault();
  const content = $('message-input').value;
  if (!content.trim() || !state.conversation || state.busy) return;
  const id = state.conversation;
  $('message-input').readOnly = true;
  write($('send-message'), 'chat-notice', async () => {
    requestStatus('Сохраняем сообщение…', 'Ожидаем подтверждение локального сервера. Запрос модели ещё не запускался.');
    const message = await api(`/conversations/${id}/messages`, 'POST', {content});
    $('messages').querySelector('.empty')?.remove();
    addMessage(message);
    const item = state.conversations.find(item => item.id === id);
    // The first message determines the title; refresh from storage later on navigation.
    if (item && $('messages').querySelectorAll('.message').length === 1) item.title = content.trim().replace(/\s+/g, ' ').slice(0, 60);
    $('message-input').value = '';
    state.drafts.delete(id);
    retryMessageId = message.id;
    if (state.agent.model_id) {
      $('retry-response').hidden = false;
      notice('chat-notice', 'Сообщение сохранено. Отправка запроса модели…');
      await requestReply();
    } else {
      requestStatus('Сообщение сохранено · ответ не запрошен', 'У агента пока не выбрана модель.', 'Выберите модель в настройках, затем нажмите «Получить ответ».');
      notice('chat-notice', 'Сообщение сохранено. Для ответа выберите модель в настройках.');
    }
  }).finally(() => { $('message-input').readOnly = false; $('message-input').focus(); });
};
$('message-input').addEventListener('input', rememberDraft);
$('message-input').onkeydown = event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!$('send-message').disabled) $('message-form').requestSubmit($('send-message'));
  }
};
$('chat-tab').onclick = () => setTab('chat');
$('settings-tab').onclick = () => setTab('settings');
window.addEventListener('hashchange', route);

async function requestReply() {
  requestStatus('Сообщение сохранено · запускаем запрос…', 'Ожидаем подтверждение запуска от локального сервера.');
  let generation;
  try {
    generation = await api(`/conversations/${state.conversation}/generate`, 'POST', {message_id: retryMessageId});
  } catch (error) {
    requestStatus('Запуск запроса не подтверждён', error.message, 'Нажмите «Проверить статус». Сервер мог принять запрос до потери связи; не отправляйте сообщение повторно.', '', 'error');
    throw error;
  }
  await watchGeneration(generation);
}
async function watchGeneration(generation) {
  activeGeneration = generation.id;
  $('stop-response').disabled = generation.status !== 'running';
  $('retry-response').hidden = true;
  $('message-input').readOnly = true;
  $('new-conversation').disabled = true;
  try {
    while (generation.status === 'running') {
      showGeneration(generation);
      notice('chat-notice', 'Ожидаем ответ модели…');
      await new Promise(resolve => setTimeout(resolve, 700));
      generation = await api(`/generations/${generation.id}`);
    }
    showGeneration(generation);
    if (generation.status === 'completed' && generation.message) {
      addMessage(generation.message);
      retryMessageId = null;
      notice('chat-notice', generation.finish_reason === 'length' ? 'Ответ сохранён. Модель достигла лимита длины ответа; попросите продолжить.' : 'Ответ сохранён.');
    } else {
      notice('chat-notice', generation.error || 'Ответ не получен. Можно повторить запрос.', generation.status === 'failed');
    }
  } catch (error) {
    requestStatus('Связь с сервером потеряна · результат неизвестен', error.message,
      'Запрос может продолжать выполняться. Нажмите «Проверить статус», когда сервер снова будет доступен. Автоматического повторного запроса к модели нет.', `Запрос №${generation.id}`, 'error');
    throw error;
  } finally {
    activeGeneration = null;
    $('stop-response').disabled = true;
    $('message-input').readOnly = false;
    $('new-conversation').disabled = false;
    $('retry-response').hidden = !retryMessageId || !state.agent.model_id;
  }
}
$('retry-response').onclick = () => {
  if (!retryMessageId || state.busy) return;
  write($('retry-response'), 'chat-notice', requestReply);
};
$('stop-response').onclick = async () => {
  if (!activeGeneration) return;
  $('stop-response').disabled = true;
  try { await api(`/generations/${activeGeneration}/stop`, 'POST'); }
  catch (error) { notice('chat-notice', error.message, true); $('stop-response').disabled = !activeGeneration; }
};
