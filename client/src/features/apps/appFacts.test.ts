import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import {
  languageNames,
  neverList,
  permissionDetail,
  permissionTitle,
  permissionsOf,
  ratingLabel,
  sizeLabel,
  sourceLabel,
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
    ])
  })

  // What ships until R3 PR 9 (invites only through Social Home's sheet):
  // the bridge still hands apps the contact list and ``openSession``, so
  // the text may not promise "learns only who you picked" / "only to the
  // person you picked" / "never sends without you choosing who". Changing
  // these strings is a deliberate step — update this test with PR 9.
  it.each(['en', 'de', 'nl', 'fr', 'es'])('ships only true permission claims (%s)', (lang) => {
    const cat = JSON.parse(readFileSync(join(__dirname, '..', '..', 'i18n', 'locales', `${lang}.json`), 'utf8'))
    const shipped = {
      en: ['Lets it invite people you know', 'It sees the people you can message in Social Home, so you can invite them.', 'Encrypted home to home, to people you know.'],
      de: ['Kann Leute einladen, die du kennst', 'Es sieht die Leute, denen du in Social Home schreiben kannst, damit du sie einladen kannst.', 'Verschlüsselt von Zuhause zu Zuhause, an Leute, die du kennst.'],
      nl: ['Kan mensen uitnodigen die je kent', 'Het ziet de mensen aan wie je in Social Home berichten kunt sturen, zodat je ze kunt uitnodigen.', 'Versleuteld van thuis naar thuis, naar mensen die je kent.'],
      fr: ['Peut inviter des personnes que vous connaissez', 'Elle voit les personnes à qui vous pouvez écrire dans Social Home, pour que vous puissiez les inviter.', 'Chiffré de foyer à foyer, vers des personnes que vous connaissez.'],
      es: ['Puede invitar a personas que conoces', 'Ve a las personas a las que puedes escribir en Social Home, para que puedas invitarlas.', 'Cifrado de hogar a hogar, a personas que conoces.'],
    }[lang]!
    expect([cat['apps.perm.people'], cat['apps.perm.people_detail'], cat['apps.perm.federation_detail']]).toEqual(shipped)
    // "without you choosing who" comes back with PR 9.
    expect(cat['apps.never.unasked']).toBeUndefined()
  })

  it('a CalVer version reads as a date, anything else as v…', () => {
    expect(versionLabel('2026.10.29')).toMatch(/29.*2026|2026.*29/)
    expect(versionLabel('2026.10.29.1')).toMatch(/2026/)
    expect(versionLabel('1.2.3')).toBe('v1.2.3')
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
})
