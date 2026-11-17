const UI=window.SeriesUI||{t:(s,p={})=>String(s).replace(/\{(\w+)\}/g,(m,k)=>Object.hasOwn(p,k)?String(p[k]??''):m),msg(s,p={}){return this.t(s,p);},text(n,v){n.textContent=String(v??'');return n;},bind(n,s,p={},a=''){if(a)n.setAttribute(a,this.t(s,p));else n.textContent=this.t(s,p);return n;},onLocaleChange:()=>()=>{}};
const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginPage;
let profiles = [];
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
function renderProfiles() {
  const query = $("profile-search").value.trim().toLocaleLowerCase();
  const matches = profiles.filter(profile => `${profile.name} ${profile.key} ${profile.appearance}`.toLocaleLowerCase().includes(query));
  const visible = charactersPage || query ? matches : matches.slice(0, 3);
  $("profile-result-count").textContent = query ? `${matches.length} / ${profiles.length}` : String(profiles.length);
  $("profiles").replaceChildren();
  if (!visible.length) $("profiles").append(el("p", query ? UI.t("没有匹配的角色，试试其他关键词。") : UI.t("还没有角色形象。添加外观与画风，让之后的创作保持一致。"), "empty"));
  for (const profile of visible) {
    const card = el("article", "", "profile");
    const heading = el("div", "", "profile-heading");
    const identity = el("div");
    identity.append(el("h3", profile.name || profile.key), el("span", profile.key, "profile-key"));
    if(profile.references?.length) {
      const cover=el("button","","vm-cover-button");cover.type="button";cover.setAttribute("aria-label",UI.t("预览 {0} 的标准参考图",{0:profile.name}));
      const image=el("img","","vm-profile-cover");image.alt=profile.name;cover.append(image);cover.onclick=()=>window.VisualReferences.preview(profile.references[0],profile.name);
      window.VisualReferences.load(profile.references[0]).then(url=>{if(image.isConnected)image.src=url;}).catch(()=>{image.alt=UI.t("参考图不可用");});heading.append(cover,identity);
    } else heading.append(el("span", (profile.name || profile.key).slice(0, 1), "profile-avatar"), identity);
    const edit = el("button", UI.msg("编辑形象")); edit.onclick = () => editProfile(profile);
    const create = el("button", UI.msg("用角色创作")); create.onclick = () => { $("generate-form").reset(); formError($("generate-form")); $("generate-form").elements.character.value = profile.key; $("generate-dialog").showModal(); };
    const actions = el("div", "", "profile-actions"); actions.append(edit, create);
    card.append(heading, el("p", profile.appearance || UI.t("尚未填写外观")), el("span", UI.msg("{0} 张参考图",{0:profile.references.length}), "reference-badge"), actions);
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
  url.searchParams.set("umo", window.DynamicsGetActiveScope?.() || "");
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
    const data = await get("media/overview");
    profiles = Array.isArray(data.profiles) ? data.profiles : [];
    const generation = data.generation || {};
    $("meme-count").textContent = data.memes;
    $("profile-count").textContent = profiles.length;
    $("generation-state").textContent = generation.ready ? UI.t("已就绪") : UI.t("待配置");
    $("queue-count").textContent = `${generation.active_requests || 0} / ${generation.queued_requests || 0}`;
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
async function editProfile(profile = {}) {
  const form = $("profile-form"); form.reset();
  formError(form);
  for (const key of ["key", "name", "appearance", "style"]) form.elements[key].value = profile[key] || "";
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
    await post("media/profiles", data); $("profile-dialog").close(); notice(UI.t("角色形象已保存")); await refresh();
  } catch(error) { formError(form, error.message); notice(error.message); } finally { button.disabled = false; }
};
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
void refresh();
