import { afterEach, describe, expect, it } from 'vitest'
import { setLocale } from '@/i18n/i18n'
import { featureLabels } from './capabilityLabels'

afterEach(async () => {
  await setLocale('en')
})

describe('featureLabels', () => {
  it('renders each slug through capability.<slug>', () => {
    expect(featureLabels(['Bids and offers in the bazaar'], ['bazaar_bids']))
      .toEqual(['Bids and offers in the bazaar'])
  })

  it('speaks the UI language', async () => {
    await setLocale('de')
    expect(featureLabels(
      ['Bids and offers in the bazaar', 'Space moderators'],
      ['bazaar_bids', 'space_moderators'],
    )).toEqual(['Gebote im Basar', 'Moderatoren in Räumen'])
  })

  it('falls back to the English label for a slug this build does not know', async () => {
    await setLocale('de')
    expect(featureLabels(
      ['Something from a newer server', 'Space moderators'],
      ['from_the_future', 'space_moderators'],
    )).toEqual(['Something from a newer server', 'Moderatoren in Räumen'])
  })

  it('falls back to the labels when the server sent no slugs', () => {
    expect(featureLabels(['A', 'B'], undefined)).toEqual(['A', 'B'])
    expect(featureLabels(['A', 'B'], [])).toEqual(['A', 'B'])
  })
})
