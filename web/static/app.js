const csrf = document.querySelector('meta[name="csrf"]')?.content;

const settingsUrl = document.querySelector('[data-settings-url]')?.dataset.settingsUrl;

const failText = (e) => (e instanceof TypeError ? '서버에 연결하지 못했습니다. 잠시 후 다시 시도해주세요.' : e.message);

// kind는 ok, error, hint. 저장 완료만 잠시 뒤 지움
const showState = (el, text, kind = 'ok') => {
  const box = el.closest('[data-setting]')?.querySelector('.save-state');
  if (!box) return;
  clearTimeout(box.timer);
  box.textContent = text;
  box.dataset.kind = kind;
  if (kind === 'ok' && text) box.timer = setTimeout(() => { box.textContent = ''; }, 2400);
};

const addChange = (entry) => {
  const list = document.querySelector('.changes');
  if (!entry || !list) return;
  const li = document.createElement('li');
  const p = document.createElement('p');
  const meta = document.createElement('span');
  p.textContent = entry.text;
  meta.className = 'change-meta';
  meta.textContent = entry.meta;
  li.append(p, meta);
  list.prepend(li);
  list.hidden = false;
  document.querySelector('.changes-empty')?.remove();
  while (list.children.length > 15) list.lastElementChild.remove();
};

const send = async (url, payload) => {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
    body: JSON.stringify(payload),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || '저장하지 못했습니다. 새로고침한 뒤 다시 시도해주세요.');
  addChange(body.entry);
  return body;
};

for (const sw of document.querySelectorAll('.switch[data-url]')) {
  sw.addEventListener('click', async () => {
    if (sw.getAttribute('aria-busy') === 'true') return;
    const next = sw.getAttribute('aria-checked') !== 'true';
    sw.setAttribute('aria-checked', String(next));
    sw.setAttribute('aria-busy', 'true');
    showState(sw, '');
    try {
      const body = await send(sw.dataset.url, { enabled: next });
      sw.setAttribute('aria-checked', String(body.enabled));
      showState(sw, body.enabled ? '켰습니다.' : '껐습니다.');
    } catch (e) {
      sw.setAttribute('aria-checked', String(!next));
      showState(sw, failText(e), 'error');
    } finally {
      sw.removeAttribute('aria-busy');
    }
  });
}

for (const select of document.querySelectorAll('select[data-key]')) {
  select.dataset.saved = select.value;
  select.addEventListener('change', async () => {
    select.disabled = true;
    showState(select, '');
    try {
      await send(settingsUrl, { key: select.dataset.key, value: Number(select.value) });
      select.dataset.saved = select.value;
      showState(select, '저장했습니다.');
      if (select.dataset.key === 'alert_cut') {
        const hour = select.closest('.controls').querySelector('[data-key="cut_hour"]');
        hour.disabled = select.value === '0';
      }
    } catch (e) {
      select.value = select.dataset.saved;
      showState(select, failText(e), 'error');
    } finally {
      select.disabled = select.dataset.key === 'cut_hour' && select.closest('.controls').querySelector('[data-key="alert_cut"]').value === '0';
    }
  });
}

for (const scope of document.querySelectorAll('[data-scope]')) {
  const picker = scope.querySelector('.picker');
  const save = scope.querySelector('.picker-save');
  const count = scope.querySelector('.picker-count');
  const boxes = [...scope.querySelectorAll('.pick input')];
  const radios = [...scope.querySelectorAll('input[type="radio"]')];
  const chosen = () => boxes.filter((b) => b.checked).map((b) => b.value);
  const mode = () => radios.find((r) => r.checked).value;
  let saved = { mode: mode(), channels: chosen().join() };

  const refresh = () => {
    const n = chosen().length;
    count.textContent = n ? `고른 채널 ${n}개` : '채널을 골라주세요';
    save.disabled = !n || (mode() === saved.mode && chosen().join() === saved.channels);
  };

  const commit = async () => {
    save.disabled = true;
    showState(scope, '');
    try {
      await send(settingsUrl, { key: scope.dataset.scope, value: { mode: mode(), channels: chosen() } });
      saved = { mode: mode(), channels: chosen().join() };
      showState(scope, '저장했습니다.');
    } catch (e) {
      showState(scope, failText(e), 'error');
    }
    refresh();
  };

  for (const radio of radios) {
    radio.addEventListener('change', () => {
      picker.hidden = mode() === 'all';
      refresh();
      if (mode() === 'all' || chosen().length) commit();
      else showState(scope, '채널을 고른 뒤 저장해주세요.', 'hint');
    });
  }
  for (const box of boxes) {
    box.addEventListener('change', () => {
      refresh();
      if (!save.disabled) showState(scope, '');
    });
  }
  save.addEventListener('click', commit);

  const filter = scope.querySelector('.picker-filter');
  filter.addEventListener('input', () => {
    const q = filter.value.trim().toLowerCase();
    for (const group of scope.querySelectorAll('.picker-group')) {
      let any = false;
      for (const pick of group.querySelectorAll('.pick')) {
        pick.hidden = q && !pick.dataset.name.includes(q);
        any ||= !pick.hidden;
      }
      group.hidden = !any;
    }
  });
  refresh();
}

for (const plot of document.querySelectorAll('.plot')) {
  const cols = [...plot.querySelectorAll('.col')];
  const tip = plot.querySelector('.tip');
  let current = -1;

  const show = (i) => {
    if (current >= 0) cols[current].classList.remove('active');
    current = i;
    const col = cols[i];
    col.classList.add('active');
    tip.querySelector('strong').textContent = col.dataset.value;
    tip.querySelector('span').textContent = col.dataset.label;
    tip.hidden = false;
    const center = col.offsetLeft + col.offsetWidth / 2;
    const half = tip.offsetWidth / 2;
    tip.style.left = `${Math.min(Math.max(center, half), plot.offsetWidth - half)}px`;
  };
  const hide = () => {
    if (current >= 0) cols[current].classList.remove('active');
    current = -1;
    tip.hidden = true;
  };

  plot.addEventListener('pointermove', (e) => {
    const x = e.clientX - plot.getBoundingClientRect().left;
    const i = Math.min(cols.length - 1, Math.max(0, Math.floor((x / plot.offsetWidth) * cols.length)));
    if (i !== current) show(i);
  });
  plot.addEventListener('pointerleave', hide);
  plot.addEventListener('focus', () => show(cols.length - 1));
  plot.addEventListener('blur', hide);
  plot.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowLeft' && current > 0) show(current - 1);
    else if (e.key === 'ArrowRight' && current < cols.length - 1) show(current + 1);
    else if (e.key === 'Home') show(0);
    else if (e.key === 'End') show(cols.length - 1);
    else return;
    e.preventDefault();
  });
}

const account = document.querySelector('.account');
if (account) {
  document.addEventListener('click', (e) => {
    if (account.open && !account.contains(e.target)) account.open = false;
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && account.open) {
      account.open = false;
      account.querySelector('summary').focus();
    }
  });
}

const tablist = document.querySelector('.cmd-tabs[role="tablist"]');
if (tablist) {
  const tabs = [...tablist.querySelectorAll('[role="tab"]')];
  const select = (tab, focus) => {
    for (const t of tabs) {
      const on = t === tab;
      t.setAttribute('aria-selected', String(on));
      t.tabIndex = on ? 0 : -1;
      document.getElementById(t.getAttribute('aria-controls')).hidden = !on;
    }
    if (focus) tab.focus();
    tab.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'instant' });
  };
  tablist.addEventListener('click', (e) => {
    const tab = e.target.closest('[role="tab"]');
    if (tab) select(tab, false);
  });
  tablist.addEventListener('keydown', (e) => {
    const i = tabs.indexOf(document.activeElement);
    if (i < 0) return;
    const next = { ArrowDown: i + 1, ArrowRight: i + 1, ArrowUp: i - 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
    if (next === undefined) return;
    e.preventDefault();
    select(tabs[(next + tabs.length) % tabs.length], true);
  });
}

for (const seg of document.querySelectorAll('.tabs[role="tablist"]')) {
  const tabs = [...seg.querySelectorAll('[role="tab"]')];
  const select = (tab, focus) => {
    for (const t of tabs) {
      const on = t === tab;
      t.setAttribute('aria-selected', String(on));
      t.tabIndex = on ? 0 : -1;
      document.getElementById(t.getAttribute('aria-controls')).hidden = !on;
    }
    if (focus) tab.focus();
  };
  seg.addEventListener('click', (e) => {
    const tab = e.target.closest('[role="tab"]');
    if (tab) select(tab, false);
  });
  seg.addEventListener('keydown', (e) => {
    const i = tabs.indexOf(document.activeElement);
    const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
    if (i < 0 || next === undefined) return;
    e.preventDefault();
    select(tabs[(next + tabs.length) % tabs.length], true);
  });
}

const filter = document.querySelector('input[data-filter]');
if (filter) {
  const items = [...document.querySelectorAll(filter.dataset.filter)];
  const empty = document.getElementById('filter-empty');
  filter.addEventListener('input', () => {
    const q = filter.value.trim().toLowerCase();
    let shown = 0;
    for (const item of items) {
      const match = !q || item.dataset.name.includes(q);
      item.hidden = !match;
      shown += match;
    }
    for (const block of document.querySelectorAll('.block')) {
      block.hidden = q && !block.querySelector('.guild-item:not([hidden])');
    }
    empty.hidden = shown > 0;
  });
}
