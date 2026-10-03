const csrf = document.querySelector('meta[name="csrf"]')?.content;

for (const sw of document.querySelectorAll('.switch[data-url]')) {
  const errorBox = document.getElementById('zoom-error');
  sw.addEventListener('click', async () => {
    if (sw.getAttribute('aria-busy') === 'true') return;
    const next = sw.getAttribute('aria-checked') !== 'true';
    sw.setAttribute('aria-checked', String(next));
    sw.setAttribute('aria-busy', 'true');
    errorBox.hidden = true;
    try {
      const res = await fetch(sw.dataset.url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
        body: JSON.stringify({ enabled: next }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.error || '저장하지 못했습니다. 새로고침한 뒤 다시 시도해주세요.');
      sw.setAttribute('aria-checked', String(body.enabled));
    } catch (e) {
      sw.setAttribute('aria-checked', String(!next));
      errorBox.textContent = e instanceof TypeError
        ? '서버에 연결하지 못했습니다. 잠시 후 다시 시도해주세요.'
        : e.message;
      errorBox.hidden = false;
    } finally {
      sw.removeAttribute('aria-busy');
    }
  });
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

const tablist = document.querySelector('.cmd-list[role="tablist"]');
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
    tab.scrollIntoView({ block: 'nearest', inline: 'nearest' });
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

for (const seg of document.querySelectorAll('.seg[role="tablist"]')) {
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
