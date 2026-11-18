(() => {
  const view = document.getElementById("view");
  const msgEl = document.getElementById("msg");
  let tab = "batches";
  let templates = [];
  let batches = [];
  let reports = [];
  let residues = [];
  let makeupTasks = [];
  let events = [];
  let consistency = { ok: true, issues: {} };
  let selected = null;
  let sheet = null;
  let stampValue = "";
  let boundary = "回滚";
  let drill = false;
  let drillRow = 2;
  let traceValue = "WM-ORD-131";
  let traceResult = null;

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (r.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      const err = new Error(j.error || "HTTP " + r.status);
      err.payload = j;
      err.status = r.status;
      throw err;
    }
    return j;
  }

  function setMsg(t, kind) {
    msgEl.textContent = t || "";
    msgEl.className = "muted " + (kind || "");
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function boundaryChip(b) {
    const cls = b === "回滚" ? "chip-rollback" : "chip-retain";
    return `<span class="chip ${cls}">残段·${esc(b)}</span>`;
  }

  function closedChip(b) {
    const cls = b === "回滚" ? "chip-rollback" : "chip-retain";
    return `<span class="chip ${cls}">已收口·${esc(b)}</span>`;
  }

  function stateChip(batch) {
    if (batch.residue && batch.residue.state === "待续写") {
      return boundaryChip(batch.residue.boundary);
    }
    if (batch.makeup_task && batch.makeup_task.status === "待补盖") {
      return '<span class="chip chip-makeup">补盖·待补盖</span>';
    }
    const cls =
      batch.status === "已盖印" ? "chip-done" : batch.status === "待盖印" ? "chip-open" : "";
    return `<span class="chip ${cls}">${esc(batch.status)}</span>`;
  }

  async function refresh() {
    const meta = await api("/api/meta");
    document.getElementById("lab-name").textContent = meta.lab_name || "血缘水印台";
    templates = (await api("/api/templates")).items || [];
    batches = (await api("/api/batches")).items || [];
    reports = (await api("/api/gap-reports")).items || [];
    residues = (await api("/api/residues")).items || [];
    makeupTasks = (await api("/api/makeup-tasks")).items || [];
    events = (await api("/api/events")).items || [];
    consistency = await api("/api/consistency");
    if (sheet) {
      try {
        sheet = (await api("/api/batches/" + sheet.id + "/sheet")).sheet;
      } catch (e) {
        sheet = null;
      }
    }
  }

  function syncDock() {
    document.querySelectorAll(".dock [data-tab]").forEach((btn) => {
      btn.classList.toggle("on", btn.dataset.tab === tab);
    });
  }

  const TABS = {
    templates: renderTemplates,
    batches: renderBatches,
    residues: renderResidues,
    trace: renderTrace,
    gaps: renderGaps,
  };

  function renderTemplates() {
    return `
      <h2>水印模板</h2>
      <table>
        <thead><tr><th>表</th><th>列</th><th>版本</th><th>状态</th></tr></thead>
        <tbody>
          ${templates
            .map(
              (t) => `<tr>
            <td>${esc(t.table_name)}</td>
            <td>${esc(t.column_name)}</td>
            <td>${t.version_no}</td>
            <td><span class="chip">${esc(t.status)}</span></td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderDriftBanner() {
    if (consistency.ok) return "";
    const lines = Object.entries(consistency.issues)
      .map(([code, items]) => `<li><strong>${esc(code)}</strong>：${items.map(esc).join("；")}</li>`)
      .join("");
    return `<div class="alert">
      <strong>残段行与批次态漂移，写入口已全部拦截</strong>
      <ul>${lines}</ul>
    </div>`;
  }

  function renderBatches() {
    return `
      <h2>运行批次</h2>
      <p class="muted">主链：选批盖印 → 用水印追索下游 → 缺口对账；中断即登记残段，到「残段」页续写收口。</p>
      ${renderDriftBanner()}
      <table>
        <thead><tr><th>编号</th><th>表</th><th>运行码</th><th>业务日</th><th>状态</th><th>批次态 / 残段</th><th>水印</th><th></th></tr></thead>
        <tbody>
          ${batches
            .map(
              (b) => `<tr>
            <td>${b.id}</td>
            <td>${esc(b.table_name)}</td>
            <td>${esc(b.run_code)}</td>
            <td>${esc(b.biz_date)}</td>
            <td>${stateChip(b)}</td>
            <td>${esc(b.stamp_state)}${
                b.residue && b.residue.state === "待续写" ? " · " + boundaryChip(b.residue.boundary) : ""
              }${b.makeup_task && b.makeup_task.status === "待补盖" ? ' · <span class="chip chip-makeup">补盖·待补盖</span>' : ""}</td>
            <td>${esc(b.watermark_value || "-")}</td>
            <td class="ops">
              <button type="button" class="ghost" data-sheet="${b.id}">附页</button>
              ${
                b.status === "待盖印" && !(b.residue && b.residue.state === "待续写") && !(b.makeup_task && b.makeup_task.status === "待补盖")
                  ? `<button type="button" class="ghost" data-pick="${b.id}">盖印</button>`
                  : ""
              }
              ${
                b.residue && b.residue.state === "待续写"
                  ? `<button type="button" data-resume="${b.residue.id}">续写</button>`
                  : ""
              }
            </td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>
      ${
        selected
          ? `<div class="cardish">
        <h3>盖印 · ${esc(selected.run_code)}</h3>
        <p class="muted">${esc(selected.table_name)} · 共 4 行逐行落库；中断时按所选边界登记残段。</p>
        <label>水印值<input id="stamp-input" value="${esc(stampValue)}" /></label>
        <div class="field">
          <span class="label">残段提交边界</span>
          <label class="inline"><input type="radio" name="boundary" value="回滚" ${
            boundary === "回滚" ? "checked" : ""
          } /> 整笔回滚：已提交行恢复待盖，残段不占库存</label>
          <label class="inline"><input type="radio" name="boundary" value="留存" ${
            boundary === "留存" ? "checked" : ""
          } /> 部分留存：已提交行保持已盖，另开补盖任务写剩余码</label>
        </div>
        <label class="inline"><input type="checkbox" id="drill" ${drill ? "checked" : ""} /> 中断演练（在第 <input type="number" id="drill-row" min="1" max="4" value="${drillRow}" class="tiny" /> 行后中断）</label>
        <div class="row">
          <button type="button" id="btn-stamp">写入盖印</button>
          <button type="button" class="ghost" id="btn-cancel">取消</button>
        </div>
      </div>`
          : ""
      }
      ${sheet ? renderSheet() : ""}`;
  }

  function renderSheet() {
    const rows = (sheet.rows || [])
      .map(
        (r) => `<tr>
          <td>${r.row_no}</td>
          <td>${esc(r.partition_code)}</td>
          <td><span class="chip ${r.row_status === "已盖" ? "chip-done" : "chip-open"}">${esc(r.row_status)}</span></td>
          <td>${esc(r.watermark_value || "-")}</td>
          <td>${esc(r.stamped_at || "-")}</td>
        </tr>`
      )
      .join("");
    const residuesHtml = (sheet.residues || []).length
      ? sheet.residues
          .map(
            (r) => `<li>#${r.id} · ${
              r.state === "待续写" ? boundaryChip(r.boundary) : closedChip(r.boundary)
            } · 已提交 ${r.committed_rows} 行 / 待盖 ${r.pending_rows} 行 / 回滚 ${r.rolled_back_rows} 行<span class="muted"> · ${esc(
              r.detail
            )}</span></li>`
          )
          .join("")
      : '<li class="muted">无残段</li>';
    const tasksHtml = (sheet.makeup_tasks || []).length
      ? sheet.makeup_tasks
          .map(
            (t) => `<li>${esc(t.task_code)} · <span class="chip ${
              t.status === "待补盖" ? "chip-makeup" : "chip-done"
            }">${esc(t.status)}</span> · 剩余 ${t.remaining_rows} 行${
              t.remaining_codes.length ? "（" + t.remaining_codes.map(esc).join("、") + "）" : ""
            }</li>`
          )
          .join("")
      : '<li class="muted">无补盖任务</li>';
    return `<div class="cardish">
      <div class="row between">
        <h3>盖印附页 · ${esc(sheet.run_code)}</h3>
        <button type="button" class="ghost" id="btn-close-sheet">收起</button>
      </div>
      <p class="muted">批次状态 ${esc(sheet.status)} · 批次态 ${esc(sheet.stamp_state)} · 当前标记 ${stateChip(
        sheet
      )}</p>
      ${renderDriftBanner()}
      ${
        (sheet.consistency || []).length
          ? `<div class="alert"><strong>本批残段行与批次态漂移：</strong><ul>${sheet.consistency
              .map((i) => `<li>${esc(i)}</li>`)
              .join("")}</ul></div>`
          : ""
      }
      <table>
        <thead><tr><th>行</th><th>分区码</th><th>行态</th><th>水印</th><th>盖印时间</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <h3>残段</h3>
      <ul>${residuesHtml}</ul>
      <h3>补盖任务</h3>
      <ul>${tasksHtml}</ul>
    </div>`;
  }

  function renderResidues() {
    const openCount = residues.filter((r) => r.state === "待续写").length;
    const taskByResidue = {};
    makeupTasks.forEach((t) => (taskByResidue[t.residue_id] = t));
    return `
      <h2>残段目录</h2>
      <p class="muted">每处残段登记边界；点「续写」由后端按边界一次落库：回滚恢复待盖，留存另开补盖任务。写锁排他，双侧同时点只落一笔。</p>
      ${renderDriftBanner()}
      <p class="muted">待续写 ${openCount} 处 · 已收口 ${residues.length - openCount} 处</p>
      ${
        residues.length
          ? `<table>
        <thead><tr><th>#</th><th>批次</th><th>边界芯片</th><th>已提交</th><th>待盖</th><th>回滚</th><th>补盖任务</th><th>收口动作</th></tr></thead>
        <tbody>
          ${residues
            .map((r) => {
              const task = taskByResidue[r.id];
              return `<tr>
              <td>${r.id}</td>
              <td>${esc(r.run_code)}</td>
              <td>${
                r.state === "待续写" ? boundaryChip(r.boundary) : closedChip(r.boundary)
              }</td>
              <td>${r.committed_rows}</td>
              <td>${r.pending_rows}</td>
              <td>${r.rolled_back_rows}</td>
              <td>${
                task
                  ? `<span class="chip ${task.status === "待补盖" ? "chip-makeup" : "chip-done"}">${esc(
                      task.status
                    )}</span> ${esc(task.task_code)}（剩 ${task.remaining_rows} 行）`
                  : '<span class="muted">-</span>'
              }</td>
              <td class="ops">${
                r.state === "待续写"
                  ? `<button type="button" data-resume="${r.id}">续写收口</button>`
                  : task && task.status === "待补盖"
                  ? `<button type="button" data-fill="${task.id}">补盖剩余码</button>`
                  : '<span class="muted">已收口</span>'
              }</td>
            </tr>
            <tr class="sub"><td colspan="8" class="muted">${esc(r.detail || "")}</td></tr>`;
            })
            .join("")}
        </tbody>
      </table>`
          : '<p class="muted">暂无残段。</p>'
      }
      <h3>编排任务落档</h3>
      ${
        events.length
          ? `<table>
        <thead><tr><th>#</th><th>批次</th><th>残段</th><th>事件</th><th>明细</th><th>时间</th></tr></thead>
        <tbody>${events
          .map(
            (e) => `<tr>
            <td>${e.id}</td>
            <td>${esc(e.batch_id == null ? "-" : e.batch_id)}</td>
            <td>${esc(e.residue_id == null ? "-" : e.residue_id)}</td>
            <td>${esc(EVENT_LABEL[e.event] || e.event)}</td>
            <td>${esc(e.detail)}</td>
            <td>${esc(e.created_at)}</td>
          </tr>`
          )
          .join("")}</tbody>
      </table>`
          : '<p class="muted">暂无落档事件。</p>'
      }`;
  }

  const EVENT_LABEL = {
    residue_resume_rollback: "续写 · 回滚收口",
    residue_resume_retain: "续写 · 留存收口",
    makeup_filled: "补盖 · 写剩余码",
  };

  function renderTrace() {
    const src = (traceResult && traceResult.sources) || [];
    const down = (traceResult && traceResult.downstream) || [];
    return `
      <h2>下游追索</h2>
      <label>水印值<input id="trace-input" value="${esc(traceValue)}" placeholder="例如 WM-ORD-131" /></label>
      <div class="row"><button type="button" id="btn-trace">追索</button></div>
      ${
        traceResult
          ? `<div>
        <h3>源批次</h3>
        <ul>${src.map((s) => `<li>${esc(s.run_code)} · ${esc(s.table_name)}</li>`).join("")}</ul>
        <h3>下游</h3>
        <ul>${down
          .map(
            (d) =>
              `<li>${esc(d.run_code)} · ${esc(d.status)} · ${esc(d.watermark_value || "未盖印")}</li>`
          )
          .join("")}</ul>
      </div>`
          : ""
      }`;
  }

  function renderGaps() {
    return `
      <h2>缺口对账席</h2>
      <p class="muted">对账与残段目录同源：摘要露出残段与留存行数，漂移时拒绝生成对账。</p>
      ${renderDriftBanner()}
      <div class="row"><button type="button" id="btn-gap">生成对账</button></div>
      ${
        reports.length
          ? `<div class="cardish">
          <h3>最近一次对账</h3>
          <p>${esc(reports[0].summary)}</p>
          <p class="muted">应盖印 ${reports[0].expected_count} 批 / ${reports[0].expected_rows} 行 · 已盖印 ${reports[0].stamped_count} 批 / ${reports[0].stamped_rows} 行 · 缺口 ${reports[0].gap_count} 批 / ${reports[0].gap_rows} 行</p>
          <p>残段标记：${
            residues.length
              ? residues
                  .map((r) =>
                    r.state === "待续写" ? boundaryChip(r.boundary) : closedChip(r.boundary)
                  )
                  .join(" ")
              : '<span class="muted">无残段</span>'
          }</p>
        </div>`
          : '<p class="muted">尚未生成对账。</p>'
      }
      <table>
        <thead><tr><th>编号</th><th>截至</th><th>缺口（批/行）</th><th>残段</th><th>摘要</th></tr></thead>
        <tbody>
          ${reports
            .map(
              (r) => `<tr>
            <td>${r.id}</td>
            <td>${esc(r.as_of_date)}</td>
            <td>${r.gap_count} / ${r.gap_rows}</td>
            <td>${r.residue_count} 处待续写 · ${r.retained_rows} 行留存已收口</td>
            <td>${esc(r.summary)}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  async function runResume(id) {
    setMsg("");
    try {
      const r = await api("/api/residues/" + id + "/resume", { method: "POST" });
      const closed = r.residue.boundary;
      await refresh();
      setMsg(
        closed === "回滚"
          ? `续写收口（回滚）：已提交行恢复待盖，残段已标记「已收口·回滚」，缺口回升`
          : `续写收口（留存）：已提交行保持已盖并占库存，补盖任务 ${r.makeup_task.task_code} 承接剩余 ${r.makeup_task.remaining_rows} 行`,
        "ok"
      );
      render();
    } catch (e) {
      setMsg(e.message, "err");
      await refresh().catch(() => {});
      render();
    }
  }

  async function runFill(id) {
    setMsg("");
    try {
      const r = await api("/api/makeup-tasks/" + id + "/fill", { method: "POST", body: "{}" });
      await refresh();
      setMsg(
        r.remaining_rows
          ? `已补盖 ${r.filled_rows} 行，仍剩 ${r.remaining_rows} 行`
          : `补盖完成：剩余码已写完，批次收为「已盖印」`,
        "ok"
      );
      render();
    } catch (e) {
      setMsg(e.message, "err");
      await refresh().catch(() => {});
      render();
    }
  }

  function bindCommon() {
    view.querySelectorAll("[data-resume]").forEach((btn) => {
      btn.onclick = () => runResume(Number(btn.dataset.resume));
    });
    view.querySelectorAll("[data-fill]").forEach((btn) => {
      btn.onclick = () => runFill(Number(btn.dataset.fill));
    });
    view.querySelectorAll("[data-sheet]").forEach((btn) => {
      btn.onclick = async () => {
        setMsg("");
        try {
          sheet = (await api("/api/batches/" + btn.dataset.sheet + "/sheet")).sheet;
          selected = null;
          render();
        } catch (e) {
          setMsg(e.message, "err");
        }
      };
    });
  }

  function render() {
    view.innerHTML = (TABS[tab] || renderBatches)();
    bindCommon();

    view.querySelectorAll("[data-pick]").forEach((btn) => {
      btn.onclick = () => {
        const id = Number(btn.dataset.pick);
        selected = batches.find((b) => b.id === id) || null;
        sheet = null;
        stampValue = selected ? "WM-" + selected.run_code.toUpperCase() : "";
        render();
      };
    });
    const stampInput = document.getElementById("stamp-input");
    if (stampInput) {
      stampInput.oninput = () => (stampValue = stampInput.value);
      view.querySelectorAll('input[name="boundary"]').forEach((el) => {
        el.onchange = () => (boundary = el.value);
      });
      const drillBox = document.getElementById("drill");
      drillBox.onchange = () => (drill = drillBox.checked);
      const drillInput = document.getElementById("drill-row");
      drillInput.oninput = () => (drillRow = Number(drillInput.value) || 2);
      document.getElementById("btn-cancel").onclick = () => {
        selected = null;
        render();
      };
      document.getElementById("btn-stamp").onclick = async () => {
        setMsg("");
        try {
          await api("/api/batches/" + selected.id + "/stamp", {
            method: "POST",
            body: JSON.stringify({
              watermark_value: stampValue,
              boundary,
              fail_after: drill ? drillRow : null,
            }),
          });
          selected = null;
          await refresh();
          setMsg("盖印完成", "ok");
          render();
        } catch (e) {
          selected = null;
          await refresh().catch(() => {});
          setMsg(e.message, "err");
          render();
        }
      };
    }
    const closeSheet = document.getElementById("btn-close-sheet");
    if (closeSheet) closeSheet.onclick = () => {
      sheet = null;
      render();
    };
    const traceInput = document.getElementById("trace-input");
    if (traceInput) {
      traceInput.oninput = () => (traceValue = traceInput.value);
      document.getElementById("btn-trace").onclick = async () => {
        setMsg("");
        try {
          traceResult = await api("/api/trace", {
            method: "POST",
            body: JSON.stringify({ watermark_value: traceValue }),
          });
          setMsg("追索完成", "ok");
          render();
        } catch (e) {
          setMsg(e.message, "err");
        }
      };
    }
    const gapBtn = document.getElementById("btn-gap");
    if (gapBtn) {
      gapBtn.onclick = async () => {
        setMsg("");
        try {
          const r = await api("/api/gap-reports", {
            method: "POST",
            body: JSON.stringify({ as_of_date: "2026-11-16" }),
          });
          await refresh();
          setMsg(r.report.summary, "ok");
          render();
        } catch (e) {
          setMsg(e.message, "err");
          await refresh().catch(() => {});
          render();
        }
      };
    }
  }

  document.querySelectorAll(".dock [data-tab]").forEach((btn) => {
    btn.onclick = () => {
      tab = btn.dataset.tab;
      if (tab !== "batches") {
        selected = null;
        sheet = null;
      }
      syncDock();
      render();
    };
  });
  document.getElementById("btn-logout").onclick = () => {
    fetch("/api/logout", { method: "POST" }).then(() => (location.href = "/login"));
  };

  refresh()
    .then(() => {
      syncDock();
      render();
    })
    .catch((e) => setMsg(e.message, "err"));
})();
