/* DriveSync dashboard — vanilla JS front-end for the local API */
"use strict";

const $ = (sel) => document.querySelector(sel);
/* escape every server value before it goes into innerHTML: file names,
   folder names and log messages are user/Drive-controlled text */
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));
const api = async (path, opts = {}) => {
  const resp = await fetch(path, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      "X-DriveSync": "1",
      ...(opts.headers || {}),
    },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await resp.json().catch(() => ({ ok: false, error: "bad JSON" }));
  if (!resp.ok || data.ok === false) throw new Error(data.error || resp.statusText);
  return data;
};

function toast(msg, ms = 2600) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add("hidden"), ms);
}

function setMsg(id, text, cls) {
  const el = $(id);
  el.textContent = text;
  el.className = "msg " + (cls || "");
}

/* ---------- i18n (English keys, Spanish translations) ---------- */
/* Same gettext-lite scheme as the GTK app: the English string IS the key.
   Missing entries fall back to English instead of crashing; the dict holds
   HTML for the data-i18n-html labels (authored here, never user input). */
const ES = {
  "Dashboard": "Panel",
  "Sync pairs": "Pares de sincronización",
  "Accounts": "Cuentas",
  "Activity": "Actividad",
  "Settings": "Ajustes",
  "Sync now": "Sincronizar ahora",
  "Background worker": "Trabajo en segundo plano",
  "Recent activity": "Actividad reciente",
  "Add an account →": "Agregar una cuenta →",
  "Add a sync pair": "Agregar un par de sincronización",
  "Current pairs": "Pares actuales",
  "Connect a Google account": "Conectar una cuenta de Google",
  "Connected accounts": "Cuentas conectadas",
  "Activity log": "Registro de actividad",
  "About": "Acerca de",
  "Appearance & language": "Apariencia e idioma",
  "Saved in this browser; the desktop app keeps its own choices.":
    "Se guarda en este navegador; la app de escritorio guarda sus propias " +
    "elecciones.",
  "Google account": "Cuenta de Google",
  "pair.local": 'Local folder <span class="muted">(absolute path)</span>|' +
    'Carpeta local <span class="muted">(ruta absoluta)</span>',
  "Drive folder": "Carpeta de Drive",
  "Browse…": "Explorar…",
  "pair.scope": 'Sync scope <span class="muted">(optional — chosen before the first sync)</span>|' +
    'Alcance <span class="muted">(opcional — se elige antes de la primera sincronización)</span>',
  "Choose…": "Elegir…",
  "Direction": "Dirección",
  "Conflict policy": "Política de conflictos",
  "Two-way (bidirectional)": "Bidireccional (dos vías)",
  "Upload only (local → Drive)": "Solo subida (local → Drive)",
  "Download only (Drive → local)": "Solo descarga (Drive → local)",
  "Keep newer file": "Conservar el más reciente",
  "Keep local copy": "Conservar la copia local",
  "Keep remote copy": "Conservar la copia en Drive",
  "Keep both copies": "Conservar ambas copias",
  "Add pair": "Agregar par",
  "#": "#",
  "Local": "Local",
  "Conflicts": "Conflictos",
  "Deletes": "Borrados",
  "Scope": "Alcance",
  "State": "Estado",
  "Last sync": "Última sincronización",
  "Email": "Correo",
  "Storage": "Almacenamiento",
  "Client ID": "ID de cliente",
  "Client secret": "Secreto del cliente",
  "Connect account": "Conectar cuenta",
  "auth-hint": 'DriveSync uses <b>your own OAuth client</b> — nothing of yours flows through third-party servers. Create a “Desktop app” client in the <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noreferrer">Google Cloud Console</a> (Drive API enabled), then paste its credentials below. See the README for a 2-minute walkthrough.|' +
    'DriveSync usa <b>tu propio cliente OAuth</b> — nada tuyo pasa por servidores de terceros. Crea un cliente de tipo “Desktop app” en la <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noreferrer">consola de Google Cloud</a> (con la API de Drive habilitada) y pega sus credenciales abajo. El README tiene una guía de 2 minutos.',
  "All levels": "Todos los niveles",
  "Conflicts only": "Solo conflictos",
  "Errors only": "Solo errores",
  "Refresh": "Actualizar",
  "Sync interval (seconds)": "Intervalo de sincronización (segundos)",
  "Default conflict policy": "Política de conflictos por defecto",
  "set.delete": 'Delete safety limit <span class="muted">(max deletions per pair per pass; 0 = unlimited)</span>|' +
    'Límite de seguridad de borrados <span class="muted">(máximo de borrados por par por ciclo; 0 = sin límite)</span>',
  "set.concurrency": 'Parallel transfers <span class="muted">(uploads/downloads at once, 1–16)</span>|' +
    'Transferencias en paralelo <span class="muted">(subidas/descargas a la vez, 1–16)</span>',
  "set.history": 'Kept versions per file <span class="muted">(the previous local version is saved before a pull overwrites a file; 0 = off)</span>|' +
    'Versiones guardadas por archivo <span class="muted">(la versión local anterior se guarda antes de que una descarga la sobrescriba; 0 = apagado)</span>',
  "set.bandwidth": 'Bandwidth limit <span class="muted">(KB/s shared by uploads + downloads; 0 = unlimited)</span>|' +
    'Límite de ancho de banda <span class="muted">(KB/s compartidos por subidas + descargas; 0 = sin límite)</span>',
  "set.from": 'Sync window from <span class="muted">(HH:MM; empty = always)</span>|' +
    'Ventana de sincronización desde <span class="muted">(HH:MM; vacío = siempre)</span>',
  "set.to": 'Sync window to <span class="muted">(may cross midnight)</span>|' +
    'Ventana de sincronización hasta <span class="muted">(puede cruzar la medianoche)</span>',
  "set.watch": 'Sync local changes instantly <span class="muted">(needs watchdog)</span>|' +
    'Sincronizar cambios locales al instante <span class="muted">(necesita watchdog)</span>',
  "set.incremental": 'Use the Drive changes feed <span class="muted">(incremental scan; off = full relist every pass)</span>|' +
    'Usar el feed de cambios de Drive <span class="muted">(escaneo incremental; apagado = relistar todo cada ciclo)</span>',
  "set.convert": 'Convert Google Docs/Sheets/Slides to Office files <span class="muted">(Docs → .docx, Sheets → .xlsx, Slides → .pptx, Drawings → .png)</span>|' +
    'Convertir Docs/Sheets/Slides de Google a archivos Office <span class="muted">(Docs → .docx, Sheets → .xlsx, Slides → .pptx, Drawings → .png)</span>',
  "set.ignores": 'Ignore patterns <span class="muted">(one per line, glob style)</span>|' +
    'Patrones a ignorar <span class="muted">(uno por línea, estilo glob)</span>',
  "Save settings": "Guardar ajustes",
  "Choose a Drive folder": "Elegir una carpeta de Drive",
  "↑ Up": "↑ Subir",
  "Use this folder": "Usar esta carpeta",
  "Search all of Drive by folder name…": "Buscar carpetas en todo Drive por nombre…",
  "loading…": "cargando…",
  "Choose subfolders": "Elegir subcarpetas",
  "scope-hint": 'Files directly in the folder always sync. Unchosen subfolders stay on Drive and on disk untouched.|' +
    'Los archivos sueltos en la carpeta siempre se sincronizan. Las subcarpetas no elegidas se quedan en Drive y en el disco sin tocarse.',
  "Select all": "Elegir todas",
  "Select none": "Quitar todas",
  "Save": "Guardar",
  "Edit pair": "Editar par",
  "Symlinks": "Enlaces simbólicos",
  "Skip — never follow links (safest)": "Saltar — nunca seguir enlaces (lo más seguro)",
  "Follow — sync what they point at": "Seguir — sincronizar a donde apuntan",
  "edit.deletes": 'Allow deletions <span class="muted">(files go to trash, never vanish)</span>|' +
    'Permitir borrados <span class="muted">(los archivos van a la papelera, nunca desaparecen)</span>',
  "edit.casefold": 'Case-insensitive paths <span class="muted">(for pairs on FAT/exFAT/NTFS folders)</span>|' +
    'Rutas sin distinguir mayúsculas <span class="muted">(para pares en carpetas FAT/exFAT/NTFS)</span>',
  "Conflict copies": "Copias en conflicto",
  "conflicts-hint": 'Files kept as “<i>name.conflict-…</i>” copies by the keep-both policy. Pick which side to keep — the copy wins at the original path, the original stays and the copy is discarded, or leave both.|' +
    'Archivos guardados como copias “<i>name.conflict-…</i>” por la política de conservar ambos. Elige qué lado conservar: la copia gana en la ruta original, el original se queda y la copia se descarta, o deja ambos.',
  "Activity detail": "Detalle de actividad",
  "Nothing is applied — this only shows what the next pass would do.":
    "No se aplica nada — esto solo muestra lo que haría el próximo ciclo.",
  // dynamic strings rendered by app.js
  "syncing": "sincronizando",
  "paused": "en pausa",
  "idle": "inactivo",
  "enabled": "activo",
  "Interval": "Intervalo",
  "Last run": "Última ejecución",
  "Next run": "Próxima ejecución",
  "Last result": "Último resultado",
  "Last error": "Último error",
  "sign-in expired": "sesión expirada",
  "{used} / {limit} used": "{used} / {limit} usados",
  "{used} used": "{used} usados",
  "No Google account connected yet.": "Aún no hay cuenta de Google conectada.",
  "No sync pairs yet — add one in the \"Sync pairs\" tab.":
    "Aún no hay pares — agrega uno en la pestaña \"Pares de sincronización\".",
  "No sync pairs yet.": "Aún no hay pares de sincronización.",
  "No accounts connected.": "No hay cuentas conectadas.",
  "Sync": "Sincronizar",
  "On": "Sí",
  "Off": "No",
  "Deletions are allowed (files go to trash)": "Los borrados están permitidos (los archivos van a la papelera)",
  "Deletions blocked — files are kept on both sides": "Borrados bloqueados — los archivos se conservan en ambos lados",
  "everything": "todo",
  "{n} subfolder(s)": "{n} subcarpeta(s)",
  "Pause": "Pausar",
  "Resume": "Reanudar",
  "Edit…": "Editar…",
  "Remove": "Quitar",
  "Preview": "Vista previa",
  "Until tomorrow": "Hasta mañana",
  "Cancel pause": "Cancelar pausa",
  "never": "nunca",
  "{sec}s ago": "hace {sec}s",
  "{h}h{m}m ago": "hace {h}h{m}m",
  "⚠ ERROR {ago}": "⚠ ERROR {ago}",
  "{n} change(s)": "{n} cambio(s)",
  "up to date": "al día",
  ", {n} conflict(s)": ", {n} conflicto(s)",
  "ok {ago} — {detail}": "bien {ago} — {detail}",
  "paused until {when}": "en pausa hasta {when}",
  "Pause until tomorrow 09:00 (click again to cancel)":
    "Pausar hasta mañana 09:00 (pulsa otra vez para cancelar)",
  "Pause cancelled — pair active again": "Pausa cancelada — el par vuelve a estar activo",
  "Paused until tomorrow 09:00": "En pausa hasta mañana 09:00",
  "Remove this account and all of its sync pairs?":
    "¿Quitar esta cuenta y todos sus pares de sincronización?",
  "Account removed": "Cuenta quitada",
  "Remove pair #{pid}? Local files and Drive contents are kept.":
    "¿Quitar el par #{pid}? Los archivos locales y lo que hay en Drive se conservan.",
  "A Google window should open in your browser; approve access there…":
    "Una ventana de Google debería abrirse en tu navegador; aprueba el acceso ahí…",
  "Connected {email} 🎉": "Conectada {email} 🎉",
  "Pair #{pid} updated": "Par #{pid} actualizado",
  "Sync started for pair #{pid}": "Sincronización iniciada para el par #{pid}",
  "Sync started for all pairs": "Sincronización iniciada para todos los pares",
  "Could not start sync: {err}": "No se pudo iniciar la sincronización: {err}",
  "Pick a Drive folder first.": "Elige primero una carpeta de Drive.",
  "Pair added — first sync starts in the background.":
    "Par agregado — la primera sincronización comienza en segundo plano.",
  " Only {n} chosen subfolder(s) will sync.":
    " Solo se sincronizarán {n} subcarpeta(s) elegidas.",
  "Everything in this folder": "Todo en esta carpeta",
  "Edit": "Editar",
  "Both sides match, but {n} deletion(s) remain blocked by the safety limit.":
    "Ambos lados coinciden, pero {n} borrado(s) siguen bloqueados por el límite de seguridad.",
  "Deletions allowed again": "Borrados permitidos otra vez",
  "Deletions blocked — files will be kept":
    "Borrados bloqueados — los archivos se conservan",
  "Subfolders": "Subcarpetas",
  "pair": "par",
  "{u} pushed · {q} pulled · {d} deleted · {c} conflicts":
    "{u} subidos · {q} bajados · {d} borrados · {c} conflictos",
  "Keep copy": "Quedarme con la copia",
  "Keep original": "Quedarme con el original",
  "Keep both": "Conservar ambos",
  "original: {path} · {size}": "original: {path} · {size}",
  " · <b>missing locally</b>": " · <b>falta en el disco</b>",
  "No conflict copies — everything is resolved. 🎉":
    "No hay copias en conflicto — todo está resuelto. 🎉",
  "Kept the copy as {path}": "La copia reemplazó a {path}",
  "Discarded the copy {path}": "Copia descartada: {path}",
  "Left as is — both copies stay in place":
    "Sin cambios — ambas copias se quedan donde están",
  "Could not resolve: {err}": "No se pudo resolver: {err}",
  "Could not save scope: {err}": "No se pudo guardar el alcance: {err}",
  "Only {n} subfolder(s) will sync": "Solo se sincronizarán {n} subcarpeta(s)",
  "Scope reset — everything syncs": "Alcance restablecido — se sincroniza todo",
  "No folders shared with this account yet.": "Aún no hay carpetas compartidas con esta cuenta.",
  "No subfolders.": "No hay subcarpetas.",
  "open ›": "abrir ›",
  "select": "elegir",
  "✅ Use “{name}”": "✅ Usar “{name}”",
  "(no subfolders)": "(sin subcarpetas)",
  "Search: “{q}”": "Búsqueda: “{q}”",
  "searching…": "buscando…",
  "No folder on this Drive matches the search.":
    "Ninguna carpeta de este Drive coincide con la búsqueda.",
  "Search path, action or detail…": "Buscar ruta, acción o detalle…",
  "No entries match the search.": "Ninguna entrada coincide con la búsqueda.",
  "Nothing logged yet.": "Aún no hay nada en el registro.",
  "Click for detail": "Clic para ver el detalle",
  "Action: {action}": "Acción: {action}",
  "Path: {path}": "Ruta: {path}",
  "Pair: #{pid}": "Par: #{pid}",
  "No detail.": "Sin detalle.",
  "failed to load events: {err}": "no se pudo cargar el registro: {err}",
  "status unavailable: {err}": "estado no disponible: {err}",
  "Saved.": "Guardado.",
  "Settings saved": "Ajustes guardados",
  "— local dashboard port {port}. Data lives in {db}.": "— panel local en el puerto {port}. Los datos viven en {db}.",
  "Theme": "Tema",
  "System": "Según el sistema",
  "Light": "Claro",
  "Dark": "Oscuro",
  "Liquid glass light": "Cristal líquido claro",
  "Liquid glass dark": "Cristal líquido oscuro",
  "Graphite light": "Grafito claro",
  "Graphite dark": "Grafito oscuro",
  "Base theme": "Tema base",
  "Style variants": "Variantes de estilo",
  "Button & highlight color": "Color de botones y resaltado",
  "Language": "Idioma",
  "Scanning…": "Analizando…",
  "Could not scan — {err}": "No se pudo analizar — {err}",
  "Nothing to sync — both sides already match.":
    "Nada que sincronizar — ambos lados ya coinciden.",
  "{u} upload(s), {d} download(s), {x} deletion(s), {r} rename(s)":
    "{u} subida(s), {d} descarga(s), {x} borrado(s), {r} renombramiento(s)",
  "({n} deletion(s) blocked by the safety limit)":
    "({n} borrado(s) bloqueados por el límite de seguridad)",
  "Preview — pair #{pid}": "Vista previa — par #{pid}",
  "Upload": "Subir",
  "Download": "Descargar",
  "Delete local": "Borrar local",
  "Delete on Drive": "Borrar en Drive",
  "Remove local folder": "Quitar carpeta local",
  "Remove Drive folder": "Quitar carpeta de Drive",
  "Create local folder": "Crear carpeta local",
  "Create Drive folder": "Crear carpeta en Drive",
  "Rename local": "Renombrar local",
  "Rename on Drive": "Renombrar en Drive",
  "Reconcile": "Conciliar",
  "Forget": "Olvidar",
  "Record Google doc": "Registrar documento de Google",
  "Skip (Google doc)": "Omitir (documento de Google)",
  "Ack local deletion": "Confirmar borrado local",
  "Ack Drive deletion": "Confirmar borrado en Drive",
  "Action": "Acción",
  "Path": "Ruta",
  "Detail": "Detalle",
};

const LANG_NAMES = { en: "English", es: "Español" };
let _lang = (localStorage.getItem("ds_lang")
  || ((navigator.language || "en").toLowerCase().startsWith("es") ? "es" : "en"));

function t(s) {
  if (_lang === "en") return s;
  const v = ES[s];
  return (v !== undefined && v.includes("|") ? v.split("|")[1] : v) || s;
}
/* tiny template: T("ok {ago} — {detail}", {ago: "5s", detail: "…"}) */
function T(s, obj = {}) {
  return t(s).replace(/\{(\w+)\}/g, (m, k) => (obj[k] !== undefined ? obj[k] : m));
}

/* ---------- appearance: theme + accent (per browser, localStorage) ---------- */
/* Base themes first, then the decorative styles; each style has an explicit
   light and dark variant. */
const THEMES = ["system", "light", "dark",
  "glass-light", "glass-dark", "graphite-light", "graphite-dark"];
const THEME_GROUPS = [
  { label: "Base theme", keys: ["system", "light", "dark"] },
  { label: "Style variants",
    keys: ["glass-light", "glass-dark", "graphite-light", "graphite-dark"] },
];
const THEME_LABELS = {
  system: "System", light: "Light", dark: "Dark",
  "glass-light": "Liquid glass light", "glass-dark": "Liquid glass dark",
  "graphite-light": "Graphite light", "graphite-dark": "Graphite dark",
};
/* accent palettes: [filled-button bg, pressed bg] per light/dark render */
const ACCENTS = {
  blue:   { light: ["#0b57d0", "#0842a0"], dark: ["#6ea8ff", "#3b82f6"] },
  teal:   { light: ["#00796b", "#005c50"], dark: ["#4dd0c0", "#26a69a"] },
  green:  { light: ["#188038", "#106328"], dark: ["#66d19e", "#34a06a"] },
  purple: { light: ["#7b1fa2", "#5e1780"], dark: ["#ce93d8", "#ab47bc"] },
  pink:   { light: ["#c2185b", "#95134a"], dark: ["#f48fb1", "#ec407a"] },
  orange: { light: ["#e65100", "#a83a00"], dark: ["#ffb74d", "#fb8c00"] },
  red:    { light: ["#b3261e", "#871a15"], dark: ["#ef8a80", "#e05548"] },
};
let _theme = localStorage.getItem("ds_theme") || "system";
/* configs saved before the light/dark split used dark-only names */
if (_theme === "glass") _theme = "glass-dark";
if (_theme === "graphite") _theme = "graphite-dark";
if (!THEMES.includes(_theme)) _theme = "system";
let _accent = localStorage.getItem("ds_accent") || "blue";

const _darkMQ = window.matchMedia("(prefers-color-scheme: dark)");
function themeIsDark() {
  return _theme === "dark" || _theme.endsWith("-dark")
    || (_theme === "system" && _darkMQ.matches);
}

function applyTheme() {
  document.body.dataset.theme = _theme;
  applyAccent();                     // accent shades differ per light/dark
}

function applyAccent() {
  const variant = ACCENTS[_accent] || ACCENTS.blue;
  const [a, press] = themeIsDark() ? variant.dark : variant.light;
  document.body.style.setProperty("--accent", a);
  document.body.style.setProperty("--accent-press", press);
}
_darkMQ.addEventListener?.("change", applyAccent);

function renderAppearance() {
  const themes = $("#themeBtns");
  if (themes) {
    themes.innerHTML = "";
    for (const group of THEME_GROUPS) {
      const wrap = document.createElement("div");
      wrap.className = "theme-group";
      const lbl = document.createElement("span");
      lbl.className = "lbl";
      lbl.textContent = t(group.label);
      wrap.appendChild(lbl);
      for (const th of group.keys) {
        const b = document.createElement("button");
        b.className = "theme-chip" + (th === _theme ? " active" : "");
        b.textContent = t(THEME_LABELS[th]);
        b.addEventListener("click", () => {
          _theme = th;
          localStorage.setItem("ds_theme", th);
          applyTheme();
          renderAppearance();
        });
        wrap.appendChild(b);
      }
      themes.appendChild(wrap);
    }
  }
  const accents = $("#accentBtns");
  if (accents) {
    accents.innerHTML = "";
    for (const [name, pal] of Object.entries(ACCENTS)) {
      const b = document.createElement("button");
      const variant = themeIsDark() ? pal.dark : pal.light;
      b.className = "swatch" + (name === _accent ? " active" : "");
      b.style.background = variant[0];
      b.title = name;
      b.setAttribute("aria-label", name);
      b.addEventListener("click", () => {
        _accent = name;
        localStorage.setItem("ds_accent", name);
        applyAccent();
        renderAppearance();
      });
      accents.appendChild(b);
    }
  }
  const lang = $("#langBtn");
  if (lang) {
    const other = _lang === "en" ? "es" : "en";
    lang.textContent = "\uD83C\uDF10 " + LANG_NAMES[other];
    lang.title = "Language / Idioma";
  }
}

function applyLang() {
  document.documentElement.lang = _lang;
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-html]").forEach((el) => {
    const v = ES[el.dataset.i18nHtml];
    if (v !== undefined && v.includes("|")) {
      el.innerHTML = _lang === "en" ? v.split("|")[0] : v.split("|")[1];
    }
  });
  document.querySelectorAll("[data-i18n-ph]").forEach((el) => {
    el.placeholder = t(el.dataset.i18nPh);
  });
  renderAppearance();
}

$("#langBtn")?.addEventListener?.("click", () => {
  _lang = _lang === "en" ? "es" : "en";
  localStorage.setItem("ds_lang", _lang);
  applyLang();
  loadTab(state.tab);            // re-render the dynamic parts in Spanish
});

/* ---------- tabs ---------- */
document.querySelectorAll("#tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    btn.classList.add("active");
    $("#tab-" + btn.dataset.tab).classList.add("active");
    state.tab = btn.dataset.tab;
    loadTab(btn.dataset.tab);
  });
});
document.querySelectorAll("[data-goto]").forEach((a) =>
  a.addEventListener("click", (e) => {
    e.preventDefault();
    document.querySelector(`#tabs button[data-tab="${a.dataset.goto}"]`).click();
  }));

function loadTab(name) {
  if (name === "dashboard") loadDashboard();
  if (name === "pairs") loadPairs();
  if (name === "accounts") loadAccounts();
  if (name === "activity") loadEvents();
  if (name === "settings") loadSettings();
}

/* ---------- state ---------- */
let state = { accounts: [], pairs: [], events: [], newScope: [], editId: null,
  conflictPair: null, tab: "dashboard",
  picker: { stack: [], current: { id: "root", name: "My Drive" } },
  scope: { accountId: null, remoteId: null, selected: [], target: "new" } };

/* ---------- dashboard ---------- */
async function loadDashboard() {
  try {
    const st = await api("/api/status");
    $("#version").textContent = "v" + st.version;
    const w = st.worker || {};
    const running = w.running ? `<span class="pill on">${esc(t("syncing"))}</span>`
      : (w.paused ? `<span class="pill off">${esc(t("paused"))}</span>`
         : `<span class="pill off">${esc(t("idle"))}</span>`);
    const last = w.last_run ? new Date(w.last_run * 1000).toLocaleString() : "—";
    const next = (!w.paused && w.next_run)
      ? new Date(w.next_run * 1000).toLocaleTimeString() : "—";
    const ls = w.last_stats ? T("{u} pushed · {q} pulled · {d} deleted · {c} conflicts",
      { u: w.last_stats.pushed, q: w.last_stats.pulled,
        d: w.last_stats.deleted, c: w.last_stats.conflicts }) : "—";
    $("#workerStatus").innerHTML =
      `<b>${esc(t("State"))}</b><span>${running}</span>
       <b>${esc(t("Interval"))}</b><span>${st.interval}s</span>
       <b>${esc(t("Last run"))}</b><span>${last}</span>
       <b>${esc(t("Next run"))}</b><span>${next}</span>
       <b>${esc(t("Last result"))}</b><span>${esc(ls)}</span>` +
      (w.last_error ? `<b>${esc(t("Last error"))}</b><span class="msg err">${esc(w.last_error)}</span>` : "");
  } catch (e) { $("#workerStatus").textContent = T("status unavailable: {err}", { err: e.message }); }

  try {
    const { accounts } = await api("/api/accounts");
    state.accounts = accounts;
    $("#accountCards").innerHTML = accounts.length
      ? accounts.map((a) => {
          const q = a.quota || {};
          const used = humanBytes(+q.usage || 0), limit = +q.limit || 0;
          const storage = limit ? T("{used} / {limit} used", { used, limit: humanBytes(limit) })
            : (used !== "—" ? T("{used} used", { used }) : "");
          const extra = a.error ? ` · ${t("sign-in expired")}`
            : storage ? ` · ${esc(storage)}` : "";
          return `<div class="acct"><span>📮 ${esc(a.email)}</span>
          <span class="muted small">${esc(a.name || "")}${extra}</span></div>`;
        }).join("")
      : `<p class="muted small">${esc(t("No Google account connected yet."))}</p>`;
  } catch (e) { /* ignore */ }

  try {
    const { pairs } = await api("/api/pairs");
    state.pairs = pairs;
    $("#pairsSummary").innerHTML = pairs.length
      ? `<table class="tbl"><tbody>${pairs.map((p) => `<tr>
          <td>#${p.id}</td><td>${esc(p.local_path)}</td>
          <td>${esc(p.remote_path || p.remote_id)}</td>
          <td>${esc(p.direction)}</td><td>${esc(stateText(p))}</td>
          <td class="muted small">${esc(lastSyncText(p))}</td>
          <td><button class="btn ghost small" data-sync="${p.id}">${esc(t("Sync"))}</button>
              <button class="btn ghost small" data-conflicts="${p.id}">${esc(t("Conflicts"))}</button></td>
        </tr>`).join("")}</tbody></table>`
      : `<p class="muted small">${esc(t("No sync pairs yet — add one in the \"Sync pairs\" tab."))}</p>`;
    bindSyncButtons();
    document.querySelectorAll("[data-conflicts]").forEach((b) =>
      b.addEventListener("click", () => openConflicts(+b.dataset.conflicts)));
  } catch (e) { /* ignore */ }

  await renderEvents($("#recentEvents"), 12);
}

function bindSyncButtons() {
  document.querySelectorAll("[data-sync]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      await api(`/api/pairs/${btn.dataset.sync}/sync`, { method: "POST" });
      toast(T("Sync started for pair #{pid}", { pid: btn.dataset.sync }));
    }));
}

/* ---------- accounts ---------- */
async function loadAccounts() {
  try {
    const { accounts } = await api("/api/accounts");
    state.accounts = accounts;
    $("#accountsTable tbody").innerHTML = accounts.length
      ? accounts.map((a) => {
          const q = a.quota || {};
          const used = humanBytes(+q.usage || 0), limit = +q.limit || 0;
          const storage = limit ? `${used} / ${humanBytes(limit)}` : used || "—";
          return `<tr><td>${a.id}</td><td>${esc(a.email)}</td><td>${storage}</td>
            <td><button class="btn danger small" data-del-acct="${a.id}">${esc(t("Remove"))}</button></td></tr>`;
        }).join("")
      : `<tr><td colspan="4" class="muted">${esc(t("No accounts connected."))}</td></tr>`;
    document.querySelectorAll("[data-del-acct]").forEach((b) =>
      b.addEventListener("click", async () => {
        if (!confirm(t("Remove this account and all of its sync pairs?"))) return;
        await api(`/api/accounts/${b.dataset.delAcct}`, { method: "DELETE" });
        toast(t("Account removed"));
        loadAccounts();
      }));
    const sel = $("#pairAccount");
    const prev = sel.value;
    sel.innerHTML = accounts.map((a) => `<option value="${esc(a.id)}">${esc(a.email)}</option>`).join("");
    // keep the user's choice across refreshes instead of snapping to #1
    if (prev && accounts.some((a) => String(a.id) === prev)) sel.value = prev;
  } catch (e) { /* ignore */ }
}

$("#authForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  setMsg("#authMsg", t("A Google window should open in your browser; approve access there…"), "");
  $("#authBtn").disabled = true;
  try {
    const { account } = await api("/api/accounts", {
      method: "POST",
      body: { client_id: $("#clientId").value.trim(), client_secret: $("#clientSecret").value.trim() },
    });
    setMsg("#authMsg", T("Connected {email} 🎉", { email: account.email }), "ok");
    loadAccounts();
  } catch (err) {
    setMsg("#authMsg", err.message, "err");
  } finally {
    $("#authBtn").disabled = false;
  }
});

/* ---------- pairs ---------- */
function lastSyncText(p) {
  const ls = p.last_sync;
  if (!ls || !ls.ts) return t("never");
  const age = Math.max(0, Math.floor(Date.now() / 1000 - ls.ts));
  const ago = age < 3600 ? T("{sec}s ago", { sec: age })
    : T("{h}h{m}m ago", { h: Math.floor(age / 3600), m: Math.floor((age % 3600) / 60) });
  if (!ls.ok) return T("⚠ ERROR {ago}", { ago });
  const s = ls.stats || {};
  const moved = (+s.pushed || 0) + (+s.pulled || 0) + (+s.deleted || 0) + (+s.renamed || 0);
  let detail = moved ? T("{n} change(s)", { n: moved }) : t("up to date");
  if (+s.conflicts) detail += T(", {n} conflict(s)", { n: s.conflicts });
  return T("ok {ago} — {detail}", { ago, detail });
}

function stateText(p) {
  if (!p.enabled) return t("paused");
  if (p.paused_until) {
    const until = new Date(p.paused_until);
    if (until > new Date()) return T("paused until {when}", { when: until.toLocaleString() });
  }
  return t("enabled");
}

/* "pause until tomorrow 09:00" — computed here, stored as UTC ISO */
function untilTomorrow() {
  const d = new Date();
  d.setDate(d.getDate() + 1);
  d.setHours(9, 0, 0, 0);
  return d.toISOString();
}

async function loadPairs() {
  await loadAccounts();
  try {
    const { pairs } = await api("/api/pairs");
    state.pairs = pairs;
    $("#pairsTable tbody").innerHTML = pairs.length
      ? pairs.map((p) => {
        const scopeTxt = (p.include_patterns && p.include_patterns.length)
          ? T("{n} subfolder(s)", { n: p.include_patterns.length }) : t("everything");
        const untilActive = p.paused_until && new Date(p.paused_until) > new Date();
        return `<tr>
          <td>#${p.id}</td><td>${esc(p.local_path)}</td>
          <td>${esc(p.remote_path || p.remote_id)}</td><td>${esc(p.direction)}</td>
          <td>${esc(p.conflict_policy)}</td>
          <td>${p.allow_deletes
            ? `<button class="btn ghost small" data-nodel="${p.id}" title="${esc(t("Deletions are allowed (files go to trash)"))}">${esc(t("On"))}</button>`
            : `<button class="btn ghost small" data-yodel="${p.id}" title="${esc(t("Deletions blocked — files are kept on both sides"))}">${esc(t("Off"))}</button>`}</td>
          <td>${esc(scopeTxt)}
            <button class="btn ghost small" data-scope="${p.id}" data-account="${p.account_id}" data-remote="${esc(p.remote_id)}">${esc(t("Edit"))}</button></td>
          <td>${esc(stateText(p))}
            <button class="btn ghost small" data-until="${p.id}" title="${esc(t("Pause until tomorrow 09:00 (click again to cancel)"))}">${esc(untilActive ? t("Cancel pause") : t("Until tomorrow"))}</button>
            ${p.enabled
              ? `<button class="btn ghost small" data-pause="${p.id}">${esc(t("Pause"))}</button>`
              : `<button class="btn ghost small" data-resume="${p.id}">${esc(t("Resume"))}</button>`}</td>
          <td class="muted small">${esc(lastSyncText(p))}</td>
          <td>
            <button class="btn ghost small" data-edit="${p.id}">${esc(t("Edit…"))}</button>
            <button class="btn ghost small" data-preview="${p.id}">${esc(t("Preview"))}</button>
            <button class="btn ghost small" data-conflicts="${p.id}">${esc(t("Conflicts"))}</button>
            <button class="btn ghost small" data-sync="${p.id}">${esc(t("Sync"))}</button>
            <button class="btn danger small" data-del="${p.id}">${esc(t("Remove"))}</button>
          </td></tr>`;
      }).join("")
      : `<tr><td colspan="10" class="muted">${esc(t("No sync pairs yet."))}</td></tr>`;

    document.querySelectorAll("[data-nodel],[data-yodel]").forEach((b) =>
      b.addEventListener("click", async () => {
        const on = !!b.dataset.yodel;  // currently off -> turning on
        await api(`/api/pairs/${b.dataset.nodel || b.dataset.yodel}`,
                  { method: "PATCH", body: { allow_deletes: on ? 1 : 0 } });
        toast(on ? t("Deletions allowed again") : t("Deletions blocked — files will be kept"));
        loadPairs();
      }));
    document.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
      if (!confirm(T("Remove pair #{pid}? Local files and Drive contents are kept.", { pid: b.dataset.del }))) return;
      await api(`/api/pairs/${b.dataset.del}`, { method: "DELETE" });
      loadPairs();
    }));
    document.querySelectorAll("[data-pause],[data-resume]").forEach((b) =>
      b.addEventListener("click", async () => {
        const id = b.dataset.pause || b.dataset.resume;
        await api(`/api/pairs/${id}`, { method: "PATCH", body: { enabled: b.dataset.resume ? 1 : 0 } });
        loadPairs();
      }));
    document.querySelectorAll("[data-until]").forEach((b) =>
      b.addEventListener("click", async () => {
        const id = b.dataset.until;
        const pair = state.pairs.find((p) => p.id === id);
        const active = pair && pair.paused_until
          && new Date(pair.paused_until) > new Date();
        await api(`/api/pairs/${id}`, { method: "PATCH",
          body: { paused_until: active ? null : untilTomorrow() } });
        toast(active ? t("Pause cancelled — pair active again")
                     : t("Paused until tomorrow 09:00"));
        loadPairs();
      }));
    document.querySelectorAll("[data-edit]").forEach((b) =>
      b.addEventListener("click", () => openEdit(+b.dataset.edit)));
    document.querySelectorAll("[data-preview]").forEach((b) =>
      b.addEventListener("click", () => openPreview(+b.dataset.preview)));
    document.querySelectorAll("[data-conflicts]").forEach((b) =>
      b.addEventListener("click", () => openConflicts(+b.dataset.conflicts)));
    document.querySelectorAll("[data-scope]").forEach((b) =>
      b.addEventListener("click", () => {
        const pair = state.pairs.find((p) => p.id === +b.dataset.scope);
        openScope(+b.dataset.account, b.dataset.remote,
                  (pair && pair.include_patterns) || [], b.dataset.scope);
      }));
    bindSyncButtons();
  } catch (e) { /* ignore */ }
}

/* ---------- sync preview (dry-run) ---------- */
const OP_LABELS = {
  upload: "Upload", download: "Download",
  delete_local: "Delete local", delete_remote: "Delete on Drive",
  rmdir_local: "Remove local folder", rmdir_remote: "Remove Drive folder",
  mkdir_local: "Create local folder", mkdir_remote: "Create Drive folder",
  rename_local: "Rename local", rename_remote: "Rename on Drive",
  reconcile: "Reconcile", forget: "Forget",
  record_gdoc: "Record Google doc", skip_gdoc_collision: "Skip (Google doc)",
  ack_local_delete: "Ack local deletion", ack_remote_delete: "Ack Drive deletion",
};

async function openPreview(pairId) {
  $("#previewTitle").textContent = T("Preview — pair #{pid}", { pid: pairId });
  $("#previewSummary").textContent = t("Scanning…");
  $("#previewList").innerHTML = `<p class="muted">${esc(t("Scanning…"))}</p>`;
  $("#previewBackdrop").classList.remove("hidden");
  try {
    const res = await api(`/api/pairs/${pairId}/preview`);
    if (res.error) {
      $("#previewSummary").textContent = T("Could not scan — {err}", { err: res.error });
      $("#previewList").innerHTML = "";
      return;
    }
    const tt = res.totals || {};
    let summary = T("{u} upload(s), {d} download(s), {x} deletion(s), {r} rename(s)",
      { u: tt.uploads || 0, d: tt.downloads || 0, x: tt.deletions || 0,
        r: tt.renames || 0 });
    if (res.blocked) summary += " " + T("({n} deletion(s) blocked by the safety limit)", { n: res.blocked });
    if (!(res.ops || []).length) {
      summary = res.blocked
        ? T("Both sides match, but {n} deletion(s) remain blocked by the safety limit.", { n: res.blocked })
        : t("Nothing to sync — both sides already match.");
    }
    $("#previewSummary").textContent = summary;
    $("#previewList").innerHTML = (res.ops || []).length
      ? res.ops.map((op) => `<div class="picker-item">
          <span>${op[0] in OP_LABELS && (op[0].includes("delete") || op[0].includes("rmdir"))
            ? "⚠ " : ""}<b>${esc(t(OP_LABELS[op[0]] || op[0]))}</b>
            <span class="muted small"> ${esc(op[1] || "")}</span></span>
          <span class="muted small">${esc(op[2] || "")}</span></div>`).join("")
      : `<p class="muted">${esc(t("Nothing to sync — both sides already match."))}</p>`;
  } catch (e) {
    $("#previewSummary").textContent = T("Could not scan — {err}", { err: e.message });
    $("#previewList").innerHTML = "";
  }
}
$("#previewClose").addEventListener("click", () => $("#previewBackdrop").classList.add("hidden"));
$("#previewBackdrop").addEventListener("click", (e) => {
  if (e.target === $("#previewBackdrop")) $("#previewBackdrop").classList.add("hidden");
});

/* ---------- pair editing ---------- */
function openEdit(pairId) {
  const pair = state.pairs.find((p) => p.id === pairId);
  if (!pair) return;
  state.editId = pairId;
  $("#editTitle").textContent = `${t("Edit pair")} #${pairId} — ${pair.local_path}`;
  $("#editDirection").value = pair.direction;
  $("#editPolicy").value = pair.conflict_policy;
  $("#editDeletes").checked = !!pair.allow_deletes;
  $("#editSymlinks").value = pair.symlink_policy || "skip";
  $("#editCaseFold").checked = !!pair.case_insensitive;
  setMsg("#editMsg", "", "");
  $("#editBackdrop").classList.remove("hidden");
}
$("#editClose").addEventListener("click", () => $("#editBackdrop").classList.add("hidden"));
$("#editBackdrop").addEventListener("click", (e) => {
  if (e.target === $("#editBackdrop")) $("#editBackdrop").classList.add("hidden");
});
$("#editForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api(`/api/pairs/${state.editId}`, {
      method: "PATCH",
      body: {
        direction: $("#editDirection").value,
        conflict_policy: $("#editPolicy").value,
        allow_deletes: $("#editDeletes").checked ? 1 : 0,
        symlink_policy: $("#editSymlinks").value,
        case_insensitive: $("#editCaseFold").checked ? 1 : 0,
      },
    });
    toast(T("Pair #{pid} updated", { pid: state.editId }));
    $("#editBackdrop").classList.add("hidden");
    loadPairs();
  } catch (err) { setMsg("#editMsg", err.message, "err"); }
});

/* ---------- conflict copies ---------- */
async function openConflicts(pairId) {
  state.conflictPair = pairId;
  $("#conflictsTitle").textContent = `${t("Conflict copies")} — ${t("pair")} #${pairId}`;
  $("#conflictsList").innerHTML = `<p class="muted">${esc(t("loading…"))}</p>`;
  $("#conflictsBackdrop").classList.remove("hidden");
  try {
    const { conflicts } = await api(`/api/pairs/${pairId}/conflicts`);
    $("#conflictsList").innerHTML = conflicts.length
      ? conflicts.map((c) => {
          const size = c.size == null ? "?" : humanBytes(c.size);
          const onDisk = c.local_exists ? "" : T(" · <b>missing locally</b>");
          return `<div class="picker-item conflict-row">
            <span>⚠ <b>${esc(c.path)}</b><br>
              <span class="muted small">${esc(T("original: {path} · {size}", { path: c.original, size }))}${onDisk}</span></span>
            <span class="row">
              <button class="btn ghost small" data-ccopy="${esc(c.path)}">${esc(t("Keep copy"))}</button>
              <button class="btn ghost small" data-corig="${esc(c.path)}">${esc(t("Keep original"))}</button>
              <button class="btn ghost small" data-cboth="${esc(c.path)}">${esc(t("Keep both"))}</button>
            </span></div>`;
        }).join("")
      : `<p class="muted">${esc(t("No conflict copies — everything is resolved. 🎉"))}</p>`;
    document.querySelectorAll("[data-ccopy],[data-corig],[data-cboth]").forEach((b) =>
      b.addEventListener("click", async () => {
        const path = b.dataset.ccopy || b.dataset.corig || b.dataset.cboth;
        const choice = b.dataset.ccopy ? "keep_copy"
          : b.dataset.corig ? "keep_original" : "keep_both";
        if (choice === "keep_both") {
          toast(t("Left as is — both copies stay in place"));
          return;
        }
        b.disabled = true;
        try {
          await api(`/api/pairs/${pairId}/conflicts/resolve`,
                    { method: "POST", body: { path, choice } });
          toast(choice === "keep_copy"
            ? T("Kept the copy as {path}", { path: path.replace(/\.conflict-\d{8}-\d{6}/, "") })
            : T("Discarded the copy {path}", { path }));
          openConflicts(pairId);
        } catch (err) {
          toast(T("Could not resolve: {err}", { err: err.message }));
          b.disabled = false;
        }
      }));
  } catch (e) {
    $("#conflictsList").innerHTML = `<p class="msg err">${esc(e.message)}</p>`;
  }
}
$("#conflictsClose").addEventListener("click", () => $("#conflictsBackdrop").classList.add("hidden"));
$("#conflictsBackdrop").addEventListener("click", (e) => {
  if (e.target === $("#conflictsBackdrop")) $("#conflictsBackdrop").classList.add("hidden");
});

$("#pairForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const remoteId = $("#pairRemoteId").value;
  if (!remoteId) return setMsg("#pairFormMsg", t("Pick a Drive folder first."), "err");
  try {
    await api("/api/pairs", {
      method: "POST",
      body: {
        account_id: +$("#pairAccount").value,
        local_path: $("#pairLocal").value.trim(),
        remote_id: remoteId,
        remote_path: $("#pairRemoteName").value,
        direction: $("#pairDirection").value,
        conflict_policy: $("#pairPolicy").value,
        include_patterns: state.newScope,
      },
    });
    const scopeMsg = state.newScope.length
      ? T(" Only {n} chosen subfolder(s) will sync.", { n: state.newScope.length }) : "";
    setMsg("#pairFormMsg", t("Pair added — first sync starts in the background.") + scopeMsg, "ok");
    state.newScope = [];
    $("#pairScopeSummary").value = t("Everything in this folder");
    loadPairs();
  } catch (err) { setMsg("#pairFormMsg", err.message, "err"); }
});

/* ---------- folder picker ---------- */
$("#pickFolderBtn").addEventListener("click", () => {
  state.picker = { stack: [], current: { id: "root", name: "My Drive" } };
  $("#pickerSearch").value = "";
  renderPicker();
  $("#pickerBackdrop").classList.remove("hidden");
});
let searchTimer = null;
$("#pickerSearch").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(renderPicker, 300);
});
$("#pickerClose").addEventListener("click", () => $("#pickerBackdrop").classList.add("hidden"));
$("#pickerBackdrop").addEventListener("click", (e) => {
  if (e.target === $("#pickerBackdrop")) $("#pickerBackdrop").classList.add("hidden");
});
$("#pickerUp").addEventListener("click", () => {
  const prev = state.picker.stack.pop();
  if (prev) { state.picker.current = prev; renderPicker(); }
});
$("#pickerChoose").addEventListener("click", () => {
  setPairRemote(state.picker.current.id, state.picker.current.name);
  $("#pickerBackdrop").classList.add("hidden");
});

/* ---------- selective-sync scope ---------- */
function setPairRemote(id, name) {
  $("#pairRemoteId").value = id;
  $("#pairRemoteName").value = name;
  $("#pairScopeBtn").disabled = !id;
  state.newScope = [];
  $("#pairScopeSummary").value = id
    ? t("Everything in this folder") : t("Pick a Drive folder first.");
}

async function openScope(accountId, remoteId, selected, target) {
  if (!remoteId) { toast(t("Pick a Drive folder first.")); return; }
  state.scope = { accountId, remoteId, selected: [...selected], target };
  $("#scopeTitle").textContent = target === "new"
    ? t("Choose subfolders")
    : `${t("Subfolders")} — #${target}`;
  $("#scopeBackdrop").classList.remove("hidden");
  $("#scopeList").innerHTML = `<p class="muted">${esc(t("loading…"))}</p>`;
  $("#scopeSave").disabled = true;
  try {
    const { folders } = await api(
      `/api/drive/${accountId}/folders?parent=${encodeURIComponent(remoteId)}`);
    $("#scopeList").innerHTML = folders.length
      ? folders.map((f) => {
        const on = selected.some((p) => p === f.name
          || p.startsWith(f.name + "/"));
        return `<label class="picker-item"><span>
          <input type="checkbox" value="${esc(f.name)}"${on ? " checked" : ""}>
          ${esc(f.name)}</span></label>`;
      }).join("")
      : `<p class="muted">${esc(t("No subfolders."))}</p>`;
    $("#scopeSave").disabled = false;
  } catch (e) {
    $("#scopeList").innerHTML = `<p class="msg err">${esc(e.message)}</p>`;
    // the folder list failed, but "no boxes ticked" still means the safe
    // default (sync everything), so let the user save that
    $("#scopeSave").disabled = false;
  }
}

$("#pairScopeBtn").addEventListener("click", () => {
  const acct = +$("#pairAccount").value
    || (state.accounts[0] && state.accounts[0].id);
  openScope(acct, $("#pairRemoteId").value, state.newScope, "new");
});
$("#pairAccount").addEventListener("change", () => {
  // the chosen Drive folder belongs to the previous account — clear it so
  // a pair can never be created against another account's folder id
  setPairRemote("", "");
});
$("#scopeClose").addEventListener("click",
  () => $("#scopeBackdrop").classList.add("hidden"));
$("#scopeBackdrop").addEventListener("click", (e) => {
  if (e.target === $("#scopeBackdrop")) {
    $("#scopeBackdrop").classList.add("hidden");
  }
});
$("#scopeAll").addEventListener("click", () =>
  document.querySelectorAll("#scopeList input[type=checkbox]")
    .forEach((c) => { c.checked = true; }));
$("#scopeNone").addEventListener("click", () =>
  document.querySelectorAll("#scopeList input[type=checkbox]")
    .forEach((c) => { c.checked = false; }));
$("#scopeSave").addEventListener("click", async () => {
  const chosen = [...document.querySelectorAll(
    "#scopeList input[type=checkbox]:checked")].map((c) => c.value);
  const sc = state.scope;
  if (sc.target === "new") {
    state.newScope = chosen;
    $("#pairScopeSummary").value = chosen.length
      ? `${T("{n} subfolder(s)", { n: chosen.length })}: ${chosen.slice(0, 3).join(", ")}${chosen.length > 3 ? "…" : ""}`
      : t("Everything in this folder");
    $("#scopeBackdrop").classList.add("hidden");
    return;
  }
  try {
    await api(`/api/pairs/${sc.target}`,
              { method: "PATCH", body: { include_patterns: chosen } });
    toast(chosen.length
      ? T("Only {n} subfolder(s) will sync", { n: chosen.length })
      : t("Scope reset — everything syncs"));
    $("#scopeBackdrop").classList.add("hidden");
    loadPairs();
  } catch (e) { toast(T("Could not save scope: {err}", { err: e.message })); }
});

async function renderPicker() {
  const query = $("#pickerSearch").value.trim();
  if (query) return renderPickerSearch(query);
  const cur = state.picker.current;
  $("#pickerPath").textContent = state.picker.stack.map((s) => s.name).concat(cur.name).join(" / ");
  $("#pickerList").innerHTML = `<p class="muted">${esc(t("loading…"))}</p>`;
  try {
    const acct = $("#pairAccount").value || (state.accounts[0] && state.accounts[0].id);
    const { folders } = await api(`/api/drive/${acct}/folders?parent=${encodeURIComponent(cur.id)}`);
    const list = folders.length
      ? folders.map((f) => `<div class="picker-item" data-id="${esc(f.id)}" data-name="${esc(f.name)}">
          <span>📁 ${esc(f.name)}</span><span class="muted small">${esc(t("open ›"))}</span></div>`).join("")
      : `<p class="muted">${esc(cur.id === "shared"
          ? t("No folders shared with this account yet.") : t("No subfolders."))}</p>`;
    const useRow = cur.id === "shared" ? "" :
      `<div class="picker-item" data-id="${esc(cur.id)}" data-name="${esc(cur.name)}" data-choose="1">
        <span>${esc(T("✅ Use “{name}”", { name: cur.name }))}${folders.length ? "" : " " + esc(t("(no subfolders)"))}</span>
        <span class="muted small">${esc(t("select"))}</span></div>`;
    $("#pickerList").innerHTML = list + useRow;
    document.querySelectorAll(".picker-item").forEach((item) =>
      item.addEventListener("click", () => {
        if (item.dataset.choose) {
          setPairRemote(item.dataset.id, item.dataset.name);
          $("#pickerBackdrop").classList.add("hidden");
        } else {
          state.picker.stack.push(state.picker.current);
          state.picker.current = { id: item.dataset.id, name: item.dataset.name };
          renderPicker();
        }
      }));
  } catch (e) {
    $("#pickerList").innerHTML = `<p class="msg err">${esc(e.message)}</p>`;
  }
}

/* Drive-wide folder search inside the picker: finds a folder anywhere in
   the account instead of stepping through the tree one level at a time */
async function renderPickerSearch(query) {
  $("#pickerPath").textContent = T("Search: “{q}”", { q: query });
  $("#pickerList").innerHTML = `<p class="muted">${esc(t("searching…"))}</p>`;
  try {
    const acct = $("#pairAccount").value || (state.accounts[0] && state.accounts[0].id);
    const { items } = await api(`/api/drive/${acct}/search?folders_only=1&q=${encodeURIComponent(query)}`);
    $("#pickerList").innerHTML = items.length
      ? items.map((f) => `<div class="picker-item" data-id="${esc(f.id)}" data-name="${esc(f.name)}">
          <span>📁 ${esc(f.name)}</span><span class="muted small">${esc(t("open ›"))}</span></div>`).join("")
      : `<p class="muted">${esc(t("No folder on this Drive matches the search."))}</p>`;
    document.querySelectorAll("#pickerList .picker-item").forEach((item) =>
      item.addEventListener("click", () => {
        // land in the folder so "Use this folder" still works on it
        state.picker.stack = [{ id: "root", name: "My Drive" }];
        state.picker.current = { id: item.dataset.id, name: item.dataset.name };
        $("#pickerSearch").value = "";
        renderPicker();
      }));
  } catch (e) {
    $("#pickerList").innerHTML = `<p class="msg err">${esc(e.message)}</p>`;
  }
}

/* ---------- activity ---------- */
async function renderEvents(container, limit = 200) {
  const filter = $("#eventFilter") ? $("#eventFilter").value : "";
  // the search box belongs to the Activity tab; the dashboard's compact
  // "Recent activity" panel always shows the plain newest rows
  const needle = container.id === "eventList"
    ? ($("#eventSearch") ? $("#eventSearch").value : "").trim().toLowerCase()
    : "";
  // a search scans a wider slice of the log than the plain listing
  const { events } = await api("/api/events?limit=" + (needle ? 1000 : limit) +
    (filter ? "&level=" + filter : ""));
  const visible = needle ? events.filter((e) =>
    [e.path, e.message, e.action, e.level, String(e.pair_id || "")]
      .some((v) => (v || "").toLowerCase().includes(needle))) : events;
  state.events = visible;
  container.innerHTML = visible.length
    ? visible.map((e, i) => `<div class="ev ${esc(e.level)}" data-ev="${i}" title="${esc(t("Click for detail"))}">
        <span class="t">${esc(e.ts.replace("T", " "))}</span>
        <span class="a">${esc(e.action)}</span>
        <span class="p">${esc(e.path)}</span>
        <span class="m muted">${esc(e.message)}</span></div>`).join("")
    : (needle ? `<p class="muted small">${esc(t("No entries match the search."))}</p>`
       : `<p class="muted small">${esc(t("Nothing logged yet."))}</p>`);
  container.querySelectorAll("[data-ev]").forEach((row) =>
    row.addEventListener("click", () => {
      const ev = state.events[+row.dataset.ev];
      if (!ev) return;
      $("#eventDetail").textContent =
        `${ev.ts.replace("T", " ")} — ${ev.level}\n` +
        `${T("Action: {action}", { action: ev.action })}\n` +
        `${T("Path: {path}", { path: ev.path || "—" })}\n` +
        (ev.pair_id ? `${T("Pair: #{pid}", { pid: ev.pair_id })}\n` : "") +
        `\n${ev.message || t("No detail.")}`;
      $("#eventBackdrop").classList.remove("hidden");
    }));
}
async function loadEvents() {
  await renderEvents($("#eventList")).catch((e) =>
    { $("#eventList").textContent = T("failed to load events: {err}", { err: e.message }); });
}
$("#refreshEvents").addEventListener("click", loadEvents);
$("#eventFilter").addEventListener("change", loadEvents);
$("#eventSearch").addEventListener("input", loadEvents);
$("#eventClose").addEventListener("click", () => $("#eventBackdrop").classList.add("hidden"));
$("#eventBackdrop").addEventListener("click", (e) => {
  if (e.target === $("#eventBackdrop")) $("#eventBackdrop").classList.add("hidden");
});

/* ---------- settings ---------- */
async function loadSettings() {
  try {
    const { settings } = await api("/api/settings");
    $("#setInterval").value = settings.sync_interval;
    $("#setPolicy").value = settings.conflict_policy;
    $("#setIgnores").value = settings.ignore_patterns.join("\n");
    $("#setDeleteLimit").value = settings.delete_safety_limit;
    $("#setConcurrency").value = settings.transfer_concurrency;
    $("#setHistory").value = settings.history_versions ?? 10;
    $("#setBandwidth").value = settings.bandwidth_limit_kb;
    $("#setActiveFrom").value = settings.active_from || "";
    $("#setActiveTo").value = settings.active_to || "";
    $("#setWatch").checked = settings.watch_enabled !== false;
    $("#setIncremental").checked = settings.incremental_scan !== false;
    $("#setConvertDocs").checked = settings.convert_google_docs !== false;
    const st = await api("/api/status");
    $("#version").textContent = "v" + st.version;
    $("#aboutText").textContent =
      `DriveSync ${T("— local dashboard port {port}. Data lives in {db}.",
        { port: settings.server_port, db: st.db })}`;
  } catch (e) { /* ignore */ }
}
$("#settingsForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/settings", {
      method: "POST",
      body: {
        sync_interval: +$("#setInterval").value,
        conflict_policy: $("#setPolicy").value,
        delete_safety_limit: +$("#setDeleteLimit").value,
        transfer_concurrency: +$("#setConcurrency").value,
        history_versions: +$("#setHistory").value,
        bandwidth_limit_kb: +$("#setBandwidth").value,
        active_from: $("#setActiveFrom").value,
        active_to: $("#setActiveTo").value,
        watch_enabled: $("#setWatch").checked,
        incremental_scan: $("#setIncremental").checked,
        convert_google_docs: $("#setConvertDocs").checked,
        ignore_patterns: $("#setIgnores").value.split("\n").map((s) => s.trim()).filter(Boolean),
      },
    });
    setMsg("#settingsMsg", t("Saved."), "ok");
    toast(t("Settings saved"));
  } catch (err) { setMsg("#settingsMsg", err.message, "err"); }
});

/* ---------- global actions ---------- */
$("#syncNowBtn").addEventListener("click", async () => {
  try {
    await api("/api/sync", { method: "POST" });
    toast(t("Sync started for all pairs"));
  } catch (e) { toast(T("Could not start sync: {err}", { err: e.message })); }
});

function humanBytes(n) {
  if (!n) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return n.toFixed(1) + " " + units[i];
}

/* ---------- boot ---------- */
applyTheme();
applyLang();
loadDashboard();
