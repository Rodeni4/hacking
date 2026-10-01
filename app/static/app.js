'use strict';
const $ = id => document.getElementById(id);
const state = {agent: null, conversations: [], conversation: null, epoch: 0, busy: false, drafts: new Map()};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function notice(id, message = '', error = false) {
  const node = $(id);
  node.textContent = message;
  node.hidden = !message;
  node.classList.toggle('error', error);
}
async function api(path, method = 'GET', body) {
  let response;
  try {
    response = await fetch('/api' + path, {method, headers: {'Content-Type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)});
  } catch { throw new Error('Нет связи с сервером. Проверьте, что приложение запущено, и повторите попытку.'); }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(typeof data.detail === 'string' ? data.detail : 'Не удалось выполнить операцию. Повторите попытку.');
  }
  return response.status === 204 ? null : response.json();
}
function readAgent(prefix) {
  return {name: $(prefix + '-name').value.trim(), description: $(prefix + '-description').value, instruction: $(prefix + '-instruction').value,
    connection_id: Number($(prefix + '-connection').value) || null, model_id: $(prefix + '-model').value || null};
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
  $('model-status').textContent = state.agent.model_id ? 'Модель выбрана. Ответы ещё не подключены' : 'Модель не подключена';
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
    list.append(button);
  }
}
function emptyMessages(title, description) {
  const box = element('div', 'empty');
  box.append(element('h2', '', title), element('p', '', description));
  $('messages').replaceChildren(box);
}
function addMessage(message) {
  const node = element('article', 'message');
  node.append(element('div', 'message-meta', 'Вы · ' + new Date(message.created_at).toLocaleString('ru-RU')), element('div', 'message-body', message.content));
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
  renderConversations();
  chatEnabled(false);
  if (!id) { emptyMessages('Начните новый разговор', 'Нажмите «＋ Новый» в списке разговоров.'); return; }
  emptyMessages('Загрузка истории…', '');
  try {
    const messages = await api(`/conversations/${id}/messages`);
    if (epoch !== state.epoch) return;
    $('messages').replaceChildren();
    if (!messages.length) emptyMessages('Разговор пока пуст', 'Вы можете сохранить сообщение. Ответы моделей ещё не подключены.');
    messages.forEach(addMessage);
    chatEnabled(true);
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
    const message = await api(`/conversations/${id}/messages`, 'POST', {content});
    $('messages').querySelector('.empty')?.remove();
    addMessage(message);
    const item = state.conversations.find(item => item.id === id);
    // The first message determines the title; refresh from storage later on navigation.
    if (item && $('messages').querySelectorAll('.message').length === 1) item.title = content.trim().replace(/\s+/g, ' ').slice(0, 60);
    $('message-input').value = '';
    state.drafts.delete(id);
    notice('chat-notice', state.agent.model_id ? 'Сообщение сохранено. Модель выбрана. Ответы ещё не подключены' : 'Сообщение сохранено. Для ответа подключите модель');
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
