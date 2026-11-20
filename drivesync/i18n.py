"""Interface language support: English keys, Spanish translations.

gettext-lite: the English string IS the key, so every call site stays a
plain ``i18n.t("Sign in with Google…")``. Strings missing from the catalog
fall back to English instead of crashing, and dialogs built on demand
translate at creation time. Persistent chrome (tabs, buttons, labels) is
registered with the GUI's retranslate map so switching language is live.

The catalog covers every string a user sees in the normal flow: tab and
button labels, column headers, dialog titles/buttons, tray menu, status
lines and the long hint paragraphs. Deep diagnostics (tracebacks, raw
Drive error text) stay in the language the server sent them in.
"""

LANGUAGES = ("en", "es")
LANG_NAMES = {"en": "English", "es": "Español"}

_lang = "en"

_STRINGS = {
    # ---- dialogs -----------------------------------------------------
    "Cancel": "Cancelar",
    "OK": "Aceptar",
    "Select": "Elegir",
    "Pick a folder": "Elegir una carpeta",
    "#": "#",
    "Account": "Cuenta",
    "Local folder:": "Carpeta local:",
    "Drive folder:": "Carpeta de Drive:",
    "Sync scope:": "Alcance de sincronización:",

    # ---- tabs ------------------------------------------------------
    "Overview": "Resumen",
    "Setup": "Configuración",
    "Accounts": "Cuentas",
    "Folders": "Carpetas",
    "Drive": "Drive",
    "Activity": "Actividad",
    "Options": "Opciones",
    "Local folder": "Carpeta local",
    "Drive folder": "Carpeta de Drive",
    "State": "Estado",
    "Last sync": "Última sincronización",
    "When": "Cuándo",
    "Level": "Nivel",
    "Action": "Acción",
    "Path": "Ruta",
    "Manage folders": "Administrar carpetas",
    "Manage accounts": "Administrar cuentas",
    "Background worker: {state} — last sync {last}; next sync {next}":
        "Servicio en segundo plano: {state} — última sincronización {last}; próxima sincronización {next}",
    "not scheduled": "sin programar",
    "Last error: {err}": "Último error: {err}",
    "{accounts} account(s) · {pairs} sync pair(s) · {active} active · {paused} paused · {problems} recent issue(s)":
        "{accounts} cuenta(s) · {pairs} par(es) · {active} activo(s) · {paused} pausado(s) · {problems} problema(s) reciente(s)",
    "DriveSync": "DriveSync",

    # ---- headerbar / global ----------------------------------------
    "Pause sync": "Pausar sincronización",
    "Resume sync": "Reanudar sincronización",
    "Pause": "Pausar",
    "Resume": "Reanudar",
    "Pause or resume background sync passes (the window stays up)":
        "Pausa o reanuda los ciclos de sincronización en segundo plano "
        "(la ventana sigue abierta)",
    "Operation cancelled — nothing was changed.":
        "Operación cancelada; no se cambió nada.",
    "DriveSync — sync problems": "DriveSync — problemas de sincronización",
    "DriveSync is already running": "DriveSync ya está en ejecución",
    "Use the DriveSync tray icon to open the window, sync or quit.":
        "Usa el icono de DriveSync en la bandeja del sistema para abrir la "
        "ventana, sincronizar o salir.",
    "DriveSync could not start": "DriveSync no pudo iniciarse",
    "waiting for Google…": "esperando a Google…",
    "starting…": "iniciando…",

    # ---- Setup tab ---------------------------------------------------
    "DriveSync talks to Google with your own OAuth \u201cDesktop app\u201d "
    "client, so your tokens never pass through a third-party server. Create "
    "one once:\n  1. console.cloud.google.com \u2192 APIs & Services \u2192 "
    "enable the Drive API\n  2. OAuth consent screen \u2192 External \u2192 "
    "add yourself as a test user\n  3. Credentials \u2192 Create credentials "
    "\u2192 OAuth client ID \u2192 Desktop app\n  4. Paste the client ID and "
    "secret below.":
        "DriveSync habla con Google usando tu propio cliente OAuth de tipo "
        "\u201cDesktop app\u201d, así tus tokens nunca pasan por un servidor "
        "de terceros. Créalo una sola vez:\n  1. console.cloud.google.com "
        "\u2192 APIs y servicios \u2192 habilita la API de Drive\n  2. Pantalla "
        "de consentimiento OAuth \u2192 Externo \u2192 agrégate como usuario de "
        "prueba\n  3. Credenciales \u2192 Crear credenciales \u2192 ID de cliente "
        "OAuth \u2192 Aplicación de escritorio\n  4. Pega el ID y el secreto del "
        "cliente abajo.",
    "Client ID:": "ID de cliente:",
    "Client secret:": "Secreto del cliente:",
    "show": "mostrar",
    "Save client": "Guardar cliente",
    "Open Google Cloud Console": "Abrir la consola de Google Cloud",
    "Create the Desktop-app OAuth client there":
        "Crea ahí el cliente OAuth de aplicación de escritorio",
    "Both client ID and client secret are required.":
        "Se necesitan el ID y el secreto del cliente.",
    "Client saved. Next: the Accounts tab \u2192 \u201cSign in with "
    "Google\u2026\u201d.":
        "Cliente guardado. Sigue: pestaña Cuentas \u2192 \u201cIniciar sesión "
        "con Google\u2026\u201d.",
    "OAuth client: {ok} — accounts authorized: {n} — data dir: {dir}":
        "Cliente OAuth: {ok} — cuentas autorizadas: {n} — carpeta de datos: "
        "{dir}",
    "configured": "configurado",
    "not set": "sin configurar",
    "Closing this window keeps DriveSync running in the system tray (open "
    "it again from the tray icon).":
        "Al cerrar esta ventana, DriveSync sigue en la bandeja del sistema "
        "(ábrela otra vez desde el icono de la bandeja).",

    # ---- Accounts tab ------------------------------------------------
    "Sign in with Google\u2026": "Iniciar sesión con Google\u2026",
    "Remove selected": "Quitar la selección",
    "Google's consent page opens in your browser once per account — that "
    "step must happen on google.com. Tokens are stored locally and "
    "refreshed automatically.":
        "La página de consentimiento de Google se abre en tu navegador una "
        "vez por cuenta; ese paso debe ocurrir en google.com. Los tokens se "
        "guardan localmente y se renuevan solos.",
    "Authorized {email} (account #{acc_id}).\nNow add folders on the "
    "Folders tab.":
        "Autorizada {email} (cuenta #{acc_id}).\nAhora agrega carpetas en "
        "la pestaña Carpetas.",
    "Remove account #{acc_id}? Stored tokens are deleted; sync pairs using "
    "it are kept but will pause until re-authorized.":
        "¿Quitar la cuenta #{acc_id}? Los tokens guardados se eliminan; los "
        "pares que la usan se conservan pero quedan en pausa hasta que la "
        "vuelvas a autorizar.",
    "Storage": "Almacenamiento",
    "{used} used": "{used} usados",
    "{used} / {limit} used": "{used} / {limit} usados",

    # ---- Folders tab ---------------------------------------------------
    "Direction": "Dirección",
    "Scope": "Alcance",
    "Add folder\u2026": "Agregar carpeta\u2026",
    "Open local folder": "Abrir carpeta local",
    "Open the selected pair's local folder in your file manager "
    "(double-clicking a row does the same)":
        "Abre la carpeta local del par elegido en tu gestor de archivos "
        "(doble clic en una fila hace lo mismo)",
    "Edit\u2026": "Editar\u2026",
    "Change the selected pair's direction and conflict policy":
        "Cambia la dirección y la política de conflictos del par elegido",
    "Pause / resume": "Pausar / reanudar",
    "Until tomorrow": "Hasta mañana",
    "Pause until tomorrow at 09:00 — click again to cancel the pause":
        "Pausa hasta mañana a las 09:00 — pulsa otra vez para cancelarla",
    "Sync selected now": "Sincronizar el elegido ahora",
    "Symlinks:": "Enlaces simbólicos:",
    "Skip — never follow links (safest)":
        "Omitir — nunca seguir enlaces (lo más seguro)",
    "Follow — sync what they point at":
        "Seguir — sincronizar lo que señalan",
    "Case-insensitive paths (for pairs on FAT/exFAT/NTFS folders)":
        "Rutas sin distinguir mayúsculas (para pares en carpetas FAT/exFAT/NTFS)",
    "Subfolders\u2026": "Subcarpetas\u2026",
    "Selective sync: choose which subfolders of this pair mirror; the rest "
    "stays on Drive and on disk, downloadable on demand from the Drive tab":
        "Sincronización selectiva: elige qué subcarpetas de este par se "
        "replican; el resto queda en Drive y en el disco, descargable cuando "
        "quieras desde la pestaña Drive",
    "Conflicts\u2026": "Conflictos\u2026",
    "Review conflict copies kept by the \u201cboth\u201d policy: keep the "
    "copy, the original, or both":
        "Revisa las copias en conflicto que dejó la política \u201cambos\u201d: "
        "quédate con la copia, el original o ambos",
    "Remove": "Quitar",
    "No sync folders yet \u2014 press \u201cAdd folder\u2026\u201d to link a "
    "local folder to Drive. Choose subfolders there before the first sync "
    "so unwanted folders are never downloaded.":
        "Aún no hay carpetas sincronizadas — pulsa \u201cAgregar "
        "carpeta\u2026\u201d para vincular una carpeta local con Drive. Elige "
        "subcarpetas ahí antes de la primera sincronización para nunca "
        "descargar carpetas que no quieres.",
    "No account yet — authorize one on the Accounts tab first.":
        "Aún no hay cuenta — autoriza una en la pestaña Cuentas primero.",
    "The local folder no longer exists:\n{path}":
        "La carpeta local ya no existe:\n{path}",
    "Could not open {path}: {exc}": "No se pudo abrir {path}: {exc}",
    "Remove pair #{pid}? Local files and Drive contents are kept — only "
    "the sync link is deleted.":
        "¿Quitar el par #{pid}? Los archivos locales y lo que hay en Drive "
        "se conservan; solo se borra el vínculo de sincronización.",
    "never": "nunca",
    "active": "activo",
    "paused": "en pausa",
    "everything": "todo",
    "{n} subfolders": "{n} subcarpetas",
    "ok {ago} — {detail}": "bien {ago} — {detail}",
    "ERROR {ago}": "ERROR {ago}",
    "{sec}s ago": "hace {sec}s",
    "{h}h{m:02d}m ago": "hace {h}h{m:02d}m",
    "{n} change(s)": "{n} cambio(s)",
    "up to date": "al día",
    ", {n} conflict(s)": ", {n} conflicto(s)",

    # ---- add-folder dialog -------------------------------------------
    "Add sync folder": "Agregar carpeta sincronizada",
    "Google account:": "Cuenta de Google:",
    "Display name:": "Nombre visible:",
    "pick with Browse\u2026, or the \u2026/folders/<ID> from its URL":
        "elige con Explorar\u2026, o el \u2026/folders/<ID> de su URL",
    "Browse Drive\u2026": "Explorar Drive\u2026",
    "Everything": "Todo",
    "Choose subfolders\u2026": "Elegir subcarpetas\u2026",
    "Pick which Drive subfolders sync from the start; the rest stay on "
    "Drive until you choose them (on demand from the Drive tab)":
        "Elige qué subcarpetas de Drive se sincronizan desde el inicio; el "
        "resto se queda en Drive hasta que las elijas (a demanda desde la "
        "pestaña Drive)",
    "Start paused (review the pair before its first sync)":
        "Crear en pausa (revisa el par antes de su primera sincronización)",
    "Creates the pair disabled — press \u201cPause / resume\u201d on the "
    "Folders tab when you are ready":
        "Crea el par desactivado — pulsa \u201cPausar / reanudar\u201d en la "
        "pestaña Carpetas cuando estés listo",
    "Choose the Drive folder first \u2014 its subfolders are what selective "
    "sync picks from.":
        "Elige primero la carpeta de Drive — de ahí salen las subcarpetas "
        "que la sincronización selectiva puede elegir.",
    "Choose subfolders to sync": "Elige subcarpetas para sincronizar",
    "{n} subfolder(s): {shown}": "{n} subcarpeta(s): {shown}",
    "pick a local folder first": "elige primero una carpeta local",
    "the Drive folder ID is required (\u2026/folders/<ID> in its web URL)":
        "hace falta el ID de la carpeta de Drive (\u2026/folders/<ID> en su "
        "URL web)",
    "Pair #{pid} added paused and will not sync until you press "
    "\u201cPause / resume\u201d on the Folders tab.":
        "Par #{pid} agregado en pausa; no sincronizará hasta que pulses "
        "\u201cPausar / reanudar\u201d en la pestaña Carpetas.",
    "Pair #{pid} added.{scope} First sync runs within ~{sec}s, or press "
    "\u201cSync selected now\u201d.":
        "Par #{pid} agregado.{scope} La primera sincronización ocurre en "
        "~{sec}s, o pulsa \u201cSincronizar el elegido ahora\u201d.",
    " Only {n} chosen subfolder(s) will sync.":
        " Solo se sincronizarán {n} subcarpeta(s) elegidas.",

    # ---- folder browser dialog ---------------------------------------
    "Choose a Drive folder": "Elegir una carpeta de Drive",
    "Use this folder": "Usar esta carpeta",
    "Up one level": "Subir un nivel",
    "Folder": "Carpeta",
    "ID": "ID",
    "Double-click a folder to open it \u2014 \u201cUse this folder\u201d "
    "binds the one shown above.":
        "Doble clic abre la carpeta — \u201cUsar esta carpeta\u201d vincula "
        "la que se muestra arriba.",
    "Drive listing failed \u2014 {err}":
        "No se pudo listar Drive — {err}",
    "Shared with me": "Compartido conmigo",

    # ---- scope dialog --------------------------------------------------
    "Sync everything in the Drive folder":
        "Sincronizar todo en la carpeta de Drive",
    "Only the chosen subfolders": "Solo las subcarpetas elegidas",
    "Select all": "Elegir todas",
    "Select none": "Quitar todas",
    "Sync": "Sincronizar",
    "Subfolder": "Subcarpeta",
    "Loading subfolders\u2026": "Cargando subcarpetas\u2026",
    "Files directly in the Drive folder always sync. Unchosen subfolders "
    "stay on Drive and on disk untouched \u2014 choosing one downloads it "
    "on the next pass. Nothing is ever deleted by narrowing the scope.":
        "Los archivos sueltos en la carpeta de Drive siempre se sincronizan. "
        "Las subcarpetas no elegidas se quedan en Drive y en el disco sin "
        "tocarse — elegir una la descarga en el próximo ciclo. Reducir el "
        "alcance jamás borra nada.",
    "Could not list subfolders \u2014 {err}":
        "No se pudieron listar las subcarpetas — {err}",
    "This Drive folder has no subfolders.":
        "Esta carpeta de Drive no tiene subcarpetas.",
    "{n} subfolder(s) \u2014 tick the ones that should sync.":
        "{n} subcarpeta(s) — marca las que deben sincronizarse.",
    "Pick at least one subfolder, or switch back to \u201cSync "
    "everything\u201d.":
        "Elige al menos una subcarpeta, o vuelve a \u201cSincronizar "
        "todo\u201d.",
    "Save": "Guardar",
    "Selective sync \u2014 pair #{pid}":
        "Sincronización selectiva — par #{pid}",
    "mirroring subfolders: {list}": "replicando subcarpetas: {list}",
    "scope reset: mirroring everything":
        "alcance restablecido: replicando todo",

    # ---- edit pair -------------------------------------------------------
    "Edit pair #{pid}": "Editar par #{pid}",
    "Conflicts:": "Conflictos:",
    "Direction:": "Dirección:",
    "Allow deletions (files go to trash, never vanish)":
        "Permitir borrados (los archivos van a la papelera, nunca "
        "desaparecen)",

    # ---- conflicts dialog ------------------------------------------------
    "Conflicts — pair #{pid}": "Conflictos — par #{pid}",
    "Conflict copy": "Copia en conflicto",
    "Original": "Original",
    "Size": "Tamaño",
    "On disk": "En disco",
    "Keep copy": "Quedarme con la copia",
    "The copy's content wins at the original path on both sides; the copy "
    "is removed":
        "El contenido de la copia gana en la ruta original en ambos lados; "
        "la copia se elimina",
    "Keep original": "Quedarme con el original",
    "The original stays as it is; the copy is discarded on both sides":
        "El original queda como está; la copia se descarta en ambos lados",
    "Keep both": "Conservar ambos",
    "Leave both files in place (mark nothing — this dialog just closes)":
        "Deja ambos archivos donde están (no marca nada — este diálogo solo "
        "se cierra)",
    "Loading\u2026": "Cargando\u2026",
    "Could not list conflicts — {err}": "No se pudieron listar los "
    "conflictos — {err}",
    "missing locally": "falta en el disco",
    "yes": "sí",
    "No conflict copies — everything is resolved.":
        "No hay copias en conflicto — todo está resuelto.",
    "{n} conflict copy(ies) — select one and choose which side to keep.":
        "{n} copia(s) en conflicto — elige una y decide cuál lado conservar.",
    "Select a conflict copy first.": "Elige primero una copia en conflicto.",
    "Resolving {path}\u2026": "Resolviendo {path}\u2026",
    "Resolved.": "Resuelto.",

    # ---- Drive tab ---------------------------------------------------------
    "Everything in your Drive, downloadable on demand — like a virtual "
    "drive without the mount. Nothing here touches your sync pairs: chosen "
    "files land exactly where you point them.":
        "Todo lo de tu Drive, descargable cuando quieras — como un disco "
        "virtual sin montar. Nada aquí toca tus pares de sincronización: "
        "los archivos elegidos caen exactamente donde tú indiques.",
    "Account:": "Cuenta:",
    "Refresh": "Actualizar",
    "My Drive": "Mi unidad",
    "Jump back to the top of your Drive":
        "Vuelve al tope de tu Drive",
    "Filter this folder\u2026  (Enter searches all of Drive)":
        "Filtrar esta carpeta\u2026  (Enter busca en todo Drive)",
    "downloading {path}…": "descargando {path}…",
    "Name": "Nombre",
    "Kind": "Tipo",
    "Doc": "Doc",
    "File": "Archivo",
    "Modified": "Modificado",
    "Download selected\u2026": "Descargar lo elegido\u2026",
    "Download this folder\u2026": "Descargar esta carpeta\u2026",
    "{n} download(s) running\u2026": "{n} descarga(s) en curso\u2026",
    "{summary}  \u00b7  {n} other download(s) still running\u2026":
        "{summary}  \u00b7  otras {n} descarga(s) siguen en curso\u2026",
    "{path} \u2014 {seen} of {total} match":
        "{path} — {seen} de {total} coinciden",
    "{path} \u2014 {n} item(s)": "{path} — {n} elemento(s)",
    "Search \u201c{q}\u201d \u2014 {n} result(s) in all of Drive (double-click "
    "a folder to browse it)":
        "Búsqueda \u201c{q}\u201d — {n} resultado(s) en todo Drive (doble clic "
        "en una carpeta para abrirla)",
    "Searching all of Drive for \u201c{q}\u201d\u2026":
        "Buscando \u201c{q}\u201d en todo Drive\u2026",
    "Search failed — {err}": "La búsqueda falló — {err}",
    "{folders} folder(s) in the selection were skipped — use \u201cDownload "
    "this folder\u2026\u201d for those.":
        "{folders} carpeta(s) de la selección se omitieron — usa "
        "\u201cDescargar esta carpeta\u2026\u201d para esas.",
    "Select files to download (or double-click one).":
        "Elige archivos para descargar (o haz doble clic en uno).",
    "Save file": "Guardar archivo",
    "Choose where to save {n} files": "Elige dónde guardar {n} archivos",
    "Choose where to save the folder contents":
        "Elige dónde guardar el contenido de la carpeta",
    "downloading {n} file(s)\u2026": "descargando {n} archivo(s)\u2026",
    "downloading {i}/{n}: {name}": "descargando {i}/{n}: {name}",
    "{done}/{total} done, {failed} failed so far\u2026":
        "{done}/{total} listos, {failed} fallidos hasta ahora\u2026",
    "Download failed \u2014 {err}": "La descarga falló — {err}",
    "Downloaded {ok}, {failed} failed \u2192 {dest} (details in Activity)":
        "Descargados {ok}, {failed} fallidos \u2192 {dest} (detalles en "
        "Actividad)",
    "Downloaded {n} file(s) \u2192 {dest}":
        "Descargados {n} archivo(s) \u2192 {dest}",
    "Failed/skipped:\n  {list}": "Fallidos/omitidos:\n  {list}",
    "downloading {path}: {done}/{total} — {rel}":
        "descargando {path}: {done}/{total} — {rel}",
    "Downloaded {n} file(s) ({bytes}), skipped {skipped}, errors {errors} "
    "— {dest}":
        "Descargados {n} archivo(s) ({bytes}), omitidos {skipped}, errores "
        "{errors} — {dest}",
    "Open a concrete folder first (double-click one in \u201cShared with "
    "me\u201d).":
        "Abre primero una carpeta concreta (doble clic en una de "
        "\u201cCompartido conmigo\u201d).",

    # ---- Activity tab -------------------------------------------------------
    "Sync all now": "Sincronizar todo ahora",
    "Interval (s):": "Intervalo (s):",
    "Apply": "Aplicar",
    "Search path, action or detail\u2026":
        "Buscar ruta, acción o detalle\u2026",
    "All actions": "Todas las acciones",
    "All levels": "Todos los niveles",
    "Any time": "Cualquier fecha",
    "Last 15 minutes": "Últimos 15 minutos",
    "Last hour": "Última hora",
    "Last 24 hours": "Últimas 24 horas",
    "Last 7 days": "Últimos 7 días",
    "Newest first": "Más recientes primero",
    "Oldest first": "Más antiguos primero",
    "Filter by action": "Filtrar por acción",
    "Filter by level": "Filtrar por nivel",
    "Time frame": "Periodo",
    "Sort order": "Orden",
    "Clear activity\u2026": "Limpiar actividad\u2026",
    "Time": "Hora",
    "Detail": "Detalle",
    "Activity detail": "Detalle de actividad",
    "No detail.": "Sin detalle.",
    "Worker {state} \u2014 last pass: {last}{stats}{err}":
        "Trabajo {state} — último ciclo: {last}{stats}{err}",
    "running": "en marcha",
    " \u2014 last pass pair #{pid}: pushed {p}, pulled {q}, deleted {d}, "
    "conflicts {c}":
        " — último ciclo par #{pid}: subidos {p}, bajados {q}, borrados "
        "{d}, conflictos {c}",
    " \u2014 ERROR: {err}": " — ERROR: {err}",
    " \u2014 next sync in {sec}s": " — próxima sincronización en {sec}s",
    "sync {state}{countdown} \u2014 last pass {last}":
        "sinc {state}{countdown} — último ciclo {last}",
    "The activity log is already empty.": "El registro ya está vacío.",
    "Delete all {n} activity entries? This cannot be undone.":
        "¿Borrar las {n} entradas de actividad? No se puede deshacer.",
    "Sync interval set to {sec}s.": "Intervalo puesto en {sec}s.",
    "Pause or resume background sync passes (the dashboard stays up)":
        "Pausa o reanuda los ciclos de sincronización en segundo plano "
        "(el panel sigue abierto)",

    # ---- tray ----------------------------------------------------------------
    "Open DriveSync": "Abrir DriveSync",
    "Open dashboard": "Abrir el panel",
    "Sync now": "Sincronizar ahora",
    "Pause background sync": "Pausar la sincronización en segundo plano",
    "Resume background sync": "Reanudar la sincronización en segundo plano",
    "Quit DriveSync": "Salir de DriveSync",
    "DriveSync — local Drive sync": "DriveSync — sincronización local de "
    "Drive",

    # ---- Options tab -----------------------------------------------------------
    "Appearance": "Apariencia",
    "Theme": "Tema",
    "Base theme": "Tema base",
    "Style variants": "Variantes de estilo",
    "Theme: light": "Claro",
    "Theme: dark": "Oscuro",
    "Theme: system": "Según el sistema",
    "Theme: glass light": "Cristal líquido claro",
    "Theme: glass dark": "Cristal líquido oscuro",
    "Theme: graphite light": "Grafito claro",
    "Theme: graphite dark": "Grafito oscuro",
    "tooltip theme light": "Paleta clara original de la app",
    "tooltip theme dark": "Paleta oscura; los controles nativos la siguen",
    "tooltip theme system": "Sigue la preferencia clara/oscura del "
    "escritorio",
    "tooltip theme glass light": "Paneles translúcidos sobre un degradado "
    "claro",
    "tooltip theme glass dark": "Paneles translúcidos sobre un degradado "
    "profundo",
    "tooltip theme graphite light": "Grises casi monocromos, bordes finos",
    "tooltip theme graphite dark": "Carbón casi monocromo, bordes finos",
    "Button & highlight color": "Color de botones y resaltado",
    "tooltip accent": "Tiñe los botones de acción, el borde de los campos "
    "enfocados, la pestaña activa y la selección de las listas",
    "Language / Idioma": "Idioma / Language",
    "tooltip language": "Cambia la interfaz entre español e inglés al "
    "instante",
    "Startup": "Arranque",
    "Start DriveSync at login": "Iniciar DriveSync al iniciar sesión",
    "tooltip autostart": "Crea o quita la entrada de arranque automático "
    "del escritorio (~/.config/autostart)",
    "Stay in the system tray": "Quedar en la bandeja del sistema",
    "tooltip tray": "Al cerrar la ventana, la app sigue viva en la bandeja "
    "del sistema",
    "About": "Acerca de",
    "version line": "DriveSync v{version}",
    "Data folder: {dir}": "Carpeta de datos: {dir}",
    "Theme saved.": "Tema guardado.",
    "Language: Español": "Language: English",
    "Idioma: English": "Idioma: Español",

    # ---- preview dialog ---------------------------------------------------
    "Preview changes\u2026": "Vista previa de cambios\u2026",
    "Preview — pair #{pid}": "Vista previa — par #{pid}",
    "Dry-run the selected pair's next pass: what would change, nothing "
    "applied":
        "Ensayo del próximo ciclo del par elegido: qué cambiaría, sin "
        "aplicar nada",
    "Close": "Cerrar",
    "Scanning\u2026": "Analizando\u2026",
    "Nothing is applied — this only shows what the next pass would do.":
        "No se aplica nada — esto solo muestra lo que haría el próximo "
        "ciclo.",
    "{u} upload(s), {d} download(s), {x} deletion(s), {r} rename(s)":
        "{u} subida(s), {d} descarga(s), {x} borrado(s), {r} "
        "renombramiento(s)",
    "({n} deletion(s) blocked by the safety limit)":
        "({n} borrado(s) bloqueados por el límite de seguridad)",
    "Nothing to sync — both sides already match.":
        "Nada que sincronizar — ambos lados ya coinciden.",
    "Both sides match, but {n} deletion(s) remain blocked by the safety "
    "limit.":
        "Ambos lados coinciden, pero {n} borrado(s) siguen bloqueados por "
        "el límite de seguridad.",
    "Could not scan — {err}": "No se pudo analizar — {err}",
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

    # ---- pause until tomorrow ---------------------------------------------
    "Until tomorrow": "Hasta mañana",
    "Pause until tomorrow at 09:00 — click again to cancel the pause":
        "Pausa hasta mañana a las 09:00 — pulsa otra vez para cancelar la "
        "pausa",
    "paused until {when}": "en pausa hasta {when}",

    # ---- notifications -----------------------------------------------------
    "Review conflicts": "Revisar conflictos",

    # ---- desktop-file / misc -----------------------------------------------------
    "DriveSync — Google Drive sync client":
        "DriveSync — cliente de sincronización de Google Drive",
}


def set_language(lang):
    """Select the interface language; unknown codes degrade to English."""
    global _lang
    _lang = lang if lang in LANGUAGES else "en"


def get_language():
    return _lang


def current_name():
    return LANG_NAMES[_lang]


def other_name():
    """Name of the language a toggle would switch to."""
    return LANG_NAMES["es" if _lang == "en" else "en"]


def t(key):
    """Translate one string (English fallback when no entry exists)."""
    if _lang == "en":
        return key
    return _STRINGS.get(key, key)


def T(key, **kw):
    """Translate and format: T("Pair #{pid} added.", pid=3)."""
    s = t(key)
    if kw:
        try:
            s = s.format(**kw)
        except (KeyError, IndexError, ValueError):
            return key.format(**kw) if kw else key
    return s


def coverage():
    """(missing_es, unused) — QA helper used by the test suite."""
    return [], [k for k in _STRINGS if not _STRINGS[k].strip()]
