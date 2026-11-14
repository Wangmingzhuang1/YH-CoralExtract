"use strict";

const $ = (selector) => document.querySelector(selector);
const fmt = new Intl.NumberFormat("zh-CN");
const number = (value) => fmt.format(value);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const statuses = { pending: "待复核", annotated: "已批注", partial: "部分已裁决", resolved: "已保存裁决", history: "批次已有结论" };
const actions = { confirm_unit: "确认源单位", confirm_mapping: "确认列映射", exclude: "排除观测", select_column: "选择指标列",
  select_time_column: "选择年代列", confirm_coordinates: "确认精确坐标", needs_human: "需要人工核对" };
const fields = { year: "公元年（CE）", age_bp: "年 BP", age_ka_bp: "千年 BP", age_bce: "公元前（BCE）",
  "Sr/Ca_value": "Sr/Ca", "Ba/Ca_value": "Ba/Ca", "δ18O_value": "δ¹⁸O",
  "δ13C_value": "δ¹³C", "sea_surface_temperature_value": "SST" };
const query = new URLSearchParams(window.location.search);
const state = { category: query.get("category") || "", status: query.get("status") || "open",
  search: query.get("q") || "", offset: 0,
  limit: 24, caseId: query.get("case") || "", item: "", tab: "source", recordOffset: 0, detail: null,
  overview: null, listVersion: 0, detailVersion: 0, evidenceVersion: 0 };

async function api(path, params, body) {
  const url = params ? path + "?" + new URLSearchParams(params) : path;
  const options = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw Object.assign(new Error(data.error || "请求失败"), { status: response.status });
  return data;
}

function empty(message) {
  return '<p class="empty">' + escapeHtml(message) + '</p>';
}

let timer;
function notify(text) {
  const element = $("#notification");
  element.textContent = text;
  element.hidden = false;
  clearTimeout(timer);
  timer = setTimeout(() => { element.hidden = true; }, 3800);
}

function option(value, label) {
  return '<option value="' + escapeHtml(value) + '">' + escapeHtml(label) + '</option>';
}

function categoryLabel(value) {
  return state.overview?.categories.find((c) => c.id === value)?.label || value;
}

function updateAddress() {
  const params = new URLSearchParams();
  if (state.category) params.set("category", state.category);
  params.set("status", state.status);
  if (state.search) params.set("q", state.search);
  if (state.caseId) params.set("case", state.caseId);
  history.replaceState(null, "", params.size ? "?" + params : location.pathname);
}

async function loadOverview() {
  const data = await api("/api/overview");
  state.overview = data;
  $("#batch").textContent = data.batch;
  $("#metrics").innerHTML = [["论文复核后待人工", data.paper_cases, data.paper_records, "paper"],
    ["全部待处理", data.open_cases, data.open_records, "open"]].map(([label, groups, records, status]) =>
    '<div><dt><button type="button" data-queue-status="' + status + '">' + label + '</button></dt><dd>' +
    number(groups) + ' <small>组 · ' + number(records) + ' 条</small></dd></div>').join("");
  $("#metrics").querySelectorAll("button").forEach((button) => button.addEventListener("click", () => {
    state.category = "";
    state.search = "";
    $("#category").value = "";
    $("#search").value = "";
    $("#status").value = button.dataset.queueStatus;
    $("#status").dispatchEvent(new Event("change"));
  }));
  $("#category").innerHTML = option("", "全部类别") + data.categories
    .map((c) => option(c.id, c.label + " · " + number(c.count))).join("");
  $("#category").value = state.category;
  $("#export-decisions").setAttribute("aria-disabled", String(!data.decisions));
  $("#export-notes").setAttribute("aria-disabled", String(!data.annotations));
}

function updateSelected() {
  $("#cases").querySelectorAll("[data-case]").forEach((button) => {
    button.setAttribute("aria-current", String(button.dataset.case === state.caseId));
  });
}

async function loadList({ choose = true } = {}) {
  const version = ++state.listVersion;
  const target = $("#cases");
  try {
    const data = await api("/api/cases", { category: state.category, status: state.status,
      q: state.search, offset: state.offset });
    if (version !== state.listVersion) return;
    if (state.offset && state.offset >= data.total) {
      state.offset = Math.max(0, Math.floor((data.total - 1) / state.limit) * state.limit);
      return loadList({ choose });
    }
    $("#list-total").textContent = number(data.total) + " 组";
    $("#page").textContent = data.total ? number(state.offset + 1) + "–" + number(Math.min(state.offset + state.limit, data.total)) : "0 组";
    $("#previous").disabled = state.offset === 0;
    $("#next").disabled = state.offset + state.limit >= data.total;
    target.innerHTML = data.cases.map((group) => {
      const meta = [categoryLabel(group.category), group.column || group.sheet, statuses[group.status]].filter(Boolean).join(" · ");
      return '<button type="button" class="case" data-case="' + group.id + '">' +
        '<span class="case-info"><span class="case-name">' + escapeHtml(group.name) + '</span>' +
        '<span class="case-reason">' + escapeHtml(group.reason) + '</span>' +
        '<span class="case-meta">' + escapeHtml(meta) + '</span></span>' +
        '<span class="case-count"><b>' + number(["paper", "open"].includes(state.status) ? group.count - group.completed : group.count) + '</b>条记录</span></button>';
    }).join("") || empty("没有符合条件的复核组。");
    target.querySelectorAll("[data-case]").forEach((button) => button.addEventListener("click", () => {
      openCase(button.dataset.case, { focus: true });
    }));
    if (choose) {
      const current = data.cases.find((c) => c.id === state.caseId);
      const group = current || data.cases[0];
      if (group) openCase(group.id);
      else {
        ++state.detailVersion;
        ++state.evidenceVersion;
        state.caseId = "";
        state.detail = null;
        $("#detail").innerHTML = empty("调整筛选条件后选择复核组。");
        updateAddress();
      }
    }
    updateSelected();
  } catch (error) {
    if (version === state.listVersion) target.innerHTML = empty(error.message);
  }
}

async function openCase(id, { item = "", focus = false, keepTab = false } = {}) {
  const version = ++state.detailVersion;
  ++state.evidenceVersion;
  state.caseId = id;
  state.item = item;
  if (!keepTab) {
    state.tab = "source";
    state.recordOffset = 0;
  }
  updateSelected();
  updateAddress();
  const section = $("#detail");
  section.innerHTML = empty("正在载入复核组…");
  try {
    const detail = await api("/api/case", { id, item, offset: state.recordOffset });
    if (version !== state.detailVersion) return;
    state.detail = detail;
    state.item = detail.item.item_key;
    renderDetail(detail);
    if (focus && matchMedia("(max-width: 760px)").matches) section.scrollIntoView({ block: "start" });
  } catch (error) {
    if (version !== state.detailVersion) return;
    if (error.status === 404) {
      state.caseId = "";
      if (!query.has("status") && !state.category && state.overview.paper_cases) {
        state.status = "paper";
        $("#status").value = state.status;
      }
      await loadList();
      return;
    }
    section.innerHTML = empty(error.message);
  }
}

function renderDetail(detail) {
  const record = detail.item;
  const facts = [["表块", record.sheet], ["列", record.column], ["指标 / 年代", fields[record.decision] || record.decision],
    ["源值", record.source_value], ["源单位", record.source_unit], ["源行", record.source_line || record.source_row]];
  $("#detail").innerHTML =
    '<header class="case-head"><div class="eyebrow">' + escapeHtml(categoryLabel(detail.category)) +
    '<span class="status ' + detail.status + '">' + statuses[detail.status] + '</span><span>' + number(detail.count) +
    ' 条队列记录</span></div><h2>' + escapeHtml(detail.name) + '</h2><p class="case-path">' + escapeHtml(detail.file) +
    '</p><p class="case-reason-full">' + escapeHtml(detail.reason) + '</p><div class="fact-list">' +
    facts.filter(([, value]) => value).map(([label, value]) => "<span>" + label + " <b>" + escapeHtml(value) + "</b></span>").join("") +
    '</div></header><div class="tabs" role="tablist" aria-label="复核证据">' +
    [["source", "源文件"], ["records", "组内记录"], ["linked", "已提取观测"]].map(([key, label]) =>
      '<button type="button" class="tab" id="tab-' + key + '" role="tab" aria-controls="evidence" data-tab="' + key + '">' + label + '</button>').join("") +
    '</div><div class="evidence" id="evidence" role="tabpanel"></div><section class="decision-area" id="decision-area"></section>';
  $("#detail").querySelectorAll("[data-tab]").forEach((button) => button.addEventListener("click", () => {
    state.tab = button.dataset.tab;
    loadEvidence();
  }));
  renderDecision(detail);
  loadEvidence();
}

function table(headers, rows, { highlightLine = null, highlighted = [] } = {}) {
  if (!rows.length) return empty("没有可显示的记录。");
  const count = Math.max(headers.length, ...rows.map((row) => row.cells.length));
  const firstColumn = ["行", ...Array.from({ length: count }, (_, i) => headers[i] || "第 " + (i + 1) + " 列")];
  return '<div class="table-scroll" tabindex="0" aria-label="数据表"><table><thead><tr>' +
    firstColumn.map((value, i) => '<th scope="col" class="' + (highlighted.includes(i - 1) ? "cell-hit" : "") + '">' +
      escapeHtml(value) + '</th>').join("") + '</tr></thead><tbody>' +
    rows.map((row) => '<tr class="' + (row.line === highlightLine ? "row-hit" : "") + '"><td>' + escapeHtml(row.line) +
      '</td>' + Array.from({ length: count }, (_, i) => '<td class="' + (highlighted.includes(i) ? "cell-hit" : "") + '">' +
        escapeHtml(row.cells[i] ?? "") + '</td>').join("") + '</tr>').join("") + '</tbody></table></div>';
}

function disclosures(detail, source) {
  const blocks = [];
  if (detail.max_review) {
    const review = detail.max_review;
    const evidence = (review.evidence || []).map((entry, index) => {
      const page = entry.at?.match(/PDF 第 (\d+) 页/)?.[1];
      const evidenceUrl = '/api/review-evidence?id=' + detail.id + '&amp;index=' + index;
      const link = page ? '<a target="_blank" rel="noopener" href="' + evidenceUrl + '#page=' + page + '">打开原文</a> ' :
        entry.kind === "attachment" ? '<a href="' + evidenceUrl + '">下载附件</a> ' : "";
      return '<li><b>' + escapeHtml([entry.file, entry.at].filter(Boolean).join(" · ")) + '</b> ' +
        link + escapeHtml(entry.quote) + '</li>';
    }).join("");
    blocks.push('<details open><summary>批次证据复核 · ' +
      (review.state === "verified" ? "已核验，可回灌" : review.state === "excluded" ? "已自动排除" : review.state === "pending" ? "待核" : "待人工") +
      '</summary><div class="declarations"><p>' + escapeHtml(review.reason) + '</p>' +
      (evidence ? '<ul class="declarations">' + evidence + '</ul>' : "") + '</div></details>');
  }
  if (source.declarations?.length) {
    blocks.push('<details open><summary>源文件变量与站点说明</summary><ul class="declarations">' +
      source.declarations.map((d) => '<li><b>第 ' + d.line + ' 行</b> ' + escapeHtml(d.text) + '</li>').join("") + '</ul></details>');
  }
  if (Array.isArray(detail.unit_evidence) && detail.unit_evidence.length) {
    const labels = { header: "列名", unit_row: "单位行", archive_definition: "档案定义", database_definition: "档案定义", body: "正文引句", table_note: "表注" };
    blocks.push('<details><summary>提取时记录的单位证据</summary><ul class="declarations">' +
      detail.unit_evidence.filter((entry) => entry && typeof entry === "object").map((entry) => {
        const unit = entry.source === "body" ? (entry.quoted_units || []).join(" / ") : entry.label || entry.unit;
        return '<li><b>' + escapeHtml(labels[entry.source] || entry.source) + '</b> ' +
          escapeHtml([unit, entry.locator, entry.quote].filter(Boolean).join(" · ")) + '</li>';
      }).join("") + '</ul></details>');
  }
  if (detail.bounds && Object.keys(detail.bounds).length) {
    const labels = { Northernmost_Latitude: "北界", Southernmost_Latitude: "南界", Easternmost_Longitude: "东界", Westernmost_Longitude: "西界" };
    const text = Object.entries(detail.bounds).map(([key, value]) => (labels[key] || key) + " " + value).join(" · ");
    const midpoint = detail.suggested_lat && detail.suggested_lon ?
      "<p>范围中点 " + escapeHtml(detail.suggested_lat + ", " + detail.suggested_lon) + "，仅供定位。</p>" : "";
    blocks.push('<details open><summary>档案空间范围</summary><div class="declarations"><p>' + escapeHtml(text) + '</p>' + midpoint + '</div></details>');
  }
  return blocks.length ? '<div class="disclosures">' + blocks.join("") + '</div>' : "";
}

async function loadEvidence() {
  const version = ++state.evidenceVersion;
  const detail = state.detail;
  const box = $("#evidence");
  if (!detail || !box) return;
  $("#detail").querySelectorAll("[data-tab]").forEach((button) => {
    button.setAttribute("aria-selected", String(button.dataset.tab === state.tab));
  });
  box.setAttribute("aria-labelledby", "tab-" + state.tab);
  if (state.tab === "records") return renderRecords(box, detail);
  box.innerHTML = empty("正在读取证据…");
  try {
    const params = { id: detail.id, item: state.item };
    const source = state.tab === "source";
    const data = await api(source ? "/api/source" : "/api/linked", params);
    if (version !== state.evidenceVersion) return;
    if (source) {
      box.innerHTML = '<div class="evidence-head"><span class="quiet-text">' + escapeHtml(data.note || "源文件") + '</span>' +
        (data.downloadable ? '<a class="button quiet" href="/api/source-file?' + escapeHtml(new URLSearchParams(params)) + '" download>下载原件</a>' : "") + '</div>' +
        (data.rows?.length ? table(data.header || [], data.rows, { highlightLine: data.highlight_line, highlighted: data.highlight_cols }) :
          (data.entries?.length ? '<ul class="declarations">' + data.entries.map((name) => '<li>' + escapeHtml(name) + '</li>').join("") + '</ul>' : "")) +
        disclosures(detail, data);
    } else {
      const columns = Object.keys(data.rows[0] || {}).filter((key) => !["source_file", "source_row", "source_line", "sheet_name"].includes(key));
      const names = { year_CE: "年代（CE）", qc_flag: "质控标记", sample_id: "样品编号" };
      box.innerHTML = data.rows.length ?
        '<p class="quiet-text">' + (data.scope === "row" ? "与所选源行对应" : "同一源文件或表块的前几条观测") + '</p>' +
        table(columns.map((key) => names[key] || key), data.rows.map((row) => ({ line: row.source_line || row.source_row,
          cells: columns.map((key) => row[key]) }))) : empty(data.scope === "row" ? "该源行没有对应的已提取观测。" : "该源文件或表块没有已提取观测。");
    }
  } catch (error) {
    if (version === state.evidenceVersion) box.innerHTML = empty(error.message);
  }
}

function renderRecords(box, detail) {
  box.innerHTML = '<div class="table-scroll"><table><thead><tr><th>源行</th><th>源值</th><th>源单位 / 指标</th><th>状态</th></tr></thead><tbody>' +
    detail.records.map((row, index) => '<tr class="' + (row.item_key === state.item ? "row-hit" : "") +
      '"><td><button class="button quiet" type="button" data-record="' + escapeHtml(row.item_key) + '">' +
      escapeHtml(row.source_line || row.source_row || ("记录 " + (state.recordOffset + index + 1))) +
      '</button></td><td>' + escapeHtml(row.source_value) + '</td><td>' +
      escapeHtml(row.source_unit || fields[row.decision] || row.decision) + '</td><td>' +
      (row.saved ? "已保存裁决" : "") + '</td></tr>').join("") + '</tbody></table></div>' +
    (detail.count > 20 ? '<div class="pager"><button id="records-previous" class="button quiet" type="button">上一页</button><span>' +
      number(state.recordOffset + 1) + "–" + number(Math.min(state.recordOffset + 20, detail.count)) + '</span>' +
      '<button id="records-next" class="button quiet" type="button">下一页</button></div>' : "");
  box.querySelectorAll("[data-record]").forEach((button) => button.addEventListener("click", () => {
    state.tab = "source";
    openCase(detail.id, { item: button.dataset.record, keepTab: true });
  }));
  if ($("#records-previous")) {
    $("#records-previous").disabled = state.recordOffset === 0;
    $("#records-next").disabled = state.recordOffset + 20 >= detail.count;
    for (const [selector, change] of [["#records-previous", -20], ["#records-next", 20]]) {
      $(selector).addEventListener("click", () => {
        state.recordOffset += change;
        openCase(detail.id, { item: state.item, keepTab: true });
      });
    }
  }
}

function renderDecision(detail) {
  const area = $("#decision-area");
  const record = detail.decision_record;
  const annotation = detail.annotation;
  if (detail.category === "history") {
    area.innerHTML = '<h3>批次已有结论</h3><p>' + escapeHtml(detail.item.review_decision ? (actions[detail.item.review_decision] || detail.item.review_decision) + "。 " : "") +
      escapeHtml(detail.item.reason) + '</p>';
    return;
  }
  const unit = detail.action === "confirm_unit";
  const coordinates = detail.action === "confirm_coordinates";
  const column = ["select_column", "select_time_column"].includes(detail.action);
  const editable = unit || coordinates || column;
  let inputs = "";
  if (unit) {
    inputs = '<div class="form-row' + (detail.count > 1 ? ' form-three' : '') + '"><label>操作<select id="decision-action">' + option("confirm_unit", "确认源单位或标尺") +
      option("exclude", "排除观测") + '</select></label><label id="unit-label">源单位或标尺<input id="unit" value="' +
      escapeHtml(record?.confirmed_unit) + '" placeholder="如 ‰ VPDB 或 mmol/mol"></label>';
    if (detail.count > 1) inputs += '<label>应用范围<select id="scope">' +
      option("item", "当前记录") + option("group", "同组待处理记录（" + number(detail.count - detail.completed) + " 条）") + '</select></label>';
    inputs += '</div>';
  } else if (column) {
    inputs = '<div class="form-row"><label>' + actions[detail.action] + '<select id="selected-column">' +
      option("", "请选择源表列") + detail.candidates.map((value) => option(value, value)).join("") + '</select></label>' +
      (detail.action === "select_time_column" && !detail.target ? '<label>年代基准<select id="time-target">' +
        option("", "请选择年代基准") + ["year", "age_bp", "age_ka_bp", "age_bce"].map((key) => option(key, fields[key])).join("") + '</select></label>' : "") + '</div>';
  } else if (coordinates) {
    inputs = '<div class="form-row"><label>纬度<input type="number" step="any" min="-90" max="90" id="lat" value="' + escapeHtml(record?.coord_lat) +
      '" placeholder="十进制度"></label><label>经度<input type="number" step="any" min="-180" max="180" id="lon" value="' +
      escapeHtml(record?.coord_lon) + '" placeholder="十进制度"></label></div>';
  }
  area.innerHTML = '<div class="section-heading"><h3>' + (editable ? "复核裁决" : "人工批注") + '</h3>' +
    (unit ? '<div class="model-tools"><label><span class="sr-only">辅助模型</span><select id="model"><option>flash</option><option>max</option></select></label>' +
      '<button type="button" id="suggest" class="button quiet">获取模型建议</button></div>' : "") + '</div>' +
    (record ? '<p class="saved-note"><strong>已保存 ' + escapeHtml(actions[record.review_decision] || record.review_decision) +
      '</strong> · ' + escapeHtml(record.review_note) + '</p>' : "") +
    (annotation ? '<p class="saved-note"><strong>已有批注</strong> · ' + escapeHtml(annotation.note) + '</p>' : "") +
    (detail.conversion_missing ? '<p class="saved-note">源单位已有声明，当前规则暂缓换算。可把待补规则记为批注。</p>' : "") +
    '<div id="model-result"></div><form id="review-form">' + inputs +
    '<label class="note-label">' + (editable ? "依据或批注" : "批注") +
    '<textarea id="note" placeholder="记录源文件页码、表注、列名或待查事项。">' +
    escapeHtml(record?.review_note || annotation?.note || "") + '</textarea></label>' +
    (editable ? '<label class="verify"><input id="verified" type="checkbox">' +
      (coordinates ? '已核实来源明确给出了精确站点坐标' : '已核对源文件及依据') + '</label>' : "") +
    '<div class="form-actions">' +
    (editable ? '<button type="submit" class="button primary" id="save-decision">保存裁决</button>' : "") +
    '<button type="' + (editable ? "button" : "submit") + '" id="save-note" class="button ' +
    (editable ? "quiet" : "primary") + '">保存批注</button></div><p id="form-error" class="inline-error" role="alert" hidden></p></form>';
  if (unit) {
    $("#decision-action").value = record?.review_decision === "exclude" ? "exclude" : "confirm_unit";
    const showUnit = () => { $("#unit-label").hidden = $("#decision-action").value === "exclude"; };
    $("#decision-action").addEventListener("change", showUnit);
    showUnit();
    $("#suggest").addEventListener("click", () => suggest(detail));
  }
  if (column) {
    $("#selected-column").value = record?.selected_column || "";
    if ($("#time-target")) $("#time-target").value = record?.decision || "";
  }
  $("#review-form").addEventListener("submit", (event) => {
    event.preventDefault();
    save(detail, editable);
  });
  if (editable) $("#save-note").addEventListener("click", () => save(detail, false));
}

async function save(detail, isDecision) {
  const button = $(isDecision ? "#save-decision" : "#save-note");
  const error = $("#form-error");
  const note = $("#note").value;
  if (!note.trim() || (isDecision && !$("#verified").checked)) {
    error.textContent = !note.trim() ? "请填写依据或批注。" : "请先核对源证据。";
    error.hidden = false;
    return;
  }
  error.hidden = true;
  button.disabled = true;
  const item = state.item;
  const body = { id: detail.id, item_key: item, note };
  if (isDecision) Object.assign(body, {
    action: $("#decision-action")?.value || detail.action, scope: $("#scope")?.value || "item",
    source_verified: $("#verified").checked, unit: $("#unit")?.value || "",
    selected_column: $("#selected-column")?.value || "", target: $("#time-target")?.value || "",
    lat: $("#lat")?.value || "", lon: $("#lon")?.value || "",
  });
  try {
    const result = await api(isDecision ? "/api/decision" : "/api/annotation", null, body);
    notify(isDecision ? "已保存 " + number(result.saved) + " 条裁决，可导出回灌。" : "批注已保存。");
    await Promise.all([loadOverview(), loadList({ choose: false })]);
    if (state.caseId === detail.id && state.item === item) openCase(detail.id, { item, keepTab: true });
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  } finally {
    button.disabled = false;
  }
}

async function suggest(detail) {
  const button = $("#suggest");
  const box = $("#model-result");
  const model = $("#model").value;
  button.disabled = true;
  box.innerHTML = empty("正在读取模型建议…");
  try {
    const result = await api("/api/assist", null, { id: detail.id, item_key: state.item, model });
    box.innerHTML = '<div class="model-result"><strong>' + escapeHtml(actions[result.action] || result.action) +
      '</strong> ' + escapeHtml(result.confirmed_unit) + '<p>' + escapeHtml(result.rationale || result.note) + '</p>' +
      (result.note ? '<button type="button" class="button quiet">带入批注</button>' : "") + '</div>';
    box.querySelector("button")?.addEventListener("click", () => {
      $("#note").value = [$("#note").value, "模型建议（待核）：" + result.note].filter(Boolean).join("\n");
      $("#verified").checked = false;
      $("#note").focus();
    });
  } catch (err) {
    box.innerHTML = empty(err.message);
  } finally {
    button.disabled = false;
  }
}

$("#filters").addEventListener("submit", (event) => {
  event.preventDefault();
  state.search = $("#search").value.trim();
  state.offset = 0;
  loadList();
});
$("#category").addEventListener("change", () => {
  state.category = $("#category").value;
  if (state.category === "history") state.status = "history";
  else if (state.status === "history") state.status = "open";
  $("#status").value = state.status;
  state.offset = 0;
  loadList();
});
$("#status").addEventListener("change", () => {
  state.status = $("#status").value;
  state.offset = 0;
  loadList();
});
$("#previous").addEventListener("click", () => { state.offset -= state.limit; loadList(); });
$("#next").addEventListener("click", () => { state.offset += state.limit; loadList(); });
document.querySelectorAll(".exports a").forEach((link) => link.addEventListener("click", (event) => {
  if (link.getAttribute("aria-disabled") === "true") event.preventDefault();
}));
if (state.category === "history") {
  state.status = "history";
}
$("#status").value = state.status;
$("#search").value = state.search;
loadOverview().then(async () => {
  const initialCase = state.caseId;
  if (!initialCase && !query.has("status") && !state.category && state.overview.paper_cases) {
    state.status = "paper";
    $("#status").value = state.status;
  }
  await loadList({ choose: !initialCase });
  if (initialCase) openCase(initialCase);
}).catch((error) => { $("#cases").innerHTML = empty(error.message); });
