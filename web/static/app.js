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

for (const input of document.querySelectorAll('input[data-filter]')) {
  const table = document.querySelector(input.dataset.filter);
  const rows = [...table.querySelectorAll('tbody tr')];
  const empty = document.querySelector(`[data-empty-for="${input.dataset.filter}"]`);
  input.addEventListener('input', () => {
    const q = input.value.trim().toLowerCase();
    let shown = 0;
    for (const row of rows) {
      const hit = !q || row.dataset.search.includes(q);
      row.hidden = !hit;
      shown += hit;
    }
    table.hidden = shown === 0;
    empty.hidden = shown !== 0;
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
