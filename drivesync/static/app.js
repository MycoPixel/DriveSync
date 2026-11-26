/* DriveSync dashboard — vanilla JS front-end for the local API */
"use strict";

const $ = (sel) => document.querySelector(sel);
/* per-server random token injected into the page; the API rejects the old
   static "1" header so a blind local script cannot drive it. */
const API_TOKEN = document.querySelector('meta[name="drivesync-token"]')
  ?.getAttribute("content") || "";
/* escape every server value before it goes into innerHTML: file names,
   folder names and log messages are user/Drive-controlled text */
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

/* Turn raw Google/network text into an actionable sentence. The server
   still returns the precise error; this only decides how to phrase it. */
function friendlyError(message) {
  const raw = String(message || "").trim();
  const low = raw.toLowerCase();
  if (/invalid_grant|reauth|sign-in (has )?expired|no longer accepts/.test(low))
    return t("Your Google sign-in expired. Open Accounts and press Reconnect.");
  if (/quota|storagequota|storage quota/.test(low))
    return t("Google Drive storage is full. Free some space, then retry.");
  if (/rate.?limit|userratelimitexceeded|ratelimitexceeded|too many requests|429/.test(low))
    return t("Google Drive is rate-limiting requests. Wait a minute and retry.");
  if (/offline|connection|timed out|timeout|network is unreachable|name resolution|dns/.test(low))
    return t("You appear to be offline, or Google Drive is unreachable. Check the connection and retry.");
  if (/403|forbidden|permission/.test(low))
    return t("Google refused that operation. Check the account's permissions and whether this is a read-only sign-in.");
  if (/500|502|503|504|server error|backend error/.test(low))
    return t("Google Drive is having trouble right now. Try again shortly.");
  return raw;
}

const api = async (path, opts = {}) => {
  const resp = await fetch(path, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      "X-DriveSync": API_TOKEN,
      ...(opts.headers || {}),
    },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await resp.json().catch(() => ({ ok: false, error: "bad JSON" }));
  if (!resp.ok || data.ok === false) throw new Error(friendlyError(data.error || resp.statusText));
  return data;
};

function toast(msg, ms = 2600, action = null) {
  const t = $("#toast");
  t.textContent = "";
  const text = document.createElement("span");
  text.textContent = msg;
  t.appendChild(text);
  if (action) {
    const btn = document.createElement("button");
    btn.className = "btn ghost small toast-action";
    btn.textContent = action.label;
    btn.addEventListener("click", () => {
      t.classList.add("hidden");
      action.run();
    });
    t.appendChild(btn);
    ms = Math.max(ms, 6000);
  }
  t.classList.remove("hidden");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add("hidden"), ms);
}

/* In-app confirmation modal (the browser's confirm() is blocking, ugly and
   impossible to style). Resolves true/false; optional checkbox result is
   available as the second argument of the resolved object. */
function confirmDialog({ title, text, yes = "Continue", no = "Cancel",
                        check = null, danger = false }) {
  return new Promise((resolve) => {
    const back = $("#confirmBackdrop");
    const done = (ok) => {
      back.classList.add("hidden");
      $("#confirmYes").classList.remove("danger");
      $("#confirmYes").classList.add("primary");
      resolve({ ok, checked: $("#confirmCheck").checked });
    };
    $("#confirmTitle").textContent = title || t("Confirm");
    $("#confirmText").textContent = text || "";
    $("#confirmYes").textContent = t(yes);
    $("#confirmNo").textContent = t(no);
    if (danger) {
      $("#confirmYes").classList.remove("primary");
      $("#confirmYes").classList.add("danger");
    }
    const row = $("#confirmCheckRow");
    if (check) {
      row.classList.remove("hidden");
      $("#confirmCheckLabel").textContent = t(check.label);
      $("#confirmCheck").checked = !!check.checked;
    } else {
      row.classList.add("hidden");
      $("#confirmCheck").checked = false;
    }
    $("#confirmYes").onclick = () => done(true);
    $("#confirmNo").onclick = () => done(false);
    $("#confirmClose").onclick = () => done(false);
    back.onclick = (e) => { if (e.target === back) done(false); };
    back.classList.remove("hidden");
    $("#confirmYes").focus();
  });
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
  "Google accounts": "Cuentas de Google",
  "Failed pairs": "Pares con última sincronización fallida",
  "Failed or conflicted pairs": "Pares con errores o conflictos",
  "{active} active · {paused} paused": "{active} activos · {paused} pausados",
  "Sync pairs": "Pares de sincronización",
  "Accounts": "Cuentas",
  "Activity": "Actividad",
  "Transfers": "Transferencias",
  "Transfer queue": "Cola de transferencias",
  "Status": "Estado",
  "Sync pair": "Par de sincronización",
  "File": "Archivo",
  "Size": "Tamaño",
  "Updated": "Actualizado",
  "No transfers yet.": "Aún no hay transferencias.",
  "transfer-queue-hint": "Shows active transfers and the most recent " +
    "completed or failed files. Queue state resets when DriveSync " +
    "restarts.|Muestra transferencias activas y los archivos completados o " +
    "fallidos más recientes. La cola se reinicia al reiniciar DriveSync.",
  "queued": "En cola",
  "uploading": "Subiendo",
  "downloading": "Descargando",
  "retrying": "Reintentando",
  "completed": "Completado",
  "failed": "Fallido",
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
  "Start new pairs paused": "Iniciar los pares nuevos en pausa",
  "first-sync-review-hint": "Recomendado: revisa la vista previa de la primera sincronización antes de permitir transferencias.|Recomendado: revisa la vista previa de la primera sincronización antes de permitir transferencias.",
  "Review new sync pair": "Revisar nuevo par de sincronización",
  "The pair will remain paused until you review its preview and resume it.": "El par permanecerá en pausa hasta que revises su vista previa y lo reanudes.",
  "The first synchronization may begin immediately and transfer files.": "La primera sincronización puede comenzar de inmediato y transferir archivos.",
  "Continue?": "¿Continuar?",
  "Pair added paused. Review its preview, then resume it when ready.": "Par agregado en pausa. Revisa su vista previa y reanúdalo cuando estés listo.",
  "#": "#",
  "Local": "Local",
  "Conflicts": "Conflictos",
  "Deletes": "Borrados",
  "Scope": "Alcance",
  "State": "Estado",
  "Last sync": "Última sincronización",
  "Actions": "Acciones",
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
  "set.retries": 'Retry attempts <span class="muted">(extra tries for a failed transfer, 0–10)</span>|' +
    'Reintentos <span class="muted">(intentos extra para una transferencia fallida, 0–10)</span>',
  "set.backoff": 'Retry backoff <span class="muted">(seconds before the first retry; doubles each time, max 30 s)</span>|' +
    'Espera entre reintentos <span class="muted">(segundos antes del primer reintento; se duplica cada vez, máx. 30 s)</span>',
  "set.resumable": 'Resumable upload threshold <span class="muted">(MiB; files at least this big upload in resumable sessions; 0 = always)</span>|' +
    'Umbral de subida reanudable <span class="muted">(MiB; los archivos de este tamaño o más se suben en sesiones reanudables; 0 = siempre)</span>',
  "set.history": 'Kept versions per file <span class="muted">(the previous local version is saved before a pull overwrites a file; 0 = off)</span>|' +
    'Versiones guardadas por archivo <span class="muted">(la versión local anterior se guarda antes de que una descarga la sobrescriba; 0 = apagado)</span>',
  "set.bandwidth": 'Bandwidth limit <span class="muted">(KB/s shared by uploads + downloads; 0 = unlimited)</span>|' +
    'Límite de ancho de banda <span class="muted">(KB/s compartidos por subidas + descargas; 0 = sin límite)</span>',
  "set.from": 'Sync window from <span class="muted">(HH:MM; empty = always)</span>|' +
    'Ventana de sincronización desde <span class="muted">(HH:MM; vacío = siempre)</span>',
  "set.to": 'Sync window to <span class="muted">(may cross midnight)</span>|' +
    'Ventana de sincronización hasta <span class="muted">(puede cruzar la medianoche)</span>',
  "set.peakfrom": 'Peak window from <span class="muted">(HH:MM; empty = no peak hours)</span>|' +
    'Horario punta desde <span class="muted">(HH:MM; vacío = sin horas punta)</span>',
  "set.peakto": 'Peak window to <span class="muted">(may cross midnight)</span>|' +
    'Horario punta hasta <span class="muted">(puede cruzar la medianoche)</span>',
  "set.peakbw": 'Peak bandwidth limit <span class="muted">(KB/s inside the peak window; 0 = same as the general limit)</span>|' +
    'Límite de ancho de banda en punta <span class="muted">(KB/s dentro del horario punta; 0 = igual que el límite general)</span>',
  "Peak throttle": 'Peak throttle|Limitación en punta',
  "Network": 'Network|Red',
  "metered — throttled": 'metered — throttled|limitada — conexiones de tarifa medida',
  "metered — automatic sync paused":
    'metered — automatic sync paused|limitada — sincronización automática en pausa',
  "Pause automatic sync": 'Pause automatic sync|Pausar la sincronización automática',
  "Throttle to the cap below": 'Throttle to the cap below|Limitar a la tasa indicada',
  "set.metered": 'Respect metered connections <span class="muted">(NetworkManager reports the network as metered, e.g. a mobile hotspot; unknown networks count as not metered)</span>|' +
    'Respetar conexiones de tarifa medida <span class="muted">(NetworkManager informa de que la red es de tarifa medida, p. ej. un punto de acceso móvil; las redes desconocidas cuentan como no medidas)</span>',
  "set.meteredaction": 'On a metered connection <span class="muted">(pause defers automatic passes; throttle caps transfers at the rate below; manual syncs always run)</span>|' +
    'En una conexión de tarifa medida <span class="muted">(pausa aplaza los pases automáticos; limitar fija la tasa indicada; las sincronizaciones manuales siempre se ejecutan)</span>',
  "set.meteredbw": 'Metered throttle cap <span class="muted">(KB/s while the connection is metered and throttling is on; 0 = no cap)</span>|' +
    'Tasa en conexiones de tarifa medida <span class="muted">(KB/s mientras la conexión sea de tarifa medida y esté activa la limitación; 0 = sin límite)</span>',
  "Last digest": 'Last digest|Último resumen',
  "set.digest": 'Daily digest notification <span class="muted">(once a day, a summary of what synced — as a desktop notification and in the activity log)</span>|' +
    'Notificación de resumen diario <span class="muted">(una vez al día, un resumen de lo sincronizado, como notificación de escritorio y en el registro de actividad)</span>',
  "set.digesttime": 'Digest time <span class="muted">(HH:MM; fires on the first tick at/after it — a late boot catches up; empty = first tick of the day)</span>|' +
    'Hora del resumen <span class="muted">(HH:MM; se envía en el primer tick a partir de esa hora — un arranque tardío lo recupera; vacío = primer tick del día)</span>',
  "set.digestskip": 'Skip the digest on days with no activity <span class="muted">(nothing pushed, pulled or deleted, no conflicts, no errors)</span>|' +
    'Omitir el resumen en días sin actividad <span class="muted">(nada enviado, descargado o eliminado, sin conflictos ni errores)</span>',
  "unlimited": 'unlimited|sin límite',
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
  "pair.bw": 'Bandwidth cap <span class="muted">(KB/s for this pair; empty = the global limit)</span>|' +
    'Límite de ancho de banda <span class="muted">(KB/s para este par; vacío = el límite global)</span>',
  "pair.peakbw": 'Peak bandwidth cap <span class="muted">(KB/s inside this pair\'s peak window; empty = inherit)</span>|' +
    'Límite de ancho de banda en punta <span class="muted">(KB/s dentro del horario punta de este par; vacío = heredar)</span>',
  "pair.peakfrom": 'Peak window from <span class="muted">(HH:MM; empty = no pair window)</span>|' +
    'Horario punta desde <span class="muted">(HH:MM; vacío = sin ventana para el par)</span>',
  "pair.peakto": 'Peak window to <span class="muted">(may cross midnight)</span>|' +
    'Horario punta hasta <span class="muted">(puede cruzar la medianoche)</span>',
  "edit.bw": 'Bandwidth cap <span class="muted">(KB/s for this pair; empty = the global limit)</span>|' +
    'Límite de ancho de banda <span class="muted">(KB/s para este par; vacío = el límite global)</span>',
  "edit.peakbw": 'Peak bandwidth cap <span class="muted">(KB/s inside this pair\'s peak window; empty = inherit)</span>|' +
    'Límite de ancho de banda en punta <span class="muted">(KB/s dentro del horario punta de este par; vacío = heredar)</span>',
  "edit.peakfrom": 'Peak window from <span class="muted">(HH:MM; empty = no pair window)</span>|' +
    'Horario punta desde <span class="muted">(HH:MM; vacío = sin ventana para el par)</span>',
  "edit.peakto": 'Peak window to <span class="muted">(may cross midnight)</span>|' +
    'Horario punta hasta <span class="muted">(puede cruzar la medianoche)</span>',
  "Conflict copies": "Copias en conflicto",
  "conflicts-hint": 'Files kept as “<i>name.conflict-…</i>” copies by the keep-both policy. Pick which side to keep — the copy wins at the original path, the original stays and the copy is discarded, or leave both.|' +
    'Archivos guardados como copias “<i>name.conflict-…</i>” por la política de conservar ambos. Elige qué lado conservar: la copia gana en la ruta original, el original se queda y la copia se descarta, o deja ambos.',
  "Activity detail": "Detalle de actividad",
  "Nothing is applied — this only shows what the next pass would do, and why.":
    "No se aplica nada — esto solo muestra lo que haría el próximo ciclo, y por qué.",
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
  "account unavailable": "cuenta no disponible",
  "Reconnect": "Reconectar",
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
  "No shared drives in this account.": "No hay unidades compartidas en esta cuenta.",
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
  // Phase 2.5 — Google Drive browser tab
  "Drive": "Drive",
  "Google Drive browser": "Navegador de Google Drive",
  "drive-hint": "Browse this account's Drive without leaving DriveSync — descend into folders, search everywhere, filter by type, and send any folder straight to the sync-pair form.|" +
    "Explora el Drive de esta cuenta sin salir de DriveSync — entra en las carpetas, busca en todo Drive, filtra por tipo y envía cualquier carpeta directamente al formulario de pares.",
  "Type": "Tipo",
  "All types": "Todos los tipos",
  "Folders": "Carpetas",
  "Google files": "Archivos de Google",
  "PDFs": "PDF",
  "Images": "Imágenes",
  "Videos": "Vídeos",
  "Audio": "Audio",
  "Archives": "Comprimidos",
  "Text files": "Texto",
  "Other": "Otros",
  "Sort": "Ordenar",
  "Name (A→Z)": "Nombre (A→Z)",
  "Name (Z→A)": "Nombre (Z→A)",
  "Newest first": "Más recientes primero",
  "Oldest first": "Más antiguos primero",
  "Largest first": "Más grandes primero",
  "Smallest first": "Más pequeños primero",
  "Search": "Buscar",
  "Search all of Drive… (Enter)": "Buscar en todo Drive… (Enter)",
  "Modified": "Modificado",
  "Owner": "Propietario",
  "Shared with me": "Compartido conmigo",
  "Shared drives": "Unidades compartidas",
  "This folder is empty.": "Esta carpeta está vacía.",
  "No items match the type filter.": "Ningún elemento coincide con el filtro de tipo.",
  "No items match the search.": "Ningún elemento coincide con la búsqueda.",
  "Search results for “{q}” — {n} item(s) in all of Drive":
    "Resultados de “{q}” — {n} elemento(s) en todo Drive",
  "Searching all of Drive…": "Buscando en todo Drive…",
  "Nothing on this Drive matches the search.":
    "Nada en este Drive coincide con la búsqueda.",
  "Clear the search box to go back to browsing.":
    "Vacía el cuadro de búsqueda para volver a explorar.",
  "Use this folder for a new sync pair":
    "Usar esta carpeta para un nuevo par",
  "Folder sent to the sync-pair form.": "Carpeta enviada al formulario de pares.",
  "“Shared with me” cannot be a sync destination — open a folder inside it first.":
    "“Compartido conmigo” no puede ser un destino de sincronización — abre antes una carpeta dentro.",
  "“Shared drives” is a list of drives — open one first.":
    "“Unidades compartidas” es una lista de unidades — abre una primero.",
  "Choose an account to browse its Drive.":
    "Elige una cuenta para explorar su Drive.",
  "Open in search results": "Abrir desde los resultados",
  "double-click opens": "doble clic abre",
  // Phase 2.6 — Recovery tab
  "Recovery": "Recuperación",
  "Deletions awaiting confirmation": "Borrados pendientes de confirmación",
  "recovery-pending-hint": "A pass wanted to delete more files than the safety limit allows. Nothing was deleted — confirm below or keep every file. Recovery never propagates a deletion until you decide.|" +
    "Un ciclo quiso borrar más archivos de lo que permite el límite de " +
    "seguridad. No se borró nada — confirma abajo o conserva todos los " +
    "archivos. La recuperación nunca propaga un borrado sin que decidas.",
  "Conflict copies": "Copias en conflicto",
  "Locally preserved copies (trash)": "Copias conservadas localmente (papelera)",
  "recovery-trash-hint": "Deleted files are never destroyed — the app moves them here first. Restore any copy to its original path; if the path is taken, it comes back renamed.|" +
    "Los archivos borrados nunca se destruyen — la app los mueve primero " +
    "aquí. Restaura cualquier copia a su ruta original; si la ruta está " +
    "ocupada, vuelve con otro nombre.",
  "The local trash is empty.": "La papelera local está vacía.",
  "Drive trash (whole account)": "Papelera de Drive (toda la cuenta)",
  "recovery-drive-trash-hint": "Google Drive's own trash is account-wide. Restoring a file here puts it back where it was on Drive; the next pass re-downloads it if it belongs to one of your pairs.|" +
    "La papelera de Google Drive abarca toda la cuenta. Restaurar un " +
    "archivo aquí lo devuelve a su sitio en Drive; el próximo ciclo lo " +
    "descarga de nuevo si pertenece a uno de tus pares.",
  "List Drive trash": "Listar papelera de Drive",
  "Trashed": "En la papelera",
  "recovery-drive-trash-empty": "Choose a pair and press “List Drive trash”.|" +
    "Elige un par y pulsa “Listar papelera de Drive”.",
  "Previous versions": "Versiones anteriores",
  "recovery-versions-hint": "Before an overwrite pull, the app keeps the previous bytes. Restore any version; the current file is itself kept, so a restore is undoable.|" +
    "Antes de una descarga que sobrescribe, la app guarda los bytes " +
    "anteriores. Restaura cualquier versión; el archivo actual también se " +
    "conserva, así que restaurar es reversible.",
  "File path": "Ruta del archivo",
  "List versions": "Listar versiones",
  "Version": "Versión",
  "recovery-versions-empty": "List the versions of a path to restore one.|" +
    "Lista las versiones de una ruta para restaurar una.",
  "Recent failures": "Fallos recientes",
  "Recovery history": "Historial de recuperación",
  "recovery-history-empty": "Nothing to show yet.|" + "Nada que mostrar todavía.",
  "When": "Cuándo",
  "Deleted": "Borrado",
  "Allow these deletions (one pass)": "Permitir estos borrados (un ciclo)",
  "Keep my files (deletes off)": "Conservar mis archivos (sin borrados)",
  "Restore": "Restaurar",
  "Restore version": "Restaurar versión",
  "No deletions are awaiting confirmation.": "No hay borrados pendientes de confirmación.",
  "{count} deletion(s) held for pair #{pair_id} ({name}):":
    "{count} borrado(s) retenidos para el par #{pair_id} ({name}):",
  "Deletion types: {kinds}": "Tipos de borrado: {kinds}",
  "restore.ack": "The deletions were approved for one pass. The next sync applies them.|Los borrados quedaron aprobados por un ciclo. La próxima sincronización los aplica.",
  "restore.keep": "Every file was kept — deletes are now off for this pair (re-enable from the pair editor).|Se conservaron todos los archivos — los borrados quedaron desactivados para este par (reactívalos desde el editor del par).",
  "No conflict copies — everything is resolved.": "Sin copias en conflicto — todo está resuelto.",
  "{n} conflict copy(ies) across your pairs — resolve them from each pair's “Conflicts” button on the Sync pairs tab.":
    "{n} copia(s) en conflicto entre tus pares — resuélvelas desde el botón “Conflictos” de cada par en la pestaña Pares.",
  "No recent failures.": "Sin fallos recientes.",
  "Drive trash listed.": "Papelera de Drive listada.",
  "The Drive trash is empty.": "La papelera de Drive está vacía.",
  "File restored to its original path.": "Archivo restaurado a su ruta original.",
  "File restored under a new name (the original path was occupied).": "Archivo restaurado con otro nombre (la ruta original estaba ocupada).",
  "File taken out of the Drive trash.": "Archivo sacado de la papelera de Drive.",
  "Version restored; the current copy was kept too.": "Versión restaurada; la copia actual también se conservó.",
  "Enter a file path first.": "Escribe antes una ruta de archivo.",
};

const LANG_NAMES = { en: "English", es: "Español" };
let _lang = (localStorage.getItem("ds_lang")
  || ((navigator.language || "en").toLowerCase().startsWith("es") ? "es" : "en"));

function t(s) {
  const v = ES[s];
  if (_lang === "en") {
    // hint keys are stored "English|Spanish"; the key itself is NOT the
    // English text for those, so serve the stored English half instead of
    // the raw key (fixes hints rendering as their key names in English)
    return v !== undefined && v.includes("|") ? v.split("|")[0] : s;
  }
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

/* Appearance is shared with the desktop app through config.json. The
   localStorage copy is only a fast first paint; the server wins on boot. */
function saveAppearance(partial) {
  api("/api/settings", { method: "POST", body: partial }).catch(() => {});
}

async function loadAppearanceFromConfig() {
  try {
    const { settings } = await api("/api/settings");
    let changed = false;
    if (settings.theme && THEMES.includes(settings.theme)
        && settings.theme !== _theme) {
      _theme = settings.theme;
      localStorage.setItem("ds_theme", _theme);
      changed = true;
    }
    if (settings.accent && ACCENTS[settings.accent]
        && settings.accent !== _accent) {
      _accent = settings.accent;
      localStorage.setItem("ds_accent", _accent);
      changed = true;
    }
    const lang = settings.language === "es" ? "es" : "en";
    if (lang !== _lang) {
      _lang = lang;
      localStorage.setItem("ds_lang", _lang);
      changed = true;
    }
    if (changed) {
      applyTheme();
      applyLang();
      loadTab(state.tab);
    }
  } catch (e) { /* keep the local fallback */ }
}

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
          saveAppearance({ theme: th });
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
        saveAppearance({ accent: name });
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
  saveAppearance({ language: _lang });
  applyLang();
  loadTab(state.tab);            // re-render the dynamic parts in Spanish
});

/* ---------- tabs ---------- */
document.querySelectorAll("#tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((b) => {
      b.classList.remove("active");
      b.setAttribute("aria-selected", "false");
    });
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    btn.classList.add("active");
    btn.setAttribute("aria-selected", "true");
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
  if (name === "drive") loadDrive();
  if (name === "transfers") loadTransfers();
  if (name === "recovery") loadRecovery();
  if (name === "activity") loadEvents();
  if (name === "settings") loadSettings();
}

/* ---------- state ---------- */
let state = { accounts: [], pairs: [], events: [], newScope: [],
  conflictPair: null, tab: "dashboard",
  picker: { stack: [], current: { id: "root", name: "My Drive" } },
  scope: { accountId: null, remoteId: null, selected: [], target: "new" },
  drive: { stack: [{ id: "root", name: "My Drive" }], crumbs: [],
    items: [], search: null, loading: false } };

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
      (w.peak_active
        ? `<b>${esc(t("Peak throttle"))}</b><span>${
            w.bandwidth_now_kb ? esc(w.bandwidth_now_kb + " KB/s")
                               : esc(t("unlimited"))}</span>`
        : "") +
      (w.network && w.network.metered
        ? `<b>${esc(t("Network"))}</b><span class="msg err">${esc(
            w.network.action === "throttle"
              ? t("metered — throttled")
              : t("metered — automatic sync paused"))}</span>`
        : "") +
      (w.digest && w.digest.last_message
        ? `<b>${esc(t("Last digest"))}</b><span>${esc(w.digest.last_message)}</span>`
        : "") +
      (w.last_error ? `<b>${esc(t("Last error"))}</b><span class="msg err">${esc(w.last_error)}</span>` : "");
  } catch (e) { $("#workerStatus").textContent = T("status unavailable: {err}", { err: e.message }); }

  try {
    const { accounts } = await api("/api/accounts");
    state.accounts = accounts;
    $("#metricAccounts").textContent = accounts.length;
    $("#accountCards").innerHTML = accounts.length
      ? accounts.map((a) => {
          const q = a.quota || {};
          const used = humanBytes(+q.usage || 0), limit = +q.limit || 0;
          const storage = limit ? T("{used} / {limit} used", { used, limit: humanBytes(limit) })
            : (used !== "—" ? T("{used} used", { used }) : "");
          const extra = a.reauth_required ? ` · ${esc(t("sign-in expired"))}`
            : a.error ? ` · ${esc(t("account unavailable"))}`
            : storage ? ` · ${esc(storage)}` : "";
          const reconnect = a.reauth_required
            ? ` <button class="btn ghost small" data-reconnect>${esc(t("Reconnect"))}</button>` : "";
          return `<div class="acct"><span>📮 ${esc(a.email)}</span>
          <span class="muted small" ${a.error ? `title="${esc(a.error)}"` : ""}>${esc(a.name || "")}${extra}</span>${reconnect}</div>`;
        }).join("")
      : `<p class="muted small">${esc(t("No Google account connected yet."))}</p>`;
    bindReconnectButtons();
  } catch (e) { /* ignore */ }

  try {
    const { pairs } = await api("/api/pairs");
    state.pairs = pairs;
    const activePairs = pairs.filter((p) => p.enabled && !(p.paused_until && new Date(p.paused_until) > new Date())).length;
    const issuePairs = pairs.filter((p) => p.last_sync && !p.last_sync.ok).length;
    $("#metricPairs").textContent = pairs.length;
    $("#metricPairState").textContent = T("{active} active · {paused} paused", { active: activePairs, paused: pairs.length - activePairs });
    $("#metricIssues").textContent = issuePairs;
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
          const storage = a.reauth_required ? esc(t("sign-in expired"))
            : a.error ? `<span title="${esc(a.error)}">${esc(t("account unavailable"))}</span>`
            : limit ? `${used} / ${humanBytes(limit)}` : used || "—";
          const reconnect = a.reauth_required
            ? `<button class="btn small" data-reconnect>${esc(t("Reconnect"))}</button> ` : "";
          return `<tr><td>${a.id}</td><td>${esc(a.email)}</td><td>${storage}</td>
            <td>${reconnect}<button class="btn danger small" data-del-acct="${a.id}">${esc(t("Remove"))}</button></td></tr>`;
        }).join("")
      : `<tr><td colspan="4" class="muted">${esc(t("No accounts connected."))}</td></tr>`;
    bindReconnectButtons();
    document.querySelectorAll("[data-del-acct]").forEach((b) =>
      b.addEventListener("click", async () => {
        const account = state.accounts.find(
          (a) => String(a.id) === b.dataset.delAcct);
        const { ok, checked } = await confirmDialog({
          title: t("Remove account"),
          text: T("Remove {email} and all of its sync pairs? Local files and Drive contents are kept.",
                  { email: account ? account.email : "#" + b.dataset.delAcct }),
          yes: "Remove account",
          danger: true,
          check: { label: "Also revoke DriveSync's access token at Google (recommended)",
                   checked: true },
        });
        if (!ok) return;
        try {
          const res = await api(`/api/accounts/${b.dataset.delAcct}`,
            { method: "DELETE", body: { revoke: checked } });
          const revoked = res.revoked;
          if (checked && revoked && !revoked.ok) {
            toast(T("Account removed, but Google revocation failed: {detail}",
                    { detail: revoked.detail }));
          } else {
            toast(t("Account removed"));
          }
          loadAccounts();
        } catch (err) { toast(err.message); }
      }));
    const sel = $("#pairAccount");
    const prev = sel.value;
    sel.innerHTML = accounts.map((a) => `<option value="${esc(a.id)}">${esc(a.email)}</option>`).join("");
    // keep the user's choice across refreshes instead of snapping to #1
    if (prev && accounts.some((a) => String(a.id) === prev)) sel.value = prev;
    const dsel = $("#driveAccount");
    const dprev = dsel.value;
    dsel.innerHTML = accounts.map((a) => `<option value="${esc(a.id)}">${esc(a.email)}</option>`).join("");
    if (dprev && accounts.some((a) => String(a.id) === dprev)) dsel.value = dprev;
  } catch (e) { /* ignore */ }
}

/* Re-authorize with the saved OAuth client. The server upserts the account
   by e-mail, so the existing pairs keep working once Google confirms. */
function bindReconnectButtons() {
  document.querySelectorAll("[data-reconnect]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      toast(t("A Google window should open in your browser; approve access there…"));
      try {
        const started = await api("/api/accounts", { method: "POST", body: {} });
        const job = started.job;
        const account = await new Promise((resolve, reject) => {
          const timer = setInterval(async () => {
            try {
              const st = await api(`/api/accounts/auth/${job}`);
              if (st.status === "done") {
                clearInterval(timer);
                resolve(st.account);
              } else if (st.status === "error" || st.status === "cancelled") {
                clearInterval(timer);
                reject(new Error(st.error || t("Sign-in did not finish.")));
              }
            } catch (err) {
              clearInterval(timer);
              reject(err);
            }
          }, 1000);
        });
        toast(T("Connected {email} 🎉", { email: account.email }));
        loadAccounts();
        loadDashboard();
      } catch (err) {
        toast(err.message);
        btn.disabled = false;
      }
    }));
}

let _authJob = null;
let _authPoll = null;

function setAuthBusy(busy) {
  $("#authBtn").disabled = busy;
  $("#authTestBtn").disabled = busy;
  $("#authCancelBtn").classList.toggle("hidden", !busy);
}

$("#authTestBtn").addEventListener("click", async () => {
  const cid = $("#clientId").value.trim();
  const csec = $("#clientSecret").value.trim();
  setMsg("#authMsg", t("Checking the credentials with Google…"), "");
  try {
    const res = await api("/api/oauth/test", {
      method: "POST",
      body: { client_id: cid, client_secret: csec },
    });
    setMsg("#authMsg", t(res.message), "ok");
  } catch (err) {
    setMsg("#authMsg", err.message, "err");
  }
});

$("#authCancelBtn").addEventListener("click", async () => {
  if (!_authJob) return;
  try {
    await api(`/api/accounts/auth/${_authJob}`, { method: "DELETE" });
    setMsg("#authMsg", t("Sign-in cancelled."), "");
  } catch (err) {
    setMsg("#authMsg", err.message, "err");
  } finally {
    clearInterval(_authPoll);
    _authPoll = null;
    _authJob = null;
    setAuthBusy(false);
  }
});

$("#authForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  setMsg("#authMsg", t("A Google window should open in your browser; approve access there…"), "");
  setAuthBusy(true);
  try {
    const started = await api("/api/accounts", {
      method: "POST",
      body: {
        client_id: $("#clientId").value.trim(),
        client_secret: $("#clientSecret").value.trim(),
        drive_scope: $("#authScope").value,
      },
    });
    _authJob = started.job;
    clearInterval(_authPoll);
    _authPoll = setInterval(async () => {
      try {
        const st = await api(`/api/accounts/auth/${_authJob}`);
        if (st.status === "done") {
          clearInterval(_authPoll);
          _authPoll = null;
          _authJob = null;
          setAuthBusy(false);
          setMsg("#authMsg", T("Connected {email} 🎉",
                               { email: st.account.email }), "ok");
          loadAccounts();
          loadDashboard();
        } else if (st.status === "error" || st.status === "cancelled") {
          clearInterval(_authPoll);
          _authPoll = null;
          _authJob = null;
          setAuthBusy(false);
          setMsg("#authMsg", st.error || t("Sign-in did not finish."), "err");
        }
      } catch (err) {
        clearInterval(_authPoll);
        _authPoll = null;
        _authJob = null;
        setAuthBusy(false);
        setMsg("#authMsg", err.message, "err");
      }
    }, 1000);
  } catch (err) {
    setAuthBusy(false);
    setMsg("#authMsg", err.message, "err");
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

function etaText(bytes, kb) {
  if (!bytes) return "";
  if (!kb) return t("calculating…");
  const seconds = Math.max(1, Math.round(bytes / (kb * 1024)));
  if (seconds < 60) return T("{sec}s", { sec: seconds });
  return T("{m}m", { m: Math.ceil(seconds / 60) });
}

async function loadPairs() {
  await loadAccounts();
  try {
    const { pairs } = await api("/api/pairs");
    state.pairs = pairs;
    const box = $("#pairsCards");
    if (!pairs.length) {
      box.innerHTML = `<p class="muted small">${esc(t("No sync pairs yet."))}</p>`;
      return;
    }
    box.innerHTML = pairs.map((p) => {
      const scopeTxt = (p.include_patterns && p.include_patterns.length)
        ? T("{n} subfolder(s)", { n: p.include_patterns.length }) : t("everything");
      const untilActive = p.paused_until && new Date(p.paused_until) > new Date();
      const state = !p.enabled ? "off" : untilActive ? "warn" : "on";
      const stateLabel = !p.enabled ? t("paused")
        : untilActive ? T("paused until {when}",
                          { when: new Date(p.paused_until).toLocaleString() })
        : t("enabled");
      const last = p.last_sync;
      const err = last && !last.ok && last.error;
      const progress = p.progress
        ? `<span class="chip info">${esc(T("{n} transfer(s) active", { n: p.progress.active }))}${
            p.progress.bytes ? ` · ${esc(humanBytes(p.progress.bytes))} · ${esc(t("ETA"))} ${esc(etaText(p.progress.bytes, (last && last.bandwidth_kb) || 0))}` : ""}</span>`
        : "";
      const driveUrl = `https://drive.google.com/drive/folders/${encodeURIComponent(p.remote_id)}`;
      return `<article class="pair-card" data-pair="${p.id}">
        <div class="pair-card-head">
          <div class="pair-title">
            <b>#${p.id}</b>
            <span class="chip">${esc(p.direction)}</span>
            <span class="chip state-${state}">${esc(stateLabel)}</span>
            <span class="chip ${p.allow_deletes ? "info" : "warn"}">${esc(t("Deletes"))}: ${p.allow_deletes ? esc(t("On")) : esc(t("Off"))}</span>
            ${progress}
            ${err ? `<span class="chip error" title="${esc(err)}">${esc(t("last sync failed"))}</span>` : ""}
          </div>
          <div class="row wrap">
            <button class="btn primary small" data-sync="${p.id}">${esc(t("Sync"))}</button>
            <button class="btn ghost small" data-preview="${p.id}">${esc(t("Preview"))}</button>
            <details class="pair-menu">
              <summary class="btn ghost small">${esc(t("More"))}</summary>
              <div class="pair-menu-list">
                <button class="btn ghost small" data-edit="${p.id}">${esc(t("Edit…"))}</button>
                <button class="btn ghost small" data-open-folder="${p.id}">${esc(t("Open folder"))}</button>
                <a class="btn ghost small" href="${esc(driveUrl)}" target="_blank" rel="noreferrer">${esc(t("Open in Drive"))}</a>
                <button class="btn ghost small" data-conflicts="${p.id}">${esc(t("Conflicts"))}</button>
                <button class="btn ghost small" data-scope="${p.id}" data-account="${p.account_id}" data-remote="${esc(p.remote_id)}">${esc(t("Scope"))}: ${esc(scopeTxt)}</button>
                ${p.enabled
                  ? `<button class="btn ghost small" data-pause="${p.id}">${esc(t("Pause"))}</button>`
                  : `<button class="btn ghost small" data-resume="${p.id}">${esc(t("Resume"))}</button>`}
                <button class="btn ghost small" data-until="${p.id}" title="${esc(t("Pause until tomorrow 09:00 (click again to cancel)"))}">${esc(untilActive ? t("Cancel pause") : t("Until tomorrow"))}</button>
                ${p.allow_deletes
                  ? `<button class="btn ghost small" data-nodel="${p.id}" title="${esc(t("Deletions are allowed (files go to trash)"))}">${esc(t("Block deletions"))}</button>`
                  : `<button class="btn ghost small" data-yodel="${p.id}" title="${esc(t("Deletions blocked — files are kept on both sides"))}">${esc(t("Allow deletions"))}</button>`}
                <button class="btn danger small" data-del="${p.id}">${esc(t("Remove"))}</button>
              </div>
            </details>
          </div>
        </div>
        <div class="pair-paths">
          <div><span class="muted small">${esc(t("Local"))}</span> <span class="break-all">${esc(p.local_path)}</span></div>
          <div><span class="muted small">${esc(t("Drive folder"))}</span> <span class="break-all">${esc(p.remote_path || p.remote_id)}</span></div>
        </div>
        <div class="pair-meta muted small">${esc(t("Last sync"))}: ${esc(lastSyncText(p))}${
          p.bandwidth_limit_kb ? ` · ${esc(T("cap {kb} KB/s", { kb: p.bandwidth_limit_kb }))}` : ""}</div>
      </article>`;
    }).join("");

    document.querySelectorAll("[data-nodel],[data-yodel]").forEach((b) =>
      b.addEventListener("click", async () => {
        const on = !!b.dataset.yodel;  // currently off -> turning on
        try {
          await api(`/api/pairs/${b.dataset.nodel || b.dataset.yodel}`,
                    { method: "PATCH", body: { allow_deletes: on ? 1 : 0 } });
          toast(on ? t("Deletions allowed again") : t("Deletions blocked — files will be kept"),
                6000, { label: t("Undo"), run: async () => {
                  await api(`/api/pairs/${b.dataset.nodel || b.dataset.yodel}`,
                            { method: "PATCH", body: { allow_deletes: on ? 0 : 1 } });
                  loadPairs();
                } });
          loadPairs();
        } catch (err) { toast(err.message); }
      }));
    document.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
      const pair = state.pairs.find((p) => p.id === +b.dataset.del);
      const { ok } = await confirmDialog({
        title: t("Remove pair"),
        text: T("Remove pair #{pid}? Local files and Drive contents are kept.",
                { pid: b.dataset.del }) + (pair ? `\n\n${pair.local_path}` : ""),
        yes: "Remove pair", danger: true,
      });
      if (!ok) return;
      try {
        const res = await api(`/api/pairs/${b.dataset.del}`, { method: "DELETE" });
        toast(T("Pair #{pid} removed", { pid: b.dataset.del }), 8000,
              { label: t("Undo"), run: async () => {
                  try {
                    await api("/api/pairs/undo", { method: "POST",
                                                   body: { undo: res.undo } });
                    toast(t("Pair restored"));
                    loadPairs();
                  } catch (err) { toast(err.message); }
                } });
        loadPairs();
      } catch (err) { toast(err.message); }
    }));
    document.querySelectorAll("[data-pause],[data-resume]").forEach((b) =>
      b.addEventListener("click", async () => {
        const id = b.dataset.pause || b.dataset.resume;
        const resuming = !!b.dataset.resume;
        try {
          await api(`/api/pairs/${id}`, { method: "PATCH", body: { enabled: resuming ? 1 : 0 } });
          toast(resuming ? t("Pair resumed") : t("Pair paused"), 6000,
                { label: t("Undo"), run: async () => {
                    await api(`/api/pairs/${id}`, { method: "PATCH",
                                                   body: { enabled: resuming ? 0 : 1 } });
                    loadPairs();
                  } });
          loadPairs();
        } catch (err) { toast(err.message); }
      }));
    document.querySelectorAll("[data-until]").forEach((b) =>
      b.addEventListener("click", async () => {
        const id = b.dataset.until;
        const pair = state.pairs.find((p) => String(p.id) === id);
        const active = pair && pair.paused_until
          && new Date(pair.paused_until) > new Date();
        try {
          await api(`/api/pairs/${id}`, { method: "PATCH",
            body: { paused_until: active ? null : untilTomorrow() } });
          toast(active ? t("Pause cancelled — pair active again")
                       : t("Paused until tomorrow 09:00"), 6000,
                { label: t("Undo"), run: async () => {
                    await api(`/api/pairs/${id}`, { method: "PATCH",
                                                   body: { paused_until: active ? untilTomorrow() : null } });
                    loadPairs();
                  } });
          loadPairs();
        } catch (err) { toast(err.message); }
      }));
    document.querySelectorAll("[data-open-folder]").forEach((b) =>
      b.addEventListener("click", async () => {
        try {
          await api(`/api/pairs/${b.dataset.openFolder}/open-folder`,
                    { method: "POST" });
        } catch (err) { toast(err.message); }
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
    // Phase 3.1: the planner explains every op (reasons[]) and knows the
    // transfer payload sizes (sizes[]); both arrays align with ops[].
    const reasons = res.reasons || [];
    const sizes = res.sizes || [];
    const destructive = res.destructive || [];
    $("#previewList").innerHTML = (res.ops || []).length
      ? res.ops.map((op, i) => {
        const rename = op[0].startsWith("rename") && op[2] ? ` → ${op[2]}` : "";
        const sizeTxt = sizes[i] != null && (op[0] === "upload" || op[0] === "download")
          ? ` · ${humanBytes(+sizes[i])}` : "";
        return `<div class="picker-item">
          <span>${destructive[i] ? "⚠ " : ""}<b>${esc(t(OP_LABELS[op[0]] || op[0]))}</b>
            <span class="muted small"> ${esc(op[1] || "")}${esc(rename)}</span></span>
          <span class="muted small" title="${esc(reasons[i] || "")}">${esc(reasons[i] || "")}${sizeTxt}</span></div>`;
      }).join("")
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
  $("#editBw").value = pair.bandwidth_limit_kb ?? "";
  $("#editPeakBw").value = pair.bandwidth_limit_kb_peak ?? "";
  $("#editPeakFrom").value = pair.peak_from || "";
  $("#editPeakTo").value = pair.peak_to || "";
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
        bandwidth_limit_kb: $("#editBw").value === "" ? null : +$("#editBw").value,
        bandwidth_limit_kb_peak:
          $("#editPeakBw").value === "" ? null : +$("#editPeakBw").value,
        peak_from: $("#editPeakFrom").value || null,
        peak_to: $("#editPeakTo").value || null,
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
  const startPaused = $("#pairStartPaused").checked;
  const summary = [
    `${t("Local")}: ${$("#pairLocal").value.trim()}`,
    `${t("Drive folder")}: ${$("#pairRemoteName").value}`,
    `${t("Direction")}: ${t($("#pairDirection option:checked").textContent)}`,
    `${t("Conflict policy")}: ${t($("#pairPolicy option:checked").textContent)}`,
    startPaused ? t("The pair will remain paused until you review its preview and resume it.") :
      t("The first synchronization may begin immediately and transfer files."),
  ].join("\n");
  const { ok } = await confirmDialog({
    title: t("Review new sync pair"),
    text: summary,
    yes: "Continue",
  });
  if (!ok) return;
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
        enabled: startPaused ? 0 : 1,
        bandwidth_limit_kb: $("#pairBw").value === "" ? null : +$("#pairBw").value,
        bandwidth_limit_kb_peak:
          $("#pairPeakBw").value === "" ? null : +$("#pairPeakBw").value,
        peak_from: $("#pairPeakFrom").value || null,
        peak_to: $("#pairPeakTo").value || null,
      },
    });
    const scopeMsg = state.newScope.length
      ? T(" Only {n} chosen subfolder(s) will sync.", { n: state.newScope.length }) : "";
    const resultMsg = startPaused
      ? t("Pair added paused. Review its preview, then resume it when ready.")
      : t("Pair added — first sync starts in the background.");
    setMsg("#pairFormMsg", resultMsg + scopeMsg, "ok");
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
          ? t("No folders shared with this account yet.")
          : cur.id === "drives" ? t("No shared drives in this account.")
          : t("No subfolders."))}</p>`;
    const useRow = (cur.id === "shared" || cur.id === "drives") ? "" :
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

/* ---------- Google Drive browser (Phase 2.5) ---------- */
/* Every listing row carries a stable `category` token from the API; the
   label and icon maps stay UI-side so translations never touch the engine. */
const CATEGORY_LABELS = {
  folder: "Folder", shortcut: "Shortcut",
  document: "Google Doc", spreadsheet: "Google Sheets",
  presentation: "Google Slides", drawing: "Google Drawing",
  form: "Google Form", map: "Google My Map", site: "Google Site",
  doc: "Google file", pdf: "PDF", image: "Image", video: "Video",
  audio: "Audio", archive: "Archive", text: "Text file", other: "File",
};
const CATEGORY_ICONS = { folder: "📁", shortcut: "🔗", document: "📝",
  spreadsheet: "📊", presentation: "📽", drawing: "🖌", form: "📃",
  pdf: "📄", image: "🖼", video: "🎬", audio: "🎵", archive: "🗜" };
/* type-filter dropdown option -> category tokens it shows */
const CATEGORY_GROUPS = {
  folders: ["folder"],
  google: ["document", "spreadsheet", "presentation", "drawing", "form",
    "map", "site", "doc"],
  pdf: ["pdf"], image: ["image"], video: ["video"], audio: ["audio"],
  archive: ["archive"], text: ["text"], other: ["other", "shortcut"],
};

function driveAccountId() {
  return $("#driveAccount").value || (state.accounts[0] && state.accounts[0].id) || "";
}

function driveCurrent() {
  const st = state.drive;
  return st.stack[st.stack.length - 1] || { id: "root", name: "My Drive" };
}

function driveParseSize(item) {
  const n = parseFloat(item.size);
  return isNaN(n) ? -1 : n;       // unknown sizes (Google docs) sink quietly
}

function driveCompare(a, b, key, desc) {
  let d = 0;
  if (key === "size") d = driveParseSize(a) - driveParseSize(b);
  else if (key === "modified")
    d = (Date.parse(a.modifiedTime) || 0) - (Date.parse(b.modifiedTime) || 0);
  else
    d = String(a.name || "").localeCompare(String(b.name || ""),
                                           undefined, { sensitivity: "base" });
  return desc ? -d : d;
}

function driveDescend(id, name) {
  if (state.drive.loading) return;
  state.drive.stack.push({ id, name });
  clearDriveSearch(true);
  loadDriveFolder();
}

async function loadDrive() {
  await loadAccounts();          // fills #driveAccount (and the pair form's)
  loadDriveFolder();
}

async function loadDriveFolder() {
  const st = state.drive;
  const cur = driveCurrent();
  st.loading = true;
  $("#driveUp").disabled = st.search || st.stack.length <= 1;
  $("#driveUseFolder").disabled = true;
  $("#driveCrumbs").innerHTML = "";
  $("#drivePath").textContent = "";
  const tbody = $("#driveTable tbody");
  const acct = driveAccountId();
  if (!acct) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted">${esc(t("Choose an account to browse its Drive."))}</td></tr>`;
    return;
  }
  tbody.innerHTML = `<tr><td colspan="5" class="muted">${esc(t("loading…"))}</td></tr>`;
  try {
    const res = await api(`/api/drive/${acct}/children?parent=${encodeURIComponent(cur.id)}`);
    st.items = res.items || [];
    st.crumbs = res.breadcrumbs || [];
    st.loading = false;
    renderDrive();
  } catch (e) {
    st.loading = false;
    tbody.innerHTML = `<tr><td colspan="5" class="msg err">${esc(e.message)}</td></tr>`;
  }
}

function renderDriveCrumbs() {
  const nav = $("#driveCrumbs");
  nav.innerHTML = "";
  const st = state.drive;
  const chain = st.search ? [] : st.crumbs;
  if (!chain.length) {
    const span = document.createElement("span");
    span.className = "muted";
    span.textContent = driveCurrent().name;
    nav.appendChild(span);
    return;
  }
  chain.forEach((crumb, i) => {
    if (i) {
      const sep = document.createElement("span");
      sep.className = "crumb-sep";
      sep.textContent = "/";
      nav.appendChild(sep);
    }
    const last = i === chain.length - 1;
    const btn = document.createElement("button");
    btn.className = "crumb" + (last ? " active" : "");
    btn.textContent = crumb.name || crumb.id;
    btn.addEventListener("click", () => {
      if (state.drive.loading || last) return;
      state.drive.stack = state.drive.crumbs.slice(0, i + 1);
      clearDriveSearch(true);
      loadDriveFolder();
    });
    nav.appendChild(btn);
  });
}

function renderDrive() {
  const st = state.drive;
  const cur = driveCurrent();
  const group = $("#driveType").value;
  const [key, dir] = $("#driveSort").value.split("-");
  const needle = ($("#driveSearch").value || "").trim().toLowerCase();
  let items = st.items;
  if (!st.search && needle)
    items = items.filter((it) =>
      String(it.name || "").toLowerCase().includes(needle));
  items = items.filter((it) =>
    group === "all" || (CATEGORY_GROUPS[group] || []).includes(it.category));
  // folders always surface first; the chosen key orders within each group
  items = items.slice().sort((a, b) => {
    const fa = a.category === "folder" ? 0 : 1;
    const fb = b.category === "folder" ? 0 : 1;
    return fa !== fb ? fa - fb : driveCompare(a, b, key, dir === "desc");
  });

  renderDriveCrumbs();
  $("#driveUp").disabled = !!st.search || st.stack.length <= 1;
  $("#driveUseFolder").disabled = !!st.search ||
    cur.id === "shared" || cur.id === "drives";
  $("#drivePath").textContent = st.search
    ? T("Search results for “{q}” — {n} item(s) in all of Drive",
        { q: st.search, n: st.items.length })
    : `${cur.name} — ID: ${cur.id}`;

  const tbody = $("#driveTable tbody");
  if (!items.length) {
    let why;
    if (st.search)
      why = st.items.length
        ? t("No items match the type filter.")
        : t("Nothing on this Drive matches the search.");
    else if (cur.id === "shared")
      why = t("No folders shared with this account yet.");
    else if (cur.id === "drives")
      why = t("No shared drives in this account.");
    else
      why = st.items.length
        ? (needle || group !== "all" ? t("No items match the type filter.")
                                     : t("This folder is empty."))
        : t("This folder is empty.");
    tbody.innerHTML = `<tr><td colspan="5" class="muted">${esc(why)}</td></tr>`;
    return;
  }
  tbody.innerHTML = items.map((it) => {
    const isFolder = it.category === "folder";
    const icon = CATEGORY_ICONS[it.category] || "📄";
    const label = t(CATEGORY_LABELS[it.category] || it.category);
    const size = it.size == null ? "—" : humanBytes(+it.size);
    const mod = it.modifiedTime ? new Date(it.modifiedTime).toLocaleString() : "—";
    const owner = it.owner || "—";
    const hint = `${it.mimeType || label} · ID: ${it.id}`;
    return `<tr class="drive-row${isFolder ? " is-folder" : ""}"
      data-id="${esc(it.id)}" data-name="${esc(it.name)}" title="${esc(hint)}">
      <td>${icon} ${esc(it.name || "?")}</td>
      <td>${esc(label)}</td><td>${esc(size)}</td><td>${esc(mod)}</td>
      <td class="muted small">${esc(owner)}</td></tr>`;
  }).join("");
  tbody.querySelectorAll(".drive-row.is-folder").forEach((row) =>
    row.addEventListener("click", () =>
      driveDescend(row.dataset.id, row.dataset.name)));
}

async function driveRunSearch(query) {
  const acct = driveAccountId();
  state.drive.search = query;
  $("#driveTable tbody").innerHTML =
    `<tr><td colspan="5" class="muted">${esc(t("Searching all of Drive…"))}</td></tr>`;
  try {
    const res = await api(`/api/drive/${acct}/search?q=${encodeURIComponent(query)}`);
    state.drive.items = res.items || [];
    state.drive.crumbs = [];
    renderDrive();
  } catch (e) {
    $("#driveTable tbody").innerHTML =
      `<tr><td colspan="5" class="msg err">${esc(e.message)}</td></tr>`;
  }
}

function clearDriveSearch(keepText = false) {
  state.drive.search = null;
  if (!keepText) {
    $("#driveSearch").value = "";
    state.drive.crumbs = [];
  }
}

$("#driveSearch").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  e.preventDefault();
  const q = $("#driveSearch").value.trim();
  if (!q) {
    clearDriveSearch();
    loadDriveFolder();
    return;
  }
  driveRunSearch(q);
});
$("#driveSearch").addEventListener("input", () => {
  clearTimeout($("#driveSearch")._timer);
  $("#driveSearch")._timer = setTimeout(() => {
    if (state.drive.search) {
      if (!$("#driveSearch").value.trim()) {   // box emptied: back to browsing
        clearDriveSearch();
        loadDriveFolder();
      }
      return;
    }
    renderDrive();          // live in-folder filter while browsing
  }, 200);
});
$("#driveUp").addEventListener("click", () => {
  const st = state.drive;
  if (st.loading) return;
  if (st.search) { clearDriveSearch(); loadDriveFolder(); return; }
  if (st.stack.length > 1) { st.stack.pop(); loadDriveFolder(); }
});
$("#driveRefresh").addEventListener("click", () => {
  clearDriveSearch();
  loadDriveFolder();
});
$("#driveAccount").addEventListener("change", () => {
  state.drive.stack = [{ id: "root", name: "My Drive" }];
  clearDriveSearch();
  loadDriveFolder();
});
$("#driveType").addEventListener("change", renderDrive);
$("#driveSort").addEventListener("change", renderDrive);
$("#driveUseFolder").addEventListener("click", () => {
  const cur = driveCurrent();
  if (!cur || cur.id === "shared") {
    toast(t("“Shared with me” cannot be a sync destination — open a folder inside it first."));
    return;
  }
  if (cur.id === "drives") {
    toast(t("“Shared drives” is a list of drives — open one first."));
    return;
  }
  setPairRemote(cur.id, cur.name);
  document.querySelector('#tabs button[data-tab="pairs"]').click();
  toast(t("Folder sent to the sync-pair form."));
});

/* ---------- Recovery (Phase 2.6) ---------- */
/* Everything here reads the same backend the CLI uses: pending-deletion
   holds from the safety valve, the recoverable local trash, the Drive
   trash, kept previous versions and the decision history. No destructive
   action happens without an explicit button press. */
const RECOVERY_ACTIONS = {
  safety_stop: "Deletion safety limit tripped — files held",
  deletions_allowed: "Held deletions approved (one pass)",
  deletions_kept: "Held files kept — deletes switched off",
  pending_cleared: "Hold released — the risky pass resolved itself",
  trash_restored: "File restored from the local trash",
  drive_trash_restored: "File restored from the Drive trash",
  version_restored: "Previous version restored",
  conflict_resolved: "Conflict copy resolved",
};

async function loadRecovery() {
  await loadAccounts();          // fills the pair selects
  fillPairSelect("#recoveryDrivePair");
  fillPairSelect("#versionPair");
  await loadRecoverySummary();
  loadRecoveryTrash();
}

function fillPairSelect(sel) {
  const el = $(sel);
  const prev = el.value;
  el.innerHTML = state.accounts.length
    ? state.pairs.map((p) =>
        `<option value="${esc(p.id)}">#${esc(p.id)} ${esc(p.remote_path || p.local_path)}</option>`).join("")
    : "";
  if (prev && state.pairs.some((p) => String(p.id) === prev)) el.value = prev;
}

async function loadRecoverySummary() {
  const el = $("#recoveryPending");
  const conflictsEl = $("#recoveryConflicts");
  const note = $("#recoveryConflictsNote");
  const failuresEl = $("#recoveryFailures");
  try {
    const data = await api("/api/recovery/summary");
    // pending deletions
    if (!data.pending.length) {
      el.innerHTML = `<span class="muted small">${esc(t("No deletions are awaiting confirmation."))}</span>`;
    } else {
      el.innerHTML = data.pending.map((hold) => {
        const kinds = Object.entries(hold.kinds || {})
          .map(([k, n]) => `${k}×${n}`).join(", ");
        return `<div class="rec-block conflict">
          <div><b>${esc(T("{count} deletion(s) held for pair #{pair_id} ({name}):",
            { count: hold.count, pair_id: hold.pair_id, name: hold.pair_name }))}</b></div>
          <div class="muted small">${esc(T("Deletion types: {kinds}", { kinds }))}</div>
          <ul class="pending-paths">${(hold.paths || []).slice(0, 12)
            .map((p) => `<li>${esc(p)}</li>`).join("")}
            ${hold.more ? `<li class="muted">… +${esc(hold.more)}</li>` : ""}</ul>
          <div class="row">
            <button class="btn primary small" data-allow="${esc(hold.pair_id)}">${esc(t("Allow these deletions (one pass)"))}</button>
            <button class="btn ghost small" data-keep="${esc(hold.pair_id)}">${esc(t("Keep my files (deletes off)"))}</button>
          </div></div>`;
      }).join("");
      el.querySelectorAll("[data-allow]").forEach((btn) =>
        btn.addEventListener("click", () => recoveryDecide(btn.dataset.allow, "allow")));
      el.querySelectorAll("[data-keep]").forEach((btn) =>
        btn.addEventListener("click", () => recoveryDecide(btn.dataset.keep, "keep")));
    }
    // conflict copies
    if (!data.conflicts.length) {
      conflictsEl.innerHTML = `<span class="muted small">${esc(t("No conflict copies — everything is resolved."))}</span>`;
      note.textContent = "";
    } else {
      conflictsEl.innerHTML = data.conflicts.map((c) =>
        `<div class="rec-block conflict"><b>pair #${esc(c.pair_id)}</b> (${esc(c.name)}) — ${esc(c.count)} copy(ies)</div>`).join("");
      note.textContent = t("{n} conflict copy(ies) across your pairs — resolve them from each pair's “Conflicts” button on the Sync pairs tab.")
        .replace("{n}", data.conflicts.reduce((s, c) => s + c.count, 0));
    }
    // recent failures
    failuresEl.innerHTML = data.failures.length
      ? data.failures.map((f) =>
          `<div class="rec-block error"><b>${esc(f.action)}</b> ${esc(f.path || "")} <span class="muted small">${esc(f.ts)}</span><div>${esc(f.message)}</div></div>`).join("")
      : `<span class="muted small">${esc(t("No recent failures."))}</span>`;
    // decision history
    const tbody = $("#recoveryHistoryTable tbody");
    if (!data.history.length) {
      tbody.innerHTML = `<tr><td colspan="5" class="muted">${esc(t("recovery-history-empty"))}</td></tr>`;
    } else {
      tbody.innerHTML = data.history.map((h) =>
        `<tr><td>${esc(h.ts)}</td><td>${h.pair_id == null ? "—" : "#" + esc(h.pair_id)}</td>
         <td>${esc(t(RECOVERY_ACTIONS[h.action] || h.action))}</td>
         <td>${esc(h.path || "—")}</td>
         <td class="muted small">${esc(h.detail || h.reason || "")}</td></tr>`).join("");
    }
  } catch (e) {
    el.innerHTML = `<div class="rec-block error">${esc(e.message)}</div>`;
  }
}

async function recoveryDecide(pairId, choice) {
  const path = choice === "allow" ? "deletions/allow" : "deletions/keep";
  try {
    await api(`/api/recovery/${path}`, { method: "POST", body: { pair_id: +pairId } });
    toast(t(choice === "allow" ? "restore.ack" : "restore.keep"));
  } catch (e) {
    toast(e.message);
  }
  loadRecoverySummary();
}

async function loadRecoveryTrash() {
  const tbody = $("#recoveryTrashTable tbody");
  try {
    const data = await api("/api/recovery/trash?limit=200");
    if (!data.items.length) {
      tbody.innerHTML = `<tr><td colspan="5" class="muted">${esc(t("The local trash is empty."))}</td></tr>`;
      return;
    }
    tbody.innerHTML = data.items.map((it) =>
      `<tr><td>#${esc(it.pair_id)} ${esc(it.pair_name)}</td>
       <td title="${esc(it.stamp)} / ${esc(it.rel)}">${esc(it.rel)}</td>
       <td>${esc(humanBytes(it.size))}</td>
       <td class="muted small">${esc(it.when || "")}</td>
       <td><button class="btn ghost small" data-restore="${esc(it.pair_id)}|${esc(it.stamp)}|${esc(it.rel)}">${esc(t("Restore"))}</button></td></tr>`).join("");
    tbody.querySelectorAll("[data-restore]").forEach((btn) =>
      btn.addEventListener("click", () => {
        const [pairId, stamp, rel] = btn.dataset.restore.split("|");
        recoveryRestoreTrash(pairId, stamp, rel);
      }));
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="msg err">${esc(e.message)}</td></tr>`;
  }
}

async function recoveryRestoreTrash(pairId, stamp, rel) {
  try {
    const res = await api("/api/recovery/trash/restore",
      { method: "POST", body: { pair_id: +pairId, stamp, rel } });
    toast(t(res.result.renamed
      ? "File restored under a new name (the original path was occupied)."
      : "File restored to its original path."));
  } catch (e) {
    toast(e.message);
  }
  loadRecoveryTrash();
}

async function loadDriveTrash() {
  const pairId = $("#recoveryDrivePair").value;
  const tbody = $("#driveTrashTable tbody");
  if (!pairId) {
    tbody.innerHTML = `<tr><td colspan="4" class="muted">${esc(t("recovery-drive-trash-empty"))}</td></tr>`;
    return;
  }
  tbody.innerHTML = `<tr><td colspan="4" class="muted">${esc(t("loading…"))}</td></tr>`;
  try {
    const data = await api(`/api/recovery/drive-trash?pair_id=${encodeURIComponent(pairId)}`);
    if (!data.items.length) {
      tbody.innerHTML = `<tr><td colspan="4" class="muted">${esc(t("The Drive trash is empty."))}</td></tr>`;
      return;
    }
    tbody.innerHTML = data.items.map((it) =>
      `<tr><td>${esc(it.name || it.id)}</td>
       <td>${it.size == null ? "—" : esc(humanBytes(+it.size))}</td>
       <td class="muted small">${esc(it.trashedTime || "")}${it.trashed_ago_days != null ? ` (${esc(it.trashed_ago_days)}d)` : ""}</td>
       <td><button class="btn ghost small" data-untrash="${esc(it.id)}">${esc(t("Restore"))}</button></td></tr>`).join("");
    tbody.querySelectorAll("[data-untrash]").forEach((btn) =>
      btn.addEventListener("click", () => recoveryRestoreDrive(pairId, btn.dataset.untrash)));
    toast(t("Drive trash listed."));
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="4" class="msg err">${esc(e.message)}</td></tr>`;
  }
}

async function recoveryRestoreDrive(pairId, fileId) {
  try {
    await api("/api/recovery/drive-trash/restore",
      { method: "POST", body: { pair_id: +pairId, file_id: fileId } });
    toast(t("File taken out of the Drive trash."));
  } catch (e) {
    toast(e.message);
  }
  loadDriveTrash();
}

async function loadVersions() {
  const pairId = $("#versionPair").value;
  const rel = $("#versionRel").value.trim();
  const tbody = $("#versionsTable tbody");
  if (!pairId || !rel) {
    tbody.innerHTML = `<tr><td colspan="4" class="muted">${esc(t("Enter a file path first."))}</td></tr>`;
    return;
  }
  try {
    const data = await api(`/api/recovery/versions?pair_id=${encodeURIComponent(pairId)}&rel=${encodeURIComponent(rel)}`);
    if (!data.items.length) {
      tbody.innerHTML = `<tr><td colspan="4" class="muted">${esc(t("recovery-versions-empty"))}</td></tr>`;
      return;
    }
    tbody.innerHTML = data.items.map((it) =>
      `<tr><td title="${esc(it.stamp)}">${esc(it.stamp)}</td>
       <td>${esc(it.rel)}</td><td>${esc(humanBytes(it.size))}</td>
       <td><button class="btn ghost small" data-version="${esc(it.stamp)}">${esc(t("Restore version"))}</button></td></tr>`).join("");
    tbody.querySelectorAll("[data-version]").forEach((btn) =>
      btn.addEventListener("click", () => recoveryRestoreVersion(pairId, rel, btn.dataset.version)));
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="4" class="msg err">${esc(e.message)}</td></tr>`;
  }
}

async function recoveryRestoreVersion(pairId, rel, stamp) {
  try {
    await api("/api/recovery/versions/restore",
      { method: "POST", body: { pair_id: +pairId, rel, stamp } });
    toast(t("Version restored; the current copy was kept too."));
    loadVersions();
  } catch (e) {
    toast(e.message);
  }
}

$("#refreshRecovery").addEventListener("click", () => {
  loadRecoverySummary();
  loadRecoveryTrash();
});
$("#loadDriveTrash").addEventListener("click", loadDriveTrash);
$("#loadVersions").addEventListener("click", loadVersions);

/* ---------- transfer queue ---------- */
async function loadTransfers() {
  const body = $("#transfersTable tbody");
  try {
    const { transfers } = await api("/api/transfers");
    body.innerHTML = transfers.length
      ? transfers.map((item) => {
          const pair = state.pairs.find((p) => p.id === item.pair_id);
          const pairName = pair ? `#${pair.id} · ${pair.local_path}` : `#${item.pair_id}`;
          const direction = item.direction === "upload" ? "Upload" : "Download";
          const size = item.size == null ? "—" : humanBytes(item.size);
          const updated = item.updated_at ? new Date(item.updated_at * 1000).toLocaleTimeString() : "—";
          const error = item.error ? `<div class="msg err small">${esc(item.error)}</div>` : "";
          return `<tr><td><span class="pill ${item.status === "failed" ? "off" : item.status === "completed" ? "on" : ""}">${esc(t(item.status))}</span>${error}</td>
            <td>${esc(t(direction))}</td><td>${esc(pairName)}</td><td>${esc(item.path)}</td><td>${esc(size)}</td><td>${esc(updated)}</td></tr>`;
        }).join("")
      : `<tr><td colspan="6" class="muted">${esc(t("No transfers yet."))}</td></tr>`;
  } catch (e) { body.innerHTML = `<tr><td colspan="6" class="msg err">${esc(e.message)}</td></tr>`; }
}
$("#refreshTransfers").addEventListener("click", loadTransfers);

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
let _resumableLoaded = null;
async function loadSettings() {
  try {
    const { settings } = await api("/api/settings");
    $("#setInterval").value = settings.sync_interval;
    $("#setPolicy").value = settings.conflict_policy;
    $("#setIgnores").value = settings.ignore_patterns.join("\n");
    $("#setDeleteLimit").value = settings.delete_safety_limit;
    $("#setConcurrency").value = settings.transfer_concurrency;
    $("#setHistory").value = settings.history_versions ?? 10;
    $("#setRetries").value = settings.transfer_retries ?? 2;
    $("#setRetryBase").value = settings.transfer_retry_base ?? 2;
    // shown in MiB; remember the exact byte value so saving an untouched
    // field cannot round a hand-edited threshold (e.g. 1024 bytes) to 0
    const thrRaw = settings.resumable_threshold ?? (8 << 20);
    $("#setResumable").value = String(+(thrRaw / 1048576).toFixed(3));
    _resumableLoaded = { raw: thrRaw, text: $("#setResumable").value };
    $("#setBandwidth").value = settings.bandwidth_limit_kb;
    $("#setPeakBandwidth").value = settings.bandwidth_limit_kb_peak ?? 0;
    $("#setActiveFrom").value = settings.active_from || "";
    $("#setActiveTo").value = settings.active_to || "";
    $("#setPeakFrom").value = settings.peak_from || "";
    $("#setPeakTo").value = settings.peak_to || "";
    $("#setRespectMetered").checked = settings.respect_metered !== false;
    $("#setMeteredAction").value = settings.metered_action || "pause";
    $("#setMeteredBw").value = settings.metered_bandwidth_kb ?? 1024;
    $("#setDigest").checked = settings.digest_enabled === true;
    $("#setDigestTime").value = settings.digest_time || "";
    $("#setDigestSkip").checked = settings.digest_skip_empty !== false;
    $("#setWatch").checked = settings.watch_enabled !== false;
    $("#setKeyring").checked = settings.keyring_tokens === true;
    if ($("#authScope")) {
      $("#authScope").value = settings.drive_scope || "full";
    }
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
        transfer_retries: +$("#setRetries").value,
        transfer_retry_base: +$("#setRetryBase").value,
        resumable_threshold: (_resumableLoaded && $("#setResumable").value === _resumableLoaded.text)
          ? _resumableLoaded.raw
          : Math.round(+$("#setResumable").value * 1048576),
        bandwidth_limit_kb: +$("#setBandwidth").value,
        bandwidth_limit_kb_peak: +$("#setPeakBandwidth").value,
        active_from: $("#setActiveFrom").value,
        active_to: $("#setActiveTo").value,
        peak_from: $("#setPeakFrom").value,
        peak_to: $("#setPeakTo").value,
        respect_metered: $("#setRespectMetered").checked,
        metered_action: $("#setMeteredAction").value,
        metered_bandwidth_kb: +$("#setMeteredBw").value,
        digest_enabled: $("#setDigest").checked,
        digest_time: $("#setDigestTime").value,
        digest_skip_empty: $("#setDigestSkip").checked,
        watch_enabled: $("#setWatch").checked,
        keyring_tokens: $("#setKeyring").checked,
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
loadAppearanceFromConfig();
loadDashboard();

/* Keep the visible tab current. Transfers change fastest (1.5 s); status,
   recent activity and the activity log refresh on a calmer 4 s cadence.
   Hidden tabs and a hidden browser window do no work at all. */
function refreshCurrentTab() {
  if (document.hidden) return;
  const el = document.activeElement;
  // do not re-render a list while the user is typing in its search box
  if (el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return;
  if (state.tab === "dashboard") loadDashboard();
  else if (state.tab === "activity") loadEvents();
}
setInterval(() => {
  if (!document.hidden && state.tab === "transfers") loadTransfers();
}, 1500);
setInterval(refreshCurrentTab, 4000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshCurrentTab();
});
