import { getSharedScope, setSharedScope, formatScopeLabel } from "./series-ui.js";
const UI=window.SeriesUI||{t:(s,p={})=>String(s).replace(/\{(\w+)\}/g,(m,k)=>Object.hasOwn(p,k)?String(p[k]??''):m),msg(s,p={}){return this.t(s,p);},text(n,v){n.textContent=String(v??'');return n;},bind(n,s,p={},a=''){if(a)n.setAttribute(a,this.t(s,p));else n.textContent=this.t(s,p);return n;},onLocaleChange:()=>()=>{}};
const $ = (id) => document.getElementById(id);
const bridge = (window.DynamicsPanelBridge || window.AstrBotPluginPage);
let profiles = [];
let activeScope = getSharedScope();
const picker = window.VisualReferences.create($("reference-picker"), {onChange(refs) { const select=$("profile-form").elements.references; select.replaceChildren(...refs.map(hash=>new Option(hash,hash,true,true))); }});
const notice = (text) => { $("notice").textContent = text; };
const charactersPage = new URLSearchParams(location.search).get("section") === "characters";
UI.bind($("page-title"),charactersPage?"角色形象":"工坊总览");
UI.bind($("page-description"),charactersPage?"维护角色的外观档案与参考图，创作时自动带入。":"查看运行状态，管理角色形象与最近的创作。");
if (charactersPage) {
  $("page-title").textContent = UI.t("角色形象");
  $("page-description").textContent = UI.t("维护角色的外观档案与参考图，创作时自动带入。");
  document.querySelector(".stats").hidden = true;
  $("history-section").hidden = true;
  $("all-profiles").hidden = true;
  $("characters-section").after($("task-section"));
}
function formError(form, message = "") {
  let node = form.querySelector(".form-error");
  if (!node) { node = el("p", "", "form-error"); node.setAttribute("role", "alert"); form.querySelector("footer").before(node); }
  node.textContent = message;
}
function createPolaroidPlaceholder(profile) {
  const wrap = el("div", "", "vm-polaroid-placeholder");
  const avatar = el("span", (profile.name || profile.key || "?").slice(0, 1), "profile-avatar");
  const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  icon.setAttribute("viewBox", "0 0 24 24");
  icon.setAttribute("width", "24");
  icon.setAttribute("height", "24");
  icon.setAttribute("fill", "none");
  icon.setAttribute("stroke", "currentColor");
  icon.setAttribute("stroke-width", "1.6");
  icon.setAttribute("aria-hidden", "true");
  icon.innerHTML = '<rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="9" cy="10" r="2"/><path d="m21 16-5-5-6 6"/><path d="m14 14-2-2-4 4"/>';
  wrap.append(avatar, icon);
  return wrap;
}
function renderProfiles() {
  const query = $("profile-search").value.trim().toLocaleLowerCase();
  const matches = profiles.filter(profile => `${profile.name} ${profile.key} ${profile.appearance} ${profile.style || ""}`.toLocaleLowerCase().includes(query));
  const visible = charactersPage || query ? matches : matches.slice(0, 3);
  $("profile-result-count").textContent = query ? `${matches.length} / ${profiles.length}` : String(profiles.length);
  $("profiles").replaceChildren();
  if (!visible.length) $("profiles").append(el("p", query ? UI.t("没有匹配的角色，试试其他关键词。") : UI.t("还没有角色形象。添加外观与画风，让之后的创作保持一致。"), "empty"));
  for (const profile of visible) {
    const refs = Array.isArray(profile.references) ? profile.references : [];
    const card = el("article", "", "profile vm-polaroid");
    const frame = el("div", "", "vm-polaroid-frame");
    const stage = el("div", "", "vm-polaroid-stage" + (refs.length ? " is-loading" : " is-empty"));
    if (refs.length) {
      const coverBtn = el("button", "", "vm-cover-button vm-polaroid-cover-btn");
      coverBtn.type = "button";
      coverBtn.setAttribute("aria-label", UI.t("预览 {0} 的标准参考图", { 0: profile.name || profile.key }));
      const image = el("img", "", "vm-profile-cover vm-polaroid-photo");
      image.alt = profile.name || profile.key;
      image.loading = "lazy";
      image.onload = () => stage.classList.remove("is-loading");
      coverBtn.append(image);
      coverBtn.onclick = () => window.VisualReferences.preview(refs[0], profile.name || profile.key);
      stage.append(coverBtn);
      window.VisualReferences.load(refs[0], 420).then(url => {
        if (image.isConnected) image.src = url;
      }).catch(() => {
        if (!stage.isConnected) return;
        stage.classList.remove("is-loading");
        stage.classList.add("is-empty");
        stage.replaceChildren(createPolaroidPlaceholder(profile));
      });
    } else {
      stage.append(createPolaroidPlaceholder(profile));
    }
    const stamp = el("span", UI.msg("{0} 张参考图", { 0: refs.length }), "reference-badge vm-polaroid-stamp");
    stage.append(stamp);
    frame.append(stage);

    if (refs.length > 1) {
      const strip = el("div", "", "vm-polaroid-thumbs");
      for (const [idx, hash] of refs.slice(1, 4).entries()) {
        const thumbBtn = el("button", "", "vm-polaroid-thumb");
        thumbBtn.type = "button";
        thumbBtn.setAttribute("aria-label", UI.t("参考图 {0}", { 0: idx + 2 }));
        const thumbImg = el("img");
        thumbImg.alt = UI.t("参考图 {0}", { 0: idx + 2 });
        thumbImg.loading = "lazy";
        thumbBtn.append(thumbImg);
        thumbBtn.onclick = () => window.VisualReferences.preview(hash, `${profile.name || profile.key} · ${UI.t("参考图 {0}", { 0: idx + 2 })}`);
        window.VisualReferences.load(hash, 160).then(url => {
          if (thumbImg.isConnected) thumbImg.src = url;
        }).catch(() => { thumbBtn.classList.add("is-missing"); });
        strip.append(thumbBtn);
      }
      if (refs.length > 4) {
        strip.append(el("span", `+${refs.length - 4}`, "vm-polaroid-more tabular-nums"));
      }
      frame.append(strip);
    }

    const heading = el("div", "", "profile-heading vm-polaroid-caption");
    const identity = el("div", "", "vm-polaroid-identity");
    identity.append(el("h3", profile.name || profile.key), el("span", profile.key, "profile-key"));
    heading.append(identity);

    const body = el("div", "", "vm-polaroid-body");
    body.append(el("p", profile.appearance || UI.t("尚未填写外观")));
    if (profile.style) {
      const styleChip = el("div", "", "vm-polaroid-style");
      styleChip.append(el("span", UI.msg("画风"), "vm-polaroid-style-label"), el("span", profile.style, "vm-polaroid-style-text"));
      body.append(styleChip);
    }

    const edit = el("button", UI.msg("编辑形象"));
    edit.type = "button";
    edit.onclick = () => editProfile(profile);
    const create = el("button", UI.msg("用角色创作"));
    create.type = "button";
    create.onclick = () => {
      $("generate-form").reset();
      formError($("generate-form"));
      $("generate-form").elements.character.value = profile.key;
      $("generate-dialog").showModal();
    };
    const packBtn = el("button", UI.msg("一键表情套图"));
    packBtn.type = "button";
    packBtn.onclick = () => openCharacterPackDialog(profile);
    const actions = el("div", "", "profile-actions");
    actions.append(edit, create, packBtn);

    card.append(frame, heading, body, actions);
    $("profiles").append(card);
  }
}
UI.onLocaleChange(renderProfiles);$("profile-search").addEventListener("input", renderProfiles);
function unwrap(value) {
  if (value?.status === "error" || value?.success === false) throw new Error(value.message || value.error || UI.t("请求失败"));
  return value?.status === "ok" ? value.data : value;
}
async function get(path, params) { return unwrap(await bridge.apiGet(path, params)); }
async function post(path, body) { return unwrap(await bridge.apiPost(path, body)); }
function el(tag, text, className) { const node = document.createElement(tag); if(text) UI.text(node,text); if(className) node.className = className; return node; }
function buildRemixHref(job) {
  const url = new URL("studio/index.html", location.href);
  url.searchParams.set("ui", document.documentElement.dataset.uiTheme || "day");
  const jobId = job?.id || job?.job_id || "";
  if (jobId) url.searchParams.set("reuse", jobId);
  if (job?.character) url.searchParams.set("character", job.character);
  if (job?.prompt) url.searchParams.set("prompt", String(job.prompt).slice(0, 500));
  url.searchParams.set("umo", activeScope || getSharedScope() || window.DynamicsGetActiveScope?.() || "");
  const language = window.getLanguage?.();
  if (language) url.searchParams.set("lang", language);
  return url.href;
}

async function imageSource(name, thumbnail = false) {
  const data = await get(`webui/image_b64/${encodeURIComponent(name)}`, thumbnail ? {thumb: 1} : {});
  const inlineSource = data?.data_url || data?.url;
  if (inlineSource) return inlineSource;
  const encoded = data?.b64 || data?.base64 || data?.data;
  if (typeof encoded !== "string" || !encoded) throw new Error(UI.t("预览暂不可用"));
  return `data:${data.mime_type || data.mime || "image/png"};base64,${encoded}`;
}

let activeLightboxContext = null;
async function loadLightbox(context, fallback) {
  const {name} = context;
  const img = $("lightbox-preview-img"), status = $("lightbox-preview-status"), retry = $("lightbox-retry");
  const isCurrent = () => activeLightboxContext === context && $("lightbox-dialog").open;
  const showError = () => {
    if (!isCurrent()) return;
    img.onload = img.onerror = null;
    img.hidden = !fallback;
    if (fallback) img.src = fallback;
    else img.removeAttribute("src");
    UI.bind(status, "预览暂不可用");
    retry.hidden = false;
  };
  retry.hidden = true;
  img.onload = img.onerror = null;
  img.hidden = !fallback;
  if (fallback) img.src = fallback;
  else img.removeAttribute("src");
  UI.bind(status, "正在读取图片…");
  try {
    const url = await imageSource(name);
    if (!isCurrent()) return;
    img.onload = () => {
      if (!isCurrent()) return;
      img.hidden = false;
      UI.text(status, "");
    };
    img.onerror = showError;
    img.src = url;
  } catch {
    showError();
  }
}
function openLightbox(job, index, name, thumbnail = "") {
  const dialog = $("lightbox-dialog");
  if (!dialog) return;
  const context = { job, index, name };
  activeLightboxContext = context;
  const img = $("lightbox-preview-img");
  img.alt = job.prompt || UI.t("生成图片");
  $("lightbox-preview-prompt").textContent = job.prompt || UI.t("（未记录画面描述）");
  const meta = $("lightbox-preview-meta");
  meta.replaceChildren();
  if (job.character) meta.append(el("span", `${UI.t("角色")}：${job.character}`, "vm-lightbox-chip"));
  if (job.created_at) {
    const dt = new Date(job.created_at);
    if (!Number.isNaN(dt.getTime())) meta.append(el("span", dt.toLocaleString(), "vm-lightbox-chip"));
  }
  const jid = job.id || job.job_id;
  if (jid) meta.append(el("span", `#${String(jid).slice(0, 10)}`, "vm-lightbox-chip"));
  $("lightbox-remix-link").href = buildRemixHref(job);
  if (!dialog.open) dialog.showModal();
  $("lightbox-retry").onclick = () => loadLightbox(context, thumbnail);
  void loadLightbox(context, thumbnail);
}
$("lightbox-dialog").addEventListener("close", () => {
  activeLightboxContext = null;
  const img = $("lightbox-preview-img");
  img.onload = img.onerror = null;
  img.removeAttribute("src");
});
if ($("lightbox-save-meme")) {
  $("lightbox-save-meme").onclick = () => {
    if (!activeLightboxContext) return;
    $("lightbox-dialog").close();
    saveDialog(activeLightboxContext.job, activeLightboxContext.index);
  };
}

function renderGallerySkeletons(count = 4) {
  const host = $("history");
  host.setAttribute("aria-busy", "true");
  host.replaceChildren();
  for (let i = 0; i < count; i++) {
    const card = el("article", "", "image-card is-skeleton");
    card.setAttribute("aria-hidden", "true");
    const media = el("div", "", "vm-card-media is-loading");
    const details = el("div", "", "details");
    details.append(el("div", "", "vm-skeleton vm-skeleton-line"), el("div", "", "vm-skeleton vm-skeleton-btn"));
    card.append(media, details);
    host.append(card);
  }
}

const overview = {loading: false, ready: false};
function galleryError(error) {
  const message = UI.t("读取失败：{0}", {0:error.message});
  notice(message);
  if (overview.ready) return;
  const retry = el("button", UI.msg("重试"));
  retry.type = "button";
  retry.onclick = refresh;
  const alert = el("div", "", "vm-gallery-error");
  alert.append(el("p", message), retry);
  const host = charactersPage ? $("profiles") : $("history");
  host.replaceChildren(alert);
  $("generation-state").textContent = UI.t("读取失败");
}

async function refresh() {
  if (overview.loading) return;
  overview.loading = true;
  $("refresh").disabled = true;
  if (!overview.ready && !charactersPage) renderGallerySkeletons(4);
  try {
    if (!bridge) throw new Error(UI.t("请从 AstrBot 插件面板打开视觉工坊。"));
    await bridge.ready();
    const overviewParams = activeScope ? { umo: activeScope } : {};
    const data = await get("media/overview", overviewParams);
    profiles = Array.isArray(data.profiles) ? data.profiles : [];
    const generation = data.generation || {};
    $("meme-count").textContent = data.memes;
    $("profile-count").textContent = profiles.length;
    $("generation-state").textContent = generation.ready ? UI.t("已就绪") : UI.t("待配置");
    $("queue-count").textContent = `${generation.active_requests || 0} / ${generation.queued_requests || 0}`;
    renderEmptyLibraryOnboarding(data);
    renderProfiles();
    observer.disconnect();
    $("history").replaceChildren();
    const jobs = (data.history?.items || []).filter((job) => job.images?.length);
    if (!jobs.length) $("history").append(el("p", UI.msg("这里会展示生成的图片。前往生图创作台，开始第一张创作。"), "empty"));
    for (const job of jobs.slice(0, 8)) for (const [index, name] of job.images.entries()) {
      const card = el("article", "", "image-card");
      const mediaBtn = el("button", "", "vm-card-media is-loading");
      mediaBtn.type = "button";
      mediaBtn.setAttribute("aria-label", UI.t("放大预览：{0}", { 0: (job.prompt || UI.t("生成图片")).slice(0, 40) }));
      const image = el("img"), details = el("div", "", "details"), actions = el("div", "", "card-actions");
      image.alt = job.prompt || UI.t("生成图片"); image.loading = "lazy";
      image.dataset.name = name;
      image.onload = () => mediaBtn.classList.remove("is-loading");
      image.onerror = () => renderImageFailure(image);
      mediaBtn.append(image);
      mediaBtn.onclick = () => openLightbox(job, index, name, image.naturalWidth > 0 ? image.currentSrc || image.src : "");
      const button = el("button", UI.msg("收藏为表情")); button.onclick = () => saveDialog(job, index);
      const remix = el("a", UI.msg("带入重绘"), "vm-remix-btn");
      remix.href = buildRemixHref(job);
      actions.append(button, remix);
      details.append(el("p", job.prompt || UI.t("生成图片")), actions);
      card.append(mediaBtn, details);
      $("history").append(card);
      observer.observe(image);
    }
    overview.ready = true;
    if ($("notice").dataset.overviewError) notice("");
    delete $("notice").dataset.overviewError;
  } catch (error) {
    $("notice").dataset.overviewError = "true";
    galleryError(error);
  } finally {
    $("history").removeAttribute("aria-busy");
    overview.loading = false;
    $("refresh").disabled = false;
  }
}
function renderImageFailure(image) {
  const button = image.parentElement;
  if (!button?.isConnected || button.classList.contains("is-failed")) return;
  image.hidden = true;
  image.removeAttribute("src");
  button.classList.remove("is-loading");
  button.classList.add("is-failed");
  button.append(el("span", UI.msg("预览暂不可用 · 打开重试"), "vm-image-fallback"));
}
// Load previews only when visible, preserving responsiveness for long histories.
const observer = new IntersectionObserver((entries) => {
  for (const entry of entries) if (entry.isIntersecting) {
    observer.unobserve(entry.target);
    imageSource(entry.target.dataset.name, true).then((url) => {
      if (entry.target.isConnected) entry.target.src = url;
    }).catch(() => renderImageFailure(entry.target));
  }
});
function ensureIsolateCheckbox() {
  const form = $("profile-form");
  if (!form || form.elements.isolate_references) return;
  const fieldset = form.querySelector(".vm-reference-field");
  if (!fieldset) return;
  const label = el("label", "", "check");
  label.style.marginTop = "8px";
  const box = document.createElement("input");
  box.type = "checkbox";
  box.name = "isolate_references";
  label.append(box, el("span", UI.t("参考图仅用于角色一致性，不参与群聊随机发表情")));
  fieldset.append(label);
}

async function editProfile(profile = {}) {
  const form = $("profile-form"); form.reset();
  ensureIsolateCheckbox();
  formError(form);
  for (const key of ["key", "name", "appearance", "style"]) form.elements[key].value = profile[key] || "";
  if (form.elements.isolate_references) form.elements.isolate_references.checked = Boolean(profile.isolate_references);
  form.elements.key.readOnly = !!profile.key;
  $("profile-dialog").showModal();
  await picker.open(profile.references || []);
}
function saveDialog(job, index) {
  const form = $("save-form"); form.reset();
  formError(form);
  form.elements.generation_id.value = job.id || job.job_id;
  form.elements.image_index.value = index;
  form.elements.description.value = (job.prompt || "").slice(0, 2000);
  form.elements.character.replaceChildren(new Option(UI.t("不指定角色"), ""));
  for (const profile of profiles) form.elements.character.add(new Option(profile.name, profile.key));
  $("save-dialog").showModal();
}
$("profile-form").onsubmit = async (event) => {
  event.preventDefault(); const form = event.currentTarget, button = form.querySelector('[type="submit"]'); button.disabled = true;
  try {
    const data = Object.fromEntries(new FormData(form)); data.references = [...form.elements.references.selectedOptions].map((o) => o.value);
    data.isolate_references = Boolean(form.elements.isolate_references?.checked);
    await post("media/profiles", data); $("profile-dialog").close(); notice(UI.t("角色形象已保存")); await refresh();
  } catch(error) { formError(form, error.message); notice(error.message); } finally { button.disabled = false; }
};

let activePackProfile = null;
let stagedPackResult = null;
function ensureCharacterPackDialog() {
  let dialog = $("character-pack-dialog");
  if (dialog) return dialog;
  dialog = document.createElement("dialog");
  dialog.id = "character-pack-dialog";
  dialog.style.maxWidth = "680px";
  dialog.style.width = "92vw";
  const form = document.createElement("form");
  form.id = "character-pack-form";
  const head = el("div", "", "dialog-head");
  const title = el("h2", UI.t("一键生成角色表情套图"));
  title.id = "character-pack-title";
  const closeBtn = el("button", "×");
  closeBtn.type = "button";
  closeBtn.onclick = () => dialog.close();
  head.append(title, closeBtn);

  const hint = el("p", UI.t("将自动生成 3×3 九宫格角色表情，经自适应切分并由视觉模型打标后绑定角色入库（VLM 不可用时自动回退预设标签）。"), "hint");
  const emotionsLabel = el("label");
  emotionsLabel.append(el("span", UI.t("9 格情绪（逗号分隔，留空使用默认 9 种常用情绪）")));
  const emotionsInput = document.createElement("input");
  emotionsInput.name = "emotions";
  emotionsInput.placeholder = "开心大笑,生气炸毛,委屈哭哭,震惊呆住,无语流汗,比心贴贴,疑惑问号,困倦摸鱼,点赞好耶";
  emotionsLabel.append(emotionsInput);

  const styleLabel = el("label");
  styleLabel.append(el("span", UI.t("套图附加画风（可选）")));
  const styleInput = document.createElement("input");
  styleInput.name = "style";
  styleInput.placeholder = "例如 Q 版粗描边贴纸、手绘涂鸦风";
  styleLabel.append(styleInput);

  const modeLabel = el("label", "", "check");
  const previewCheck = document.createElement("input");
  previewCheck.type = "checkbox";
  previewCheck.name = "preview_only";
  modeLabel.append(previewCheck, el("span", UI.t("先生成切片预览，人工确认后再入库（不勾选则一键直接入库）")));

  const statusBox = el("div", "", "hint");
  statusBox.id = "character-pack-status";
  const previewGrid = el("div", "", "gallery");
  previewGrid.id = "character-pack-preview";
  previewGrid.style.gridTemplateColumns = "repeat(auto-fill, minmax(120px, 1fr))";
  previewGrid.style.gap = "10px";
  previewGrid.style.marginTop = "10px";

  const footer = document.createElement("footer");
  const cancelBtn = el("button", UI.t("关闭"));
  cancelBtn.type = "button";
  cancelBtn.onclick = () => dialog.close();
  const commitBtn = el("button", UI.t("确认全部入库"), "primary");
  commitBtn.type = "button";
  commitBtn.id = "character-pack-commit";
  commitBtn.hidden = true;
  commitBtn.onclick = async () => {
    if (!stagedPackResult?.pack_id) return;
    commitBtn.disabled = true;
    statusBox.textContent = UI.t("正在将切片批量入库…");
    try {
      const committed = await post("media/character-pack/commit", {
        pack_id: stagedPackResult.pack_id,
        scope_mode: stagedPackResult.scope_mode || "public",
      });
      stagedPackResult = null;
      commitBtn.hidden = true;
      statusBox.textContent = UI.t("入库完成：成功 {0} 张，失败 {1} 张。", { 0: committed.imported_count || 0, 1: committed.failed_count || 0 });
      notice(statusBox.textContent);
      await refresh();
    } catch (error) {
      statusBox.textContent = UI.t("入库失败：{0}", { 0: error.message });
    } finally {
      commitBtn.disabled = false;
    }
  };
  const submitBtn = el("button", UI.t("开始生成套图"), "primary");
  submitBtn.type = "submit";
  footer.append(cancelBtn, commitBtn, submitBtn);

  form.append(head, hint, emotionsLabel, styleLabel, modeLabel, statusBox, previewGrid, footer);
  form.onsubmit = async (event) => {
    event.preventDefault();
    if (!activePackProfile) return;
    submitBtn.disabled = true;
    commitBtn.hidden = true;
    stagedPackResult = null;
    previewGrid.replaceChildren();
    statusBox.textContent = UI.t("正在生成 9 宫格套图并自动切分打标，请稍候…");
    try {
      const fd = new FormData(form);
      const res = await post("media/character-pack", {
        character: activePackProfile.key,
        emotions: String(fd.get("emotions") || "").trim(),
        style: String(fd.get("style") || "").trim(),
        preview_only: Boolean(previewCheck.checked),
        scope_mode: "public",
        umo: activeScope || "",
      });
      for (const item of res.items || []) {
        const tile = el("div", "", "image-card");
        tile.style.padding = "6px";
        if (item.preview_url) {
          const img = document.createElement("img");
          img.src = item.preview_url;
          img.alt = item.desc || item.emotion_label || "";
          img.style.width = "100%";
          img.style.aspectRatio = "1 / 1";
          img.style.objectFit = "contain";
          tile.append(img);
        }
        const meta = el("div", `${item.emotion_label || item.category}${item.imported ? " ✓" : item.error ? " ✗" : ""}`, "hint");
        meta.style.marginTop = "4px";
        tile.append(meta);
        previewGrid.append(tile);
      }
      if (res.preview_only) {
        stagedPackResult = res;
        commitBtn.hidden = false;
        statusBox.textContent = UI.t("已生成并切分 {0} 张切片，确认效果后点击「确认全部入库」。", { 0: res.total_slices || 0 });
      } else {
        statusBox.textContent = UI.t("一键套图完成：共切分 {0} 张，成功入库 {1} 张，失败 {2} 张。", { 0: res.total_slices || 0, 1: res.imported_count || 0, 2: res.failed_count || 0 });
        notice(statusBox.textContent);
        await refresh();
      }
    } catch (error) {
      statusBox.textContent = UI.t("套图生成失败：{0}", { 0: error.message });
      notice(statusBox.textContent);
    } finally {
      submitBtn.disabled = false;
    }
  };
  dialog.append(form);
  document.body.append(dialog);
  return dialog;
}

function openCharacterPackDialog(profile) {
  activePackProfile = profile;
  stagedPackResult = null;
  const dialog = ensureCharacterPackDialog();
  const title = $("character-pack-title");
  if (title) title.textContent = UI.t("一键生成角色表情套图 · {0}", { 0: profile.name || profile.key });
  const statusBox = $("character-pack-status");
  if (statusBox) statusBox.textContent = "";
  const previewGrid = $("character-pack-preview");
  if (previewGrid) previewGrid.replaceChildren();
  const commitBtn = $("character-pack-commit");
  if (commitBtn) commitBtn.hidden = true;
  dialog.showModal();
}

function renderEmptyLibraryOnboarding(data) {
  const existing = $("empty-library-onboarding");
  if (charactersPage || Number(data?.memes || 0) > 0) {
    if (existing) existing.remove();
    return;
  }
  const statsSection = document.querySelector(".stats");
  if (!statsSection) return;
  const card = existing || el("section", "", "vm-onboarding-card");
  card.id = "empty-library-onboarding";
  card.style.margin = "16px 0";
  card.style.padding = "18px 20px";
  card.style.borderRadius = "14px";
  card.style.border = "1px dashed var(--line, rgba(128,128,128,0.35))";
  card.style.background = "var(--surface, rgba(128,128,128,0.06))";
  card.replaceChildren();

  const header = el("div");
  header.append(
    el("h2", UI.t("表情库还是空的 · 选一种方式 1 分钟冷启动")),
    el("p", UI.t("从下面任选一种方式快速充实你的表情库，之后 Bot 就能在群聊中自然接梗斗图。"), "hint")
  );
  const grid = el("div");
  grid.style.display = "grid";
  grid.style.gridTemplateColumns = "repeat(auto-fit, minmax(200px, 1fr))";
  grid.style.gap = "12px";
  grid.style.marginTop = "12px";

  // Option 1: Import external pack
  const opt1 = el("article");
  opt1.style.padding = "12px";
  opt1.style.borderRadius = "10px";
  opt1.style.border = "1px solid var(--line, rgba(128,128,128,0.2))";
  opt1.append(el("h3", UI.t("① 导入外部表情包")), el("p", UI.t("支持 ZIP/文件夹批量导入，或接入外部开源表情仓库自动同步。"), "hint"));
  const importLink = el("a", UI.t("前往导入外部包 →"), "primary");
  importLink.href = "memes/index.html?open=sources";
  opt1.append(importLink);

  // Option 2: Enable auto-steal
  const opt2 = el("article");
  opt2.style.padding = "12px";
  opt2.style.borderRadius = "10px";
  opt2.style.border = "1px solid var(--line, rgba(128,128,128,0.2))";
  const stealOn = Boolean(data?.steal_enabled);
  opt2.append(el("h3", UI.t("② 开启自动偷图")), el("p", UI.t("自动收集群聊中大家爱发的表情进待审核池，或用 /meme 审图 一起众包筛选。"), "hint"));
  const stealBtn = el("button", stealOn ? UI.t("✓ 已开启自动偷图") : UI.t("一键开启自动偷图"));
  stealBtn.type = "button";
  stealBtn.disabled = stealOn;
  stealBtn.onclick = async () => {
    stealBtn.disabled = true;
    try {
      await post("media/quick-steal", { enabled: true });
      stealBtn.textContent = UI.t("✓ 已开启自动偷图");
      notice(UI.t("已开启自动偷图，群聊新图将自动进入待审核池。"));
    } catch (error) {
      stealBtn.disabled = false;
      notice(error.message);
    }
  };
  opt2.append(stealBtn);

  // Option 3: One-click character sticker pack
  const opt3 = el("article");
  opt3.style.padding = "12px";
  opt3.style.borderRadius = "10px";
  opt3.style.border = "1px solid var(--line, rgba(128,128,128,0.2))";
  opt3.append(el("h3", UI.t("③ 一键生成角色套图")), el("p", UI.t("根据角色档案生成 9 宫格 Q 版表情，自动切分并打标入库。"), "hint"));
  const packAction = el("button", profiles.length ? UI.t("生成首套角色表情") : UI.t("先创建角色档案"));
  packAction.type = "button";
  packAction.onclick = () => {
    if (profiles.length) openCharacterPackDialog(profiles[0]);
    else void editProfile();
  };
  opt3.append(packAction);

  grid.append(opt1, opt2, opt3);
  card.append(header, grid);
  if (!existing) statsSection.after(card);
}
$("save-form").onsubmit = async (event) => {
  event.preventDefault(); const form = event.currentTarget, button = form.querySelector('[type="submit"]'); button.disabled = true;
  try {
    const data = Object.fromEntries(new FormData(form)); data.image_index = Number(data.image_index); data.public = form.elements.public.checked;
    await post("media/save", data); $("save-dialog").close(); notice(UI.t("已收藏到表情库")); await refresh();
  } catch(error) { formError(form, error.message); notice(error.message); } finally { button.disabled = false; }
};
$("generate-form").onsubmit = async (event) => {
  event.preventDefault(); const form = event.currentTarget, button = form.querySelector('[type="submit"]'); button.disabled = true;
  try { const result = await post("media/generate", Object.fromEntries(new FormData(form))); $("generate-dialog").close(); notice(result.message); window.dispatchEvent(new CustomEvent("visual-task-submitted", {detail:result})); await refresh(); $("task-section").scrollIntoView?.({block:"start",behavior:"auto"}); }
  catch(error) { formError(form, error.message); notice(error.message); } finally { button.disabled = false; }
};
document.querySelectorAll("[data-close]").forEach((button) => { button.onclick = () => $(button.dataset.close).close(); });
$("new-profile").onclick = () => editProfile(); $("refresh").onclick = refresh;
function syncWorkshopScope(umo, { pushShared = false } = {}) {
  const next = String(umo ?? "").trim();
  if (pushShared) setSharedScope(next, { syncUrl: true, emit: true });
  if (next === activeScope) return;
  activeScope = next;
  if (activeScope) {
    notice(UI.t("当前作用域：{0}", { 0: formatScopeLabel(activeScope) || activeScope }));
  } else if (!$("notice").dataset.overviewError) {
    notice("");
  }
  void refresh();
}
window.addEventListener("dynamics-scopechange", (event) => {
  syncWorkshopScope(event.detail?.umo ?? getSharedScope());
});
window.VisualWorkshopScope = {
  get: () => activeScope,
  set: (umo) => syncWorkshopScope(umo, { pushShared: true }),
};
void refresh();
