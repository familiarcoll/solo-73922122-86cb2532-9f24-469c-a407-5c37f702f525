/* 水位日志 UTC 对齐工具前端 */
let currentFileId = null;

const $ = (sel) => document.querySelector(sel);

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function api(path, opts = {}) {
  const resp = await fetch(path, opts);
  if (!resp.ok) {
    let detail;
    try {
      const body = await resp.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      detail = resp.statusText;
    }
    const err = new Error(detail);
    err.status = resp.status;
    throw err;
  }
  return resp.json();
}

function showMsg(el, text, isError) {
  el.textContent = text || "";
  el.className = "msg" + (text ? (isError ? " error" : " ok") : "");
}

const STATE_LABEL = {
  error: ["错误", "st-error"],
  conflict: ["矛盾", "st-conflict"],
  ambiguous: ["歧义", "st-ambiguous"],
  unique: ["唯一", "st-unique"],
};
const STATUS_LABEL = {
  resolved: ["✅ 全部唯一，可导出", "st-unique"],
  ambiguous: ["⚠️ 存在歧义行，需人工选择", "st-ambiguous"],
  blocked: ["⛔ 存在错误/矛盾行，禁止导出", "st-error"],
};

async function loadFiles() {
  const files = await api("/api/files");
  const ul = $("#fileList");
  ul.innerHTML = "";
  for (const f of files) {
    const li = document.createElement("li");
    li.className = f.id === currentFileId ? "active" : "";
    const [label, cls] = STATUS_LABEL[f.status];
    li.innerHTML =
      `<span class="fname">#${f.id} ${esc(f.name)}</span>` +
      `<span class="meta">${f.row_count} 行 · <span class="badge ${cls}">${esc(label)}</span></span>`;
    li.onclick = () => selectFile(f.id);
    ul.appendChild(li);
  }
  if (!files.length) ul.innerHTML = '<li class="muted">（空）</li>';
}

async function selectFile(fid) {
  currentFileId = fid;
  await loadFiles();
  const data = await api(`/api/files/${fid}`);
  renderDetail(data);
}

function renderDetail({ file, validation: v }) {
  $("#empty").hidden = true;
  $("#detail").hidden = false;

  const [label, cls] = STATUS_LABEL[v.status];
  $("#summary").innerHTML =
    `<h2>#${file.id} ${esc(file.name)}</h2>` +
    `<p><span class="badge ${cls}">${esc(label)}</span> ` +
    `共 <b>${v.total}</b> 行 · 唯一 <b>${v.n_unique}</b> · ` +
    `歧义 <b>${v.n_ambiguous}</b> · 错误/矛盾 <b>${v.n_problems}</b></p>`;

  const prob = $("#problems");
  if (v.problems.length) {
    prob.innerHTML = "<ul>" + v.problems.map((p) =>
      `<li>行 ${p.line_no}（序号 ${p.seq}）：${esc(p.reason)}</li>`).join("") + "</ul>";
    prob.className = "problems";
  } else {
    prob.innerHTML = "";
    prob.className = "";
  }

  $("#exportBtn").disabled = !v.can_export;

  const tbody = $("#rowsTable tbody");
  tbody.innerHTML = "";
  for (const r of v.rows) {
    const tr = document.createElement("tr");
    tr.className = "row-" + r.state;
    const [slabel, scls] = STATE_LABEL[r.state];
    let candHtml;
    if (r.state === "error") {
      candHtml = `<span class="reason">${esc(r.error)}</span>`;
    } else {
      const parts = r.candidates.map((c) => {
        const lbl = esc(c.label);
        if (!c.feasible) {
          return `<div class="cand infeasible" title="与全文件严格递增约束冲突">✗ ${lbl}</div>`;
        }
        if (r.candidates.filter((x) => x.feasible).length > 1 || c.selected) {
          const checked = c.selected ? "checked" : "";
          return `<label class="cand feasible">
              <input type="radio" name="row_${r.row_id}" data-row="${r.row_id}"
                     data-utc="${esc(c.utc)}" ${checked}> ${lbl}</label>`;
        }
        return `<div class="cand auto">✓ ${lbl}</div>`;
      });
      candHtml = parts.join("");
      if (r.selected_utc) {
        candHtml += `<button class="clear" data-clear="${r.row_id}">清除选择</button>`;
      }
    }
    tr.innerHTML =
      `<td>${r.line_no}</td><td>${r.seq}</td>` +
      `<td><code>${esc(r.local_text)}</code></td><td>${esc(r.water_text)}</td>` +
      `<td><span class="badge ${scls}">${slabel}</span></td>` +
      `<td>${candHtml}</td>`;
    tbody.appendChild(tr);
  }

  tbody.querySelectorAll('input[type="radio"]').forEach((el) => {
    el.addEventListener("change", () => choose(el.dataset.row, el.dataset.utc));
  });
  tbody.querySelectorAll("[data-clear]").forEach((el) => {
    el.addEventListener("click", () => clearSel(el.dataset.clear));
  });
}

async function refresh() {
  if (currentFileId != null) await selectFile(currentFileId);
  else await loadFiles();
}

async function choose(rowId, utc) {
  try {
    await api(`/api/files/${currentFileId}/selections`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ row_id: Number(rowId), utc }),
    });
    showMsg($("#actionMsg"), "选择已保存，全文件重新校验通过。", false);
  } catch (e) {
    showMsg($("#actionMsg"), `冲突，选择未保存：${e.message}`, true);
  }
  await refresh();
}

async function clearSel(rowId) {
  await api(`/api/files/${currentFileId}/selections/${rowId}`, { method: "DELETE" });
  showMsg($("#actionMsg"), "已清除该选择。", false);
  await refresh();
}

async function uploadBlob(blob, filename) {
  const fd = new FormData();
  fd.append("file", blob, filename);
  try {
    const res = await api("/api/files", { method: "POST", body: fd });
    showMsg($("#uploadMsg"), `已上传 ${filename}（#${res.file_id}）。`, false);
    currentFileId = res.file_id;
    await refresh();
  } catch (e) {
    showMsg($("#uploadMsg"), `上传被拒绝：${e.message}`, true);
    await loadFiles();
  }
}

$("#fileInput").addEventListener("change", (ev) => {
  const f = ev.target.files[0];
  if (f) uploadBlob(f, f.name);
  ev.target.value = "";
});

document.querySelectorAll("[data-sample]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const name = btn.dataset.sample;
    const resp = await fetch(`/static/samples/${name}`);
    if (!resp.ok) { showMsg($("#uploadMsg"), `样例 ${name} 加载失败`, true); return; }
    const blob = await resp.blob();
    await uploadBlob(new File([blob], name, { type: "text/csv" }), name);
  });
});

$("#exportBtn").addEventListener("click", () => {
  window.location = `/api/files/${currentFileId}/export`;
});
$("#originalBtn").addEventListener("click", () => {
  window.location = `/api/files/${currentFileId}/original`;
});
$("#deleteBtn").addEventListener("click", async () => {
  if (!confirm("确定删除该文件及其全部选择？")) return;
  await api(`/api/files/${currentFileId}`, { method: "DELETE" });
  currentFileId = null;
  $("#detail").hidden = true;
  $("#empty").hidden = false;
  await loadFiles();
});

loadFiles();
