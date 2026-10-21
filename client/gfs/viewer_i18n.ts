/* The pinned viewer's words, in English, German, Dutch, French and Spanish.
 *
 * The viewer is a static page for people who don't use Social Home yet, so
 * it speaks the visitor's language: the first of ``navigator.languages``
 * we have (by its primary subtag), else English. The tables are bundled —
 * nothing is fetched for a string — and kept small: only what the viewer
 * shows. Words follow the shared glossary (social-home-io/translations):
 * du / je / vous / tú; "Space" / "space" stays; Social Home, GFS,
 * Highlights and Momentum are names; never "end-to-end".
 *
 * ``{name}`` placeholders are filled by ``t``; a ``_one`` key is the
 * singular, picked by ``tn`` for a count of 1.
 */

const en = {
  'title.highlight': 'Highlight',
  'title.moment': 'Moment',
  'title.space': '{space} — {server}',
  'title.join': 'Join {space} — {server}',
  'title.join_dead': 'Invite unavailable — {server}',
  'title.person': '{name} — Momentum',
  'title.not_found': 'Not found — {server}',

  'link.broken': 'This link can’t be opened.',
  'link.broken_body': 'This link is incomplete or damaged. Ask whoever shared it for a new link.',
  'link.outdated_body':
    'Highlight links are encrypted now, and this one is from before that or is missing its end. Ask whoever shared it for a new link.',
  'retry': 'Try again',
  'loading': 'Loading…',
  'error.connect': 'Couldn’t connect.',
  'error.unavailable': 'This isn’t available right now.',
  'error.busy': 'Too many viewers — try again in a minute.',
  'error.page': 'This page couldn’t be loaded. Check your connection and try again.',

  'hl.cant_open': 'This highlight couldn’t be opened. Ask for a new link.',
  'hl.connecting': 'Connecting…',
  'hl.closed_early': 'The connection closed before the highlight arrived.',
  'hl.via_server': 'Connecting via server…',
  'hl.no_crypto': 'This browser can’t open encrypted highlights. Try another browser.',
  'hl.loading': 'Loading highlight…',
  'hl.part_missing': 'Part of this highlight didn’t arrive.',
  'hl.finished': 'Highlight finished.',
  'hl.this_part': 'This part didn’t arrive.',
  'hl.buffering': 'Buffering…',
  'hl.ended': 'This highlight has ended.',
  'hl.outdated_page': 'This page is out of date. Reload it, or ask for a new link.',
  'hl.shared_by': 'Shared by {name}',

  'moment.opening': 'Opening this moment…',
  'moment.gone': 'This moment is no longer available.',
  'moment.gone_body': 'Moments end after a while, and the person who shared it may have deleted it.',
  'moment.someone': 'Someone',

  'page.back': 'Back to {server}',
  'page.footer': 'Hosted on {server} · powered by',

  'space.members_one': '{count} member',
  'space.members': '{count} members',
  'space.cta': 'Connect your Social Home',
  'space.cta_hint': 'Already connected? Find this space under Spaces → Discover → Global.',
  'space.about': 'About',
  'space.not_found': 'This space isn’t listed here',
  'space.not_found_body': 'It may have been taken off {server}, or the link is wrong.',
  'cat.general': 'General',
  'cat.hobby_crafts': 'Hobby & crafts',
  'cat.sports_outdoors': 'Sports & outdoors',
  'cat.gaming': 'Gaming',
  'cat.music_arts': 'Music & arts',
  'cat.food_drink': 'Food & drink',
  'cat.tech': 'Tech',
  'cat.local': 'Local / neighborhood',
  'cat.family_parenting': 'Family & parenting',
  'cat.learning': 'Learning',

  'join.title': 'You’re invited to {space}',
  'join.on': 'on {server}',
  'join.step1': 'Open your own Social Home',
  'join.step2': 'Spaces → Join with code',
  'join.step3': 'Scan the QR code or paste the code below',
  'join.code_label': 'Invite code',
  'join.copy': 'Copy code',
  'join.copied': 'Copied ✓',
  'join.copy_failed': 'Select the code and copy it.',
  'join.qr_alt': 'QR code of the invite code',
  'join.hint': 'You join from your own household — this code tells it where to knock.',
  'join.new': 'New to Social Home?',
  'join.learn': 'Learn how to get it',
  'join.dead': 'This invite has expired or was revoked',
  'join.dead_body':
    'Invite links don’t last forever — this one has run out of uses, passed its expiry date, or was taken back by whoever created it.',
  'join.dead_ask': 'Ask whoever shared it for a fresh link.',

  'person.directory': 'Directory',
  'person.followers_one': '{count} follower',
  'person.followers': '{count} followers',
  'person.follow': 'Follow on your Social Home',
  'person.not_found': 'This person isn’t listed here',
  'person.not_found_body': 'They may have left the public directory of {server}.',
} as const

export type MessageKey = keyof typeof en
type Table = Record<MessageKey, string>

const de: Table = {
  'title.highlight': 'Highlight',
  'title.moment': 'Moment',
  'title.space': '{space} – {server}',
  'title.join': '{space} beitreten – {server}',
  'title.join_dead': 'Einladung nicht verfügbar – {server}',
  'title.person': '{name} – Momentum',
  'title.not_found': 'Nicht gefunden – {server}',

  'link.broken': 'Dieser Link lässt sich nicht öffnen.',
  'link.broken_body': 'Der Link ist unvollständig oder beschädigt. Bitte die Person, die ihn geteilt hat, um einen neuen.',
  'link.outdated_body':
    'Highlight-Links sind jetzt verschlüsselt, und dieser ist älter oder unvollständig. Bitte die Person, die ihn geteilt hat, um einen neuen.',
  'retry': 'Erneut versuchen',
  'loading': 'Wird geladen …',
  'error.connect': 'Keine Verbindung möglich.',
  'error.unavailable': 'Das ist gerade nicht verfügbar.',
  'error.busy': 'Zu viele Zuschauer – versuch es in einer Minute noch einmal.',
  'error.page': 'Die Seite konnte nicht geladen werden. Prüf deine Verbindung und versuch es noch einmal.',

  'hl.cant_open': 'Dieses Highlight lässt sich nicht öffnen. Bitte um einen neuen Link.',
  'hl.connecting': 'Verbinden …',
  'hl.closed_early': 'Die Verbindung wurde getrennt, bevor das Highlight ankam.',
  'hl.via_server': 'Verbinden über den Server …',
  'hl.no_crypto': 'Dieser Browser kann verschlüsselte Highlights nicht öffnen. Versuch einen anderen Browser.',
  'hl.loading': 'Highlight wird geladen …',
  'hl.part_missing': 'Ein Teil dieses Highlights ist nicht angekommen.',
  'hl.finished': 'Highlight zu Ende.',
  'hl.this_part': 'Dieser Teil ist nicht angekommen.',
  'hl.buffering': 'Wird gepuffert …',
  'hl.ended': 'Dieses Highlight ist abgelaufen.',
  'hl.outdated_page': 'Diese Seite ist veraltet. Lade sie neu oder bitte um einen neuen Link.',
  'hl.shared_by': 'Geteilt von {name}',

  'moment.opening': 'Moment wird geöffnet …',
  'moment.gone': 'Dieser Moment ist nicht mehr verfügbar.',
  'moment.gone_body': 'Momente enden nach einer Weile, und die Person, die ihn geteilt hat, hat ihn vielleicht gelöscht.',
  'moment.someone': 'Jemand',

  'page.back': 'Zurück zu {server}',
  'page.footer': 'Gehostet auf {server} · mit',

  'space.members_one': '{count} Mitglied',
  'space.members': '{count} Mitglieder',
  'space.cta': 'Verbinde dein Social Home',
  'space.cta_hint': 'Schon verbunden? Du findest diesen Space unter Spaces → Entdecken → Global.',
  'space.about': 'Über',
  'space.not_found': 'Dieser Space ist hier nicht gelistet',
  'space.not_found_body': 'Vielleicht wurde er von {server} entfernt, oder der Link stimmt nicht.',
  'cat.general': 'Allgemein',
  'cat.hobby_crafts': 'Hobby & Basteln',
  'cat.sports_outdoors': 'Sport & Natur',
  'cat.gaming': 'Spiele',
  'cat.music_arts': 'Musik & Kunst',
  'cat.food_drink': 'Essen & Trinken',
  'cat.tech': 'Technik',
  'cat.local': 'Lokal / Nachbarschaft',
  'cat.family_parenting': 'Familie & Erziehung',
  'cat.learning': 'Lernen',

  'join.title': 'Du bist zu {space} eingeladen',
  'join.on': 'auf {server}',
  'join.step1': 'Öffne dein eigenes Social Home',
  'join.step2': 'Spaces → Mit Code beitreten',
  'join.step3': 'Scanne den QR-Code oder füge den Code unten ein',
  'join.code_label': 'Einladungscode',
  'join.copy': 'Code kopieren',
  'join.copied': 'Kopiert ✓',
  'join.copy_failed': 'Markiere den Code und kopiere ihn.',
  'join.qr_alt': 'QR-Code des Einladungscodes',
  'join.hint': 'Du trittst aus deinem eigenen Haushalt bei – dieser Code sagt ihm, wo er anklopfen soll.',
  'join.new': 'Neu bei Social Home?',
  'join.learn': 'So bekommst du es',
  'join.dead': 'Diese Einladung ist abgelaufen oder wurde zurückgezogen',
  'join.dead_body':
    'Einladungslinks gelten nicht ewig – dieser ist aufgebraucht, abgelaufen oder wurde von der Person zurückgenommen, die ihn erstellt hat.',
  'join.dead_ask': 'Bitte die Person, die ihn geteilt hat, um einen neuen Link.',

  'person.directory': 'Verzeichnis',
  'person.followers_one': '{count} Follower',
  'person.followers': '{count} Follower',
  'person.follow': 'Mit deinem Social Home folgen',
  'person.not_found': 'Diese Person ist hier nicht gelistet',
  'person.not_found_body': 'Vielleicht hat sie das öffentliche Verzeichnis von {server} verlassen.',
}

const nl: Table = {
  'title.highlight': 'Highlight',
  'title.moment': 'Moment',
  'title.space': '{space} — {server}',
  'title.join': 'Word lid van {space} — {server}',
  'title.join_dead': 'Uitnodiging niet beschikbaar — {server}',
  'title.person': '{name} — Momentum',
  'title.not_found': 'Niet gevonden — {server}',

  'link.broken': 'Deze link kan niet worden geopend.',
  'link.broken_body': 'Deze link is onvolledig of beschadigd. Vraag degene die hem deelde om een nieuwe link.',
  'link.outdated_body':
    'Highlight-links zijn nu versleuteld, en deze is van daarvoor of mist zijn einde. Vraag degene die hem deelde om een nieuwe link.',
  'retry': 'Opnieuw proberen',
  'loading': 'Laden…',
  'error.connect': 'Kan geen verbinding maken.',
  'error.unavailable': 'Dit is nu niet beschikbaar.',
  'error.busy': 'Te veel kijkers — probeer het over een minuut opnieuw.',
  'error.page': 'Deze pagina kon niet worden geladen. Controleer je verbinding en probeer het opnieuw.',

  'hl.cant_open': 'Deze highlight kan niet worden geopend. Vraag om een nieuwe link.',
  'hl.connecting': 'Verbinden…',
  'hl.closed_early': 'De verbinding werd verbroken voordat de highlight binnen was.',
  'hl.via_server': 'Verbinden via de server…',
  'hl.no_crypto': 'Deze browser kan versleutelde highlights niet openen. Probeer een andere browser.',
  'hl.loading': 'Highlight laden…',
  'hl.part_missing': 'Een deel van deze highlight is niet aangekomen.',
  'hl.finished': 'Highlight afgelopen.',
  'hl.this_part': 'Dit deel is niet aangekomen.',
  'hl.buffering': 'Bufferen…',
  'hl.ended': 'Deze highlight is afgelopen.',
  'hl.outdated_page': 'Deze pagina is verouderd. Laad hem opnieuw of vraag om een nieuwe link.',
  'hl.shared_by': 'Gedeeld door {name}',

  'moment.opening': 'Dit moment openen…',
  'moment.gone': 'Dit moment is niet meer beschikbaar.',
  'moment.gone_body': 'Momenten verdwijnen na een tijdje, en degene die het deelde heeft het misschien verwijderd.',
  'moment.someone': 'Iemand',

  'page.back': 'Terug naar {server}',
  'page.footer': 'Gehost op {server} · mogelijk gemaakt door',

  'space.members_one': '{count} lid',
  'space.members': '{count} leden',
  'space.cta': 'Koppel je Social Home',
  'space.cta_hint': 'Al gekoppeld? Je vindt deze space onder Spaces → Ontdekken → Wereldwijd.',
  'space.about': 'Over',
  'space.not_found': 'Deze space staat hier niet vermeld',
  'space.not_found_body': 'Misschien is hij van {server} gehaald, of de link klopt niet.',
  'cat.general': 'Algemeen',
  'cat.hobby_crafts': 'Hobby & knutselen',
  'cat.sports_outdoors': 'Sport & buiten',
  'cat.gaming': 'Gamen',
  'cat.music_arts': 'Muziek & kunst',
  'cat.food_drink': 'Eten & drinken',
  'cat.tech': 'Techniek',
  'cat.local': 'Lokaal / buurt',
  'cat.family_parenting': 'Gezin & opvoeding',
  'cat.learning': 'Leren',

  'join.title': 'Je bent uitgenodigd voor {space}',
  'join.on': 'op {server}',
  'join.step1': 'Open je eigen Social Home',
  'join.step2': 'Spaces → Deelnemen met code',
  'join.step3': 'Scan de QR-code of plak de code hieronder',
  'join.code_label': 'Uitnodigingscode',
  'join.copy': 'Code kopiëren',
  'join.copied': 'Gekopieerd ✓',
  'join.copy_failed': 'Selecteer de code en kopieer hem.',
  'join.qr_alt': 'QR-code van de uitnodigingscode',
  'join.hint': 'Je wordt lid vanuit je eigen huishouden — deze code vertelt het waar het moet aankloppen.',
  'join.new': 'Nieuw bij Social Home?',
  'join.learn': 'Zo krijg je het',
  'join.dead': 'Deze uitnodiging is verlopen of ingetrokken',
  'join.dead_body':
    'Uitnodigingslinks blijven niet eeuwig geldig — deze is opgebruikt, verlopen of ingetrokken door degene die hem maakte.',
  'join.dead_ask': 'Vraag degene die hem deelde om een nieuwe link.',

  'person.directory': 'Overzicht',
  'person.followers_one': '{count} volger',
  'person.followers': '{count} volgers',
  'person.follow': 'Volgen met je Social Home',
  'person.not_found': 'Deze persoon staat hier niet vermeld',
  'person.not_found_body': 'Misschien staat die niet meer in het openbare overzicht van {server}.',
}

const fr: Table = {
  'title.highlight': 'Highlight',
  'title.moment': 'Moment',
  'title.space': '{space} — {server}',
  'title.join': 'Rejoindre {space} — {server}',
  'title.join_dead': 'Invitation indisponible — {server}',
  'title.person': '{name} — Momentum',
  'title.not_found': 'Introuvable — {server}',

  'link.broken': 'Ce lien ne peut pas être ouvert.',
  'link.broken_body': 'Ce lien est incomplet ou endommagé. Demandez un nouveau lien à la personne qui l’a partagé.',
  'link.outdated_body':
    'Les liens Highlights sont désormais chiffrés, et celui-ci date d’avant ou il lui manque sa fin. Demandez un nouveau lien à la personne qui l’a partagé.',
  'retry': 'Réessayer',
  'loading': 'Chargement…',
  'error.connect': 'Connexion impossible.',
  'error.unavailable': 'Ce contenu n’est pas disponible pour le moment.',
  'error.busy': 'Trop de spectateurs — réessayez dans une minute.',
  'error.page': 'Cette page n’a pas pu être chargée. Vérifiez votre connexion et réessayez.',

  'hl.cant_open': 'Ce highlight ne peut pas être ouvert. Demandez un nouveau lien.',
  'hl.connecting': 'Connexion…',
  'hl.closed_early': 'La connexion s’est fermée avant l’arrivée du highlight.',
  'hl.via_server': 'Connexion via le serveur…',
  'hl.no_crypto': 'Ce navigateur ne peut pas ouvrir les highlights chiffrés. Essayez un autre navigateur.',
  'hl.loading': 'Chargement du highlight…',
  'hl.part_missing': 'Une partie de ce highlight n’est pas arrivée.',
  'hl.finished': 'Highlight terminé.',
  'hl.this_part': 'Cette partie n’est pas arrivée.',
  'hl.buffering': 'Mise en mémoire tampon…',
  'hl.ended': 'Ce highlight est terminé.',
  'hl.outdated_page': 'Cette page n’est plus à jour. Rechargez-la ou demandez un nouveau lien.',
  'hl.shared_by': 'Partagé par {name}',

  'moment.opening': 'Ouverture de ce moment…',
  'moment.gone': 'Ce moment n’est plus disponible.',
  'moment.gone_body': 'Les moments disparaissent après un certain temps, et la personne qui l’a partagé l’a peut-être supprimé.',
  'moment.someone': 'Quelqu’un',

  'page.back': 'Retour à {server}',
  'page.footer': 'Hébergé sur {server} · propulsé par',

  'space.members_one': '{count} membre',
  'space.members': '{count} membres',
  'space.cta': 'Connectez votre Social Home',
  'space.cta_hint': 'Déjà connecté ? Retrouvez cet espace sous Espaces → Découvrir → Global.',
  'space.about': 'À propos',
  'space.not_found': 'Cet espace n’est pas répertorié ici',
  'space.not_found_body': 'Il a peut-être été retiré de {server}, ou le lien est erroné.',
  'cat.general': 'Général',
  'cat.hobby_crafts': 'Loisirs et bricolage',
  'cat.sports_outdoors': 'Sport et plein air',
  'cat.gaming': 'Jeux vidéo',
  'cat.music_arts': 'Musique et arts',
  'cat.food_drink': 'Cuisine et boissons',
  'cat.tech': 'Technologie',
  'cat.local': 'Local / quartier',
  'cat.family_parenting': 'Famille et parentalité',
  'cat.learning': 'Apprentissage',

  'join.title': 'Vous êtes invité à rejoindre {space}',
  'join.on': 'sur {server}',
  'join.step1': 'Ouvrez votre propre Social Home',
  'join.step2': 'Espaces → Rejoindre avec un code',
  'join.step3': 'Scannez le code QR ou collez le code ci-dessous',
  'join.code_label': 'Code d’invitation',
  'join.copy': 'Copier le code',
  'join.copied': 'Copié ✓',
  'join.copy_failed': 'Sélectionnez le code et copiez-le.',
  'join.qr_alt': 'Code QR du code d’invitation',
  'join.hint': 'Vous rejoignez depuis votre propre foyer — ce code lui indique où frapper.',
  'join.new': 'Nouveau sur Social Home ?',
  'join.learn': 'Découvrez comment l’obtenir',
  'join.dead': 'Cette invitation a expiré ou a été révoquée',
  'join.dead_body':
    'Les liens d’invitation ne durent pas éternellement — celui-ci a atteint sa limite d’utilisations, a expiré ou a été retiré par la personne qui l’a créé.',
  'join.dead_ask': 'Demandez un nouveau lien à la personne qui l’a partagé.',

  'person.directory': 'Annuaire',
  'person.followers_one': '{count} abonné',
  'person.followers': '{count} abonnés',
  'person.follow': 'Suivre avec votre Social Home',
  'person.not_found': 'Cette personne n’est pas répertoriée ici',
  'person.not_found_body': 'Elle a peut-être quitté l’annuaire public de {server}.',
}

const es: Table = {
  'title.highlight': 'Highlight',
  'title.moment': 'Momento',
  'title.space': '{space} — {server}',
  'title.join': 'Únete a {space} — {server}',
  'title.join_dead': 'Invitación no disponible — {server}',
  'title.person': '{name} — Momentum',
  'title.not_found': 'No encontrado — {server}',

  'link.broken': 'Este enlace no se puede abrir.',
  'link.broken_body': 'Este enlace está incompleto o dañado. Pide uno nuevo a quien lo compartió.',
  'link.outdated_body':
    'Los enlaces de Highlights ahora están cifrados, y este es de antes o le falta el final. Pide uno nuevo a quien lo compartió.',
  'retry': 'Reintentar',
  'loading': 'Cargando…',
  'error.connect': 'No se pudo conectar.',
  'error.unavailable': 'Esto no está disponible ahora mismo.',
  'error.busy': 'Demasiados espectadores: vuelve a intentarlo en un minuto.',
  'error.page': 'No se pudo cargar esta página. Revisa tu conexión y vuelve a intentarlo.',

  'hl.cant_open': 'Este highlight no se pudo abrir. Pide un enlace nuevo.',
  'hl.connecting': 'Conectando…',
  'hl.closed_early': 'La conexión se cerró antes de que llegara el highlight.',
  'hl.via_server': 'Conectando a través del servidor…',
  'hl.no_crypto': 'Este navegador no puede abrir highlights cifrados. Prueba con otro navegador.',
  'hl.loading': 'Cargando el highlight…',
  'hl.part_missing': 'Una parte de este highlight no llegó.',
  'hl.finished': 'Highlight terminado.',
  'hl.this_part': 'Esta parte no llegó.',
  'hl.buffering': 'Almacenando en búfer…',
  'hl.ended': 'Este highlight ha terminado.',
  'hl.outdated_page': 'Esta página está desactualizada. Recárgala o pide un enlace nuevo.',
  'hl.shared_by': 'Compartido por {name}',

  'moment.opening': 'Abriendo este momento…',
  'moment.gone': 'Este momento ya no está disponible.',
  'moment.gone_body': 'Los momentos terminan después de un tiempo, y quien lo compartió puede haberlo borrado.',
  'moment.someone': 'Alguien',

  'page.back': 'Volver a {server}',
  'page.footer': 'Alojado en {server} · con la tecnología de',

  'space.members_one': '{count} miembro',
  'space.members': '{count} miembros',
  'space.cta': 'Conecta tu Social Home',
  'space.cta_hint': '¿Ya estás conectado? Encuentra este espacio en Espacios → Descubrir → Global.',
  'space.about': 'Acerca de',
  'space.not_found': 'Este espacio no aparece aquí',
  'space.not_found_body': 'Puede que lo hayan retirado de {server}, o el enlace es incorrecto.',
  'cat.general': 'General',
  'cat.hobby_crafts': 'Aficiones y manualidades',
  'cat.sports_outdoors': 'Deporte y aire libre',
  'cat.gaming': 'Videojuegos',
  'cat.music_arts': 'Música y arte',
  'cat.food_drink': 'Comida y bebida',
  'cat.tech': 'Tecnología',
  'cat.local': 'Local / barrio',
  'cat.family_parenting': 'Familia y crianza',
  'cat.learning': 'Aprendizaje',

  'join.title': 'Te han invitado a {space}',
  'join.on': 'en {server}',
  'join.step1': 'Abre tu propio Social Home',
  'join.step2': 'Espacios → Unirse con código',
  'join.step3': 'Escanea el código QR o pega el código de abajo',
  'join.code_label': 'Código de invitación',
  'join.copy': 'Copiar código',
  'join.copied': 'Copiado ✓',
  'join.copy_failed': 'Selecciona el código y cópialo.',
  'join.qr_alt': 'Código QR del código de invitación',
  'join.hint': 'Te unes desde tu propio hogar: este código le dice dónde llamar.',
  'join.new': '¿Nuevo en Social Home?',
  'join.learn': 'Descubre cómo conseguirlo',
  'join.dead': 'Esta invitación caducó o fue revocada',
  'join.dead_body':
    'Los enlaces de invitación no duran para siempre: este agotó sus usos, pasó su fecha de caducidad o lo retiró quien lo creó.',
  'join.dead_ask': 'Pide un enlace nuevo a quien lo compartió.',

  'person.directory': 'Directorio',
  'person.followers_one': '{count} seguidor',
  'person.followers': '{count} seguidores',
  'person.follow': 'Seguir con tu Social Home',
  'person.not_found': 'Esta persona no aparece aquí',
  'person.not_found_body': 'Puede que haya dejado el directorio público de {server}.',
}

export const LOCALES = { en, de, nl, fr, es } as const satisfies Record<string, Table>
export type Locale = keyof typeof LOCALES

/** The first of ``languages`` we have a table for, by primary subtag. */
export function pickLocale(languages: readonly string[]): Locale {
  for (const lang of languages) {
    const primary = lang.toLowerCase().split('-')[0] ?? ''
    if (Object.hasOwn(LOCALES, primary)) return primary as Locale
  }
  return 'en'
}

function browserLanguages(): readonly string[] {
  if (typeof navigator === 'undefined') return []
  if (navigator.languages?.length) return navigator.languages
  return navigator.language ? [navigator.language] : []
}

let current: Locale = pickLocale(browserLanguages())

export function locale(): Locale {
  return current
}

/** Switch the language (tests; the page picks it once at load). */
export function setLocale(next: Locale): void {
  current = next
}

/** The message for ``key`` with ``{name}`` placeholders filled. */
export function t(key: MessageKey, vars: Record<string, string | number> = {}): string {
  const text = LOCALES[current][key] ?? en[key]
  return text.replace(/\{(\w+)\}/g, (whole, name: string) =>
    Object.hasOwn(vars, name) ? String(vars[name]) : whole,
  )
}

/** ``key`` for a count: its ``_one`` sibling for exactly 1. */
export function tn(key: MessageKey, count: number): string {
  const one = `${key}_one` as MessageKey
  return t(count === 1 && Object.hasOwn(en, one) ? one : key, { count })
}
