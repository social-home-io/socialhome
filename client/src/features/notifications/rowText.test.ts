import { describe, it, expect, afterEach } from 'vitest'
import en from '@/i18n/locales/en.json'
import de from '@/i18n/locales/de.json'
import nl from '@/i18n/locales/nl.json'
import fr from '@/i18n/locales/fr.json'
import es from '@/i18n/locales/es.json'
import { setLocale } from '@/i18n/i18n'
import type { NotifParams } from '@/types'
import {
  HANDLED_KEYS,
  ROW_TYPES,
  actorsPhrase,
  countsMeta,
  groupSentence,
  handledMeta,
  plainText,
  rowKeys,
  rowSentence,
} from './rowText'

/** The "Needs you" section's own strings (NeedsYouSection / needsYou). */
const NEEDS_YOU_KEYS = [
  'notifications.needs_you.title',
  'notifications.needs_you.count',
  'notifications.needs_you.count_one',
  'notifications.needs_you.show_more',
  'notifications.needs_you.show_more_one',
  'notifications.needs_you.show_less',
  'notifications.needs_you.approve',
  'notifications.needs_you.decline',
  'notifications.needs_you.review',
  'notifications.needs_you.view_invite',
  'notifications.needs_you.group',
  'notifications.needs_you.group_unnamed',
  'notifications.needs_you.approved',
  'notifications.needs_you.declining',
  'notifications.needs_you.declining_invite',
  'notifications.needs_you.declined_sent',
  'notifications.needs_you.invite_declined',
  'notifications.needs_you.already',
  'notifications.needs_you.already_anon',
  'notifications.needs_you.failed',
  'notifications.needs_you.flag.edge',
  'notifications.needs_you.flag.unpaired',
  'notifications.needs_you.flag.admin',
  'notifications.needs_you.flag.age',
]

const LOCALES: Record<string, Record<string, string>> = { en, de, nl, fr, es }

function placeholders(s: string): string[] {
  return [...s.matchAll(/\{(\w+)\}/g)].map(m => m[1]).sort()
}

function params(names: (string | null)[], extra: Partial<NotifParams> = {}): NotifParams {
  return {
    actors: names.map((name, i) => ({ id: `u${i}`, name })),
    actor_count: names.length,
    new: 1,
    total: 1,
    space_name: 'Familie Alpha',
    ...extra,
  }
}

function render(type: string, p: NotifParams | null, title = 'TITLE'): string {
  const segs = rowSentence({ type, title, params: p })
  return segs ? plainText(segs) : title
}

afterEach(async () => { await setLocale('en') })

describe('row catalog', () => {
  it.each(Object.keys(LOCALES))('every key the renderer asks for exists in %s with the same placeholders', (loc) => {
    const cat = LOCALES[loc]
    for (const key of [...rowKeys(), ...HANDLED_KEYS, ...NEEDS_YOU_KEYS]) {
      expect(cat[key], `${loc}: ${key}`).toBeTruthy()
      expect(placeholders(cat[key]), `${loc}: ${key}`).toEqual(placeholders(en[key as keyof typeof en]))
    }
  })

  it('every rendered type has its sentence keys', () => {
    for (const [type, spec] of Object.entries(ROW_TYPES)) {
      const base = `notifications.row.${type}`
      const e = en as Record<string, string>
      for (const v of spec.chat ? ['_in', '_household'] : ['']) {
        if (spec.noActor) break
        expect(e[`${base}${v}`]).toContain('{actor}')
        expect(e[`${base}_many${v}`]).toContain('{actors}')
      }
      if (spec.count) expect(e[`${base}_count`]).toMatch(/\{count\}.*\{actors\}|\{actors\}.*\{count\}/)
      if (spec.none) expect(e[`${base}_none`]).toBeTruthy()
    }
  })
})

describe('rowSentence', () => {
  it('one person', () => {
    expect(render('space_comment_added', params(['Ben']))).toBe('Ben commented in Familie Alpha')
  })

  it('two people take the plural sentence', () => {
    expect(render('space_comment_added', params(['Ben', 'Anna'])))
      .toBe('Ben and Anna commented in Familie Alpha')
  })

  it('more than two: two names and how many others', () => {
    expect(render('user_followed', params(['Ben', 'Anna'], { actor_count: 5 })))
      .toBe('Ben, Anna and 3 others started following you')
    expect(render('user_followed', params(['Ben', 'Anna'], { actor_count: 3 })))
      .toBe('Ben, Anna and 1 other started following you')
  })

  it('one known name and people nobody named', () => {
    expect(render('moment_reacted', params(['Ben'], { actor_count: 3 })))
      .toBe('Ben and 2 others reacted to your moment')
  })

  it('counts the things, not the people', () => {
    expect(render('space_post_created', params(['Ben', 'Anna'], { actor_count: 5, new: 25, total: 25 })))
      .toBe('25 new posts in Familie Alpha from Ben, Anna and 3 others')
    expect(render('space_post_created', params(['Ben'], { new: 3, total: 3 })))
      .toBe('3 new posts in Familie Alpha from Ben')
  })

  it('a row nobody caused uses its _none sentence', () => {
    expect(render('moderation_pending', params([]))).toBe('Something is waiting for review in Familie Alpha')
  })

  it('a nameless actor is "Someone"', () => {
    expect(render('moment_replied', params([null]))).toBe('Someone replied to your moment')
  })

  it('the live space name wins over the one the row was written with', () => {
    const segs = rowSentence({ type: 'space_mention', title: 'x', params: params(['Ben']) }, 'Renamed')
    expect(plainText(segs!)).toBe('Ben mentioned you in Renamed')
  })

  it('names and the space are their own segments — text, never markup', () => {
    const evil = '<img src=x onerror=alert(1)>'
    const segs = rowSentence({ type: 'space_mention', title: 'x', params: params([evil], { space_name: '<b>S</b>' }) })!
    expect(segs).toContainEqual({ kind: 'person', text: evil })
    expect(segs).toContainEqual({ kind: 'place', text: '<b>S</b>' })
  })

  it('a name holding a placeholder is not filled again', () => {
    expect(render('space_mention', params(['{space}']))).toBe('{space} mentioned you in Familie Alpha')
  })

  describe('falls back to the title', () => {
    it('for a row from before 0129 (no params)', () => {
      expect(render('space_mention', null, 'Ben mentioned you in X')).toBe('Ben mentioned you in X')
    })
    it('for a type it does not render', () => {
      expect(render('task_assigned', params(['Ben']), 'You were assigned: Dishes')).toBe('You were assigned: Dishes')
    })
    it('when the sentence needs a space it does not know', () => {
      expect(render('space_mention', params(['Ben'], { space_name: null }))).toBe('TITLE')
    })
    it('when a type needs an actor and has none', () => {
      expect(render('user_followed', params([]))).toBe('TITLE')
    })
  })

  describe('replies and reactions to your own things (#1214)', () => {
    const household = (p: NotifParams) => ({ ...p, space_name: null })
    const sentence = (type: string, p: NotifParams, space_id: string | null = null) => {
      const segs = rowSentence({ type, title: 'TITLE', params: p, space_id })
      return segs ? plainText(segs) : 'TITLE'
    }

    it('post_replied: household feed and space, one / many / count', () => {
      expect(sentence('post_replied', household(params(['Ben'])))).toBe('Ben commented on your post')
      expect(sentence('post_replied', params(['Ben', 'Anna']), 's1'))
        .toBe('Ben and Anna commented on your post in Familie Alpha')
      expect(sentence('post_replied', household(params(['Ben', 'Anna'], { actor_count: 3, new: 4, total: 4 }))))
        .toBe('4 new comments on your post from Ben, Anna and 1 other')
    })

    it('comment_replied', () => {
      expect(sentence('comment_replied', params(['Ben']), 's1')).toBe('Ben replied to your comment in Familie Alpha')
      expect(sentence('comment_replied', household(params(['Ben'], { new: 2, total: 2 }))))
        .toBe('2 replies to your comment from Ben')
    })

    it('post_reacted counts reactions, grouped', () => {
      expect(sentence('post_reacted', household(params(['Ben', 'Anna'], { actor_count: 5 }))))
        .toBe('Ben, Anna and 3 others reacted to your post')
      expect(sentence('post_reacted', params(['Ben', 'Anna'], { new: 7, total: 7 }), 's1'))
        .toBe('7 reactions to your post in Familie Alpha from Ben and Anna')
    })

    it('a space row whose space name is unknown keeps its title', () => {
      expect(sentence('post_reacted', household(params(['Ben'])), 's1')).toBe('TITLE')
    })
  })

  describe('calls and small decisions (#16)', () => {
    const plain = (p: NotifParams) => ({ ...p, space_name: null })
    const sentence = (type: string, p: NotifParams, space_id: string | null = null) => {
      const segs = rowSentence({ type, title: 'TITLE', params: p, space_id })
      return segs ? plainText(segs) : 'TITLE'
    }

    it('call_missed: a 1:1 chat, a named group, several calls grouped on the chat', () => {
      expect(sentence('call_missed', plain(params(['Anna'])))).toBe('Missed call from Anna')
      // A group chat's name is the place (no space id).
      expect(sentence('call_missed', params(['Anna'], { space_name: 'Team' })))
        .toBe('Missed call from Anna in Team')
      expect(sentence('call_missed', plain(params(['Anna'], { new: 3, total: 3 }))))
        .toBe('3 missed calls from Anna')
      expect(sentence('call_missed', plain(params(['Anna', 'Ben']))))
        .toBe('Missed calls from Anna and Ben')
    })

    it('chat_added: who added you, or that you were added', () => {
      expect(sentence('chat_added', params(['Anna'], { space_name: 'Team' })))
        .toBe('Anna added you to Team')
      expect(sentence('chat_added', plain(params(['Anna']))))
        .toBe('Anna added you to a group chat')
      expect(sentence('chat_added', params([], { space_name: 'Team' })))
        .toBe('You were added to Team')
    })

    it('poll_result names who closed it and where', () => {
      expect(sentence('poll_result', plain(params(['Anna'])))).toBe('Anna closed a poll you voted in')
      expect(sentence('poll_result', params(['Anna']), 's1')).toBe('Anna closed a poll in Familie Alpha')
    })

    it('poll_result with nobody behind it: the poll reached its deadline', () => {
      expect(sentence('poll_result', plain(params([])))).toBe('A poll you voted in has ended')
      expect(sentence('poll_result', params([]), 's1')).toBe('A poll in Familie Alpha has ended')
    })

    it('poll_scheduled names who picked the time and where', () => {
      expect(sentence('poll_scheduled', plain(params(['Anna']))))
        .toBe('Anna picked a time for a date poll you answered')
      expect(sentence('poll_scheduled', params(['Anna']), 's1')).toBe('Anna picked a time in Familie Alpha')
    })

    it('report_answer names nobody — not the moderator, not whom it was about', () => {
      expect(sentence('report_answer', plain(params([])))).toBe('Your report was reviewed')
      expect(sentence('report_answer', params([]), 's1')).toBe('Your report in Familie Alpha was reviewed')
      expect(sentence('report_answer', params(['Mod']), 's1')).toBe('Your report in Familie Alpha was reviewed')
    })

    it('in German', async () => {
      await setLocale('de')
      expect(sentence('call_missed', plain(params(['Anna'], { new: 2, total: 2 }))))
        .toBe('2 verpasste Anrufe von Anna')
      expect(sentence('chat_added', params(['Anna'], { space_name: 'Team' })))
        .toBe('Anna hat dich zu Team hinzugefügt')
    })
  })

  it('renders in the viewer\'s language', async () => {
    await setLocale('de')
    expect(render('space_comment_added', params(['Ben', 'Anna'], { actor_count: 5 })))
      .toBe('Ben, Anna und 3 weitere haben in Familie Alpha kommentiert')
    expect(render('space_post_created', params(['Ben'], { new: 25, total: 25 })))
      .toBe('25 neue Beiträge in Familie Alpha von Ben')
  })
})

describe('chat mentions (dm_mention) speak the viewer\'s language', () => {
  const chat = (p: NotifParams, extra: { space_id?: string | null, link_url?: string | null } = {}) => {
    const segs = rowSentence({ type: 'dm_mention', title: 'Ben mentioned you in Weekend plans', params: p, ...extra })
    return segs ? plainText(segs) : 'TITLE'
  }

  it('a space chat names the space, in German too (#1217 screenshots)', async () => {
    expect(chat(params(['Ben']), { space_id: 's1', link_url: '/spaces/s1?tab=chat' }))
      .toBe('Ben mentioned you in Familie Alpha')
    await setLocale('de')
    expect(chat(params(['Ben']), { space_id: 's1', link_url: '/spaces/s1?tab=chat' }))
      .toBe('Ben hat dich in Familie Alpha erwähnt')
    expect(chat(params(['Ben', 'Anna']), { space_id: 's1' }))
      .toBe('Ben und Anna haben dich in Familie Alpha erwähnt')
  })

  it('the household chat', async () => {
    const p = params(['Ben'], { space_name: null })
    expect(chat(p, { link_url: '/?tab=chat' })).toBe('Ben mentioned you in the household chat')
    await setLocale('de')
    expect(chat(p, { link_url: '/?tab=chat' })).toBe('Ben hat dich im Haushalts-Chat erwähnt')
  })

  it('a group chat keeps its title: its name is not in params (the title is written in the viewer\'s language)', () => {
    expect(chat(params(['Ben'], { space_name: null }), { link_url: '/dms/c1' })).toBe('TITLE')
  })
})

describe('notices about you, in the viewer\'s language', () => {
  const none = (extra: Partial<NotifParams> = {}) => params([], extra)
  const say = (type: string, p: NotifParams, space_id: string | null = null) => {
    const segs = rowSentence({ type, title: 'TITLE', params: p, space_id })
    return segs ? plainText(segs) : 'TITLE'
  }

  it('renders each one in English and German', async () => {
    const cases: [string, NotifParams, string | null, string, string][] = [
      ['space_join_approved', none(), 's1', "You're in: Familie Alpha", 'Du bist dabei: Familie Alpha'],
      ['space_join_denied', none({ space_name: null }), 's1', 'Your join request was declined', 'Deine Beitrittsanfrage wurde abgelehnt'],
      ['space_ownership_transferred', none(), 's1', 'You now own Familie Alpha', 'Familie Alpha gehört jetzt dir'],
      ['space_location_enabled', params(['Ben']), 's1', 'Location sharing turned on in Familie Alpha', 'Standortfreigabe in Familie Alpha eingeschaltet'],
      ['post_moderated', none(), 's1', 'Your post in Familie Alpha was moderated', 'Dein Beitrag in Familie Alpha wurde moderiert'],
      ['post_moderated', none({ space_name: null }), null, 'Your post was moderated', 'Dein Beitrag wurde moderiert'],
      ['bazaar_bid_placed', params(['Ben']), null, 'New bid on your listing', 'Neues Gebot für deine Anzeige'],
      ['bazaar_offer_accepted', params(['Ben']), null, 'Your offer was accepted', 'Dein Angebot wurde angenommen'],
      ['bazaar_offer_rejected', params(['Ben']), null, 'Your offer was declined', 'Dein Angebot wurde abgelehnt'],
      ['app_challenge', params(['Ben · Familie Okafor'], { space_name: 'Chess' }), null, 'Ben · Familie Okafor invited you to play Chess', 'Ben · Familie Okafor hat dich eingeladen, Chess zu spielen'],
    ]
    for (const [type, p, sid, en_] of cases) expect(say(type, p, sid), type).toBe(en_)
    await setLocale('de')
    for (const [type, p, sid, , de_] of cases) expect(say(type, p, sid), type).toBe(de_)
  })

  it('a game invite names the app; two inviters read in the plural; no app name keeps the title', () => {
    expect(say('app_challenge', params(['Ben', 'Anna'], { space_name: 'Checkers' })))
      .toBe('Ben and Anna invited you to play Checkers')
    // A row from before the app's name rode along ("challenged you to
    // chess") or for an app the registry didn't know: the server's title.
    expect(say('app_challenge', params(['Ben'], { space_name: null }))).toBe('TITLE')
  })

  it('a space notice whose space name is unknown keeps its title', () => {
    expect(say('space_join_approved', none({ space_name: null }), 's1')).toBe('TITLE')
    expect(say('post_moderated', none({ space_name: null }), 's1')).toBe('TITLE')
  })
})

describe('actorsPhrase', () => {
  it('is empty without actors', () => {
    expect(actorsPhrase([], 0)).toEqual([])
  })
})

describe('Needs you rows', () => {
  it('an invite names who and where; without the space it keeps its title', () => {
    expect(render('space_invite', params(['Zoë']))).toBe('Zoë invited you to Familie Alpha')
    expect(render('space_invite', params(['Zoë'], { space_name: null }), 'Zoë invited you to a space'))
      .toBe('Zoë invited you to a space')
  })

  it('a handled item says how it ended and who ended it', () => {
    const by = { id: 'u-s', name: 'Søren' }
    expect(handledMeta(params([], { handled: { outcome: 'approved', by } }))).toBe('Approved by Søren')
    expect(handledMeta(params([], { handled: { outcome: 'declined', by } }))).toBe('Declined by Søren')
    expect(handledMeta(params([], { handled: { outcome: 'done', by } }))).toBe('Handled by Søren')
    expect(handledMeta(params([], { handled: { outcome: 'declined', by } }), 'u-s')).toBe('You declined it')
    expect(handledMeta(params([], { handled: { outcome: 'done', by } }), 'u-s')).toBe('You handled it')
    expect(handledMeta(params([], { handled: { outcome: 'approved', by: { id: 'x', name: null } } })))
      .toBe('Approved by Someone')
    expect(handledMeta(params([], { handled: { outcome: 'accepted', by: null } }))).toBe('Accepted')
    expect(handledMeta(params([], { handled: { outcome: 'expired', by: null } }))).toBe('Expired')
    expect(handledMeta(params([], { handled: { outcome: 'done', by: null } }))).toBe('No action needed')
    expect(handledMeta(params([]))).toBeNull()
    expect(handledMeta(null)).toBeNull()
  })

  it('a space group counts its requests and names the space as a place', async () => {
    const segs = groupSentence(3, 'Salon')
    expect(plainText(segs)).toBe('3 requests wait for you in Salon')
    expect(segs.find(s => s.kind === 'place')?.text).toBe('Salon')
    expect(plainText(groupSentence(2, ' '))).toBe('2 requests wait for you')
    await setLocale('de')
    expect(plainText(groupSentence(3, 'Salon'))).toBe('3 Anfragen warten in Salon auf dich')
  })
})

describe('countsMeta', () => {
  it('"N new · M in all" only on a row that came back after a read', () => {
    expect(countsMeta(params(['Ben'], { new: 2, total: 5 }))).toBe('2 new · 5 in all')
    expect(countsMeta(params(['Ben'], { new: 3, total: 3 }))).toBeNull()
    expect(countsMeta(null)).toBeNull()
  })
})
