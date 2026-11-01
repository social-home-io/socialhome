import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import {
  changelogNote,
  engineDetail,
  engineTitle,
  isKnownEngine,
  languageNames,
  neverList,
  permissionDetail,
  permissionTitle,
  permissionsOf,
  ratingLabel,
  shortDay,
  sizeLabel,
  sourceLabel,
  versionDay,
  versionLabel,
} from './appFacts'

describe('appFacts', () => {
  it('permission text is Social Home\'s own words', () => {
    expect(permissionTitle('federation')).toBe('Sends invites and moves to other households')
    expect(permissionDetail('storage', 'Chess')).toContain('Only Chess can read it')
    expect(neverList()).toEqual([
      'see your password or sign-in',
      'read your posts, chats, photos or location',
      'reach the internet or other websites',
      'send anything without you choosing who',
    ])
  })

  // The claims R3 PR 9 made true (invites only through Social Home's
  // sheet — the bridge has no ``openSession``, ``contacts.list`` is only
  // who was picked and played with, ``app.send`` only reaches known
  // games): "learns only who you picked", "only to the person you
  // picked", "never sends without you choosing who". Pinned so a later
  // change to them is a deliberate step that checks they still hold.
  it.each(['en', 'de', 'nl', 'fr', 'es'])('ships only true permission claims (%s)', (lang) => {
    const cat = JSON.parse(readFileSync(join(__dirname, '..', '..', 'i18n', 'locales', `${lang}.json`), 'utf8'))
    const shipped = {
      en: ['Lets you pick people to play with', "You choose someone in Social Home. {name} learns only who you picked and people you've played with.", 'Only to the person you picked, encrypted home to home.', 'send anything without you choosing who'],
      de: ['Lässt dich Leute zum Spielen auswählen', 'Du wählst jemanden in Social Home aus. {name} erfährt nur, wen du ausgewählt hast und mit wem du gespielt hast.', 'Nur an die Person, die du ausgewählt hast, verschlüsselt von Zuhause zu Zuhause.', 'etwas schicken, ohne dass du wählst, an wen'],
      nl: ['Laat je mensen kiezen om mee te spelen', 'Je kiest iemand in Social Home. {name} komt alleen te weten wie je hebt gekozen en met wie je hebt gespeeld.', 'Alleen naar de persoon die je hebt gekozen, versleuteld van thuis naar thuis.', 'iets sturen zonder dat jij kiest naar wie'],
      fr: ['Vous laisse choisir des personnes avec qui jouer', 'Vous choisissez quelqu’un dans Social Home. {name} sait seulement qui vous avez choisi et avec qui vous avez joué.', 'Seulement à la personne que vous avez choisie, chiffré de foyer à foyer.', 'envoyer quoi que ce soit sans que vous choisissiez à qui'],
      es: ['Te deja elegir personas con quienes jugar', 'Eliges a alguien en Social Home. {name} solo sabe a quién elegiste y con quién has jugado.', 'Solo a la persona que elegiste, cifrado de hogar a hogar.', 'enviar nada sin que tú elijas a quién'],
    }[lang]!
    expect([
      cat['apps.perm.people'],
      cat['apps.perm.people_detail'],
      cat['apps.perm.federation_detail'],
      cat['apps.never.unasked'],
    ]).toEqual(shipped)
  })

  it('a CalVer version reads as a date, anything else as v…', () => {
    expect(versionLabel('2026.10.29')).toMatch(/29.*2026|2026.*29/)
    expect(versionLabel('2026.10.29.1')).toMatch(/2026/)
    expect(versionLabel('1.2.3')).toBe('v1.2.3')
  })

  it('short dates never wrap: "31 Oct" stays on one line (no "Oct" alone)', () => {
    const day = shortDay('2026-10-31T10:00:00Z')
    expect(day).toMatch(/31/)
    expect(day).not.toContain(' ')
    expect(shortDay(null)).toBe('')
  })

  it('a CalVer version reads as a short day, with the year only when not this year', () => {
    const now = new Date(2026, 9, 31)
    const same = versionDay('2026.10.28.2', now)
    expect(same).toMatch(/28/)
    expect(same).not.toMatch(/2026/)
    expect(same).not.toContain(' ')
    expect(versionDay('2025.3.1', now)).toMatch(/2025/)
    expect(versionDay('1.2.3', now)).toBe('v1.2.3')
  })

  it('sizes, ratings and the source label', () => {
    expect(sizeLabel(16198)).toMatch(/16\s?kB|16\s?KB/i)
    expect(sizeLabel(3 * 1024 * 1024)).toMatch(/3\s?MB/)
    expect(sizeLabel(null)).toBe('')
    expect(ratingLabel(0)).toBe('Everyone')
    expect(ratingLabel(13)).toBe('13+')
    expect(sourceLabel(null)).toBe('From Social Home')
    expect(sourceLabel('apps.example.org')).toBe('From apps.example.org, set by your server operator')
  })

  it('languages by their own names', () => {
    expect(languageNames(['en', 'de'])).toEqual(['English', 'Deutsch'])
    expect(languageNames(undefined)).toEqual([])
  })

  it('permissions: the agreed ones once installed, people implied for old data', () => {
    expect(permissionsOf({ permissions: ['storage'] } as any, { permissions: ['federation'] } as any)).toEqual(['storage'])
    expect(permissionsOf(null, { permissions: ['people'] } as any)).toEqual(['people'])
    expect(permissionsOf(null, { capabilities: ['federation'] } as any)).toEqual(['people', 'federation'])
  })

  it("What's new reads in the viewer's language, else English", () => {
    const c = { version: '2026.10.28', date: null, notes: { en: 'Rematch', de: 'Revanche', pt: 'Revanche!' } }
    expect(changelogNote(c, 'de')).toBe('Revanche')
    expect(changelogNote(c, 'pt-BR')).toBe('Revanche!')
    expect(changelogNote(c, 'fr')).toBe('Rematch')
    expect(changelogNote({ version: '1', date: null, notes: { nl: 'Nieuw' } }, 'fr')).toBe('Nieuw')
    expect(changelogNote({ version: '1', date: null, notes: {} }, 'en')).toBe('')
  })
})

describe('app engines (v_78)', () => {
  it('describes the gift draw in Social Home\'s words, nothing for an unknown engine', () => {
    expect(isKnownEngine('draw')).toBe(true)
    expect(isKnownEngine('deal')).toBe(false)
    expect(engineTitle('draw')).toBe('Runs gift draws with Social Home')
    expect(engineDetail('draw', 'Gift draw')).toContain('Gift draw never learns who gives to whom')
  })
})
