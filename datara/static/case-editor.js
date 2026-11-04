/* A view over the existing JSON payload: no new persisted case format. */
function installCaseEditors(profile, template) {
  for (const id of ['optimizer-ground-truth', 'optimizer-observed-output', 'optimizer-observed-edit']) {
    const area = document.getElementById(id);
    if (!area) continue;
    const host = document.createElement('div');
    host.className = 'case-row-editor';
    area.before(host);
    const advanced = document.createElement('details');
    advanced.innerHTML = '<summary>高级：直接编辑 JSON（与表格同步）</summary>';
    area.before(advanced);
    advanced.append(area);
    area.removeAttribute('style');
    area.classList.add('optimizer-ground-truth');
    const historical = id !== 'optimizer-ground-truth';
    let data;
    const keyCache = {};
    const write = () => { area.value = data === null ? '' : JSON.stringify(data, null, 2); };
    const button = (label, fn) => {
      const b = document.createElement('button'); b.type = 'button'; b.className = 'small ghost';
      b.textContent = label; b.addEventListener('click', () => {
        const invalid = host.querySelector('input:invalid');
        if (invalid) { invalid.reportValidity(); return; }
        fn(); write(); render();
      }); return b;
    };
    function render() {
      host.replaceChildren();
      const note = document.createElement('p'); note.className = 'helper';
      note.textContent = historical
        ? '按模型原始输出顺序填写，包括错行、漏行或多出的行；不要为了对齐答案而补行。仅填写真实出现过的错误。'
        : '按单据顺序填写全部明细，一行对应一条记录；小计、合计不算明细。null 表示确实无值，0 表示金额为零。';
      host.append(note);
      if (data === null) {
        host.append(button('按字段模板填写历史错误', () => { data = structuredClone(template); }));
        return;
      }
      for (const table of profile.tables) {
        if (!Object.hasOwn(data, table.name)) continue;
        const detail = table.role === 'detail', rows = detail ? data[table.name] : [data[table.name]];
        if (!Array.isArray(rows) || rows.some(row => !row || typeof row !== 'object' || Array.isArray(row))) {
          const warning = document.createElement('p'); warning.textContent = table.name + '：结构不符合表格格式，请在 JSON 中修正。'; host.append(warning); advanced.open = true; continue;
        }
        const keys = [...new Set(rows.flatMap(row => Object.keys(row)))];
        if (!keys.length && keyCache[table.name]) keys.push(...keyCache[table.name]);
        if (!keys.length) keys.push(...Object.keys((detail ? template[table.name]?.[0] : template[table.name]) || {}));
        if (!keys.length) keys.push(...table.fields.filter(f => f.source === 'AI').map(f => f.name));
        keyCache[table.name] = keys;
        const section = document.createElement('section');
        const title = document.createElement('strong'); title.textContent = table.name + (detail ? ` · ${rows.length} 行明细` : ' · 主表'); section.append(title);
        const scroll = document.createElement('div'); scroll.className = 'case-row-scroll';
        const grid = document.createElement('table'), head = grid.createTHead().insertRow();
        for (const name of ['行', ...keys, ...(detail ? ['操作'] : [])]) { const th = document.createElement('th'); th.textContent = name; head.append(th); }
        const body = grid.createTBody();
        rows.forEach((row, index) => {
          const tr = body.insertRow(); tr.insertCell().textContent = String(index + 1);
          keys.forEach(key => {
            const td = tr.insertCell(), input = document.createElement('input');
            const field = table.fields.find(f => f.name === key);
            const label = `${table.name} 第 ${index + 1} 行 ${key}`;
            input.type = 'text'; input.setAttribute('aria-label', label);
            input.value = row[key] == null ? '' : typeof row[key] === 'object' ? JSON.stringify(row[key]) : String(row[key]);
            input.disabled = !Object.hasOwn(row, key) || row[key] === null;
            input.addEventListener('input', () => {
              let value = input.value;
              if (['Integer', 'Decimal'].includes(field?.data_type)) {
                if (!value.trim() || !Number.isFinite(Number(value)) || (field.data_type === 'Integer' && !Number.isInteger(Number(value)))) {
                  input.setCustomValidity('请填写有效数字；无值请勾选 null'); return;
                }
                value = Number(value);
              } else if (field?.data_type === 'Boolean') {
                if (!['true', 'false'].includes(value)) { input.setCustomValidity('请填写 true 或 false'); return; }
                value = value === 'true';
              }
              input.setCustomValidity(''); row[key] = value; write();
            });
            td.append(input);
            const nullLabel = document.createElement('label'), check = document.createElement('input');
            check.type = 'checkbox'; check.checked = row[key] === null; check.setAttribute('aria-label', label + ' null');
            check.addEventListener('change', () => {
              input.disabled = check.checked; input.setCustomValidity('');
              if (check.checked) { row[key] = null; write(); }
              else { input.dispatchEvent(new Event('input')); input.focus(); }
            });
            nullLabel.append(check, document.createTextNode(' null')); td.append(nullLabel);
            if (!Object.hasOwn(row, key)) { const absent = document.createElement('small'); absent.textContent = '未提供（JSON 中可补充）'; td.append(absent); }
          });
          if (detail) tr.insertCell().append(button('删除第 ' + (index + 1) + ' 行', () => rows.splice(index, 1)));
        });
        scroll.append(grid); section.append(scroll);
        if (detail) section.append(button('添加明细行', () => rows.push(Object.fromEntries(keys.map(key => [key, null])))));
        host.append(section);
      }
      if (historical) host.append(button('清空历史错误', () => { data = null; }));
    }
    function read() {
      try {
        data = area.value.trim() ? JSON.parse(area.value) : null;
        if (data !== null && (typeof data !== 'object' || Array.isArray(data))) throw Error();
        render();
      } catch { host.textContent = 'JSON 尚未有效，修正后会恢复表格；当前输入已保留。'; advanced.open = true; }
    }
    // Empty history must remain absent, not become a fabricated null-filled output.
    area.addEventListener('input', read);
    read();
  }
}

// Prevent saving a stale JSON value when a numeric/boolean cell is invalid.
document.addEventListener('click', event => {
  const action = event.target.closest('[data-action]')?.dataset.action;
  if (!['create-optimizer-case', 'save-ground-truth', 'save-observed-output'].includes(action)) return;
  const invalid = document.querySelector('.case-row-editor input:invalid');
  if (invalid) { event.preventDefault(); event.stopImmediatePropagation(); invalid.reportValidity(); }
}, true);
